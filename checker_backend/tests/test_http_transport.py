import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from fastapi import HTTPException

from checker_backend import direct_instagram as direct, http_transport as transport
from checker_backend.availability import Availability


class TransportTests(unittest.TestCase):
    def test_curl_session_keeps_browser_preset_and_instagram_cookies(self):
        with patch.dict(os.environ, {"IG_HTTP_TRANSPORT": "curl_cffi"}):
            with direct._make_session('{"cookies":{"sessionid":"fixture","csrftoken":"csrf"}}') as session:
                self.assertIsInstance(session, curl_requests.Session)
                self.assertEqual(session.impersonate, transport.BROWSER_PRESET)
                self.assertEqual(session.retry.count, 0)
                self.assertNotIn("User-Agent", session.headers)
                self.assertEqual(session.headers["X-CSRFToken"], "csrf")
                self.assertEqual(session.cookies.get_dict()["sessionid"], "fixture")
                cookie = next(c for c in session.cookies.jar if c.name == "sessionid")
                self.assertEqual(cookie.domain, ".instagram.com")
                self.assertEqual(cookie.path, "/")
                self.assertEqual(direct._fallback_sessions(session), [])
                self.assertEqual(direct._relationship_sessions(session), [("primary-browser", session, False)])

    def test_invalid_transport_does_not_silently_switch(self):
        with patch.dict(os.environ, {"IG_HTTP_TRANSPORT": "invalid"}), patch.object(transport.requests, "Session") as legacy, patch.object(transport.curl_requests, "Session") as curl:
            with self.assertRaises(ValueError):
                transport.new_session()
            legacy.assert_not_called()
            curl.assert_not_called()

    def test_real_clients_follow_redirect_keep_cookies_and_decode_json(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/result")
                    self.send_header("Set-Cookie", "fixture=value; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({
                    "cookie": self.headers.get("Cookie", ""),
                    "agent": self.headers.get("User-Agent", ""),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in ("requests", "curl_cffi"):
                with self.subTest(transport=name), patch.dict(os.environ, {"IG_HTTP_TRANSPORT": name}):
                    with transport.new_session() as session:
                        response = session.get(f"http://127.0.0.1:{server.server_port}/start", timeout=(2, 2), allow_redirects=True)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("fixture=value", response.json()["cookie"])
                        self.assertEqual(session.cookies.get_dict()["fixture"], "value")
                        if name == "curl_cffi":
                            self.assertIn("Chrome/136.", response.json()["agent"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_both_network_exception_types_are_sanitized(self):
        for error in (requests.RequestException, CurlRequestException):
            with self.subTest(error=error), transport.curl_requests.Session(impersonate=transport.BROWSER_PRESET) as session:
                with patch.object(direct, "_pace"), patch.object(session, "get", side_effect=error("secret-cookie-value")):
                    with self.assertRaises(HTTPException) as raised:
                        direct._fetch_once(session, "https://i.instagram.com/api/v1/test/", None, time.monotonic() + 10)
                self.assertEqual(raised.exception.status_code, 502)
                self.assertNotIn("secret-cookie-value", raised.exception.detail)

    def test_denied_curl_session_stops_after_one_request(self):
        cases = [
            (401, {}, "https://i.instagram.com/api/v1/test/", 300),
            (403, {"message": "feedback_required"}, "https://i.instagram.com/api/v1/test/", 900),
            (200, {"message": "login_required"}, "https://i.instagram.com/api/v1/test/", 300),
            (200, {"message": "feedback_required"}, "https://i.instagram.com/api/v1/test/", 900),
            (200, {"feedback_required": True}, "https://i.instagram.com/api/v1/test/", 900),
            (200, {"message": "challenge_required"}, "https://i.instagram.com/api/v1/test/", 300),
            (200, {}, "https://www.instagram.com/accounts/login/?next=secret", 300),
            (429, {}, "https://i.instagram.com/api/v1/test/", 7200),
        ]
        for status, payload, url, pause in cases:
            with self.subTest(status=status, payload=payload, url=url), transport.curl_requests.Session(impersonate=transport.BROWSER_PRESET) as session:
                session.cookies.set("sessionid", "fixture", domain=".instagram.com")
                response = MagicMock(status_code=status, url=url, headers={"Retry-After": str(pause)})
                response.json.return_value = payload

                def rejected(*_args, **_kwargs):
                    session.cookies.clear()
                    return response

                state = Availability()
                with patch.object(direct, "availability", state), patch.object(direct, "_pace"), patch.object(session, "get", side_effect=rejected) as get, patch.object(direct, "_fallback_sessions") as alternatives:
                    with self.assertRaises(HTTPException) as raised:
                        direct._json_get(session, url, None, time.monotonic() + 10)
                self.assertEqual(raised.exception.status_code, 503)
                self.assertGreaterEqual(int(raised.exception.headers["Retry-After"]), pause - 1)
                self.assertNotIn("fixture", raised.exception.detail)
                self.assertNotIn("secret", raised.exception.detail)
                get.assert_called_once()
                alternatives.assert_not_called()

    def test_authenticated_profile_rejection_is_recorded_before_cookie_deletion(self):
        with transport.curl_requests.Session(impersonate=transport.BROWSER_PRESET) as session:
            session.cookies.set("sessionid", "fixture", domain=".instagram.com")
            response = MagicMock(status_code=401, url="https://www.instagram.com/example/", headers={})

            def rejected(*_args, **_kwargs):
                session.cookies.clear()
                return response

            with patch.object(direct, "availability", Availability()), patch.object(direct, "_pace"), patch.object(session, "get", side_effect=rejected) as get:
                with self.assertRaises(HTTPException):
                    direct._profile_page_counts(session, "example", time.monotonic() + 10, profile_out={})
            get.assert_called_once()

    def test_public_curl_lookup_has_no_checker_credentials(self):
        with transport.curl_requests.Session(impersonate=transport.BROWSER_PRESET) as session:
            response = MagicMock(status_code=404, url="https://www.instagram.com/example/", headers={})
            with patch.object(direct, "new_session", return_value=session), patch.object(direct, "_pace"), patch.object(session, "get", return_value=response):
                self.assertIsNone(direct._public_page_profile("example", time.monotonic() + 10))
                self.assertEqual(session.cookies.get_dict(), {})
                self.assertNotIn("X-CSRFToken", session.headers)
                self.assertNotIn("X-IG-App-ID", session.headers)
                self.assertNotIn("User-Agent", session.headers)

    def test_curl_relationships_merge_ids_and_reject_missing_followers(self):
        for expected in (3, 4):
            with self.subTest(expected=expected), transport.curl_requests.Session(impersonate=transport.BROWSER_PRESET) as session:
                session.cookies.set("sessionid", "fixture", domain=".instagram.com")
                pages = [
                    ([{"pk": "1", "username": "alice"}, {"pk": "2", "username": "bob"}], None),
                    ([{"pk": "2", "username": "bob"}, {"pk": "3", "username": "carol"}], None),
                ]
                with patch.object(direct, "_relation_page_from_base", side_effect=pages):
                    if expected == 3:
                        result = direct._collect_pages(session, "123", "followers", expected, "Подписчики", time.monotonic() + 10)
                        self.assertEqual({row["id"] for row in result}, {"1", "2", "3"})
                    else:
                        with self.assertRaises(HTTPException) as raised:
                            direct._collect_pages(session, "123", "followers", expected, "Подписчики", time.monotonic() + 10)
                        self.assertEqual(raised.exception.status_code, 409)
                        self.assertIn("3 из 4", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()

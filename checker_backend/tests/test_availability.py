import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from checker_backend import direct_instagram as direct, main
from checker_backend.availability import Availability, retry_after_seconds
from checker_backend.profile_page import page_kind


class AvailabilityTests(unittest.TestCase):
    def test_server_retry_after_formats(self):
        self.assertEqual(retry_after_seconds("7200"), 7200)
        self.assertEqual(retry_after_seconds("0"), 1)
        self.assertEqual(retry_after_seconds("invalid"), 900)
        self.assertEqual(retry_after_seconds(None), 900)
        self.assertEqual(retry_after_seconds("-10"), 900)
        with patch("checker_backend.availability.time.time", return_value=0):
            self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 01:00:00 GMT"), 3600)

    def test_cooldown_expires_and_never_shortens(self):
        state = Availability()
        with patch("checker_backend.availability.time.monotonic", return_value=100):
            with self.assertRaises(HTTPException):
                state.stop("limited", 900)
            with self.assertRaises(HTTPException) as shorter:
                state.stop("limited", 10)
        self.assertEqual(shorter.exception.headers["Retry-After"], "900")
        with patch("checker_backend.availability.time.monotonic", return_value=999):
            with self.assertRaises(HTTPException) as pending:
                state.check()
        self.assertEqual(pending.exception.headers["Retry-After"], "1")
        with patch("checker_backend.availability.time.monotonic", return_value=1000):
            state.check()

    def test_repeated_checks_do_not_reach_instagram_during_cooldown(self):
        state = Availability()
        with self.assertRaises(HTTPException):
            state.stop("limited", 7200)
        with patch.object(main, "availability", state), patch.object(main, "collect_direct_snapshot") as collect:
            client = TestClient(main.app)
            for account in ("example", "another"):
                response = client.post("/v1/collect", json={"username": account})
                self.assertEqual(response.status_code, 503)
                self.assertGreater(int(response.headers["Retry-After"]), 7000)
            self.assertEqual(client.get("/health").status_code, 200)
        collect.assert_not_called()
        self.assertFalse(main._collect_lock.locked())

    def test_redirect_classifier_does_not_return_tokens(self):
        for url, expected in (
            ("https://www.instagram.com/accounts/login/?next=secret", "login"),
            ("https://www.instagram.com/challenge/secret/", "confirmation"),
            ("https://www.instagram.com/accounts/suspended/", "confirmation"),
            ("https://www.instagram.com/consent/?token=secret", "consent"),
            ("https://www.instagram.com/example/", "profile"),
            ("https://evil.test/accounts/login", "external"),
        ):
            self.assertEqual(page_kind(url, "example"), expected)

    def test_challenge_response_stops_collection(self):
        state = Availability()
        response = MagicMock(status_code=200, url="https://www.instagram.com/challenge/secret/", headers={})
        with patch.object(direct, "availability", state):
            with self.assertRaises(HTTPException) as error:
                direct._check_response(response, authenticated=True)
        self.assertIn("подтверждения", error.exception.detail)
        self.assertNotIn("secret", error.exception.detail)

    def test_retry_after_is_preserved_from_instagram_response(self):
        state = Availability()
        response = MagicMock(status_code=429, url="https://www.instagram.com/api/v1/users/web_profile_info/", headers={"Retry-After": "7200"})
        with patch.object(direct, "availability", state):
            with self.assertRaises(HTTPException) as error:
                direct._check_response(response, authenticated=True)
        self.assertEqual(error.exception.headers["Retry-After"], "7200")

    def test_all_checker_pages_redirecting_to_login_report_session(self):
        state = Availability()
        session = direct.requests.Session()
        response = MagicMock(status_code=200, url="https://www.instagram.com/accounts/login/?next=secret", text="<html>Login</html>")
        with patch.object(direct, "availability", state), patch.object(direct, "_public_page_profile", return_value=None), patch.object(session, "get", return_value=response), patch.object(direct, "_fallback_sessions", return_value=[]), patch.object(direct, "_pace"), patch.object(direct, "_json_get") as api:
            with self.assertRaises(HTTPException) as error:
                direct._profile(session, "example", time.monotonic() + 10)
        session.close()
        self.assertIn("checker-сессию на вход", error.exception.detail)
        api.assert_not_called()

    def test_unauthorized_response_clearing_cookies_is_still_session_failure(self):
        state = Availability()
        session = direct.requests.Session()
        session.cookies.set("sessionid", "test-only", domain=".instagram.com")
        response = MagicMock(status_code=401, url="https://i.instagram.com/api/v1/friendships/123/followers/", headers={})

        def rejected(*args, **kwargs):
            session.cookies.clear()
            return response

        with patch.object(direct, "availability", state), patch.object(direct, "_pace"), patch.object(session, "get", side_effect=rejected) as get, patch.object(direct, "_fallback_sessions") as alternatives:
            with self.assertRaises(HTTPException) as error:
                direct._json_get(session, response.url, None, time.monotonic() + 10)
        session.close()
        self.assertIn("401", error.exception.detail)
        self.assertNotIn("test-only", error.exception.detail)
        self.assertGreater(int(error.exception.headers["Retry-After"]), 0)
        get.assert_called_once()
        alternatives.assert_not_called()


if __name__ == "__main__":
    unittest.main()

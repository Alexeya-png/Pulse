import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from checker_backend import direct_instagram, main
from checker_backend.availability import Availability


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.availability = Availability()
        patcher = patch.object(main, "availability", self.availability)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def test_health_is_direct_only(self):
        with patch.dict(os.environ, {"IG_SESSION_JSON": ""}):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "engine": "pulse-direct-instagram",
            "session_configured": False,
            "third_party_scraper": False,
            "hiker_dependency": False,
        })

    def test_input_validation(self):
        with patch.object(main, "collect_direct_snapshot") as collect:
            self.assertEqual(
                self.client.post("/v1/collect", json={"username": "bad/name"}).status_code,
                400,
            )
            self.assertEqual(self.client.post("/v1/collect", json={}).status_code, 422)
            collect.assert_not_called()

    def test_android_payload_contract_is_preserved(self):
        snapshot = {
            "account": "example",
            "captured_at": "2026-09-30T00:00:00+00:00",
            "followers_count": 1,
            "following_count": 1,
            "followers": [{"id": "1", "username": "alice"}],
            "following": [{"id": "2", "username": "bob"}],
            "complete": True,
            "source": "pulse-direct-server",
        }
        with patch.object(main, "collect_direct_snapshot", return_value=snapshot) as direct:
            response = self.client.post("/v1/collect", json={"username": " @Example "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), snapshot)
        direct.assert_called_once()
        self.assertEqual(direct.call_args.args[0], "example")

    def test_concurrent_request_is_rejected_and_lock_released(self):
        entered, release = threading.Event(), threading.Event()

        def collect(*_args):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError()
            raise HTTPException(503, "test")

        with patch.object(main, "collect_direct_snapshot", side_effect=collect),                 ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self.client.post,
                "/v1/collect",
                json={"username": "example"},
            )
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertEqual(
                    self.client.post("/v1/collect", json={"username": "example"}).status_code,
                    429,
                )
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).status_code, 503)
        self.assertFalse(main._collect_lock.locked())

    def test_unexpected_exception_does_not_leak_secret(self):
        with patch.object(
            main,
            "collect_direct_snapshot",
            side_effect=RuntimeError("secret-session-value"),
        ):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-session-value", response.text)


class DirectInstagramTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(direct_instagram, "availability", Availability())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_direct_session_uses_browser_headers_and_csrf(self):
        with patch.dict(
            os.environ,
            {"IG_SESSION_JSON": '{"sessionid":"secret","csrftoken":"csrf-token"}'},
        ):
            session = direct_instagram._make_session()
        try:
            self.assertTrue(session.headers["User-Agent"].startswith("Mozilla/5.0"))
            self.assertEqual(session.headers["X-CSRFToken"], "csrf-token")
            self.assertEqual(session.cookies.get("sessionid"), "secret")
        finally:
            session.close()

    def test_direct_transport_stops_after_rate_limit(self):
        primary = direct_instagram.requests.Session()
        alternative = direct_instagram.requests.Session()
        with patch.object(
            direct_instagram,
            "_fetch_once",
            return_value=(429, None),
        ) as fetch, patch.object(
            direct_instagram,
            "_fallback_sessions",
            return_value=[("minimal-browser", alternative)],
        ) as alternatives:
            with self.assertRaises(HTTPException) as error:
                direct_instagram._json_get(
                    primary,
                    "https://www.instagram.com/api/v1/users/web_profile_info/",
                    {"username": "example"},
                    time.monotonic() + 10,
                )
        primary.close()
        alternative.close()
        self.assertEqual(error.exception.status_code, 503)
        self.assertGreater(int(error.exception.headers["Retry-After"]), 0)
        fetch.assert_called_once()
        alternatives.assert_not_called()

    def test_relationship_hosts_merge_unique_ids(self):
        session = direct_instagram.requests.Session()
        pages = [
            ([{"pk": "1", "username": "alice"}, {"pk": "2", "username": "bob"}], None),
            ([{"pk": "2", "username": "bob"}, {"pk": "3", "username": "carol"}], None),
        ]
        with patch.object(
            direct_instagram,
            "_relation_page_from_base",
            side_effect=pages,
        ):
            result = direct_instagram._collect_pages(
                session,
                "123",
                "following",
                3,
                "Подписки",
                time.monotonic() + 10,
            )
        session.close()
        self.assertEqual({item["id"] for item in result}, {"1", "2", "3"})

    def test_profile_page_counts_parse_exact_metadata(self):
        response = MagicMock()
        response.status_code = 200
        response.url = "https://www.instagram.com/example/"
        response.text = '<meta content="142 Followers, 114 Following, 4 Posts">'
        session = direct_instagram.requests.Session()
        with patch.object(
            session,
            "get",
            return_value=response,
        ), patch.object(
            direct_instagram,
            "_fallback_sessions",
            return_value=[],
        ), patch.object(
            direct_instagram,
            "_pace",
        ):
            followers, following = direct_instagram._profile_page_counts(
                session,
                "example",
                time.monotonic() + 10,
            )
        session.close()
        self.assertEqual((followers, following), (142, 114))

    def test_incomplete_direct_list_is_rejected(self):
        session = direct_instagram.requests.Session()
        with patch.object(
            direct_instagram,
            "_relation_page_from_base",
            side_effect=[
                ([{"pk": "1", "username": "alice"}], None),
                ([{"pk": "1", "username": "alice"}], None),
            ],
        ):
            with self.assertRaises(HTTPException) as error:
                direct_instagram._collect_pages(
                    session,
                    "123",
                    "following",
                    2,
                    "Подписки",
                    time.monotonic() + 10,
                )
        session.close()
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("1 из 2", error.exception.detail)


if __name__ == "__main__":
    unittest.main()

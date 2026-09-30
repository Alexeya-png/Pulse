import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from checker_backend import main


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_health_contract(self):
        with patch.dict(os.environ, {"IG_SESSION_JSON": ""}):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "engine": "pulse-web-collector",
            "session_configured": False,
            "hiker_dependency": False,
        })

    def test_input_validation(self):
        with patch.object(main, "_collect_profile") as collect:
            self.assertEqual(
                self.client.post(
                    "/v1/collect",
                    json={"username": "bad/name"},
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.post("/v1/collect", json={}).status_code,
                422,
            )
            collect.assert_not_called()

    def test_session_is_required_before_http_session(self):
        with patch.dict(
            os.environ,
            {"IG_SESSION_JSON": ""},
        ), patch.object(main.requests, "Session") as session:
            response = self.client.post(
                "/v1/collect",
                json={"username": "example"},
            )
        self.assertEqual(response.status_code, 503)
        session.assert_not_called()

    def test_make_session_loads_checker_cookies(self):
        fake = MagicMock()
        with patch.dict(
            os.environ,
            {
                "IG_SESSION_JSON":
                    '{"sessionid":"secret","csrftoken":"csrf"}'
            },
        ), patch.object(
            main.requests,
            "Session",
            return_value=fake,
        ):
            session = main._make_session()
        self.assertIs(session, fake)
        calls = fake.cookies.set.call_args_list
        self.assertEqual(
            calls[0].args[:2],
            ("sessionid", "secret"),
        )
        self.assertEqual(
            calls[1].args[:2],
            ("csrftoken", "csrf"),
        )

    def test_web_profile_info_exact_edge_counts(self):
        session = MagicMock()
        data = {
            "data": {
                "user": {
                    "username": "example",
                    "id": "123",
                    "edge_followed_by": {"count": 7},
                    "edge_follow": {"count": 5},
                }
            }
        }
        with patch.object(
            main,
            "_json_get",
            return_value=(200, data),
        ):
            profile = main._profile(session, "example")
        self.assertEqual(profile["id"], "123")
        self.assertEqual(profile["followers_count"], 7)
        self.assertEqual(profile["following_count"], 5)

    def test_profile_falls_back_to_user_info_for_exact_counts(self):
        session = MagicMock()
        web = {
            "data": {
                "user": {
                    "username": "example",
                    "id": "123",
                }
            }
        }
        info = {
            "user": {
                "username": "example",
                "pk": "123",
                "follower_count": 7,
                "following_count": 5,
            }
        }
        with patch.object(
            main,
            "_json_get",
            side_effect=[
                (200, web),
                (200, web),
                (200, info),
            ],
        ):
            profile = main._profile(session, "example")
        self.assertEqual(profile["id"], "123")
        self.assertEqual(profile["followers_count"], 7)
        self.assertEqual(profile["following_count"], 5)

    def test_relation_pagination_uses_stable_numeric_ids(self):
        session = MagicMock()
        with patch.object(
            main,
            "_relation_page",
            side_effect=[
                (
                    [
                        {"pk": 1, "username": "Alice"},
                        {"pk": 2, "username": "Bob"},
                    ],
                    "next",
                ),
                (
                    [
                        {"pk": 1, "username": "alice"},
                        {"pk": 3, "username": "Carol"},
                    ],
                    None,
                ),
            ],
        ):
            users = main._collect_pages(
                session,
                "123",
                "followers",
                3,
                "Подписчики",
            )
        self.assertEqual(
            users,
            [
                {"id": "1", "username": "alice"},
                {"id": "2", "username": "bob"},
                {"id": "3", "username": "carol"},
            ],
        )

    def test_incomplete_snapshot_is_rejected(self):
        session = MagicMock()
        with patch.object(
            main,
            "_relation_page",
            return_value=(
                [{"pk": 1, "username": "Alice"}],
                None,
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                main._collect_pages(
                    session,
                    "123",
                    "followers",
                    2,
                    "Подписчики",
                )
        self.assertEqual(error.exception.status_code, 409)

    def test_collect_rechecks_counts_after_lists(self):
        session = MagicMock()
        session.__enter__.return_value = session
        before = {
            "id": "123",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        after = {
            "id": "123",
            "followers_count": 2,
            "following_count": 1,
            "is_private": False,
        }
        users = [{"id": "1", "username": "alice"}]
        with patch.object(
            main,
            "_make_session",
            return_value=session,
        ), patch.object(
            main,
            "_profile",
            side_effect=[before, after],
        ), patch.object(
            main,
            "_collect_pages",
            return_value=users,
        ):
            response = self.client.post(
                "/v1/collect",
                json={"username": "example"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("изменились", response.json()["detail"])

    def test_android_payload_contract_is_preserved(self):
        session = MagicMock()
        session.__enter__.return_value = session
        profile = {
            "id": "123",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        users = [{"id": "1", "username": "alice"}]
        with patch.object(
            main,
            "_make_session",
            return_value=session,
        ), patch.object(
            main,
            "_profile",
            side_effect=[profile, profile],
        ), patch.object(
            main,
            "_collect_pages",
            return_value=users,
        ):
            response = self.client.post(
                "/v1/collect",
                json={"username": " @Example "},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {
                "account",
                "captured_at",
                "followers_count",
                "following_count",
                "followers",
                "following",
                "complete",
                "source",
            },
        )
        self.assertEqual(payload["account"], "example")
        self.assertEqual(payload["followers"], users)
        self.assertEqual(payload["source"], "pulse-web-collector")
        self.assertTrue(payload["complete"])

    def test_concurrent_request_is_rejected_and_lock_is_released(self):
        entered, release = threading.Event(), threading.Event()

        def collect(_):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError()
            raise HTTPException(403, "Private account")

        with patch.object(
            main,
            "_collect_profile",
            side_effect=collect,
        ), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self.client.post,
                "/v1/collect",
                json={"username": "example"},
            )
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertEqual(
                    self.client.post(
                        "/v1/collect",
                        json={"username": "example"},
                    ).status_code,
                    429,
                )
            finally:
                release.set()
            self.assertEqual(
                first.result(timeout=10).status_code,
                403,
            )
        self.assertFalse(main._collect_lock.locked())

    def test_internal_failure_has_safe_error_and_releases_lock(self):
        with patch.object(
            main,
            "_collect_profile",
            side_effect=RuntimeError("secret cookie value"),
        ):
            response = self.client.post(
                "/v1/collect",
                json={"username": "example"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)
        self.assertFalse(main._collect_lock.locked())


if __name__ == "__main__":
    unittest.main()

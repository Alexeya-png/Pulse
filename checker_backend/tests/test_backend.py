import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from checker_backend import main
from checker_backend.browser_runtime import browser_session


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_health_contract(self):
        with patch.dict(os.environ, {"IG_SESSION_JSON": ""}):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True, "engine": "pulse-web-collector",
            "session_configured": False, "hiker_dependency": False,
        })

    def test_input_validation(self):
        with patch.object(main, "_collect_profile") as collect:
            self.assertEqual(self.client.post("/v1/collect", json={"username": "bad/name"}).status_code, 400)
            self.assertEqual(self.client.post("/v1/collect", json={}).status_code, 422)
            collect.assert_not_called()

    def test_android_payload_and_normalization(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "Profile"
        users = [{"id": "member", "username": "member"}]
        with patch.object(main, "_make_page") as make_page, patch.object(main, "_assert_logged_in"), patch.object(main, "_exact_count", return_value=1), patch.object(main, "_open_relation"), patch.object(main, "_scroll_relation", return_value=users):
            make_page.return_value.__enter__.return_value = (MagicMock(), page)
            response = self.client.post("/v1/collect", json={"username": " @Example "})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"account", "captured_at", "followers_count", "following_count", "followers", "following", "complete", "source"})
        self.assertEqual(payload["account"], "example")
        self.assertEqual(payload["source"], "pulse-web-collector")
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["followers"], users)
        self.assertEqual(payload["following_count"], 1)

    def test_unverified_or_incomplete_snapshots_are_rejected(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "Profile"
        for counts, lists in [([None, 0], [[], []]), ([1, 0], [[], []]), ([0, 1], [[], []])]:
            with self.subTest(counts=counts), patch.object(main, "_make_page") as make_page, patch.object(main, "_assert_logged_in"), patch.object(main, "_exact_count", side_effect=counts), patch.object(main, "_open_relation"), patch.object(main, "_scroll_relation", side_effect=lists):
                make_page.return_value.__enter__.return_value = (MagicMock(), page)
                response = self.client.post("/v1/collect", json={"username": "example"})
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("followers", response.json())

    def test_concurrent_request_is_rejected_and_lock_is_released(self):
        entered, release = threading.Event(), threading.Event()

        def collect(_):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError()
            raise HTTPException(403, "Private account")

        with patch.object(main, "_collect_profile", side_effect=collect), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.client.post, "/v1/collect", json={"username": "example"})
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertEqual(self.client.post("/v1/collect", json={"username": "example"}).status_code, 429)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).status_code, 403)
        self.assertFalse(main._collect_lock.locked())

    def test_browser_failure_has_safe_error_and_releases_lock(self):
        with patch.object(main, "_collect_profile", side_effect=RuntimeError("secret cookie value")):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)
        self.assertFalse(main._collect_lock.locked())

    def test_session_is_required_before_browser_launch(self):
        with patch.dict(os.environ, {"IG_SESSION_JSON": ""}), patch.object(main, "browser_session") as browser:
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 503)
        browser.assert_not_called()

    def test_context_closed_when_cookie_setup_fails(self):
        browser = MagicMock()
        context = browser.new_context.return_value
        context.add_cookies.side_effect = RuntimeError("bad cookies")
        with patch.dict(os.environ, {"IG_SESSION_JSON": '{"sessionid":"test"}'}), patch.object(main, "browser_session") as session:
            session.return_value.__enter__.return_value = browser
            with self.assertRaises(RuntimeError), main._make_page():
                pass
        context.close.assert_called_once()
        session.return_value.__exit__.assert_called_once()

    def test_startup_fails_if_browser_unavailable(self):
        with patch.object(main, "smoke_check", side_effect=RuntimeError("missing shell")):
            with self.assertRaisesRegex(RuntimeError, "missing shell"):
                with TestClient(main.app):
                    pass


class BrowserIntegrationTests(unittest.TestCase):
    def test_real_shell_reads_counts_and_lists(self):
        with browser_session() as browser:
            page = browser.new_page()
            page.set_content('''<base href="https://www.instagram.com/">
                <a href="/example/followers/"><span title="1,234">1.2k followers</span></a>
                <a href="/example/following/">2 following</a>
                <div role="dialog"><a href="https://www.instagram.com/Alice/">Alice</a>
                <a href="https://www.instagram.com/Alice/">duplicate</a>
                <a href="https://www.instagram.com/bob/">Bob</a></div>''')
            self.assertEqual(main._exact_count(page, "example", "followers"), 1234)
            self.assertEqual(main._exact_count(page, "example", "following"), 2)
            members = main._scroll_relation(page.locator('[role="dialog"]'), 2, "test")
            self.assertEqual(members, [{"id": "alice", "username": "alice"}, {"id": "bob", "username": "bob"}])
        self.assertFalse(browser.is_connected())

    def test_browser_lifecycle_across_request_threads_and_failure(self):
        def collect(fail):
            with browser_session() as browser:
                page = browser.new_page()
                page.set_content("<p>ready</p>")
                self.assertEqual(page.inner_text("p"), "ready")
                if fail:
                    raise ValueError("collection failed")
            return browser.is_connected()

        # Force distinct live threads, as FastAPI's request pool can do.
        with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
            self.assertFalse(first.submit(collect, False).result(timeout=30))
            with self.assertRaises(ValueError):
                second.submit(collect, True).result(timeout=30)
            self.assertFalse(first.submit(collect, False).result(timeout=30))


if __name__ == "__main__":
    unittest.main()

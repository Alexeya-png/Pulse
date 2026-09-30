import json
import unittest
from unittest.mock import Mock

import requests

from pulse.client import InstagramClient, parse_login_cookies, ORIGIN
from pulse.errors import SyncError


SESSION = {"version": 2, "cookies": {"sessionid": "fixture-token-not-a-session", "csrftoken": "fixture-csrf", "ds_user_id": "123"}, "user_agent": "Pulse test fixture"}


def response(body=None, status=200, raw=None):
    r = Mock()
    r.status_code = status
    r.iter_content.return_value = [raw if raw is not None else json.dumps(body).encode()]
    return r


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = InstagramClient(delay=0)
        self.client.set_settings(SESSION)
        self.client.http.get = Mock()

    def tearDown(self):
        self.client.close()

    def test_only_instagram_host_get_timeout_no_redirects(self):
        self.client.http.get.return_value = response({"status": "ok", "users": []})
        self.client.request("friendships/123/followers/", {"count": 100})
        args, kwargs = self.client.http.get.call_args
        self.assertEqual(args[0], ORIGIN + "/api/v1/friendships/123/followers/")
        self.assertEqual(kwargs["timeout"], (10, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(self.client.http.get_adapter(ORIGIN).max_retries.total, 0)
        self.assertTrue(all(cookie.secure for cookie in self.client.http.cookies))

    def test_external_and_unexpected_endpoints_are_rejected(self):
        for endpoint in ("https://example.org/", "../accounts/", "friendships/123/follow/", "media/123/like/"):
            with self.assertRaises(SyncError):
                self.client.request(endpoint)
        self.client.http.get.assert_not_called()

    def test_profile_normalization_keeps_missing_count_unknown(self):
        self.client.http.get.return_value = response({"status": "ok", "data": {"user": {"id": "123", "username": "owner", "edge_followed_by": {"count": 5}}}})
        result = self.client.request("users/owner/usernameinfo/")["user"]
        self.assertEqual(result["follower_count"], 5)
        self.assertIsNone(result["following_count"])

    def test_cookie_parser_filters_irrelevant_values(self):
        result = parse_login_cookies("sessionid=fixture; csrftoken=csrf; ds_user_id=123; unrelated=discard")
        self.assertEqual(set(result), {"sessionid", "csrftoken", "ds_user_id"})

    def test_malformed_profile_is_not_an_empty_follower_list(self):
        for payload in ([{"user": {}}], "unexpected", 1):
            with self.subTest(payload=payload):
                self.client.http.get.return_value = response({"status": "ok", "data": payload})
                with self.assertRaises(SyncError) as exc:
                    self.client.request("users/owner/usernameinfo/")
                self.assertEqual(exc.exception.code, "data")

    def test_version3_accepts_sessionid_only(self):
        client = InstagramClient(delay=0)
        try:
            client.set_settings({"version": 3, "cookies": {"sessionid": "fixture"}, "user_agent": "fixture-agent"})
            self.assertEqual(client.get_settings()["cookies"]["sessionid"], "fixture")
        finally:
            client.close()

    def test_bad_cookies_and_legacy_sessions_rejected(self):
        for value in ("sessionid=x", "sessionid=x; csrftoken=x; ds_user_id=nope", "sessionid=x\r\nInjected: yes"):
            with self.assertRaises(SyncError):
                parse_login_cookies(value)
        with self.assertRaises(SyncError):
            self.client.set_settings({"authorization_data": {}})

    def test_verified_session_must_match_cookie_owner(self):
        self.client.http.get.return_value = response({"status": "ok", "user": {"pk": 999, "username": "wrong"}})
        with self.assertRaises(SyncError):
            self.client.verify_session()
        self.client.http.get.return_value = response({"status": "ok", "user": {"pk": 123, "username": "owner"}})
        self.assertEqual(self.client.verify_session(), "owner")

    def test_429_challenge_and_login_failures_not_retried(self):
        for r, code in ((response({}, status=429), "rate_limit"), (response({"status": "fail", "message": "challenge_required"}, status=400), "challenge"), (response({}, status=302), "auth")):
            self.client.http.get.reset_mock()
            self.client.http.get.return_value = r
            with self.assertRaises(SyncError) as exc:
                self.client.request("accounts/current_user/")
            self.assertEqual(exc.exception.code, code)
            self.client.http.get.assert_called_once()
            r.close.assert_called_once()

    def test_html_error_never_interpreted_as_empty_users(self):
        self.client.http.get.return_value = response(raw=b"<html>login page</html>")
        with self.assertRaises(SyncError):
            self.client.request("friendships/123/followers/")

    def test_network_failure_redacts_sensitive_exception(self):
        self.client.http.get.side_effect = requests.Timeout("sessionid=secret")
        with self.assertRaises(SyncError) as exc:
            self.client.request("accounts/current_user/")
        self.assertNotIn("secret", str(exc.exception))
        self.client.http.get.assert_called_once()

    def test_settings_roundtrip_contains_no_password(self):
        saved = self.client.get_settings()
        self.assertNotIn("password", saved)
        self.assertEqual(saved["cookies"], SESSION["cookies"])


if __name__ == "__main__":
    unittest.main()

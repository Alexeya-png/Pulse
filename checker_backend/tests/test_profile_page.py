import json
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from checker_backend import direct_instagram as direct
from checker_backend.profile_page import parse_profile_page


URL = "https://www.instagram.com/example/"


def route(profile_id="123"):
    return {"id": profile_id, "page_logging": {
        "name": "profilePage",
        "params": {"profile_id": profile_id, "page_id": "profilePage_" + profile_id},
    }}


def page(*, description="1,234 Followers, 111 Following, 88 Posts", payload=None, url=URL):
    if payload is None:
        payload = {"rootView": {"props": route()}, "hostableView": {"props": route()}}
    return (
        f'<link rel="canonical" href="{url}">'
        f'<meta property="og:url" content="{url}">'
        f'<meta content="{description}" property="og:description">'
        '<script type="application/json">' + json.dumps(payload) + '</script>'
    )


class ProfilePageTests(unittest.TestCase):
    def test_current_route_identity_and_exact_counts(self):
        result = parse_profile_page(page(), "example", URL)
        self.assertEqual(result, {
            "id": "123", "username": "example", "followers_count": 1234,
            "following_count": 111, "is_private": None,
        })

    def test_wrong_page_and_login_redirect_are_rejected(self):
        for content, final_url in (
            (page(url="https://www.instagram.com/other/"), URL),
            (page(), "https://www.instagram.com/accounts/login/?next=/example/"),
            (page(), "https://www.instagram.com/other/"),
            (page(url="https://instagram.com.evil.test/example/"), URL),
            (page().replace('rel="canonical"', 'rel="alternate"').replace('property="og:url"', 'name="unused"'), URL),
        ):
            with self.subTest(final_url=final_url, content=content[:90]):
                self.assertIsNone(parse_profile_page(content, "example", final_url))

    def test_rounded_malformed_and_conflicting_counts_are_rejected(self):
        for description in ("1.2K Followers, 111 Following", "1,2 Followers, 111 Following", "-1 Followers, 111 Following", "12 Followers, 1.2K Following"):
            with self.subTest(description=description):
                self.assertIsNone(parse_profile_page(page(description=description), "example", URL))
        conflict = page() + '<meta property="og:description" content="1235 Followers, 111 Following">'
        self.assertIsNone(parse_profile_page(conflict, "example", URL))

    def test_viewer_and_recommended_ids_are_not_profile_identity(self):
        payload = {"viewer": {"id": "999"}, "recommendation": {"username": "other", "id": "888"}}
        self.assertIsNone(parse_profile_page(page(payload=payload), "example", URL))
        payload["route"] = route()
        self.assertEqual(parse_profile_page(page(payload=payload), "example", URL)["id"], "123")

    def test_conflicting_or_unbound_route_ids_are_rejected(self):
        for payload in ([route(), route("456")], {**route(), "id": "456"}, route("0"), route("not-an-id")):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_profile_page(page(payload=payload), "example", URL))

    def test_explicit_private_flag_is_preserved(self):
        content = page(payload={"username": "example", "id": "123", "is_private": True})
        self.assertIs(parse_profile_page(content, "example", URL)["is_private"], True)

    def test_zero_counts_and_html_entities(self):
        result = parse_profile_page(page(description="0 Followers, 0 Following, 0 Posts - &#064;example"), "example", URL)
        self.assertEqual((result["followers_count"], result["following_count"]), (0, 0))


class PublicPageCollectionTests(unittest.TestCase):
    def test_public_page_avoids_profile_api_and_checker_cookies(self):
        public = MagicMock()
        public.__enter__.return_value = public
        public.get.return_value = MagicMock(status_code=200, text=page(), url=URL)
        checker = MagicMock()
        with patch.object(direct.requests, "Session", return_value=public), patch.object(direct, "_pace"), patch.object(direct, "_json_get") as api:
            profile = direct._profile(checker, "example", time.monotonic() + 10)
        self.assertEqual(profile["id"], "123")
        api.assert_not_called()
        checker.get.assert_not_called()
        public.get.assert_called_once()
        headers = public.headers.update.call_args.args[0]
        self.assertNotIn("X-CSRFToken", headers)
        self.assertNotIn("Cookie", headers)
        public.cookies.set.assert_not_called()

    def test_public_page_rate_limit_stops_after_one_request(self):
        public = MagicMock()
        public.__enter__.return_value = public
        public.get.return_value.status_code = 429
        with patch.object(direct.requests, "Session", return_value=public), patch.object(direct, "_pace"), patch.object(direct, "_json_get") as api:
            with self.assertRaises(HTTPException) as error:
                direct._profile(MagicMock(), "example", time.monotonic() + 10)
        self.assertEqual(error.exception.status_code, 503)
        public.get.assert_called_once()
        api.assert_not_called()

    def test_unrecognized_page_retains_existing_profile_fallback(self):
        with patch.object(direct, "_public_page_profile", return_value=None), patch.object(direct, "_profile_page_counts", return_value=(None, None)), patch.object(direct, "_json_get", return_value=(200, {"user": {
            "id": "123", "username": "example", "follower_count": 2,
            "following_count": 1, "is_private": False,
        }})):
            result = direct._profile(MagicMock(), "example", time.monotonic() + 10)
        self.assertEqual(result["followers_count"], 2)

    def test_checker_page_can_resolve_identity_without_profile_api(self):
        checker = direct.requests.Session()
        response = MagicMock(status_code=200, text=page(), url=URL)
        with patch.object(direct, "_public_page_profile", return_value=None), patch.object(checker, "get", return_value=response), patch.object(direct, "_fallback_sessions", return_value=[]), patch.object(direct, "_pace"), patch.object(direct, "_json_get") as api:
            result = direct._profile(checker, "example", time.monotonic() + 10)
        checker.close()
        self.assertEqual(result["id"], "123")
        api.assert_not_called()

    def test_snapshot_still_rechecks_profile_and_uses_complete_followers(self):
        profile = parse_profile_page(page(description="2 Followers, 1 Following, 0 Posts"), "example", URL)
        lists = [[{"id": "1", "username": "alice"}], [{"id": "2", "username": "bob"}, {"id": "3", "username": "carol"}]]
        with patch.object(direct, "_make_session"), patch.object(direct, "_public_page_profile", return_value=profile) as public, patch.object(direct, "_json_get") as api, patch.object(direct, "_collect_pages", side_effect=lists) as collect:
            result = direct.collect_direct_snapshot("example", time.monotonic() + 10)
        self.assertEqual(public.call_count, 2)
        api.assert_not_called()
        self.assertEqual(result["account"], "example")
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["followers"]), 2)
        self.assertNotIn("allow_partial", collect.call_args_list[1].kwargs)

    def test_changed_profile_counts_are_rejected(self):
        before = parse_profile_page(page(description="0 Followers, 0 Following, 0 Posts"), "example", URL)
        after = {**before, "followers_count": 1}
        with patch.object(direct, "_make_session"), patch.object(direct, "_public_page_profile", side_effect=[before, after]):
            with self.assertRaises(HTTPException) as error:
                direct.collect_direct_snapshot("example", time.monotonic() + 10)
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()

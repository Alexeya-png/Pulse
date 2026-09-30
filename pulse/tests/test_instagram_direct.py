import time
import unittest
from unittest.mock import MagicMock, patch

from pulse import instagram_direct
from pulse.model import Member


class InstagramDirectTests(unittest.TestCase):
    def test_session_parser_requires_sessionid(self):
        with self.assertRaises(instagram_direct.InstagramDirectError):
            instagram_direct._normalize_session_data({"cookies": {"csrftoken": "x"}})

    def test_session_user_id_reads_cookie_or_session_prefix(self):
        self.assertEqual(instagram_direct._session_user_id({"ds_user_id": "123"}), "123")
        self.assertEqual(instagram_direct._session_user_id({"sessionid": "456%3Aabc"}), "456")

    def test_current_profile_must_match_target_account(self):
        session = MagicMock()
        with patch.object(
            instagram_direct,
            "_json_get",
            return_value=(200, {
                "user": {
                    "pk": "123",
                    "username": "other",
                    "follower_count": 1,
                    "following_count": 1,
                }
            }),
        ):
            with self.assertRaises(instagram_direct.InstagramDirectError) as error:
                instagram_direct._current_profile(
                    session,
                    {"ds_user_id": "123"},
                    "example",
                    time.monotonic() + 10,
                )
        self.assertIn("@other", str(error.exception))
        self.assertIn("@example", str(error.exception))

    def test_relationship_hosts_merge_by_numeric_id(self):
        session = MagicMock()
        pages = [
            ([{"pk": "1", "username": "alice"}, {"pk": "2", "username": "bob"}], None),
            ([{"pk": "2", "username": "bob"}, {"pk": "3", "username": "carol"}], None),
        ]
        with patch.object(instagram_direct, "_relation_page", side_effect=pages):
            result = instagram_direct._collect_relation(
                session,
                "999",
                "following",
                3,
                time.monotonic() + 10,
            )
        self.assertEqual({member.user_id for member in result}, {"1", "2", "3"})

    def test_incomplete_relationship_is_rejected(self):
        session = MagicMock()
        with patch.object(
            instagram_direct,
            "_relation_page",
            side_effect=[
                ([{"pk": "1", "username": "alice"}], None),
                ([{"pk": "1", "username": "alice"}], None),
            ],
        ):
            with self.assertRaises(instagram_direct.InstagramDirectError) as error:
                instagram_direct._collect_relation(
                    session,
                    "999",
                    "following",
                    2,
                    time.monotonic() + 10,
                )
        self.assertIn("1 из 2", str(error.exception))

    def test_complete_snapshot_rechecks_counts(self):
        before = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        followers = (Member("alice", "1"),)
        following = (Member("bob", "2"),)
        session = MagicMock()
        with patch.object(instagram_direct, "_make_session", return_value=(session, {"ds_user_id": "123"})),                 patch.object(instagram_direct, "_current_profile", side_effect=[before, before]),                 patch.object(instagram_direct, "_collect_relation", side_effect=[following, followers]):
            snapshot = instagram_direct.collect_snapshot_direct(
                "example",
                {"cookies": {"sessionid": "123:test"}},
            )
        self.assertEqual(snapshot.source, "instagram-device-direct")
        self.assertEqual(snapshot.samples[0].members, followers)
        self.assertEqual(snapshot.samples[1].members, following)
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

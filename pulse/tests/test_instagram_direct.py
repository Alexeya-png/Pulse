import time
import unittest
from unittest.mock import MagicMock, patch

from pulse import instagram_direct
from pulse.model import Member


class InstagramDirectTests(unittest.TestCase):
    def test_public_profile_is_read_without_session(self):
        session = MagicMock()
        with patch.object(
            instagram_direct,
            "_json_get",
            return_value=(200, {
                "data": {
                    "user": {
                        "id": "123",
                        "username": "example",
                        "edge_followed_by": {"count": 2},
                        "edge_follow": {"count": 3},
                        "is_private": False,
                    }
                }
            }),
        ):
            profile = instagram_direct._profile(
                session,
                "example",
                time.monotonic() + 10,
            )
        self.assertEqual(profile["id"], "123")
        self.assertEqual(profile["followers_count"], 2)
        self.assertEqual(profile["following_count"], 3)

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

    def test_incomplete_anonymous_relationship_is_rejected(self):
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

    def test_complete_anonymous_snapshot_rechecks_counts(self):
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
        with patch.object(instagram_direct, "_make_session", return_value=session),              patch.object(instagram_direct, "_profile", side_effect=[before, before]),              patch.object(instagram_direct, "_collect_relation", side_effect=[following, followers]):
            snapshot = instagram_direct.collect_snapshot_direct("example")

        self.assertEqual(snapshot.source, "instagram-device-anonymous")
        self.assertEqual(snapshot.samples[0].members, followers)
        self.assertEqual(snapshot.samples[1].members, following)
        session.close.assert_called_once()

    def test_private_profile_is_rejected_without_login(self):
        profile = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": True,
        }
        session = MagicMock()
        with patch.object(instagram_direct, "_make_session", return_value=session),              patch.object(instagram_direct, "_profile", return_value=profile):
            with self.assertRaises(instagram_direct.InstagramDirectError) as error:
                instagram_direct.collect_snapshot_direct("example")
        self.assertIn("без авторизации", str(error.exception).lower())


if __name__ == "__main__":
    unittest.main()

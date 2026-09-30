import unittest
from unittest.mock import MagicMock, patch

from pulse.checker_client import CheckerError, collect_snapshot
from pulse.instagram_direct import InstagramDirectError
from pulse.model import Member, Sample, Snapshot


class CheckerClientTests(unittest.TestCase):
    def test_collect_snapshot_uses_anonymous_device_collector_first(self):
        expected = Snapshot(
            "example",
            "2026-09-30T00:00:00+00:00",
            (
                Sample("followers", "", (Member("alice", "1"),), "id"),
                Sample("following", "", (Member("bob", "2"),), "id"),
            ),
            "instagram-device-anonymous",
        )
        with patch("pulse.checker_client.collect_snapshot_direct", return_value=expected) as direct:
            result = collect_snapshot("Example")
        self.assertIs(result, expected)
        direct.assert_called_once_with("example")

    def test_collect_snapshot_falls_back_to_our_backend_without_user_login(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "account": "example",
            "captured_at": "2026-09-30T00:00:00+00:00",
            "followers_count": 1,
            "following_count": 1,
            "followers": [{"id": "1", "username": "alice"}],
            "following": [{"id": "2", "username": "bob"}],
            "complete": True,
            "source": "pulse-direct-server",
        }
        with patch(
            "pulse.checker_client.collect_snapshot_direct",
            side_effect=InstagramDirectError("anonymous relationship endpoint unavailable"),
        ), patch("pulse.checker_client.requests.post", return_value=response) as post:
            result = collect_snapshot("Example")

        self.assertEqual(result.source, "pulse-direct-server")
        self.assertEqual(result.samples[0].members, (Member("alice", "1"),))
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"], {"username": "example"})

    def test_backend_partial_result_is_rejected(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "account": "example",
            "captured_at": "2026-09-30T00:00:00+00:00",
            "followers_count": 2,
            "following_count": 0,
            "followers": [{"id": "1", "username": "alice"}],
            "following": [],
            "complete": True,
        }
        with patch(
            "pulse.checker_client.collect_snapshot_direct",
            side_effect=InstagramDirectError("anonymous unavailable"),
        ), patch("pulse.checker_client.requests.post", return_value=response):
            with self.assertRaises(CheckerError) as error:
                collect_snapshot("example")
        self.assertIn("не совпало", str(error.exception).lower())


if __name__ == "__main__":
    unittest.main()

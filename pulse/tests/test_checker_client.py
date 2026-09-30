import unittest
from unittest.mock import patch

from pulse.checker_client import CheckerError, collect_snapshot
from pulse.model import Member, Sample, Snapshot


class CheckerClientTests(unittest.TestCase):
    def test_collect_snapshot_uses_local_direct_collector(self):
        expected = Snapshot(
            "example",
            "2026-09-30T00:00:00+00:00",
            (
                Sample("followers", "", (Member("alice", "1"),), "id"),
                Sample("following", "", (Member("bob", "2"),), "id"),
            ),
            "instagram-device-direct",
        )
        session = {"cookies": {"sessionid": "1:test", "ds_user_id": "1"}}
        with patch("pulse.checker_client.collect_snapshot_direct", return_value=expected) as direct:
            result = collect_snapshot("Example", session)
        self.assertIs(result, expected)
        direct.assert_called_once_with("example", session)

    def test_collect_snapshot_requires_private_instagram_session(self):
        with self.assertRaises(CheckerError) as error:
            collect_snapshot("example", None)
        self.assertIn("войти", str(error.exception).lower())


if __name__ == "__main__":
    unittest.main()

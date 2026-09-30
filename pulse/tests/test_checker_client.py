import io
import json
import unittest
from unittest.mock import patch

from pulse.checker_client import collect_snapshot


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _):
        return self.payload


class CheckerClientTests(unittest.TestCase):
    def test_collect_snapshot_reads_server_payload(self):
        payload = {
            "account": "example",
            "captured_at": "2026-09-30T00:00:00+00:00",
            "followers_count": 1,
            "following_count": 1,
            "followers": [{"id": "1", "username": "alice"}],
            "following": [{"id": "2", "username": "bob"}],
            "complete": True,
            "source": "apify-online",
        }
        with patch("pulse.checker_client._request", return_value=FakeResponse(payload)):
            snapshot = collect_snapshot("Example")
        self.assertEqual(snapshot.account, "example")
        self.assertEqual(snapshot.samples[0].members[0].username, "alice")
        self.assertEqual(snapshot.samples[1].members[0].username, "bob")


if __name__ == "__main__":
    unittest.main()

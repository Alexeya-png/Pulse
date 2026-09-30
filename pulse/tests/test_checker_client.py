import json
import unittest
from unittest.mock import MagicMock, patch

from pulse.checker_client import CheckerError, collect_snapshot, session_settings_from_json


class CheckerClientTests(unittest.TestCase):
    def test_accepts_flat_render_session_json(self):
        settings = session_settings_from_json(json.dumps({
            "sessionid": "fixture-session",
            "csrftoken": "fixture-csrf",
            "ds_user_id": "123",
            "unrelated": "ignored",
        }))
        self.assertEqual(settings["version"], 3)
        self.assertEqual(settings["cookies"]["sessionid"], "fixture-session")
        self.assertNotIn("unrelated", settings["cookies"])

    def test_accepts_nested_cookie_settings(self):
        settings = session_settings_from_json(json.dumps({
            "cookies": {"sessionid": "fixture-session"},
            "user_agent": "fixture-agent",
        }))
        self.assertEqual(settings["cookies"], {"sessionid": "fixture-session"})
        self.assertEqual(settings["user_agent"], "fixture-agent")

    def test_sessionid_is_required(self):
        with self.assertRaises(CheckerError):
            session_settings_from_json('{"csrftoken":"x"}')

    def test_collect_snapshot_is_direct_and_closes_client(self):
        client = MagicMock()
        collection = MagicMock()
        collection.snapshot = object()
        reader = MagicMock()
        reader.collect.return_value = collection
        settings = {"version": 3, "cookies": {"sessionid": "x"}, "user_agent": "ua"}

        with patch("pulse.checker_client.make_client", return_value=client), \
                patch("pulse.checker_client.InstagramReader", return_value=reader):
            snapshot = collect_snapshot("Example", settings)

        self.assertIs(snapshot, collection.snapshot)
        client.set_settings.assert_called_once_with(settings)
        reader.collect.assert_called_once_with("example")
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

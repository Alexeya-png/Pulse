import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pulse.checker_client import CheckerError, collect_snapshot
from pulse.instagram_direct import InstagramDirectError
from pulse.model import DataError, Member, Sample, Snapshot
from pulse.presentation import nonreciprocal_empty, nonreciprocal_notice
from pulse.store import Store


def people(*ids):
    return tuple(Member(f"user_{i}", str(i)) for i in ids)


def capture(day, followers, visible, expected):
    return Snapshot("owner", f"2026-10-{day:02d}T00:00:00Z", (
        Sample("followers", "", people(*followers), "id"),
        Sample("following", "", people(*visible), "id", len(visible) == expected, expected),
    ))


class PartialFollowingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = Store(self.path)

    def test_partial_following_has_confirmed_nonreciprocal_from_first_capture(self):
        self.store.ingest(capture(1, [1, 2], [2, 3], 4))
        result = self.store.nonreciprocal("owner")
        self.assertTrue(result["ready"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["hidden_count"], 2)
        self.assertEqual([r["member_key"] for r in result["users"]], ["3"])
        self.assertEqual(self.store.summary("owner")["latest_unfollowers"], 0)

    def test_departed_follower_is_detected_with_partial_following(self):
        self.store.ingest(capture(1, [1, 2], [1, 3], 4))
        self.store.ingest(capture(2, [2, 3], [1, 3], 4))
        self.assertEqual([r["member_key"] for r in self.store.events("owner", "followers", "removed", latest=True)], ["1"])
        self.assertEqual([r["member_key"] for r in self.store.nonreciprocal("owner")["users"]], ["1"])
        self.store.ingest(capture(3, [2, 3], [1, 3], 4))
        self.assertEqual(self.store.events("owner", "followers", "removed", latest=True), [])
        self.assertEqual(len(self.store.events("owner", "followers", "removed")), 1)

    def test_hidden_id_replacement_never_reuses_stale_nonreciprocal(self):
        self.store.ingest(capture(1, [1], [1, 2], 2))
        self.store.ingest(capture(2, [1], [1], 2))
        result = self.store.nonreciprocal("owner")
        self.assertEqual(result["users"], [])
        self.assertEqual(result["hidden_count"], 1)
        self.assertFalse(result["complete"])
        self.assertNotIn("Невзаимных подписок нет", nonreciprocal_empty(result)[0])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='following'").fetchone()[0], 0)

    def test_recovery_to_full_following_changes_coverage_without_false_events(self):
        self.store.ingest(capture(1, [1], [1], 2))
        self.store.ingest(capture(2, [1], [1, 3], 2))
        result = self.store.nonreciprocal("owner")
        self.assertTrue(result["complete"])
        self.assertEqual(result["hidden_count"], 0)
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.store.summary("owner")["latest_unfollowers"], 0)

    def test_same_rows_with_changed_counter_updates_coverage(self):
        self.store.ingest(capture(1, [1], [1], 2))
        self.store.ingest(capture(2, [1], [1], 3))
        self.assertEqual(self.store.nonreciprocal("owner")["hidden_count"], 2)
        self.store.ingest(capture(3, [1], [1], 1))
        self.assertTrue(self.store.nonreciprocal("owner")["complete"])

    def test_conflicting_coverage_at_same_time_is_rejected(self):
        self.store.ingest(capture(1, [1], [1], 2))
        with self.assertRaises(DataError):
            self.store.ingest(capture(1, [1], [1], 3))
        self.assertEqual(self.store.nonreciprocal("owner")["hidden_count"], 1)

    def test_coverage_and_history_survive_reopening(self):
        self.store.ingest(capture(1, [1, 2], [3], 4))
        self.store.ingest(capture(2, [1], [3], 4))
        reopened = Store(self.path)
        self.assertEqual(reopened.nonreciprocal("owner")["hidden_count"], 3)
        self.assertEqual(reopened.summary("owner")["latest_unfollowers"], 1)
        self.assertEqual(reopened.observations("owner")[0]["complete"], 0)

    def test_partial_followers_cannot_enter_storage(self):
        with self.assertRaises(DataError):
            Sample("followers", "", people(1), "id", False, 2)
        for count in (-1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(DataError):
                Sample("following", "", (), "id", False, count)

    def test_rejected_backend_snapshot_leaves_history_untouched(self):
        self.store.ingest(capture(1, [1, 2], [3], 4))
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "account": "owner", "captured_at": "2026-10-02T00:00:00Z",
            "followers_count": 2, "following_count": 4,
            "followers": [{"id": "1", "username": "user_1"}], "following": [],
            "followers_complete": False, "following_complete": False, "complete": False,
        }
        with patch("pulse.checker_client.collect_snapshot_direct", side_effect=InstagramDirectError("unavailable")), patch("pulse.checker_client.requests.post", return_value=response):
            with self.assertRaises(CheckerError):
                self.store.ingest(collect_snapshot("owner"))
        self.assertEqual(self.store.summary("owner")["followers"], 2)
        self.assertEqual(self.store.events("owner", "followers", "removed"), [])

    def test_empty_partial_is_unknown_not_all_mutual(self):
        self.store.ingest(capture(1, [1], [], 4))
        result = self.store.nonreciprocal("owner")
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["complete"])
        self.assertIn("4", nonreciprocal_notice(result))
        self.assertIn("пока нет данных", nonreciprocal_empty(result)[1])

    def test_partial_pagination_and_account_isolation(self):
        self.store.ingest(capture(1, [], list(range(175)), 180))
        seen = []
        cursor = None
        for _ in range(3):
            result = self.store.nonreciprocal("owner", after=cursor, limit=80)
            seen.extend(row["member_key"] for row in result["users"])
            cursor = seen[-1]
            self.assertEqual(result["hidden_count"], 5)
        self.assertEqual(len(set(seen)), 175)
        self.assertFalse(self.store.nonreciprocal("other")["ready"])

    def test_migration_preserves_followers_but_invalidates_reconciled_following(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript("""
                CREATE TABLE streams(id INTEGER PRIMARY KEY,account TEXT,kind TEXT,subject TEXT,
                    identity TEXT,captured_at TEXT,digest TEXT,member_count INTEGER,
                    UNIQUE(account,kind,subject));
                CREATE TABLE members(stream_id INTEGER,member_key TEXT,username TEXT,PRIMARY KEY(stream_id,member_key));
                CREATE TABLE observations(id INTEGER PRIMARY KEY,stream_id INTEGER,captured_at TEXT,
                    member_count INTEGER,source TEXT,added INTEGER,removed INTEGER,baseline INTEGER,
                    UNIQUE(stream_id,captured_at));
                INSERT INTO streams VALUES(1,'owner','followers','','id','2026-10-01T00:00:00.000000+00:00','old',1);
                INSERT INTO streams VALUES(2,'owner','following','','id','2026-10-01T00:00:00.000000+00:00','old',2);
                INSERT INTO members VALUES(1,'1','user_1'),(2,'1','user_1'),(2,'2','user_2');
                INSERT INTO observations VALUES(1,1,'2026-10-01T00:00:00.000000+00:00',1,'local-reconciled',0,0,1);
                INSERT INTO observations VALUES(2,2,'2026-10-01T00:00:00.000000+00:00',2,'local-reconciled',0,0,1);
                PRAGMA user_version=1;
            """)
        db.close()
        migrated = Store(legacy)
        self.assertFalse(migrated.nonreciprocal("owner")["ready"])
        self.assertEqual(migrated.current_members("owner", "followers"), people(1))
        self.assertEqual(len(migrated.observations("owner")), 2)
        migrated.ingest(capture(2, [1], [3], 2))
        self.assertEqual(migrated.nonreciprocal("owner")["total"], 1)
        self.assertFalse(migrated.nonreciprocal("owner")["complete"])
        self.assertEqual(migrated.summary("owner")["latest_unfollowers"], 0)


if __name__ == "__main__":
    unittest.main()

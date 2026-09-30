import tempfile
import unittest
from pathlib import Path

from pulse.model import Member, Sample, Snapshot
from pulse.store import Store


def people(*ids):
    return tuple(Member(f"user_{i}", str(i)) for i in ids)


def snap(day, followers, following, account="owner"):
    return Snapshot(account, f"2026-09-{day:02d}T12:00:00Z", (Sample("followers", "", followers, "id"), Sample("following", "", following, "id")))


class ReciprocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "history.sqlite3")

    def test_first_visit_shows_nonreciprocal_but_no_unfollowers(self):
        self.store.ingest(snap(1, people(1, 2), people(2, 3)))
        result = self.store.nonreciprocal("owner")
        self.assertTrue(result["ready"])
        self.assertEqual([r["member_key"] for r in result["users"]], ["3"])
        self.assertEqual(self.store.summary("owner")["latest_unfollowers"], 0)

    def test_second_visit_finds_departed_and_new_reciprocal(self):
        self.store.ingest(snap(1, people(1, 2), people(1, 3)))
        self.store.ingest(snap(2, people(2, 3), people(1, 3)))
        self.assertEqual([r["member_key"] for r in self.store.events("owner", "followers", "removed", latest=True)], ["1"])
        self.assertEqual([r["member_key"] for r in self.store.nonreciprocal("owner")["users"]], ["1"])

    def test_rename_uses_ids_not_usernames(self):
        self.store.ingest(snap(1, (Member("renamed", "1"),), (Member("previous_name", "1"),)))
        self.assertEqual(self.store.nonreciprocal("owner")["total"], 0)

    def test_missing_or_stale_pair_is_unknown_not_zero(self):
        self.assertIsNone(self.store.nonreciprocal("owner")["total"])
        self.store.ingest(snap(1, people(1), people(2)))
        self.store.ingest(Snapshot("owner", "2026-09-02T12:00:00Z", (Sample("followers", "", people(1, 2), "id"),)))
        self.assertFalse(self.store.nonreciprocal("owner")["ready"])

    def test_all_following_pages_with_cursor(self):
        self.store.ingest(snap(1, (), people(*range(175))))
        first = self.store.nonreciprocal("owner", limit=80)
        second = self.store.nonreciprocal("owner", after=first["users"][-1]["member_key"], limit=80)
        third = self.store.nonreciprocal("owner", after=second["users"][-1]["member_key"], limit=80)
        rows = first["users"] + second["users"] + third["users"]
        self.assertEqual(len({r["member_key"] for r in rows}), 175)
        self.assertEqual(first["total"], 175)

    def test_no_changes_on_third_visit_old_events_only_in_history(self):
        self.store.ingest(snap(1, people(1), people(1)))
        self.store.ingest(snap(2, (), people(1)))
        self.store.ingest(snap(3, (), people(1)))
        self.assertEqual(self.store.events("owner", "followers", "removed", latest=True), [])
        self.assertEqual(len(self.store.events("owner", "followers", "removed")), 1)
        self.assertEqual(self.store.summary("owner")["latest_unfollowers"], 0)

    def test_account_isolation_and_empty_following(self):
        self.store.ingest(snap(1, people(1), (), account="first"))
        self.store.ingest(snap(1, (), people(1), account="second"))
        self.assertEqual(self.store.nonreciprocal("first")["total"], 0)
        self.assertEqual(self.store.nonreciprocal("second")["total"], 1)


if __name__ == "__main__":
    unittest.main()

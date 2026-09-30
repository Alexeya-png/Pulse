import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pulse.importer import load_snapshot
from pulse.model import DataError, Member, Sample, Snapshot
from pulse.store import Store


def followers(*names):
    return Sample("followers", "", tuple(Member(n) for n in names))


def snapshot(day, *samples, account="owner"):
    return Snapshot(account, f"2026-09-{day:02d}T12:00:00Z", tuple(samples))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data.sqlite3")

    def test_first_snapshot_is_baseline_not_mass_addition(self):
        result = self.store.ingest(snapshot(1, followers("a", "b")))
        self.assertEqual((result.baselines, result.added, result.removed), (1, 0, 0))
        self.assertEqual(self.store.events("owner", "followers", "added"), [])

    def test_diffs_equal_counts_and_same_count_liker_replacement(self):
        self.store.ingest(snapshot(1, followers("a", "b"), Sample("likes", "post", (Member("a"),))))
        result = self.store.ingest(snapshot(2, followers("b", "c"), Sample("likes", "post", (Member("b"),))))
        self.assertEqual((result.added, result.removed), (2, 2))
        self.assertEqual(self.store.events("owner", "likes", "removed")[0]["username"], "a")

    def test_unavailable_post_is_not_zero_likes(self):
        self.store.ingest(snapshot(1, Sample("likes", "one", (Member("a"),))))
        self.store.ingest(snapshot(2, followers("b")))
        self.assertEqual(self.store.summary("owner")["unlikes"], 0)
        result = self.store.ingest(snapshot(3, Sample("likes", "one", ())))
        self.assertEqual(result.removed, 1)

    def test_id_rename_not_unfollow(self):
        self.store.ingest(snapshot(1, Sample("followers", "", (Member("old", "5"),), "id")))
        result = self.store.ingest(snapshot(2, Sample("followers", "", (Member("new", "5"),), "id")))
        self.assertEqual((result.added, result.removed), (0, 0))

    def test_replay_is_idempotent_even_with_reordered_members(self):
        self.store.ingest(snapshot(1, followers("a", "b")))
        result = self.store.ingest(snapshot(1, followers("b", "a")))
        self.assertEqual(result.unchanged_imports, 1)
        self.assertEqual(len(self.store.observations("owner")), 1)

    def test_old_and_conflicting_dates_rejected(self):
        self.store.ingest(snapshot(2, followers("a")))
        for data in (snapshot(1, followers("a")), snapshot(2, followers("b"))):
            with self.assertRaises(DataError):
                self.store.ingest(data)
        self.assertEqual(self.store.summary("owner")["unfollowers"], 0)

    def test_identity_mode_change_rolls_back_entire_import(self):
        self.store.ingest(snapshot(1, followers("a"), Sample("likes", "p", (Member("x"),))))
        bad = snapshot(2, followers("b"), Sample("likes", "p", (Member("x", "1"),), "id"))
        with self.assertRaises(DataError):
            self.store.ingest(bad)
        self.assertEqual(len(self.store.observations("owner")), 2)
        self.assertEqual(self.store.summary("owner")["unfollowers"], 0)

    def test_accounts_and_media_are_isolated(self):
        self.store.ingest(snapshot(1, followers("a"), account="first"))
        self.store.ingest(snapshot(2, followers(), account="second"))
        self.assertEqual(self.store.summary("first")["followers"], 1)
        self.assertEqual(self.store.summary("second")["unfollowers"], 0)

    def test_cursor_pages_no_duplicates(self):
        self.store.ingest(snapshot(1, followers(*(f"user_{i}" for i in range(175)))))
        self.store.ingest(snapshot(2, followers()))
        first = self.store.events("owner", "followers", "removed", limit=80)
        second = self.store.events("owner", "followers", "removed", before=first[-1]["id"], limit=80)
        third = self.store.events("owner", "followers", "removed", before=second[-1]["id"], limit=80)
        self.assertEqual(len({r["id"] for r in first + second + third}), 175)

    def test_timezone_ordering(self):
        self.store.ingest(Snapshot("owner", "2026-09-01T13:00:00+03:00", (followers("a"),)))
        result = self.store.ingest(Snapshot("owner", "2026-09-01T11:00:00Z", (followers(),)))
        self.assertEqual(result.removed, 1)

    def test_unchanged_list_advances_observation_interval(self):
        self.store.ingest(snapshot(1, followers("a")))
        same = self.store.ingest(snapshot(2, followers("a")))
        self.assertEqual((same.compared, same.removed, same.added), (1, 0, 0))
        self.store.ingest(snapshot(3, followers()))
        event = self.store.events("owner", "followers", "removed")[0]
        self.assertTrue(event["since"].startswith("2026-09-02"))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def load(self, path, complete=True):
        return load_snapshot(path, "owner", "2026-09-01T10:00:00Z", export_complete=complete)

    def entry(self, name):
        return {"string_list_data": [{"value": name, "timestamp": 1}]}

    def test_export_normalizes_deduplicates_ignores_follow_time(self):
        data = self.load(self.write("followers_1.json", [self.entry("ALICE"), self.entry("alice")]))
        self.assertEqual(len(data.samples[0].members), 1)
        self.assertTrue(data.captured_at.startswith("2026-09"))

    def test_bad_record_does_not_silently_disappear(self):
        with self.assertRaises(DataError):
            self.load(self.write("followers_1.json", [self.entry("a"), {}]))

    def test_custom_rejects_incomplete_wrong_owner_and_count(self):
        valid = {"schema_version": 1, "account": "owner", "captured_at": "2026-09-01T10:00:00Z", "followers": {"complete": True, "users": ["a"]}}
        for override in ({"account": "other"}, {"followers": {"complete": False, "users": []}}, {"followers": {"complete": True, "users": ["a"], "expected_count": 2}}):
            with self.assertRaises(DataError):
                self.load(self.write("snapshot.json", valid | override))

    def test_outgoing_likes_file_is_not_incoming_likers(self):
        with self.assertRaises(DataError):
            self.load(self.write("liked_posts.json", {"likes_media_likes": []}))

    def test_zip_merges_parts_and_ignores_unrelated_files(self):
        path = self.root / "archive.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for i, user in enumerate(("a", "b"), 1):
                archive.writestr(f"connections/followers_and_following/followers_{i}.json", json.dumps([self.entry(user)]))
            archive.writestr("media/irrelevant.txt", "not json")
        self.assertEqual(len(self.load(path).samples[0].members), 2)

    def test_zip_gaps_and_traversal_rejected(self):
        for names in (("followers_1.json", "followers_3.json"), ("../followers_1.json",)):
            path = self.root / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name in names:
                    archive.writestr(name, "[]")
            with self.assertRaises(DataError):
                self.load(path)

    def test_duplicate_json_keys_rejected(self):
        path = self.root / "snapshot.json"
        path.write_text('{"schema_version": 1, "schema_version": 2}')
        with self.assertRaises(DataError):
            self.load(path)

    def test_naive_time_and_mixed_id_modes_rejected(self):
        with self.assertRaises(DataError):
            Snapshot("owner", "2026-09-01T10:00:00", (followers(),))
        with self.assertRaises(DataError):
            Sample("followers", "", (Member("a", "1"), Member("b")), "id")

    def test_full_export_requires_confirmation(self):
        with self.assertRaises(DataError):
            self.load(self.write("followers_1.json", []), complete=False)


if __name__ == "__main__":
    unittest.main()

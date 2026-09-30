import unittest
from threading import Event

from pulse.instagram import InstagramReader, SyncError, safe_error


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, path, params):
        self.calls.append((path, params))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def ok(**fields):
    return {"status": "ok", **fields}


def user(pk, name="alice"):
    return {"pk": pk, "username": name}


def profile(count, following=0):
    return ok(user={"pk": "9", "follower_count": count, "following_count": following})


class ReaderTests(unittest.TestCase):
    def test_paginates_and_deduplicates(self):
        client = FakeClient([ok(users=[user(1)], next_max_id="next"), ok(users=[user(1), user(2, "bob")])])
        result = InstagramReader(client).users("test", 2, "followers")
        self.assertEqual(len(result), 2)
        self.assertEqual(client.calls[1][1]["max_id"], "next")

    def test_incomplete_page_is_rejected(self):
        with self.assertRaises(SyncError) as raised:
            InstagramReader(FakeClient([ok(users=[user(1)])])).users("test", 2, "")
        self.assertEqual(raised.exception.code, "incomplete")

    def test_repeated_cursor_is_rejected(self):
        client = FakeClient([ok(users=[user(1)], next_max_id="x"), ok(users=[user(1)], next_max_id="x")])
        with self.assertRaises(SyncError):
            InstagramReader(client).users("test", 2, "")

    def test_network_failure_cannot_become_empty_list(self):
        client = FakeClient([ok(users=[user(1)], next_max_id="x"), OSError("secret token")])
        with self.assertRaises(SyncError) as raised:
            InstagramReader(client).users("test", 2, "")
        self.assertNotIn("secret", str(raised.exception))

    def test_likes_skipped_when_endpoint_truncated(self):
        client = FakeClient([profile(1), ok(users=[user(1)]), ok(users=[]), profile(1), ok(items=[{"pk": 11}]), ok(items=[{"like_count": 2}]), ok(users=[user(1)])])
        result = InstagramReader(client).collect("owner")
        self.assertEqual([s.kind for s in result.snapshot.samples], ["followers", "following"])
        self.assertEqual(len(result.warnings), 1)

    def test_follower_count_changed_during_fetch(self):
        client = FakeClient([profile(1), ok(users=[user(1)]), ok(users=[]), profile(2)])
        with self.assertRaises(SyncError):
            InstagramReader(client).collect("owner", recent_posts=0)

    def test_real_zero_list_supported(self):
        client = FakeClient([profile(0), ok(users=[]), ok(users=[]), profile(0)])
        result = InstagramReader(client).collect("owner", recent_posts=0)
        self.assertEqual(result.snapshot.samples[0].members, ())

    def test_complete_likers_included(self):
        client = FakeClient([profile(1), ok(users=[user(1)]), ok(users=[]), profile(1), ok(items=[{"pk": 11}]), ok(items=[{"like_count": 1}]), ok(users=[user(2)]), ok(items=[{"like_count": 1}])])
        result = InstagramReader(client).collect("owner")
        self.assertEqual(result.snapshot.samples[2].members[0].user_id, "2")

    def test_cancellation_and_budget_make_no_requests(self):
        event = Event()
        event.set()
        for reader in (InstagramReader(FakeClient([]), cancel=event), InstagramReader(FakeClient([]), request_limit=0)):
            with self.assertRaises(SyncError):
                reader.collect("owner")
            self.assertEqual(reader.client.calls, [])

    def test_same_count_requires_fetch_not_count_only_cache(self):
        first = InstagramReader(FakeClient([profile(1), ok(users=[user(1)]), ok(users=[]), profile(1)])).collect("owner", recent_posts=0)
        second = InstagramReader(FakeClient([profile(1), ok(users=[user(2)]), ok(users=[]), profile(1)])).collect("owner", recent_posts=0)
        self.assertNotEqual(first.snapshot.samples[0].members, second.snapshot.samples[0].members)

    def test_sensitive_error_redaction(self):
        self.assertNotIn("password", str(safe_error(ValueError("password=secret"))))

    def test_fetches_every_following_page(self):
        client = FakeClient([profile(1, 2), ok(users=[user(1)]), ok(users=[user(1)], next_max_id="next"), ok(users=[user(2)]), profile(1, 2)])
        result = InstagramReader(client).collect("owner", recent_posts=0)
        self.assertEqual(len(result.snapshot.samples[1].members), 2)
        self.assertIn("following", client.calls[2][0])

    def test_incomplete_following_does_not_commit_new_followers(self):
        client = FakeClient([profile(1, 2), ok(users=[user(1)]), ok(users=[user(1)])])
        with self.assertRaises(SyncError):
            InstagramReader(client).collect("owner", recent_posts=0)

    def test_changed_following_count_invalidates_pair(self):
        client = FakeClient([profile(1, 1), ok(users=[user(1)]), ok(users=[user(2)]), profile(1, 2)])
        with self.assertRaises(SyncError):
            InstagramReader(client).collect("owner", recent_posts=0)


if __name__ == "__main__":
    unittest.main()

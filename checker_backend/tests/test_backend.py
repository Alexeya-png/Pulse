import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from checker_backend import main


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_health_contract(self):
        with patch.dict(os.environ, {"APIFY_TOKEN": ""}):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "ok": True,
            "engine": "apify-online",
            "provider_configured": False,
            "hiker_dependency": False,
        })

    def test_input_validation(self):
        with patch.object(main, "_collect_profile") as collect:
            self.assertEqual(
                self.client.post("/v1/collect", json={"username": "bad/name"}).status_code,
                400,
            )
            self.assertEqual(self.client.post("/v1/collect", json={}).status_code, 422)
            collect.assert_not_called()

    def test_apify_token_required(self):
        with patch.dict(os.environ, {"APIFY_TOKEN": ""}):
            with self.assertRaises(HTTPException) as error:
                main._api_token()
        self.assertEqual(error.exception.status_code, 503)

    def test_profile_parses_exact_counts(self):
        rows = [{
            "id": "123",
            "username": "example",
            "followersCount": 7,
            "followsCount": 5,
            "private": False,
        }]
        with patch.object(main, "_request_json", return_value=rows):
            profile = main._profile("example")
        self.assertEqual(profile, {
            "id": "123",
            "username": "example",
            "followers_count": 7,
            "following_count": 5,
            "is_private": False,
        })

    def test_private_profile_is_rejected_before_lists(self):
        profile = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": True,
        }
        with patch.object(main, "_profile", return_value=profile), \
                patch.object(main, "_collect_relation") as relation:
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 403)
        relation.assert_not_called()

    def test_relation_paginates_with_continuation_and_deduplicates_ids(self):
        runs = [
            {"id": "r1", "defaultDatasetId": "d1"},
            {"id": "r2", "defaultDatasetId": "d2"},
        ]
        datasets = [
            [
                {"id": "1", "username": "Alice", "username_scrape": "example", "type": "Followers"},
                {"id": "2", "username": "Bob", "username_scrape": "example", "type": "Followers"},
            ],
            [
                {"id": "1", "username": "alice", "username_scrape": "example", "type": "Followers"},
                {"id": "3", "username": "Carol", "username_scrape": "example", "type": "Followers"},
            ],
        ]
        outputs = [
            {"continuations": [{
                "account": "example",
                "dataToScrape": "Followers",
                "hasNextPage": True,
                "nextContinuationToken": "next",
            }]},
            {"continuations": [{
                "account": "example",
                "dataToScrape": "Followers",
                "hasNextPage": False,
            }]},
        ]
        with patch.object(main, "_start_actor", side_effect=runs), \
                patch.object(main, "_wait_run", side_effect=runs), \
                patch.object(main, "_dataset_items", side_effect=datasets), \
                patch.object(main, "_run_output", side_effect=outputs):
            result = main._collect_relation_from_actor(
                "actor",
                25,
                {},
                "example",
                3,
                "Followers",
                "Подписчики",
            )
        self.assertEqual(result, [
            {"id": "1", "username": "alice"},
            {"id": "2", "username": "bob"},
            {"id": "3", "username": "carol"},
        ])

    def test_incomplete_relation_is_rejected(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        with patch.object(main, "_start_actor", return_value=run), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=[
                    {"id": "1", "username": "alice", "username_scrape": "example", "type": "Followers"},
                ]), \
                patch.object(main, "_run_output", return_value={}):
            result = main._collect_relation_from_actor(
                "actor",
                25,
                {},
                "example",
                2,
                "Followers",
                "Подписчики",
            )
        self.assertEqual(result, [{"id": "1", "username": "alice"}])

    def test_checker_sessionid_accepts_flat_and_nested_json(self):
        with patch.dict(
            os.environ,
            {"IG_SESSION_JSON": '{"sessionid":"flat-secret"}'},
        ):
            self.assertEqual(main._checker_sessionid(), "flat-secret")
        with patch.dict(
            os.environ,
            {"IG_SESSION_JSON": '{"cookies":{"sessionid":"nested-secret"}}'},
        ):
            self.assertEqual(main._checker_sessionid(), "nested-secret")

    def test_session_actor_following_uses_checker_session(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{
            "ok": True,
            "recordType": "relationship_profile",
            "id": "1",
            "username": "Alice",
            "sourceUsername": "example",
            "listType": "following",
        }]
        captured = {}

        def start(actor, body):
            captured["actor"] = actor
            captured["body"] = body
            return run

        with patch.object(main, "_start_actor", side_effect=start), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=rows), \
                patch.object(main, "_checker_sessionid", return_value="server-secret"):
            result = main._collect_session_actor(
                "example",
                1,
                "Followings",
                "Подписки",
            )

        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertEqual(captured["body"]["listType"], "following")
        self.assertEqual(captured["body"]["sessionCookies"], ["server-secret"])
        self.assertTrue(captured["body"]["proxyConfiguration"]["useApifyProxy"])

    def test_session_actor_followers_does_not_send_checker_session(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{
            "ok": True,
            "recordType": "relationship_profile",
            "id": "1",
            "username": "Alice",
            "sourceUsername": "example",
            "listType": "followers",
        }]
        captured = {}

        def start(actor, body):
            captured["body"] = body
            return run

        with patch.object(main, "_start_actor", side_effect=start), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=rows), \
                patch.object(main, "_checker_sessionid", return_value="server-secret"):
            result = main._collect_session_actor(
                "example",
                1,
                "Followers",
                "Подписчики",
            )

        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertNotIn("sessionCookies", captured["body"])

    def test_full_following_actor_uses_full_schema(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{
            "source_username": "example",
            "id": "1",
            "username": "Alice",
        }]
        captured = {}

        def start(actor, body):
            captured["actor"] = actor
            captured["body"] = body
            return run

        with patch.object(main, "_start_actor", side_effect=start), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=rows):
            result = main._collect_full_following_actor(
                "example",
                1,
                "Подписки",
            )

        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertEqual(captured["actor"], main.APIFY_FULL_FOLLOWING_ACTOR)
        self.assertEqual(captured["body"], {
            "username": ["example"],
            "type": "followings",
            "maxItem": 1,
            "enrichProfile": False,
            "fullProfileDetails": False,
        })

    def test_free_following_actor_uses_free_schema(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{
            "username_scrape": "example",
            "id": "1",
            "username": "Alice",
        }]
        captured = {}

        def start(actor, body):
            captured["actor"] = actor
            captured["body"] = body
            return run

        with patch.object(main, "_start_actor", side_effect=start), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=rows), \
                patch.object(main, "_run_output", return_value={"outcome": "COMPLETED"}):
            result = main._collect_free_following_actor(
                "example",
                1,
                "Подписки",
            )

        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertEqual(captured["actor"], main.APIFY_FREE_FOLLOWING_ACTOR)
        self.assertEqual(captured["body"], {
            "Account": ["example"],
            "resultsLimit": 25,
        })

    def test_official_actor_following_uses_public_schema(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{
            "sourceUsername": "example",
            "userId": "1",
            "username": "Alice",
            "type": "FOLLOWING",
        }]
        captured = {}

        def start(actor, body):
            captured["actor"] = actor
            captured["body"] = body
            return run

        with patch.object(main, "_start_actor", side_effect=start), \
                patch.object(main, "_wait_run", return_value=run), \
                patch.object(main, "_dataset_items", return_value=rows):
            result = main._collect_official_actor(
                "example",
                1,
                "Followings",
                "Подписки",
            )

        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertEqual(captured["actor"], main.APIFY_OFFICIAL_RELATION_ACTOR)
        self.assertEqual(captured["body"], {
            "usernames": ["example"],
            "dataToScrape": "following",
            "resultsLimit": 1,
        })

    def test_following_uses_full_provider_first(self):
        full_rows = [
            {"id": "1", "username": "alice"},
            {"id": "2", "username": "bob"},
        ]
        with patch.object(
            main,
            "_collect_full_following_actor",
            return_value=full_rows,
        ) as full_collect, patch.object(
            main,
            "_collect_free_following_actor",
        ) as free_collect, patch.object(
            main,
            "_collect_official_actor",
        ) as public_collect, patch.object(
            main,
            "_collect_session_actor",
        ) as session_collect:
            result = main._collect_relation(
                "example",
                2,
                "Followings",
                "Подписки",
            )

        self.assertEqual(len(result), 2)
        full_collect.assert_called_once_with(
            "example",
            2,
            "Подписки",
        )
        free_collect.assert_not_called()
        public_collect.assert_not_called()
        session_collect.assert_not_called()


    def test_relation_retries_merge_unique_ids(self):
        attempts = [
            [
                {"id": "1", "username": "alice"},
                {"id": "2", "username": "bob"},
            ],
            [
                {"id": "2", "username": "bob"},
                {"id": "3", "username": "carol"},
            ],
        ]

        with patch.object(
            main,
            "_collect_full_followers_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_session_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_relation_from_actor",
            side_effect=attempts,
        ), patch.object(
            main,
            "APIFY_RELATION_ATTEMPTS",
            2,
        ), patch.object(
            main,
            "APIFY_RETRY_DELAY",
            0,
        ):
            result = main._collect_relation(
                "example",
                3,
                "Followers",
                "Подписчики",
            )

        self.assertEqual(
            {item["id"] for item in result},
            {"1", "2", "3"},
        )

    def test_relation_rejects_one_hidden_record_for_large_list(self):
        exposed = [
            {"id": str(i), "username": f"user{i}"}
            for i in range(1, 110)
        ]
        with patch.object(
            main,
            "_collect_full_following_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_free_following_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_official_actor",
            return_value=exposed,
        ), patch.object(
            main,
            "_collect_session_actor",
            return_value=[],
        ):
            with self.assertRaises(HTTPException) as error:
                main._collect_relation(
                    "example",
                    110,
                    "Followings",
                    "Подписки",
                )
        self.assertEqual(error.exception.status_code, 409)

    def test_relation_rejects_two_hidden_records(self):
        exposed = [
            {"id": str(i), "username": f"user{i}"}
            for i in range(1, 109)
        ]
        with patch.object(
            main,
            "_collect_full_following_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_free_following_actor",
            return_value=[],
        ), patch.object(
            main,
            "_collect_official_actor",
            return_value=exposed,
        ), patch.object(
            main,
            "_collect_session_actor",
            return_value=[],
        ):
            with self.assertRaises(HTTPException) as error:
                main._collect_relation(
                    "example",
                    110,
                    "Followings",
                    "Подписки",
                )
        self.assertEqual(error.exception.status_code, 409)

    def test_collect_profile_requests_followings_plural(self):
        profile = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        users = [{"id": "1", "username": "alice"}]
        relation_calls = []

        def relation(username, expected, data_type, label):
            relation_calls.append(data_type)
            return users

        with patch.object(main, "_profile", side_effect=[profile, profile]), \
                patch.object(main, "_collect_relation", side_effect=relation):
            payload = main._collect_profile("example")

        self.assertCountEqual(relation_calls, ["Followings", "Followers"])
        self.assertTrue(payload["complete"])

    def test_counts_are_rechecked_after_lists(self):
        before = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        after = {**before, "followers_count": 2}
        users = [{"id": "1", "username": "alice"}]
        with patch.object(main, "_profile", side_effect=[before, after]), \
                patch.object(main, "_collect_relation", return_value=users):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("изменились", response.json()["detail"])

    def test_android_payload_contract_is_preserved(self):
        profile = {
            "id": "123",
            "username": "example",
            "followers_count": 1,
            "following_count": 1,
            "is_private": False,
        }
        users = [{"id": "1", "username": "alice"}]
        with patch.object(main, "_profile", side_effect=[profile, profile]), \
                patch.object(main, "_collect_relation", return_value=users):
            response = self.client.post("/v1/collect", json={"username": " @Example "})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {
            "account",
            "captured_at",
            "followers_count",
            "following_count",
            "followers",
            "following",
            "complete",
            "source",
        })
        self.assertEqual(payload["account"], "example")
        self.assertEqual(payload["followers"], users)
        self.assertEqual(payload["source"], "apify-online")
        self.assertTrue(payload["complete"])

    def test_concurrent_request_is_rejected_and_lock_released(self):
        entered, release = threading.Event(), threading.Event()

        def collect(_):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError()
            raise HTTPException(403, "Private account")

        with patch.object(main, "_collect_profile", side_effect=collect), \
                ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                self.client.post,
                "/v1/collect",
                json={"username": "example"},
            )
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.assertEqual(
                    self.client.post("/v1/collect", json={"username": "example"}).status_code,
                    429,
                )
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).status_code, 403)
        self.assertFalse(main._collect_lock.locked())

    def test_full_provider_resumes_partial_run(self):
        runs = [
            {"id": "r1", "defaultDatasetId": "d1"},
            {"id": "r2", "defaultDatasetId": "d2"},
        ]
        pages = [[{"id": "1", "username": "alice"}, {"id": "2", "username": "bob"}],
                 [{"id": "3", "username": "carol"}]]
        with (
            patch.object(main, "_start_actor", side_effect=runs) as start,
            patch.object(main, "_wait_run", side_effect=runs),
            patch.object(main, "_dataset_items", side_effect=pages),
            patch.object(main, "_run_output", return_value={"resumeCursor": "next-page"}),
        ):
            result = main._collect_full_following_actor("example", 3, "Подписки")
        self.assertEqual({row["id"] for row in result}, {"1", "2", "3"})
        self.assertEqual(start.call_args_list[1].args[1]["resumeCursor"], "next-page")
        self.assertEqual(start.call_args_list[1].args[1]["maxItem"], 1)

    def test_full_provider_rejects_repeated_cursor(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        with (
            patch.object(main, "_start_actor", return_value=run) as start,
            patch.object(main, "_wait_run", return_value=run),
            patch.object(main, "_dataset_items", return_value=[{"id": "1", "username": "alice"}]),
            patch.object(main, "_run_output", return_value={"resumeCursor": "same-page"}),
        ):
            with self.assertRaises(HTTPException) as error:
                main._collect_full_following_actor("example", 3, "Подписки")
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(start.call_count, 2)

    def test_followers_use_full_provider_with_correct_direction(self):
        run = {"id": "r1", "defaultDatasetId": "d1"}
        rows = [{"id": "1", "username": "alice", "type": "followers"}]
        with (
            patch.object(main, "_start_actor", return_value=run) as start,
            patch.object(main, "_wait_run", return_value=run),
            patch.object(main, "_dataset_items", return_value=rows),
            patch.object(main, "_collect_session_actor") as session,
        ):
            result = main._collect_relation("example", 1, "Followers", "Подписчики")
        self.assertEqual(result, [{"id": "1", "username": "alice"}])
        self.assertEqual(start.call_args.args[1]["type"], "followers")
        session.assert_not_called()

    def test_dataset_reads_beyond_first_page(self):
        with patch.object(main, "APIFY_PAGE_SIZE", 2), patch.object(
            main, "_request_json", side_effect=[[1, 2], [3]],
        ) as request:
            self.assertEqual(main._dataset_items("dataset"), [1, 2, 3])
        self.assertEqual([call.kwargs["params"]["offset"] for call in request.call_args_list], [0, 2])

    def test_expired_collection_stops_before_next_actor_and_releases_lock(self):
        now = [100.0]

        def primary(*args):
            now[0] += 200
            return []

        with (
            patch.object(main.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(main, "_collect_profile", side_effect=lambda _: main._collect_relation("example", 110, "Followings", "Подписки")),
            patch.object(main, "_collect_full_following_actor", side_effect=primary),
            patch.object(main.requests, "request") as request,
        ):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 504)
        request.assert_not_called()
        self.assertFalse(main._collect_lock.locked())

    def test_poll_failure_aborts_only_its_unfinished_run(self):
        with patch.object(main, "_request_json", side_effect=main.CollectionTimeout()), patch.object(main, "_abort_run") as abort:
            with self.assertRaises(main.CollectionTimeout):
                main._wait_run("owned-run")
        abort.assert_called_once_with("owned-run")

    def test_successful_run_is_not_aborted(self):
        with patch.object(main, "_request_json", return_value={"data": {"status": "SUCCEEDED"}}), patch.object(main, "_abort_run") as abort:
            self.assertEqual(main._wait_run("owned-run")["status"], "SUCCEEDED")
        abort.assert_not_called()

    def test_http_timeout_uses_remaining_collection_budget(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {}}
        with patch.object(main, "_remaining", return_value=4), patch.object(main, "_api_token", return_value="test-token"), patch.object(main.requests, "request", return_value=response) as request:
            main._request_json("GET", "/actor-runs/test")
        timeout = request.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 4)
        self.assertEqual(timeout.connect_timeout, 4)

    def test_lists_run_concurrently_with_shared_deadline(self):
        profile = {"id": "123", "username": "example", "followers_count": 1, "following_count": 1, "is_private": False}
        barrier = threading.Barrier(2)
        deadlines = []

        def relation(*args):
            deadlines.append(main._collection_state.deadline)
            barrier.wait(timeout=2)
            return [{"id": "1", "username": "alice"}]

        with patch.object(main, "_profile", return_value=profile), patch.object(main, "_collect_relation", side_effect=relation):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(deadlines[0], deadlines[1])

    def test_final_snapshot_rejects_mismatched_lengths(self):
        profile = {"id": "123", "username": "example", "followers_count": 2, "following_count": 1, "is_private": False}
        with patch.object(main, "_profile", return_value=profile), patch.object(main, "_collect_relation", return_value=[{"id": "1", "username": "alice"}]):
            response = self.client.post("/v1/collect", json={"username": "example"})
        self.assertEqual(response.status_code, 409)

    def test_changing_list_does_not_fall_back_to_stale_data(self):
        with patch.object(main, "_collect_full_following_actor", side_effect=HTTPException(409, "changed")), patch.object(main, "_collect_free_following_actor") as fallback:
            with self.assertRaises(HTTPException) as error:
                main._collect_relation("example", 110, "Followings", "Подписки")
        self.assertEqual(error.exception.status_code, 409)
        fallback.assert_not_called()

    def test_diagnostic_codes_do_not_log_raw_provider_text(self):
        self.assertEqual(main._diagnostic_code({"error": {"code": "RATE_LIMITED"}}), "RATE_LIMITED")
        self.assertEqual(main._diagnostic_code({"error": "Instagram rate limited the bounded authenticated request attempts."}), "RATE_LIMITED")
        self.assertEqual(main._diagnostic_code({"error": "private-session-cookie"}), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()

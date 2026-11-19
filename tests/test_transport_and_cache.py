"""Failure handling for both GitHub transports and shared on-disk state."""

import contextlib
import datetime as dt
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordlib import budget, cache, config, gh, outage, runtime


class TransportTests(unittest.TestCase):
    def test_missing_or_unclosable_error_body_preserves_the_http_status(self):
        client = gh.GitHub("o/r", transport="http", token="test")
        for body in (None, io.BytesIO(b"quota")):
            error = urllib.error.HTTPError("https://api.github.com/test", 403, "Forbidden", {}, body)
            if body is not None:
                self.addCleanup(error.close)
                error.close = mock.Mock(side_effect=OSError("could not close"))
            with self.subTest(body=body), mock.patch.object(gh, "api_open", side_effect=error):
                with self.assertRaises(gh.GitHubError) as caught:
                    client.rest("test")
                self.assertEqual(caught.exception.status, 403)
                if body is not None:
                    self.assertIn("quota", str(caught.exception))

    def test_malformed_repository_responses_are_backend_errors(self):
        client = mock.Mock(repo="test/default-branch")
        with mock.patch.object(config, "get", return_value=None):
            for response in ("unavailable", [], {"default_branch": 4}):
                client.rest.return_value = response
                with self.subTest(response=response), self.assertRaises(gh.GitHubError):
                    runtime.default_branch(client)

    def test_remote_detection_requires_a_github_host_and_repository_path(self):
        for remote in ("https://notgithub.com/o/r", "/tmp/github.com/o/r", "https://github.com/o/r/issues/1"):
            self.assertIsNone(gh.repo_from_remote_url(remote))
        for remote in ("https://github.com/o/r.git", "ssh://git@github.com/o/r",
                       "ssh://git@ssh.github.com:443/o/r.git", "git@github.com:o/r.git"):
            self.assertEqual(gh.repo_from_remote_url(remote), "o/r")
        for repo in ("owner", "o/r/extra", "o/..", "/r"):
            with self.assertRaises(gh.GitHubError):
                gh.GitHub(repo)

    def test_user_requests_reject_off_origin_urls_before_opening(self):
        client = gh.GitHub("o/r", transport="http", token="test")
        with mock.patch.object(gh, "api_open") as opener:
            for url in ("https://example.invalid/next", "//example.invalid/next", "http://api.github.com/next"):
                with self.assertRaises(gh.GitHubError):
                    client.rest(url)
            opener.assert_not_called()
            self.assertEqual(client.calls, 0)

    def test_user_pagination_stays_on_the_configured_api_and_detects_cycles(self):
        client = gh.GitHub("o/r", transport="http", token="test")
        client.api = "https://proxy.invalid/api"
        link = '<https://api.github.com/repositories/42/issues?page=2>; rel="next"'
        reply = ([1], {"link": link})
        with mock.patch.object(client, "_http", return_value=reply) as read:
            with self.assertRaisesRegex(gh.GitHubError, "repeated pagination"):
                client.rest_paginated("repos/{repo}/issues")
            self.assertEqual(read.call_args_list, [
                mock.call("GET", "repos/o/r/issues"),
                mock.call("GET", "https://proxy.invalid/api/repos/o/r/issues?page=2"),
            ])

    def test_invalid_json_and_graphql_envelopes_are_backend_errors(self):
        for body in ("not JSON", "{}", "null", '{"data":null}', '{"errors":["unavailable"]}'):
            client = gh.GitHub("o/r", runner=lambda args, stdin: subprocess.CompletedProcess(args, 0, body, ""))
            with self.subTest(body=body), self.assertRaises(gh.GitHubError):
                client.graphql("query { viewer { login } }")
        for request in ("rest", "rest_paginated"):
            with self.subTest(request=request), mock.patch.object(gh, "api_open") as opener:
                response = io.BytesIO(b"not JSON")
                response.headers = {}
                opener.return_value = response
                with self.assertRaisesRegex(gh.GitHubError, "invalid JSON"):
                    getattr(gh.GitHub("o/r", transport="http", token="test"), request)("repos/{repo}")


class SharedCacheTests(unittest.TestCase):
    def test_case_variants_share_a_cache_but_distinct_repo_pairs_do_not(self):
        self.assertEqual(cache.cache_dir("Acme/Tools"), cache.cache_dir("acme/tools"))
        self.assertNotEqual(cache.cache_dir("a_b/c"), cache.cache_dir("a/b_c"))

    def test_malformed_cache_or_cursor_is_a_miss(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            for value in ([], 2, "old", {"seenThrough": "invalid", "seenIds": 3}):
                cache._write(cache.snapshot_path("o/r"), value)
                cache._write(cache._cursor_path("o/r", "agent"), value)
                self.assertIsNone(cache.load_snapshot("o/r", 120))
                self.assertIsNone(cache.load_cursor("o/r", "agent"))
                self.assertEqual(cache.load_seen_ids("o/r", "agent"), [])
            cache.store_cursor("o/r", "agent", "2026-10-01T12:00:00Z", [101, -102])
            self.assertEqual(cache.load_seen_ids("o/r", "agent"), [-102, 101])

    def test_budget_lock_failure_skips_the_write(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}), \
                mock.patch.object(cache, "budget_lock", return_value=contextlib.nullcontext(False)), \
                mock.patch.object(cache, "store_principal") as write:
            headers = {"X-RateLimit-Remaining": "20", "X-RateLimit-Limit": "100", "X-RateLimit-Reset": "12345"}
            self.assertEqual(budget.record("o/r", headers)["remaining"], 20)
            write.assert_not_called()

    def test_mailbox_discoveries_are_merged_under_the_lock(self):
        # The second read models a discovery published between the cheap check and our turn to write.
        with mock.patch.object(cache, "load_mailboxes", side_effect=[[], [7]]), \
                mock.patch.object(cache, "_cache_lock", return_value=contextlib.nullcontext(True)), \
                mock.patch.object(cache, "_write") as write:
            cache.remember_mailboxes("o/r", [3])
            self.assertEqual(write.call_args.args[1]["numbers"], [3, 7])


class OutageConfigTests(unittest.TestCase):
    def test_invalid_configured_zone_and_clock_do_not_become_an_eastern_reset(self):
        now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        for reset in ("Thu 12:00 Invalid/Zone", "Thu 26:00 UTC"):
            with mock.patch.dict(os.environ, {"COORD_CLAUDE_WEEKLY_RESET": reset}):
                with self.assertRaises(config.ConfigError):
                    outage.next_weekly_reset(now)
                self.assertEqual(outage.parse_limit("weekly usage limit", now)["until"], now + outage.PROBE_AFTER)


if __name__ == "__main__":
    unittest.main()

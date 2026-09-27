"""Identity isolation for concurrent harness tasks and same-named worktrees."""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cache, cli, identity, marker, snapshot  # noqa: E402


class ConcurrentIdentityTests(unittest.TestCase):
    def agent(self, env, checkout="/Users/dev/.codex/worktrees/16e5/INISA", host="Eidolon"):
        with patch.object(identity, "git_toplevel", return_value=checkout), \
                patch.object(identity, "detect_host", return_value=host):
            return identity.agent_id(env)

    def test_same_basename_worktrees_have_distinct_addresses_and_cursors(self):
        first = self.agent({})
        second = self.agent({}, "/Users/dev/.codex/worktrees/abcd/INISA")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("codex@Eidolon:INISA-"), first)
        self.assertTrue(identity.matches("codex@*", first))
        self.assertNotEqual(cache._cursor_path("owner/repo", first), cache._cursor_path("owner/repo", second))

    def test_tasks_in_one_checkout_have_distinct_addresses(self):
        first = self.agent({"CODEX_THREAD_ID": "task-one"})
        second = self.agent({"CODEX_THREAD_ID": "task-two"})
        self.assertNotEqual(first, second)
        self.assertEqual(first, self.agent({"CODEX_THREAD_ID": "task-one"}))

    def test_generic_session_id_takes_precedence_and_works_for_other_harnesses(self):
        first = self.agent({"COORD_SESSION_ID": "task-one", "CODEX_THREAD_ID": "provider-one"})
        same = self.agent({"COORD_SESSION_ID": "task-one", "CODEX_THREAD_ID": "provider-two"})
        self.assertEqual(first, same)
        unknown = self.agent({"COORD_SESSION_ID": "task-one"}, "/workspace/INISA")
        other = self.agent({"COORD_SESSION_ID": "task-two"}, "/workspace/INISA")
        self.assertNotEqual(unknown, other)

    def test_explicit_address_preserves_conductor_and_migration_identity(self):
        env = {"COORD_AGENT": "conductor@Tycho:conductor", "COORD_SESSION_ID": "ignored"}
        self.assertEqual(self.agent(env), "conductor@Tycho:conductor")
        self.assertEqual(self.agent(env, "/elsewhere/checkout", "OtherHost"), env["COORD_AGENT"])

    def test_claude_local_and_cloud_session_ids_are_not_truncated(self):
        for flags in ({"CLAUDECODE": "1"}, {"CLAUDE_CODE_REMOTE": "true"}):
            with self.subTest(flags=flags):
                first = self.agent(dict(flags, CLAUDE_CODE_SESSION_ID="same1234-one"))
                second = self.agent(dict(flags, CLAUDE_CODE_SESSION_ID="same1234-two"))
                self.assertNotEqual(first, second)
                self.assertEqual(first, self.agent(dict(flags, CLAUDE_CODE_SESSION_ID="same1234-one")))

    def test_cloud_sessions_with_same_host_and_path_are_distinct(self):
        first = self.agent({"CODEX_THREAD_ID": "cloud-one"}, "/workspace/INISA", "vm")
        second = self.agent({"CODEX_THREAD_ID": "cloud-two"}, "/workspace/INISA", "vm")
        self.assertNotEqual(first, second)
        self.assertTrue(identity.matches("codex@*", first))

    def test_claude_remote_session_id_precedes_local_session_id(self):
        env = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_REMOTE_SESSION_ID": "remote",
               "CLAUDE_CODE_SESSION_ID": "local-one"}
        self.assertEqual(self.agent(env), self.agent(dict(env, CLAUDE_CODE_SESSION_ID="local-two")))

    def test_claude_ignores_embedded_codex_thread_id(self):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "claude-task", "CODEX_THREAD_ID": "codex-one"}
        self.assertEqual(self.agent(env), self.agent(dict(env, CODEX_THREAD_ID="codex-two")))

    def test_checkout_fallback_normalizes_path_aliases_and_needs_no_state(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = os.path.join(directory, "INISA")
            alias = os.path.join(directory, "alias")
            os.mkdir(actual)
            os.symlink(actual, alias)
            with patch.object(identity, "container_id", side_effect=AssertionError("local identity must not persist state")):
                first = self.agent({"CODEX_SANDBOX": "seatbelt"}, actual)
                self.assertEqual(first, self.agent({"CODEX_SANDBOX": "seatbelt"}, actual + "/../INISA/."))
                self.assertEqual(first, self.agent({"CODEX_SANDBOX": "seatbelt"}, alias))

    def test_peer_message_is_delivered_and_own_message_is_filtered(self):
        first = self.agent({"CODEX_THREAD_ID": "task-one"})
        second = self.agent({"CODEX_THREAD_ID": "task-two"})
        comments = [
            {"id": number, "issue_url": "https://api.github.com/repos/owner/repo/issues/1",
             "created_at": "2026-09-27T01:00:00Z", "html_url": f"https://example/{number}",
             "body": marker.compose("message", {"kind": "msg", "v": 1, "agent": sender, "to": [second]})}
            for number, sender in enumerate((first, second), 1)
        ]
        items = cli.inbox_items(comments, [], {"claims": [], "prs": []}, second,
                                dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc), "")
        self.assertEqual([item["id"] for item in items], [1])

    def test_reading_one_task_does_not_advance_another_tasks_cursor(self):
        first = self.agent({"CODEX_THREAD_ID": "task-one"})
        second = self.agent({"CODEX_THREAD_ID": "task-two"})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            cache.store_cursor("owner/repo", first, "2026-09-27T01:00:00Z", [123])
            self.assertEqual(cache.load_seen_ids("owner/repo", first), [123])
            self.assertIsNone(cache.load_cursor("owner/repo", second))
            self.assertEqual(cache.load_seen_ids("owner/repo", second), [])

    def test_simultaneous_claims_remain_separate_and_contested(self):
        first = self.agent({"CODEX_THREAD_ID": "task-one"})
        second = self.agent({"CODEX_THREAD_ID": "task-two"})
        comments = [{"id": number, "createdAt": "2026-09-27T01:00:00Z", "url": f"https://example/{number}",
                     "payload": {"kind": "claim", "v": 1, "issue": 1, "agent": agent,
                                 "branch": f"branch-{number}", "files": []}}
                    for number, agent in enumerate((first, second), 1)]
        claims = snapshot.resolve_claims(comments, {}, [], dt.datetime(2026, 9, 27, 2, tzinfo=dt.timezone.utc))
        self.assertEqual([claim["agent"] for claim in claims], [first, second])
        self.assertEqual([claim["contested"] for claim in claims], [False, True])


if __name__ == "__main__":
    unittest.main()

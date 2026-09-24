"""Unit tests for coord's rules. Run: python3 -m unittest discover -s Tools/coord/tests

Every GitHub response here is synthetic but shaped like the recorded ones
(GraphQL PR nodes as snapshot.PR_FIELDS asks for them; REST comments as
issues/comments returns them).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cli, identity, marker, render, review, snapshot  # noqa: E402
from coordlib.gh import GitHub, repo_from_remote_url  # noqa: E402

NOW = dt.datetime(2026, 9, 24, 17, 0, tzinfo=dt.timezone.utc)
BOT = "chatgpt-codex-connector[bot]"


def t(minutes_ago: float) -> str:
    return snapshot.iso(NOW - dt.timedelta(minutes=minutes_ago))


def pr_node(**overrides):
    node = {
        "number": 101, "title": "Example", "url": "https://example/101", "isDraft": False,
        "headRefName": "bughunt/example", "headRefOid": "a" * 40, "baseRefName": "main",
        "createdAt": t(600), "updatedAt": t(30), "mergeable": "MERGEABLE",
        "labels": {"nodes": []},
        "files": {"totalCount": 2, "nodes": [{"path": "Packages/CoreKit/Sources/A.swift"}, {"path": "Docs/X.md"}]},
        "reactions": {"nodes": []},
        "closingIssuesReferences": {"nodes": [{"number": 55}]},
        "commits": {"nodes": [{"commit": {
            "oid": "a" * 40, "committedDate": t(90),
            "checkSuites": {"nodes": [{"createdAt": t(60)}, {"createdAt": t(59)}]},
            "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": []}},
        }}]},
        "reviewThreads": {"totalCount": 0, "nodes": []},
    }
    node.update(overrides)
    return node


def reaction(content: str, minutes_ago: float, login: str = BOT):
    return {"content": content, "createdAt": t(minutes_ago), "user": {"login": login}}


def check(name: str, status: str = "COMPLETED", conclusion: str = "SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def with_checks(*contexts):
    node = pr_node()
    node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = list(contexts)
    return node


def coord_comment(comment_id: int, on: int, minutes_ago: float, payload):
    return {"id": comment_id, "on": on, "createdAt": t(minutes_ago), "url": f"u/{comment_id}", "text": "", "payload": payload}


class MarkerTests(unittest.TestCase):
    def test_round_trip(self):
        payload = {"kind": "claim", "agent": "claude@Eidolon:wt", "issue": 5, "files": ["a b/c.swift"]}
        body = marker.compose("Claimed.", payload)
        self.assertEqual(marker.decode(body), payload)
        self.assertEqual(marker.strip(body), "Claimed.")

    def test_double_hyphen_cannot_close_the_comment(self):
        payload = {"kind": "msg", "agent": "x", "to": ["all"], "note": "a --> b -- c ---"}
        encoded = marker.encode(payload)
        self.assertNotIn("--", encoded[len(marker.PREFIX): -len(marker.SUFFIX)])
        self.assertEqual(marker.decode(encoded), payload)

    def test_only_the_last_line_counts(self):
        quoted = "> " + marker.encode({"kind": "claim", "agent": "other", "issue": 1})
        self.assertIsNone(marker.decode(quoted + "\n\nI agree"))

    def test_unknown_kind_and_garbage_are_ignored(self):
        self.assertIsNone(marker.decode(marker.PREFIX + '{"kind":"lease"}' + marker.SUFFIX))
        self.assertIsNone(marker.decode(marker.PREFIX + "{not json" + marker.SUFFIX))
        self.assertIsNone(marker.decode(None))


class IdentityTests(unittest.TestCase):
    def test_harness_detection(self):
        self.assertEqual(identity.detect_harness({"CLAUDE_CODE_REMOTE": "true", "CLAUDECODE": "1"}, "/w"), "claude-cloud")
        self.assertEqual(identity.detect_harness({"CLAUDECODE": "1", "CODEX_COMPANION_SESSION_ID": "x"}, "/w"), "claude")
        self.assertEqual(identity.detect_harness({}, "/Users/a/.codex/worktrees/x/INISA"), "codex")
        self.assertEqual(identity.detect_harness({"CODEX_SANDBOX": "seatbelt"}, "/w"), "codex")
        self.assertEqual(identity.detect_harness({"CODEX_COMPANION_SESSION_ID": "x"}, "/w"), "human")

    def test_override(self):
        self.assertEqual(identity.agent_id({"COORD_AGENT": "me@here:x"}), "me@here:x")

    def test_address_matching(self):
        me = "codex@Eidolon:leveltrim"
        self.assertTrue(identity.matches("all", me))
        self.assertTrue(identity.matches(me, me))
        self.assertTrue(identity.matches("codex@*", me))
        self.assertFalse(identity.matches("claude@*", me))
        self.assertFalse(identity.matches("codex@Eidolon", me))

    def test_repo_from_remote(self):
        self.assertEqual(repo_from_remote_url("https://github.com/example-owner/example-repo.git"), "example-owner/example-repo")
        self.assertEqual(repo_from_remote_url("git@github.com:example-owner/example-repo.git\n"), "example-owner/example-repo")
        self.assertIsNone(repo_from_remote_url("/local/path"))


class CodexStateTests(unittest.TestCase):
    def test_thumb_after_push_approves(self):
        state = review.codex_state(pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30)]}))
        self.assertTrue(state["approved"])

    def test_thumb_before_push_is_stale(self):
        # Head pushed 60 min ago (earliest check suite); 👍 is 70 min old.
        state = review.codex_state(pr_node(reactions={"nodes": [reaction("THUMBS_UP", 70)]}))
        self.assertFalse(state["approved"])
        self.assertTrue(state["stale"])

    def test_eyes_means_re_review_in_progress(self):
        state = review.codex_state(pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30), reaction("EYES", 5)]}))
        self.assertFalse(state["approved"])
        self.assertTrue(state["reviewing"])

    def test_human_thumb_is_not_codex(self):
        state = review.codex_state(pr_node(reactions={"nodes": [reaction("THUMBS_UP", 5, "alex-bridges")]}))
        self.assertFalse(state["approved"])

    def test_commit_date_is_the_fallback_push_time(self):
        node = pr_node(reactions={"nodes": [reaction("THUMBS_UP", 80)]})
        node["commits"]["nodes"][0]["commit"]["checkSuites"]["nodes"] = []
        self.assertTrue(review.codex_state(node)["approved"])  # committed 90 min ago


class FindingTests(unittest.TestCase):
    def thread(self, resolved, *authors):
        return {"isResolved": resolved, "comments": {"nodes": [{"author": {"login": a}} for a in authors]}}

    def test_counts_only_unanswered_bot_threads(self):
        node = pr_node(reviewThreads={"totalCount": 5, "nodes": [
            self.thread(False, "chatgpt-codex-connector"),                   # unanswered
            self.thread(False, "chatgpt-codex-connector", "alex-bridges"),   # answered
            self.thread(True, "chatgpt-codex-connector"),                    # resolved
            self.thread(False, "alex-bridges"),                              # human thread
            self.thread(False, "chatgpt-codex-connector", BOT),              # bot follow-up only
        ]})
        self.assertEqual(review.unanswered_findings(node), 2)


class CITests(unittest.TestCase):
    def test_verdict_success(self):
        state = review.ci_state(with_checks(check("CI verdict"), check("Select lanes")))
        self.assertEqual(state, {"verdict": "success", "failing": [], "pending": []})

    def test_failing_ignores_xcode_cloud_and_cancelled(self):
        state = review.ci_state(with_checks(
            check("CI verdict", conclusion="FAILURE"),
            check("Android", conclusion="FAILURE"),
            check("CoreKit release gate", conclusion="CANCELLED"),
            check("LegacyCI | AppBeta · Test (PR + main) | Test - iOS", conclusion="ACTION_REQUIRED"),
            {"__typename": "StatusContext", "context": "LegacyCI | LegacyApp · Test (PR + main)", "state": "FAILURE"},
        ))
        self.assertEqual(state["verdict"], "failure")
        self.assertEqual(state["failing"], ["Android", "CI verdict"])

    def test_pending(self):
        state = review.ci_state(with_checks(check("CI verdict", status="QUEUED", conclusion=None)))
        self.assertEqual(state["verdict"], "pending")
        self.assertEqual(state["pending"], ["CI verdict"])

    def test_cancelled_verdict(self):
        self.assertEqual(review.ci_state(with_checks(check("CI verdict", conclusion="CANCELLED")))["verdict"], "cancelled")


class ClaimTests(unittest.TestCase):
    def claim(self, cid, issue, agent, minutes_ago, branch="b", files=None):
        return coord_comment(cid, issue, minutes_ago, {"kind": "claim", "agent": agent, "issue": issue, "branch": branch, "files": files or []})

    def release(self, cid, issue, agent, minutes_ago, everyone=False):
        payload = {"kind": "release", "agent": agent, "issue": issue}
        if everyone:
            payload["everyone"] = True
        return coord_comment(cid, issue, minutes_ago, payload)

    def resolve(self, comments, issues=None, branches=None, prs=None):
        targets = {"issues": issues or {}, "branches": branches or {}}
        return snapshot.resolve_claims(comments, targets, prs or [], NOW)

    def test_release_ends_a_claim(self):
        claims = self.resolve([self.claim(1, 7, "a", 30), self.release(2, 7, "a", 20)], {7: {"state": "OPEN"}})
        self.assertEqual(claims, [])

    def test_release_by_another_agent_does_not_end_it(self):
        claims = self.resolve([self.claim(1, 7, "a", 30), self.release(2, 7, "b", 20)], {7: {"state": "OPEN"}})
        self.assertEqual([c["agent"] for c in claims], ["a"])

    def test_release_everyone(self):
        claims = self.resolve([self.claim(1, 7, "a", 30), self.claim(2, 7, "b", 25), self.release(3, 7, "c", 20, True)],
                              {7: {"state": "OPEN"}})
        self.assertEqual(claims, [])

    def test_earliest_holds_later_is_contested(self):
        claims = self.resolve([self.claim(2, 7, "b", 30), self.claim(1, 7, "a", 30)], {7: {"state": "OPEN"}})
        self.assertEqual([(c["agent"], c["contested"]) for c in claims], [("a", False), ("b", True)])

    def test_reclaim_by_same_agent_is_not_contested(self):
        claims = self.resolve([self.claim(1, 7, "a", 30), self.claim(2, 7, "a", 10)], {7: {"state": "OPEN"}})
        self.assertEqual(len(claims), 1)
        self.assertFalse(claims[0]["contested"])

    def test_closed_issue_drops_claim(self):
        self.assertEqual(self.resolve([self.claim(1, 7, "a", 30)], {7: {"state": "CLOSED"}}), [])

    def test_stale_without_pr_or_recent_push(self):
        claims = self.resolve([self.claim(1, 7, "a", 60 * 72)], {7: {"state": "OPEN"}},
                              {"b": {"sha": "x", "committedAt": t(60 * 50)}})
        self.assertTrue(claims[0]["stale"])
        self.assertTrue(claims[0]["branchPushed"])

    def test_recent_push_keeps_claim_fresh(self):
        claims = self.resolve([self.claim(1, 7, "a", 60 * 72)], {7: {"state": "OPEN"}},
                              {"b": {"sha": "x", "committedAt": t(60)}})
        self.assertFalse(claims[0]["stale"])

    def test_claim_links_to_pr_by_branch(self):
        pr = snapshot.summarize_pr(pr_node())
        claims = self.resolve([self.claim(1, 55, "a", 30, branch="bughunt/example")], {55: {"state": "OPEN"}}, prs=[pr])
        self.assertEqual(claims[0]["pr"], 101)


class OverlapAndTrainTests(unittest.TestCase):
    def test_directory_claim_overlaps_file(self):
        pr = snapshot.summarize_pr(pr_node())
        claims = [{"issue": 9, "agent": "a", "pr": None, "files": ["Packages/CoreKit/Sources/"]}]
        found = snapshot.overlaps([pr], claims)
        self.assertEqual(found[0]["files"], ["Packages/CoreKit/Sources/A.swift"])

    def test_sibling_prefix_is_not_an_overlap(self):
        self.assertFalse(snapshot._paths_overlap("Apps/Dragon", "Apps/AppAlpha/x.swift"))

    def test_claim_with_pr_is_not_double_counted(self):
        pr = snapshot.summarize_pr(pr_node())
        claims = [{"issue": 9, "agent": "a", "pr": 101, "files": ["Docs/X.md"]}]
        self.assertEqual(snapshot.overlaps([pr], claims), [])

    def test_train_order_and_head_moved(self):
        first = snapshot.summarize_pr(pr_node(number=1, labels={"nodes": [{"name": "train"}]}))
        second = snapshot.summarize_pr(pr_node(number=2, labels={"nodes": [{"name": "train"}]}, headRefOid="b" * 40))
        loose = snapshot.summarize_pr(pr_node(number=3))
        comments = [
            coord_comment(5, 2, 20, {"kind": "train-add", "agent": "x", "pr": 2, "head": "c" * 40}),
            coord_comment(6, 1, 10, {"kind": "train-add", "agent": "y", "pr": 1, "head": "a" * 40}),
        ]
        queue = snapshot.train_queue([first, second, loose], comments)
        self.assertEqual([(q["pr"], q["headMoved"]) for q in queue], [(2, True), (1, False)])


class InboxTests(unittest.TestCase):
    ME = "claude@Eidolon:wt"

    def comment(self, cid, number, minutes_ago, body, login="alex-bridges", review_comment=False):
        key = "pull_request_url" if review_comment else "issue_url"
        return {"id": cid, key: f"https://api.github.com/repos/o/r/issues/{number}", "created_at": t(minutes_ago),
                "html_url": f"h/{cid}", "user": {"login": login}, "body": body}

    def snap(self):
        return {"claims": [{"agent": self.ME, "issue": 40, "pr": 41}], "prs": [{"number": 50, "branch": "mine"}]}

    def msg(self, sender, to):
        return marker.compose("hello", {"kind": "msg", "agent": sender, "to": to})

    def items(self, issue_comments, review_comments=(), branch=""):
        return cli.inbox_items(list(issue_comments), list(review_comments), self.snap(), self.ME,
                               NOW - dt.timedelta(minutes=100), branch)

    def test_direct_prefix_and_broadcast(self):
        got = self.items([
            self.comment(1, 99, 10, self.msg("codex@x:y", [self.ME])),
            self.comment(2, 99, 9, self.msg("codex@x:y", ["claude@*"])),
            self.comment(3, 99, 8, self.msg("codex@x:y", ["all"])),
            self.comment(4, 99, 7, self.msg("codex@x:y", ["codex@*"])),
        ])
        self.assertEqual([i["reason"] for i in got], ["to you", "to claude@*", "to all"])

    def test_own_messages_and_old_edited_comments_are_skipped(self):
        got = self.items([
            self.comment(1, 99, 10, self.msg(self.ME, ["all"])),
            self.comment(2, 40, 500, "edited later, created before the cursor"),
        ])
        self.assertEqual(got, [])

    def test_comments_on_my_work(self):
        got = self.items(
            [self.comment(1, 40, 10, "on my claimed issue"), self.comment(2, 77, 10, "unrelated"),
             self.comment(3, 50, 5, "on the PR of my current branch")],
            [self.comment(4, 41, 3, "finding", login=BOT, review_comment=True)],
            branch="mine",
        )
        self.assertEqual([i["on"] for i in got], [40, 50, 41])


class TransportTests(unittest.TestCase):
    def test_paginated_pages_are_flattened_and_calls_counted(self):
        seen = []

        def runner(args, stdin):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, json.dumps([[{"id": 1}], [{"id": 2}]]), "")

        gh = GitHub("o/r", runner=runner)
        self.assertEqual(gh.rest_paginated("repos/{repo}/issues/comments"), [{"id": 1}, {"id": 2}])
        self.assertIn("--slurp", seen[0])
        self.assertIn("repos/o/r/issues/comments", seen[0])
        self.assertEqual(gh.calls, 1)

    def test_errors_raise(self):
        def runner(args, stdin):
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")

        with self.assertRaises(Exception):
            GitHub("o/r", runner=runner).rest("repos/{repo}/issues/1")


class RenderTests(unittest.TestCase):
    def full_snapshot(self):
        pr = snapshot.summarize_pr(pr_node(labels={"nodes": [{"name": "train"}]}))
        return {
            "schema": 1, "generatedAt": snapshot.iso(NOW), "repo": "o/r", "main": {"sha": "f" * 40},
            "prs": [pr],
            "claims": [{"issue": 9, "title": "T", "agent": "a", "branch": "b", "files": [], "claimedAt": t(10),
                        "lastActivity": t(5), "pr": None, "branchPushed": False, "stale": False, "contested": True}],
            "overlaps": [{"a": "PR #101", "b": "claim #9 (a)", "files": ["x", "y", "z", "w"]}],
            "train": [{"pr": 101, "addedAt": t(3), "agent": "a", "headAtAdd": "a" * 40, "headMoved": False}],
            "runs": {"running": [{"workflow": "CI", "branch": "main", "event": "push", "createdAt": t(30),
                                  "startedAt": t(20), "jobsRunning": [{"name": "CoreKit release gate",
                                  "runner": "Tycho", "step": "Test"}], "jobsQueued": 3}],
                     "queued": [{"workflow": "CI", "branch": "b", "event": "pull_request", "createdAt": t(12)}]},
            "messages": [], "apiCalls": 5,
        }

    def test_board_and_brief_render(self):
        text = render.board(self.full_snapshot(), "me", unread=2)
        for expected in ("PR #101", "CONTESTED", "(+1 more)", "Tycho", "waiting 12m", "1. PR #101", "INBOX  2 new"):
            self.assertIn(expected, text)
        self.assertIn("1 claim without a PR", render.brief(self.full_snapshot(), "me", unread=0))

    def test_empty_board_renders(self):
        empty = {"schema": 1, "generatedAt": snapshot.iso(NOW), "repo": "o/r", "main": {}, "prs": [], "claims": [],
                 "overlaps": [], "train": [], "runs": {"running": [], "queued": []}, "messages": []}
        self.assertIn("nothing open", render.board(empty, "me"))


if __name__ == "__main__":
    unittest.main()

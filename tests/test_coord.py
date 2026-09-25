"""Unit tests for coord's rules. Run: python3 -m unittest discover -s Tools/coord/tests

Every GitHub response here is synthetic but shaped like the recorded ones
(GraphQL PR nodes as snapshot.PR_FIELDS asks for them; REST comments as
issues/comments returns them).
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
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
    if "comments" not in overrides:
        node["comments"] = {"totalCount": 1, "nodes": [codex_summary(node["headRefOid"])]}
    return node


def codex_summary(sha: str, status: str = "✅ **Completed** <relative-time>now</relative-time>"):
    """Codex's pinned summary comment naming `sha` as reviewed (shape as seen on #975/#984)."""
    return {"author": {"login": "chatgpt-codex-connector"}, "body": (
        "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
        "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
        f"| 📝 **Code Review** | {status} | `{sha[:7]}` | New commits |\n")}


def reaction(content: str, minutes_ago: float, login: str = BOT):
    return {"content": content, "createdAt": t(minutes_ago), "user": {"login": login}}


def check(name: str, status: str = "COMPLETED", conclusion: str = "SUCCESS", annotations=None):
    """A CheckRun rollup node. `annotations` (a list of messages) adds the `annotations`
    connection snapshot.attach_first_annotations fills in; None leaves it out."""
    node = {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
    if annotations is not None:
        node["annotations"] = {"nodes": [{"message": message} for message in annotations]}
    return node


# The annotation GitHub puts on a job the Actions budget refused to start (#1026, check
# run 10000099: conclusion failure, started 01:35:55Z, completed 01:35:57Z).
BILLING_REFUSED = "The job was not started because an Actions budget is preventing further use."
REAL_FAILURE = "Process completed with exit code 1."


def refused_check(name: str):
    """A failed run as attach_first_annotations leaves a confirmed refusal: the refusal as
    its first annotation, and every other attempt of the name read and not a real failure."""
    return dict(check(name, conclusion="FAILURE", annotations=[BILLING_REFUSED]),
                **{review.NO_OTHER_ATTEMPT_FAILED: True})


def attempts_page(path: str, attempts, latest):
    """The fakes' answer to `commits/{sha}/check-runs?check_name=…&filter=all`: the named
    check's attempts from `attempts`, else only its newest run (`latest`, by name)."""
    import urllib.parse
    assert "&filter=all" in path, path
    name = urllib.parse.unquote(path.split("check_name=")[1].split("&")[0])
    return [{"check_runs": attempts[name] if name in attempts else [latest[name]]}]


def rest_run(run_id, name, conclusion, annotations_count=1, status="completed"):
    """A check run as REST's check-runs endpoints list it."""
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
            "output": {"annotations_count": annotations_count}}


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

    def test_cloud_sessions_get_distinct_ids(self):
        first = identity.agent_id({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_SESSION_ID": "0a1b2c3d-4e5f"})
        second = identity.agent_id({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_SESSION_ID": "9f8e7d6c-5b4a"})
        self.assertTrue(first.startswith("claude-cloud@0a1b2c3d:"), first)
        self.assertNotEqual(first, second)
        with tempfile.TemporaryDirectory() as cache:
            saved = os.environ.get("COORD_CACHE_DIR")
            os.environ["COORD_CACHE_DIR"] = cache
            try:
                a = identity.agent_id({"CLAUDE_CODE_REMOTE": "true"})
                b = identity.agent_id({"CLAUDE_CODE_REMOTE": "true"})
            finally:
                if saved is None:
                    os.environ.pop("COORD_CACHE_DIR", None)
                else:
                    os.environ["COORD_CACHE_DIR"] = saved
        self.assertEqual(a, b, "without a session id, one container keeps one id")

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

    def test_a_thumb_counts_only_for_the_commit_the_summary_names(self):
        # An old 👍 (for another head) with the summary naming a different commit.
        other = pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30)]},
                        comments={"nodes": [codex_summary("b" * 40)]})
        self.assertFalse(review.codex_state(other)["approved"])
        running = pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30)]},
                          comments={"nodes": [codex_summary("a" * 40, status="⏳ **Running**")]})
        self.assertFalse(review.codex_state(running)["approved"], "a review in progress is not a verdict")
        missing = pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30)]}, comments={"nodes": []})
        self.assertFalse(review.codex_state(missing)["approved"], "no summary, no approval")

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
        self.assertEqual(state, {"verdict": "success", "failing": [], "pending": [], "started": [], "not_run": []})

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

    def test_queued_checks_are_not_called_running(self):
        queued = review.ci_state(with_checks(check("CI verdict", status="QUEUED", conclusion=None),
                                             check("CoreKit release gate", status="QUEUED", conclusion=None)))
        self.assertEqual(render.ci_words(queued), "CI queued (2 checks waiting for a runner)")
        started = review.ci_state(with_checks(check("CI verdict", status="QUEUED", conclusion=None),
                                              check("CoreKit release gate", status="IN_PROGRESS", conclusion=None)))
        self.assertEqual(render.ci_words(started), "CI running (1 check running, 1 waiting)")

    def test_cancelled_verdict(self):
        self.assertEqual(review.ci_state(with_checks(check("CI verdict", conclusion="CANCELLED")))["verdict"], "cancelled")


class NotRunTests(unittest.TestCase):
    """A job GitHub never started is NOT RUN: neither red nor green (#1026), GraphQL shape."""

    def test_the_graphql_query_asks_for_the_run_id_not_its_annotations(self):
        # An annotations connection in the query is priced per PR per context (15 -> 40
        # points a board page); the id lets the snapshot read only the failed runs' first
        # annotation over REST.
        self.assertIn("... on CheckRun { databaseId name status conclusion }", snapshot.PR_FIELDS)
        self.assertNotIn("annotations", snapshot.PR_FIELDS)

    def test_a_billing_refused_run_is_not_run_not_failing(self):
        state = review.ci_state(with_checks(
            check("CI verdict"),
            refused_check("Linux plugin probe (x86_64)"),
            refused_check("Windows plugin probe (dynamic runtime)"),
        ))
        self.assertEqual(state["failing"], [])
        self.assertEqual(state["not_run"], ["Linux plugin probe (x86_64)", "Windows plugin probe (dynamic runtime)"])
        self.assertEqual(state["verdict"], "success")
        self.assertEqual(render.ci_words(state), "CI green, 2 not run (billing)")

    def test_a_real_failure_with_another_annotation_stays_failing(self):
        state = review.ci_state(with_checks(
            check("Android", conclusion="FAILURE", annotations=[REAL_FAILURE]),
            # The phrase anywhere but at the start is not the refusal.
            check("Linux plugin probe (x86_64)", conclusion="FAILURE", annotations=["Error: " + BILLING_REFUSED]),
            check("Timed out", conclusion="TIMED_OUT", annotations=["The job running on runner Tycho has exceeded the maximum execution time"]),
        ))
        self.assertEqual(state["failing"], ["Android", "Linux plugin probe (x86_64)", "Timed out"])
        self.assertEqual(state["not_run"], [])
        self.assertEqual(render.ci_words(state), "CI red: Android, Linux plugin probe (x86_64), Timed out")

    def test_a_failure_without_an_annotation_stays_failing(self):
        state = review.ci_state(with_checks(
            check("Android", conclusion="FAILURE"),                     # no connection at all
            check("AppGamma", conclusion="FAILURE", annotations=[]),     # an empty connection
        ))
        self.assertEqual(state["failing"], ["Android", "AppGamma"])
        self.assertEqual(state["not_run"], [])

    def test_only_the_first_annotation_counts(self):
        state = review.ci_state(with_checks(
            check("Android", conclusion="FAILURE", annotations=[REAL_FAILURE, BILLING_REFUSED])))
        self.assertEqual((state["failing"], state["not_run"]), (["Android"], []))

    def test_the_annotation_on_a_run_that_did_not_fail_changes_nothing(self):
        state = review.ci_state(with_checks(
            check("CI verdict", annotations=[BILLING_REFUSED]),
            check("Docs", conclusion="CANCELLED", annotations=[BILLING_REFUSED]),
        ))
        self.assertEqual(state, {"verdict": "success", "failing": [], "pending": [], "started": [], "not_run": []})

    def test_a_refused_verdict_job_is_no_verdict_and_ignored_checks_stay_ignored(self):
        state = review.ci_state(with_checks(
            refused_check("CI verdict"),
            refused_check("LegacyCI | AppBeta · Test (PR + main) | Test - iOS"),
        ))
        self.assertEqual(state["verdict"], "none", "a verdict job that never ran is not a red verdict")
        self.assertEqual((state["failing"], state["not_run"]), ([], ["CI verdict"]))
        self.assertEqual(render.ci_words(state), "CI none, 1 not run (billing)")

    def test_a_refusal_counts_only_once_the_other_attempts_are_cleared(self):
        unchecked = check("Linux plugin probe (x86_64)", conclusion="FAILURE", annotations=[BILLING_REFUSED])
        earlier_red = dict(unchecked, **{review.NO_OTHER_ATTEMPT_FAILED: False})
        for node, why in ((unchecked, "attempts never read: fail closed"), (earlier_red, "an earlier attempt failed for real")):
            state = review.ci_state(with_checks(node))
            self.assertEqual((state["failing"], state["not_run"]), (["Linux plugin probe (x86_64)"], []), why)

    def test_a_name_that_also_failed_for_real_is_red(self):
        state = review.ci_state(with_checks(
            refused_check("Android"),
            check("Android", conclusion="FAILURE", annotations=[REAL_FAILURE]),
        ))
        self.assertEqual((state["failing"], state["not_run"]), (["Android"], []))


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


class CodexRoundTwoTests(unittest.TestCase):
    """Findings from Codex's review of aea96eea9 on #989."""

    def test_forced_claim_retires_the_previous_holder(self):
        comments = [
            coord_comment(1, 7, 60, {"kind": "claim", "agent": "a", "issue": 7, "branch": "x"}),
            coord_comment(2, 7, 10, {"kind": "claim", "agent": "b", "issue": 7, "branch": "y", "takesOverFrom": ["a"]}),
        ]
        claims = snapshot.resolve_claims(comments, {"issues": {7: {"state": "OPEN"}}, "branches": {}}, [], NOW)
        self.assertEqual([(c["agent"], c["contested"]) for c in claims], [("b", False)])

    def test_a_claim_with_a_pr_keeps_its_declared_files_in_overlaps(self):
        pr = snapshot.summarize_pr(pr_node())  # touches A.swift and Docs/X.md
        claims = [
            {"issue": 55, "agent": "a", "pr": 101, "files": ["Packages/CoreKit/Sources/B.swift"]},
            {"issue": 60, "agent": "b", "pr": None, "files": ["Packages/CoreKit/Sources/B.swift"]},
        ]
        found = snapshot.overlaps([pr], claims)
        self.assertEqual(found, [{"a": "PR #101", "b": "claim #60 (b)", "files": ["Packages/CoreKit/Sources/B.swift"]}])

    def test_unread_count_includes_messages_sent_to_an_issue_i_work_on(self):
        me = "claude@Eidolon:wt"
        snap = {"claims": [{"agent": me, "issue": 40, "pr": 41}], "prs": [], "messages": [
            {"createdAt": t(5), "payload": {"kind": "msg", "agent": "codex@x:y", "to": ["#40"]}},
            {"createdAt": t(5), "payload": {"kind": "msg", "agent": "codex@x:y", "to": ["#41"]}},
            {"createdAt": t(5), "payload": {"kind": "msg", "agent": "codex@x:y", "to": ["#99"]}},
        ]}
        self.assertEqual(cli.unread_direct(snap, me, None), 2)

    def test_unread_count_includes_messages_to_the_pr_of_the_current_branch(self):
        me = "codex@Eidolon:wt"
        snap = {"claims": [], "prs": [{"number": 70, "branch": "codex/thing"}], "messages": [
            {"createdAt": t(5), "payload": {"kind": "msg", "agent": "claude@x:y", "to": ["#70"]}},
        ]}
        self.assertEqual(cli.unread_direct(snap, me, None, current_branch="codex/thing"), 1)
        self.assertEqual(cli.unread_direct(snap, me, None, current_branch="other"), 0)

    def test_truncated_files_and_review_threads_are_completed(self):
        class Fake:
            owner, name = "o", "r"

            def __init__(self):
                self.graphql_calls = 0

            def rest_paginated(self, path):
                assert "pulls/101/files" in path, path
                return [{"filename": f"f{i}"} for i in range(150)]

            def graphql(self, query, variables):
                self.graphql_calls += 1
                return {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "z"},
                    "nodes": [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "chatgpt-codex-connector"}}]}}],
                }}}}

        node = pr_node(files={"totalCount": 150, "nodes": [{"path": "f0"}]},
                       reviewThreads={"totalCount": 51, "pageInfo": {"hasNextPage": True, "endCursor": "a"}, "nodes": []})
        fake = Fake()
        snapshot.complete_pr_node(fake, node)
        summary = snapshot.summarize_pr(node)
        self.assertEqual(len(summary["files"]), 150)
        self.assertFalse(summary["filesTruncated"])
        self.assertEqual(summary["unansweredFindings"], 1)
        self.assertEqual(fake.graphql_calls, 1)

    def test_every_waiting_run_status_is_listed(self):
        class Fake:
            def __init__(self):
                self.paths = []

            def rest_paginated(self, path):
                self.paths.append(path)
                if "/jobs" in path:
                    return [{"jobs": [{"status": "queued"}]}]
                status = path.split("status=")[1].split("&")[0]
                if status == "in_progress":
                    return [{"workflow_runs": []}]
                return [{"workflow_runs": [{"id": len(self.paths), "status": status, "created_at": t(len(self.paths))}]}]

        runs = snapshot.fetch_runs(Fake())
        self.assertEqual(sorted(r["status"] for r in runs["queued"]), ["pending", "queued", "requested", "waiting"])

    def test_a_queued_run_with_a_job_in_progress_counts_as_running(self):
        class Fake:
            def rest_paginated(self, path):
                if "/jobs" in path:
                    return [{"jobs": [{"status": "in_progress", "name": "CoreKit release gate", "runner_name": "Tycho",
                                       "steps": [{"name": "Test", "status": "in_progress"}]},
                                      {"status": "queued", "name": "Android"}]}]
                if "status=queued" in path:
                    return [{"workflow_runs": [{"id": 9, "status": "queued", "head_branch": "main", "created_at": t(120)}]}]
                return [{"workflow_runs": []}]

        runs = snapshot.fetch_runs(Fake())
        self.assertEqual([r["id"] for r in runs["running"]], [9])
        self.assertEqual(runs["running"][0]["jobsRunning"][0]["runner"], "Tycho")
        self.assertEqual(runs["running"][0]["jobsQueued"], 1)
        self.assertEqual(runs["queued"], [])


class CodexRoundThreeTests(unittest.TestCase):
    """Findings from Codex's review of 9e5077302 on #989."""

    def test_claim_paths_are_normalized_to_the_pr_file_form(self):
        self.assertEqual(snapshot.normalize_path("./Packages/Foo.swift"), "Packages/Foo.swift")
        self.assertEqual(snapshot.normalize_path("Packages//X/../Foo.swift"), "Packages/Foo.swift")
        self.assertEqual(snapshot.normalize_path("Packages/Sources/"), "Packages/Sources")
        for bad in ("/abs/path", "../outside", ".", "", "a/../.."):
            self.assertEqual(snapshot.normalize_path(bad), "", bad)
        comments = [coord_comment(1, 7, 10, {"kind": "claim", "agent": "a", "issue": 7, "branch": "b",
                                               "files": ["./Packages/CoreKit/Sources/A.swift", "../x"]})]
        claims = snapshot.resolve_claims(comments, {"issues": {7: {"state": "OPEN"}}, "branches": {}}, [], NOW)
        self.assertEqual(claims[0]["files"], ["Packages/CoreKit/Sources/A.swift"])
        pr = snapshot.summarize_pr(pr_node(number=5, headRefName="other"))
        self.assertEqual(len(snapshot.overlaps([pr], claims)), 1)

    def test_the_claim_warning_sees_files_a_pr_linked_claim_declares(self):
        pr = snapshot.summarize_pr(pr_node())  # branch bughunt/example, touches A.swift, Docs/X.md
        claims = [{"issue": 55, "agent": "a", "pr": 101, "branch": "bughunt/example",
                   "files": ["Packages/CoreKit/Sources/B.swift"]}]
        items = snapshot.work_items([pr], claims)
        hits = [(label, snapshot.overlapping(["Packages/CoreKit/Sources/B.swift"], files)) for label, files, _ in items]
        self.assertEqual(hits, [("PR #101", ["Packages/CoreKit/Sources/B.swift"])])

    def test_every_label_is_fetched_before_a_policy_decision(self):
        class Fake:
            owner, name = "o", "r"

            def rest_paginated(self, path):
                assert "issues/101/labels" in path, path
                return [{"name": f"l{i}"} for i in range(120)] + [{"name": "needs-alex"}]

        node = pr_node(labels={"totalCount": 121, "nodes": [{"name": "train"}]})
        snapshot.complete_pr_node(Fake(), node)
        self.assertIn("needs-alex", snapshot.summarize_pr(node)["labels"])

    def test_train_add_records_the_head_before_applying_the_label(self):
        calls = []

        class Fake:
            repo, owner, name = "o/r", "o", "r"

            def rest(self, path, method="GET", body=None):
                calls.append((method, path))
                return {"html_url": "h"}

            def dispatch_workflow(self, *args, **kwargs):
                calls.append(("DISPATCH", args[0]))

        pr = snapshot.summarize_pr(pr_node(reactions={"nodes": [reaction("THUMBS_UP", 30)]}))
        original = cli.get_snapshot
        cli.get_snapshot = lambda gh, max_age: {"prs": [pr], "claims": []}
        try:
            args = cli.parser().parse_args(["train", "add", "101"])
            with open(os.devnull, "w") as quiet:
                saved, sys.stdout = sys.stdout, quiet
                try:
                    self.assertEqual(cli.cmd_train_add(args, Fake()), 0)
                finally:
                    sys.stdout = saved
        finally:
            cli.get_snapshot = original
        comment = calls.index(("POST", "repos/{repo}/issues/101/comments"))
        label = calls.index(("POST", "repos/{repo}/issues/101/labels"))
        self.assertLess(comment, label)

    def test_train_status_asks_without_a_trailing_slash_and_keeps_only_batches(self):
        requested = []

        class Fake:
            repo, owner, name = "o/r", "o", "r"

            def rest(self, path, method="GET", body=None):
                requested.append(path)
                return [{"ref": "refs/heads/train/20260924-180000", "object": {"sha": "t" * 40}},
                        {"ref": "refs/heads/training-notes", "object": {"sha": "x" * 40}}]

            def rest_paginated(self, path):
                # A human's narrow dispatch at the batch tip: not the train's gate.
                return [{"workflow_runs": [{"id": 5, "head_sha": "t" * 40, "event": "workflow_dispatch",
                                            "actor": {"login": "alex-bridges"}, "status": "completed",
                                            "conclusion": "success", "run_attempt": 1, "html_url": "u"}]}]

        original = cli.get_snapshot
        cli.get_snapshot = lambda gh, max_age: {"prs": [], "claims": [], "train": []}
        try:
            args = cli.parser().parse_args(["train", "status", "--json"])
            captured = io.StringIO()
            saved, sys.stdout = sys.stdout, captured
            try:
                self.assertEqual(cli.cmd_train_status(args, Fake()), 0)
            finally:
                sys.stdout = saved
        finally:
            cli.get_snapshot = original
        self.assertEqual(requested, ["repos/{repo}/git/matching-refs/heads/train"])
        batches = json.loads(captured.getvalue())["batches"]
        self.assertEqual([b["branch"] for b in batches], ["train/20260924-180000"])
        self.assertIsNone(batches[0]["run"], "someone else's green run is not the batch's gate")

    def test_a_bad_invocation_exits_with_the_usage_code_not_the_conflict_code(self):
        for argv in (["claim", "not-a-number"], ["nonsense"], ["train", "step", "--mode", "yolo"]):
            with open(os.devnull, "w") as quiet:
                saved, sys.stderr = sys.stderr, quiet
                try:
                    with self.assertRaises(SystemExit) as raised:
                        cli.parser().parse_args(argv)
                finally:
                    sys.stderr = saved
            self.assertEqual(raised.exception.code, 1, argv)

    def test_inbox_cursors_never_collide(self):
        from coordlib import cache
        self.assertNotEqual(cache._cursor_path("o/r", "codex@h:foo+bar"), cache._cursor_path("o/r", "codex@h:foo_bar"))

    def test_substitute_review_heads_come_from_the_newest_comment(self):
        prs = [{"number": 7}, {"number": 8}]
        comments = [
            coord_comment(1, 7, 30, {"kind": "review-substitute", "pr": 7, "head": "old"}),
            coord_comment(2, 7, 10, {"kind": "review-substitute", "pr": 7, "head": "new"}),
        ]
        snapshot.attach_substitute_reviews(prs, comments)
        self.assertEqual([p["substituteReviewHead"] for p in prs], ["new", None])

    def test_coord_ci_refuses_a_train_batch(self):
        args = cli.parser().parse_args(["ci", "docs", "--ref", "train/20260924-170000"])
        with self.assertRaises(cli.UsageError):
            cli.cmd_ci(args, None)


class RestFallbackTests(unittest.TestCase):
    """Claude cloud sessions: the proxy refuses GraphQL, REST works (probed 2026-09-24)."""

    class Fake:
        repo, owner, name = "o/r", "o", "r"

        def __init__(self, graphql_error=None):
            self.graphql_error = graphql_error
            self.graphql_calls = 0
            self.paths = []

        def graphql(self, query, variables=None):
            self.graphql_calls += 1
            raise self.graphql_error

        def rest(self, path, method="GET", body=None):
            self.paths.append(path)
            if path.endswith("git/ref/heads/main"):
                return {"object": {"sha": "m" * 40}}
            if path.endswith("pulls/7"):
                return {"mergeable": False}
            if path.endswith("/status"):
                return {"statuses": [{"context": "LegacyCI | AppBeta · Test (PR + main)", "state": "failure"}]}
            if path.endswith("issues/7"):
                return {"state": "closed", "title": "T", "html_url": "u", "pull_request": {"merged_at": "x"}}
            if path.endswith("issues/8"):
                return {"state": "open", "title": "Open one", "html_url": "u"}
            if "issues/9" in path or "branches/gone" in path:
                from coordlib.gh import NotFound
                raise NotFound("HTTP 404")
            if "branches/feature/x" in path:
                return {"commit": {"sha": "b" * 40, "commit": {"committer": {"date": t(5)}}}}
            raise AssertionError(path)

        def rest_paginated(self, path):
            self.paths.append(path)
            if "pulls?state=open" in path:
                return [{"number": 7, "title": "Seven", "html_url": "u", "draft": False, "body": "Fixes #55 and closes #56",
                         "head": {"ref": "bughunt/seven", "sha": "a" * 40, "repo": {"full_name": "o/r"}},
                         "base": {"ref": "main"}, "labels": [{"name": "train"}], "updated_at": t(3), "created_at": t(90)}]
            if "pulls/7/files" in path:
                return [{"filename": "Packages/CoreKit/Sources/A.swift"}]
            if "issues/7/reactions" in path:
                return [{"content": "+1", "created_at": t(10), "user": {"login": BOT}}]
            if "check-runs" in path:
                return [{"check_runs": [{"name": "CI verdict", "status": "completed", "conclusion": "success"}]}]
            if "check-suites" in path:
                return [{"check_suites": [{"created_at": t(40)}]}]
            if "issues/7/comments" in path:
                return [{"user": {"login": BOT}, "body": codex_summary("a" * 40)["body"]}]
            if "pulls/7/comments" in path:
                return [
                    {"id": 1, "in_reply_to_id": None, "user": {"login": BOT}, "created_at": t(30)},
                    {"id": 2, "in_reply_to_id": 1, "user": {"login": "alex-bridges"}, "created_at": t(20)},
                    {"id": 3, "in_reply_to_id": None, "user": {"login": BOT}, "created_at": t(15)},
                ]
            raise AssertionError(path)

    def test_a_graphql_403_falls_back_to_rest_and_stays_there(self):
        from coordlib.gh import GitHubError
        fake = self.Fake(GitHubError("HTTP 403 POST https://api.github.com/graphql: GitHub GraphQL is not available"))
        saved = os.environ.pop("CLAUDE_CODE_REMOTE", None)
        try:
            main, nodes = snapshot.fetch_prs(fake)
            snapshot.fetch_prs(fake)
        finally:
            if saved is not None:
                os.environ["CLAUDE_CODE_REMOTE"] = saved
        self.assertEqual(fake.graphql_calls, 1, "after one 403 the session stays on REST")
        pr = snapshot.summarize_pr(nodes[0])
        self.assertEqual(main["sha"], "m" * 40)
        self.assertTrue(pr["codex"]["approved"])
        self.assertEqual(pr["unansweredFindings"], 1, "thread 1 answered, thread 3 not")
        self.assertEqual(pr["ci"], {"verdict": "success", "failing": [], "pending": [], "started": [], "not_run": []},
                         "Xcode Cloud ignored")
        self.assertEqual((pr["mergeable"], pr["fromFork"], pr["labels"], pr["closes"]),
                         ("CONFLICTING", False, ["train"], [55, 56]))

    def test_other_graphql_errors_are_not_swallowed(self):
        from coordlib.gh import GitHubError
        fake = self.Fake(GitHubError("HTTP 502 POST https://api.github.com/graphql: bad gateway"))
        saved = os.environ.pop("CLAUDE_CODE_REMOTE", None)
        try:
            with self.assertRaises(GitHubError):
                snapshot.fetch_prs(fake)
        finally:
            if saved is not None:
                os.environ["CLAUDE_CODE_REMOTE"] = saved

    def test_cloud_sessions_skip_graphql_entirely(self):
        fake = self.Fake(AssertionError("GraphQL must not be tried"))
        saved = os.environ.get("CLAUDE_CODE_REMOTE")
        os.environ["CLAUDE_CODE_REMOTE"] = "true"
        try:
            snapshot.fetch_prs(fake)
            targets = snapshot.fetch_targets(fake, [7, 8, 9], ["feature/x", "gone"])
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_CODE_REMOTE", None)
            else:
                os.environ["CLAUDE_CODE_REMOTE"] = saved
        self.assertEqual(fake.graphql_calls, 0)
        self.assertEqual(targets["issues"][7]["state"], "MERGED")
        self.assertEqual(targets["issues"][8]["state"], "OPEN")
        self.assertIsNone(targets["issues"][9])
        self.assertEqual(targets["branches"]["feature/x"]["sha"], "b" * 40)
        self.assertIsNone(targets["branches"]["gone"])


class CodexRoundSevenTests(unittest.TestCase):
    """Findings from Codex's review of 0b36f6df4 on #989, fixed as classes."""

    class Fake:
        owner, name, repo = "o", "r", "o/r"

        def rest_paginated(self, path):
            if "pulls/101/files" in path:
                return [{"filename": "B.swift", "previous_filename": "A.swift"}, {"filename": "C.swift"}]
            if "issues/101/reactions" in path:
                return [{"content": "+1", "created_at": t(5), "user": {"login": BOT}}]
            if "check-runs" in path:
                return [{"check_runs": [{"name": "Android", "status": "completed", "conclusion": "failure"}]}]
            raise AssertionError(path)

        def rest(self, path, method="GET", body=None):
            if path.endswith("/status"):
                return {"statuses": []}
            raise AssertionError(path)

    def test_every_capped_list_is_completed_or_the_pr_is_held(self):
        node = pr_node(
            files={"totalCount": 2, "nodes": [{"path": "B.swift", "changeType": "RENAMED"}, {"path": "C.swift"}]},
            reactions={"totalCount": 101, "nodes": []},
            reviewThreads={"totalCount": 1, "nodes": [{"isResolved": False, "comments": {
                "totalCount": 60, "nodes": [{"author": {"login": BOT}}]}}]},
        )
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"] = {"totalCount": 101, "nodes": []}
        snapshot.complete_pr_node(self.Fake(), node)
        pr = snapshot.summarize_pr(node)
        self.assertEqual(sorted(pr["files"]), ["A.swift", "B.swift", "C.swift"], "both sides of the rename")
        self.assertTrue(pr["codex"]["approved"], "the 👍 beyond the first page was found")
        self.assertEqual(pr["ci"]["failing"], ["Android"], "a red check beyond the first page was found")
        self.assertEqual(pr["incomplete"], ["review-thread comments (a thread longer than 50)"])
        from coordlib import train
        verdict, reason = train.eligibility(dict(pr, ci={"failing": [], "pending": [], "started": []}))
        self.assertEqual(verdict, train.HOLD)
        self.assertIn("only part of", reason)

    def test_a_fork_pr_with_the_same_branch_name_is_never_taken_for_local_work(self):
        fork = snapshot.summarize_pr(pr_node(number=200, headRefName="feature/x", isCrossRepository=True))
        comments = [coord_comment(1, 7, 10, {"kind": "claim", "agent": "me", "issue": 7, "branch": "feature/x"})]
        claims = snapshot.resolve_claims(comments, {"issues": {7: {"state": "OPEN"}}, "branches": {}}, [fork], NOW)
        self.assertIsNone(claims[0]["pr"])
        self.assertIsNone(render.claim_owner({"claims": claims}, fork))
        snap = {"claims": [], "prs": [fork], "messages": [
            {"id": 1, "createdAt": t(5), "payload": {"kind": "msg", "agent": "x", "to": ["#200"]}}]}
        self.assertEqual(cli.unread_direct(snap, "me", None, current_branch="feature/x"), 0)

    def test_a_comment_in_the_cursor_second_is_delivered_once(self):
        me = "claude@Eidolon:wt"
        cursor = t(1)  # the previous read stopped one minute ago
        # Created 30 s before the cursor time, after that read had fetched: never shown.
        late = {"id": 11, "createdAt": t(1.5), "payload": {"kind": "msg", "agent": "x", "to": [me]}}
        shown = {"id": 10, "createdAt": t(1.6), "payload": {"kind": "msg", "agent": "x", "to": [me]}}
        snap = {"claims": [], "prs": [], "messages": [late, shown]}
        self.assertEqual(cli.unread_direct(snap, me, cursor, seen_ids=[10]), 1)
        comment = {"id": 11, "issue_url": "https://api.github.com/repos/o/r/issues/5", "created_at": t(1.5),
                   "html_url": "h", "user": {"login": "alex-bridges"},
                   "body": marker.compose("hi", {"kind": "msg", "agent": "x", "to": [me]})}
        since = review.parse_time(cursor) - cli.CURSOR_OVERLAP
        self.assertEqual(len(cli.inbox_items([comment], [], snap, me, since, "", [10])), 1)
        self.assertEqual(cli.inbox_items([comment], [], snap, me, since, "", [10, 11]), [])


LINUX = "Linux plugin probe (x86_64)"
WINDOWS = "Windows plugin probe (dynamic runtime)"


def annotation_path(run_id):
    return f"repos/{{repo}}/check-runs/{run_id}/annotations?per_page=1"


def attempts_path(encoded_name, sha="a" * 40):
    return f"repos/{{repo}}/commits/{sha}/check-runs?check_name={encoded_name}&filter=all&per_page=100"


LINUX_ATTEMPTS = attempts_path("Linux%20plugin%20probe%20%28x86_64%29")
WINDOWS_ATTEMPTS = attempts_path("Windows%20plugin%20probe%20%28dynamic%20runtime%29")


class NotRunGraphQLTests(unittest.TestCase):
    """The NOT RUN rule on the GraphQL path: the query carries each run's databaseId, and the
    first annotation of a failed run is read over REST by the helper the fallback shares."""

    class Fake:
        repo, owner, name = "o/r", "o", "r"

        def __init__(self, contexts, annotations, attempts=None):
            node = pr_node()
            node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = list(contexts)
            self.nodes = [node]
            self.annotations = annotations  # run id -> messages
            self.attempts = attempts or {}  # check name -> REST runs, every attempt
            self.latest = {c.get("name"): rest_run(c.get("databaseId"), c.get("name"), (c.get("conclusion") or "").lower())
                           for c in contexts if c.get("__typename") == "CheckRun"}
            self.paths = []

        def graphql(self, query, variables=None):
            return {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": "m" * 40}},
                                   "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                    "nodes": self.nodes}}}

        def rest(self, path, method="GET", body=None):
            self.paths.append(path)
            if "/annotations" in path:
                run_id = int(path.split("check-runs/")[1].split("/")[0])
                return [{"message": m, "annotation_level": "failure"} for m in self.annotations[run_id]]
            raise AssertionError(path)

        def rest_paginated(self, path):
            self.paths.append(path)
            if "check-runs?check_name=" in path:
                return attempts_page(path, self.attempts, self.latest)
            raise AssertionError(path)

    @staticmethod
    def run_node(run_id, name, conclusion="SUCCESS", status="COMPLETED"):
        return dict(check(name, status=status, conclusion=conclusion), databaseId=run_id)

    def ci(self, fake):
        _, nodes = snapshot.fetch_prs_graphql(fake)
        return snapshot.summarize_pr(nodes[0])["ci"]

    def test_the_three_cases_and_reads_only_failure_class_runs(self):
        fake = self.Fake(
            contexts=[
                self.run_node(1, "CI verdict"),
                self.run_node(10000099, LINUX, "FAILURE"),
                self.run_node(3, "Android", "FAILURE"),
                self.run_node(4, "AppGamma", "FAILURE"),
                self.run_node(5, WINDOWS, "FAILURE"),
                self.run_node(6, "Docs", "CANCELLED"),
                self.run_node(7, "Select lanes", "SKIPPED"),
                self.run_node(8, "LegacyCI | AppBeta · Test (PR + main) | Test - iOS", "ACTION_REQUIRED"),  # ignored
                self.run_node(9, "CoreKit debug gate", None, status="IN_PROGRESS"),
                {"__typename": "StatusContext", "context": "legacy status", "state": "FAILURE"},
            ],
            annotations={1: [BILLING_REFUSED], 10000099: [BILLING_REFUSED], 3: [REAL_FAILURE], 4: [],
                         5: [BILLING_REFUSED], 6: [BILLING_REFUSED], 7: [], 8: [BILLING_REFUSED], 9: []},
        )
        ci = self.ci(fake)
        self.assertEqual(ci["not_run"], [LINUX, WINDOWS])
        self.assertEqual(ci["failing"], ["Android", "AppGamma", "legacy status"],
                         "another annotation, none, and a commit status stay red")
        self.assertEqual(fake.paths, [
            annotation_path(10000099), LINUX_ATTEMPTS,
            annotation_path(3),
            annotation_path(4),
            annotation_path(5), WINDOWS_ATTEMPTS,
        ], "only completed, failure-class, non-ignored runs are read; attempts are listed only for a refusal")

    def test_a_green_head_costs_no_call(self):
        fake = self.Fake(contexts=[self.run_node(1, "CI verdict"), self.run_node(2, "Select lanes", "SKIPPED"),
                                   self.run_node(3, "CoreKit release gate", "CANCELLED")],
                         annotations={})
        self.assertEqual(self.ci(fake)["verdict"], "success")
        self.assertEqual(fake.paths, [])

    def test_a_red_head_without_a_refusal_lists_no_attempts(self):
        fake = self.Fake(contexts=[self.run_node(3, "Android", "FAILURE"), self.run_node(4, "AppGamma", "FAILURE")],
                         annotations={3: [REAL_FAILURE], 4: []})
        self.assertEqual(self.ci(fake)["failing"], ["Android", "AppGamma"])
        self.assertEqual(fake.paths, [annotation_path(3), annotation_path(4)])

    def test_a_refused_rerun_does_not_hide_an_earlier_real_failure(self):
        # Attempt 1 failed for real; the re-run (attempt 2, the only one the rollup shows) was refused.
        fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")],
                         annotations={22: [BILLING_REFUSED], 21: [REAL_FAILURE]},
                         attempts={LINUX: [rest_run(22, LINUX, "failure"), rest_run(21, LINUX, "failure")]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([LINUX], []))
        self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS, annotation_path(21)])

    def test_two_refused_attempts_are_not_run(self):
        fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")],
                         annotations={22: [BILLING_REFUSED], 21: [BILLING_REFUSED]},
                         attempts={LINUX: [rest_run(22, LINUX, "failure"), rest_run(21, LINUX, "failure")]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([], [LINUX]))
        self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS, annotation_path(21)])

    def test_a_single_refused_attempt_is_not_run(self):
        fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")], annotations={22: [BILLING_REFUSED]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([], [LINUX]))
        self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS])

    def test_earlier_attempts_that_passed_or_were_cancelled_are_not_read(self):
        fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")], annotations={22: [BILLING_REFUSED]},
                         attempts={LINUX: [rest_run(22, LINUX, "failure"), rest_run(21, LINUX, "success"),
                                           rest_run(20, LINUX, "cancelled")]})
        self.assertEqual(self.ci(fake)["not_run"], [LINUX])
        self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS])

    def test_an_earlier_failure_with_no_annotation_or_no_id_is_red_unread(self):
        for earlier in (rest_run(21, LINUX, "failure", annotations_count=0), rest_run(None, LINUX, "failure")):
            fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")], annotations={22: [BILLING_REFUSED]},
                             attempts={LINUX: [rest_run(22, LINUX, "failure"), earlier]})
            ci = self.ci(fake)
            self.assertEqual((ci["failing"], ci["not_run"]), ([LINUX], []))
            self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS])

    def test_the_cap_covers_earlier_attempts_and_what_it_leaves_unread_is_red(self):
        # 1 read for the run, 1 listing, then 8 earlier refused attempts fit; the 9th does not.
        earlier = [rest_run(n, LINUX, "failure") for n in range(100, 109)]
        fake = self.Fake(contexts=[self.run_node(22, LINUX, "FAILURE")],
                         annotations={n: [BILLING_REFUSED] for n in [22] + list(range(100, 109))},
                         attempts={LINUX: [rest_run(22, LINUX, "failure")] + earlier})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([LINUX], []))
        self.assertEqual(fake.paths, [annotation_path(22), LINUX_ATTEMPTS] + [annotation_path(n) for n in range(100, 108)])
        self.assertEqual(len(fake.paths), snapshot.MAX_CHECK_RUN_READS)

    def test_the_cap_and_a_run_without_an_id_leave_runs_red(self):
        count = 12
        fake = self.Fake(contexts=[check("no id", conclusion="FAILURE")]
                         + [self.run_node(n, f"job {n:02d}", "FAILURE") for n in range(1, count + 1)],
                         annotations={n: [BILLING_REFUSED] for n in range(1, count + 1)})
        ci = self.ci(fake)
        # Each single-attempt refusal costs two reads (its annotation, its attempts): 5 fit.
        self.assertEqual(len(fake.paths), snapshot.MAX_CHECK_RUN_READS)
        self.assertEqual(ci["not_run"], [f"job {n:02d}" for n in range(1, 6)])
        self.assertEqual(ci["failing"], [f"job {n:02d}" for n in range(6, count + 1)] + ["no id"],
                         "what was not read is held as red")


class NotRunRestTests(unittest.TestCase):
    """The same NOT RUN rule on the REST fallback, through the same helper."""

    class Fake(RestFallbackTests.Fake):
        def __init__(self, runs, annotations, attempts=None):
            super().__init__(AssertionError("GraphQL must not be tried"))
            self.runs = runs
            self.annotations = annotations  # run id -> messages
            self.attempts = attempts or {}  # check name -> REST runs, every attempt
            self.latest = {r["name"]: r for r in runs}

        def rest(self, path, method="GET", body=None):
            if "/annotations" in path:
                self.paths.append(path)
                run_id = int(path.split("check-runs/")[1].split("/")[0])
                return [{"message": m, "annotation_level": "failure"} for m in self.annotations[run_id]]
            return super().rest(path, method, body)

        def rest_paginated(self, path):
            if "check-runs?check_name=" in path:
                self.paths.append(path)
                return attempts_page(path, self.attempts, self.latest)
            if "check-runs" in path:
                self.paths.append(path)
                return [{"check_runs": self.runs}]
            return super().rest_paginated(path)

    def ci(self, fake):
        _, nodes = snapshot.fetch_prs_rest(fake)
        return snapshot.summarize_pr(nodes[0])["ci"]

    def extra_reads(self, fake):
        """What attach_first_annotations spent: annotation reads and attempt listings, in order."""
        return [p for p in fake.paths if "/annotations" in p or "check_name=" in p]

    def test_the_three_cases_and_reads_only_failure_class_runs(self):
        fake = self.Fake(
            runs=[
                rest_run(1, "CI verdict", "success"),
                rest_run(2, LINUX, "failure"),
                rest_run(3, "Android", "failure"),
                rest_run(4, "AppGamma", "failure", annotations_count=0),        # nothing to read
                rest_run(5, WINDOWS, "timed_out"),
                rest_run(6, "Docs", "cancelled"),
                rest_run(7, "Select lanes", "skipped"),
                rest_run(8, "LegacyCI | AppBeta · Test (PR + main) | Test - iOS", "action_required"),  # ignored
                rest_run(9, "CoreKit debug gate", None, status="in_progress"),
            ],
            annotations={1: [BILLING_REFUSED], 2: [BILLING_REFUSED], 3: [REAL_FAILURE], 4: [],
                         5: [BILLING_REFUSED], 6: [BILLING_REFUSED], 7: [], 8: [BILLING_REFUSED], 9: []},
        )
        ci = self.ci(fake)
        self.assertEqual(ci["not_run"], [LINUX, WINDOWS])
        self.assertEqual(ci["failing"], ["Android", "AppGamma"], "another annotation, and none, stay red")
        self.assertEqual(ci["pending"], ["CoreKit debug gate"])
        self.assertEqual(self.extra_reads(fake), [
            annotation_path(2), LINUX_ATTEMPTS,
            annotation_path(3),
            annotation_path(5), WINDOWS_ATTEMPTS,
        ], "only failure-class, non-ignored runs with an annotation are read; attempts only for a refusal")

    def test_a_failure_whose_annotation_list_comes_back_empty_stays_failing(self):
        fake = self.Fake(runs=[rest_run(3, "Android", "failure")], annotations={3: []})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), (["Android"], []))
        self.assertEqual(self.extra_reads(fake), [annotation_path(3)])

    def test_a_green_head_costs_no_annotation_reads(self):
        fake = self.Fake(runs=[rest_run(1, "CI verdict", "success")], annotations={1: [BILLING_REFUSED]})
        self.assertEqual(self.ci(fake)["not_run"], [])
        self.assertEqual(self.extra_reads(fake), [])

    def test_a_refused_rerun_does_not_hide_an_earlier_real_failure(self):
        fake = self.Fake(runs=[rest_run(22, LINUX, "failure")],
                         annotations={22: [BILLING_REFUSED], 21: [REAL_FAILURE]},
                         attempts={LINUX: [rest_run(22, LINUX, "failure"), rest_run(21, LINUX, "failure")]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([LINUX], []))
        self.assertEqual(self.extra_reads(fake), [annotation_path(22), LINUX_ATTEMPTS, annotation_path(21)])

    def test_two_refused_attempts_are_not_run(self):
        fake = self.Fake(runs=[rest_run(22, LINUX, "failure")],
                         annotations={22: [BILLING_REFUSED], 21: [BILLING_REFUSED]},
                         attempts={LINUX: [rest_run(22, LINUX, "failure"), rest_run(21, LINUX, "failure")]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([], [LINUX]))
        self.assertEqual(self.extra_reads(fake), [annotation_path(22), LINUX_ATTEMPTS, annotation_path(21)])

    def test_a_single_refused_attempt_is_not_run(self):
        fake = self.Fake(runs=[rest_run(22, LINUX, "failure")], annotations={22: [BILLING_REFUSED]})
        ci = self.ci(fake)
        self.assertEqual((ci["failing"], ci["not_run"]), ([], [LINUX]))
        self.assertEqual(self.extra_reads(fake), [annotation_path(22), LINUX_ATTEMPTS])

    def test_reads_are_capped_and_an_unread_failure_stays_red(self):
        count = 12
        runs = [rest_run(n, f"job {n:02d}", "failure") for n in range(1, count + 1)]
        fake = self.Fake(runs=runs, annotations={n: [BILLING_REFUSED] for n in range(1, count + 1)})
        ci = self.ci(fake)
        self.assertEqual(len(self.extra_reads(fake)), snapshot.MAX_CHECK_RUN_READS)
        self.assertEqual(ci["not_run"], [f"job {n:02d}" for n in range(1, 6)])
        self.assertEqual(ci["failing"], [f"job {n:02d}" for n in range(6, count + 1)],
                         "a run the cap left unread is held as red, never waved through")

    def test_the_graphql_path_completes_a_truncated_rollup_with_annotations_too(self):
        fake = self.Fake(runs=[rest_run(2, LINUX, "failure"), rest_run(3, "Android", "failure")],
                         annotations={2: [BILLING_REFUSED], 3: [REAL_FAILURE]})
        node = pr_node()
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"] = {"totalCount": 101, "nodes": []}
        snapshot.complete_pr_node(fake, node)
        ci = snapshot.summarize_pr(node)["ci"]
        self.assertEqual((ci["failing"], ci["not_run"]), (["Android"], [LINUX]))
        self.assertEqual(self.extra_reads(fake), [annotation_path(2), LINUX_ATTEMPTS, annotation_path(3)])


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

    def test_http_pagination_stays_on_the_owner_name_path(self):
        from coordlib import gh as ghmodule
        link = ("<https://api.github.com/repositories/100000001/issues/comments?since=x&per_page=100&page=2>; "
                'rel="next", <https://api.github.com/repositories/100000001/issues/comments?page=5>; rel="last"')
        self.assertEqual(ghmodule.repo_path_link(ghmodule._next_link(link), "example-owner/example-repo"),
                         "https://api.github.com/repos/example-owner/example-repo/issues/comments?since=x&per_page=100&page=2")
        requested = []
        pages = {
            "repos/o/r/issues/comments?per_page=100": ([{"id": 1}], {"link": '<https://api.github.com/repositories/42/issues/comments?per_page=100&page=2>; rel="next"'}),
            "https://api.github.com/repos/o/r/issues/comments?per_page=100&page=2": ([{"id": 2}], {}),
        }
        gh = GitHub("o/r", transport="http", token="t")
        gh._http = lambda method, url, body=None: (requested.append(url), pages[url])[1]
        self.assertEqual(gh.rest_paginated("repos/{repo}/issues/comments?per_page=100"), [{"id": 1}, {"id": 2}])
        self.assertFalse(any("repositories/" in url for url in requested), requested)

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
        brief = render.brief(self.full_snapshot(), "me", unread=0)
        self.assertIn("1 claim without a PR", brief)
        self.assertIn("workflow runs 1 running / 1 queued", brief)

    def test_never_started_checks_are_counted_on_the_board_and_do_not_hold_the_train(self):
        snap = self.full_snapshot()
        node = pr_node(labels={"nodes": [{"name": "train"}]}, reactions={"nodes": [reaction("THUMBS_UP", 30)]})
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [
            check("CI verdict"),
            refused_check("Linux plugin probe (x86_64)"),
            refused_check("Windows plugin probe (dynamic runtime)")]
        snap["prs"] = [snapshot.summarize_pr(node)]
        text = render.board(snap, "me")
        self.assertIn("CI green, 2 not run (billing) · Codex approved", text)
        self.assertIn("1. PR #101 · added 3m ago by a · ready", text)
        self.assertIn("CI green, 2 not run (billing), Codex approved", render.brief(snap, "me"))

    def test_empty_board_renders(self):
        empty = {"schema": 1, "generatedAt": snapshot.iso(NOW), "repo": "o/r", "main": {}, "prs": [], "claims": [],
                 "overlaps": [], "train": [], "runs": {"running": [], "queued": []}, "messages": []}
        self.assertIn("nothing open", render.board(empty, "me"))


if __name__ == "__main__":
    unittest.main()

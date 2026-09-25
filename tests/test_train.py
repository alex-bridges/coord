"""Tests for the merge train. Run: python3 -m unittest discover -s Tools/coord/tests

The pure rules are tested directly. The step is tested against REAL git — a bare
"origin" with main and refs/pull/N/head, and a clone as the step's repository —
and an in-memory GitHub that records every write, so each branch of the state
machine is exercised end to end without the network.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coordlib import marker, snapshot, train, train_step  # noqa: E402
from test_coord import BOT, NOW, check, codex_summary, pr_node, reaction  # noqa: E402


def ready_node(number: int, head: str, branch: str, files=("Packages/CoreKit/Sources/A.swift",), labels=("train",)):
    node = pr_node(number=number, headRefOid=head, headRefName=branch, title=f"PR {number}",
                   labels={"nodes": [{"name": l} for l in labels]},
                   files={"totalCount": len(files), "nodes": [{"path": f} for f in files]},
                   reactions={"nodes": [reaction("THUMBS_UP", 30)]})
    node["commits"]["nodes"][0]["commit"]["oid"] = head
    node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [check("CI verdict")]
    return node


class EligibilityTests(unittest.TestCase):
    def summary(self, **overrides):
        return snapshot.summarize_pr(ready_node(1, "a" * 40, "b", **overrides))

    def test_ready(self):
        self.assertEqual(train.eligibility(self.summary()), (train.READY, "ready"))

    def test_drops(self):
        cases = [
            (dict(files=(".github/workflows/ci.yml",)), "workflow"),
        ]
        for overrides, word in cases:
            verdict, reason = train.eligibility(self.summary(**overrides))
            self.assertEqual(verdict, train.DROP)
            self.assertIn(word, reason)
        pr = self.summary()
        self.assertEqual(train.eligibility(pr, head_at_add="b" * 40)[0], train.DROP)
        self.assertEqual(train.eligibility(dict(pr, base="release"))[0], train.DROP)
        self.assertEqual(train.eligibility(dict(pr, fromFork=True))[0], train.DROP)
        self.assertEqual(train.eligibility(dict(pr, mergeable="CONFLICTING"))[0], train.DROP)
        # A truncated file list is completed by the caller; the full list decides.
        self.assertEqual(train.eligibility(pr, all_files=["x", ".github/workflows/a.yml"])[0], train.DROP)

    def test_holds(self):
        pr = self.summary()
        self.assertEqual(train.eligibility(dict(pr, isDraft=True))[0], train.HOLD)
        self.assertEqual(train.eligibility(dict(pr, labels=["train", "needs-alex"]))[0], train.HOLD)
        approved = dict(pr, labels=["train", "needs-alex", "alex-approved"])
        self.assertEqual(train.eligibility(dict(approved, alexApprovedHead=pr["head"]))[0], train.READY)
        self.assertEqual(train.eligibility(dict(approved, alexApprovedHead="0" * 40))[0], train.HOLD,
                         "Alex's approval of an older head does not cover a new push")
        self.assertEqual(train.eligibility(approved)[0], train.HOLD, "the label alone is not an approval")
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": True}))[1],
                         "Codex is re-reviewing it")
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": False}))[0], train.HOLD)
        unavailable = dict(pr, codex={"approved": False, "reviewing": False}, labels=["train", "codex-unavailable"])
        self.assertEqual(train.eligibility(dict(unavailable, substituteReviewHead=pr["head"]))[0], train.READY)
        verdict, reason = train.eligibility(dict(unavailable, substituteReviewHead="0" * 40))
        self.assertEqual(verdict, train.HOLD, "a substitute review of an OLDER head does not cover a new push")
        self.assertIn("substitute review", reason)
        self.assertEqual(train.eligibility(unavailable)[0], train.HOLD, "the label alone is not a review")
        self.assertEqual(train.eligibility(dict(pr, unansweredFindings=2))[1], "2 unanswered Codex findings")
        self.assertEqual(train.eligibility(dict(pr, ci={"failing": ["Android"], "pending": [], "verdict": "failure"}))[0],
                         train.HOLD)
        self.assertEqual(train.eligibility(dict(pr, ci={"failing": [], "pending": ["x"], "verdict": "pending"}))[0],
                         train.READY)


class PlanAndRunTests(unittest.TestCase):
    def test_plan_caps_and_solo(self):
        queue = [{"pr": n} for n in (1, 2, 3, 4, 5)]
        verdicts = {1: (train.READY, ""), 2: (train.HOLD, "h"), 3: (train.READY, ""), 4: (train.DROP, "d"),
                    5: (train.READY, "")}
        chosen = train.plan(queue, verdicts, {n: [] for n in verdicts}, cap=2)
        self.assertEqual(chosen["batch"], [1, 3])
        self.assertEqual(chosen["drops"], [(4, "d")])
        self.assertEqual(chosen["holds"], [(2, "h")])
        solo_front = train.plan(queue, verdicts, {1: ["train:solo"], 3: [], 5: []}, cap=3)
        self.assertEqual(solo_front["batch"], [1])
        solo_behind = train.plan(queue, verdicts, {1: [], 3: ["train:solo"], 5: []}, cap=3)
        self.assertEqual(solo_behind["batch"], [1, 5])

    def test_is_gate_run(self):
        bot = {"head_sha": "t", "event": "workflow_dispatch", "actor": {"login": "github-actions[bot]"}}
        self.assertTrue(train.is_gate_run(bot, "t"))
        self.assertFalse(train.is_gate_run(bot, "other tip"))
        self.assertFalse(train.is_gate_run(dict(bot, actor={"login": "alex-bridges"}), "t"))
        self.assertFalse(train.is_gate_run(dict(bot, event="push"), "t"))
        # A re-run changes triggering_actor, never actor.
        self.assertTrue(train.is_gate_run(dict(bot, triggering_actor={"login": "alex-bridges"}), "t"))

    def test_run_action(self):
        self.assertEqual(train.run_action(None, 2), "dispatch")
        self.assertEqual(train.run_action({"status": "in_progress"}, 2), "wait")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "success"}, 2), "merge")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 1}, 2), "rerun")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 2}, 2), "split")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 2}, 1), "eject")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "cancelled"}, 1), "abandon")
        for infrastructure in ("timed_out", "startup_failure"):
            self.assertEqual(train.run_action({"status": "completed", "conclusion": infrastructure, "run_attempt": 2}, 1),
                             "abandon", "an outage is not the PR's fault")

    def test_parse_batch_reads_only_this_batchs_merges(self):
        log = ("c3\tc2 p3\ttrain/B\tMerge pull request #30 from o/c\n"
               "c2\tc1 p2\ttrain/B\tMerge pull request #20 from o/b\n"
               "c1\tm0 p1\ttrain/A\tMerge pull request #10 from o/a\n")  # an EARLIER batch, already on main
        self.assertEqual(train.parse_batch(log, "train/B"), ("c1", [{"pr": 20, "sha": "p2"}, {"pr": 30, "sha": "p3"}]))
        self.assertEqual(train.parse_batch(log, "train/C"), (None, []))


# MARK: - Step against real git and a fake GitHub


class FakeGitHub:
    owner, name, repo = "o", "r", "o/r"

    def __init__(self, nodes: List[Dict[str, Any]], main: str):
        self.nodes = nodes
        self.main = main
        self.runs: Dict[str, List[Dict[str, Any]]] = {}
        self.jobs: Dict[int, List[Dict[str, Any]]] = {}
        self.issue_comments: Dict[int, List[Dict[str, Any]]] = {}
        self.writes: List[tuple] = []
        self.dispatched: List[tuple] = []
        self.branch_runs: Dict[str, List[Dict[str, Any]]] = {}
        self.fail_writes = 0
        self.main_runs: List[Dict[str, Any]] = []
        self.dispatches_today: List[Dict[str, Any]] = []
        self.patch_error = ""
        self.reread_error = ""
        self.reread_main = ""
        self.calls = 0

    def graphql(self, query, variables=None):
        return {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": self.main}},
                               "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                "nodes": self.nodes}}}

    def rest(self, path, method="GET", body=None):
        from coordlib.gh import GitHubError
        if method == "PATCH" and self.patch_error:
            self.writes.append((method, path, body))
            raise GitHubError(self.patch_error)
        if path.endswith("git/ref/heads/main"):
            if self.reread_error:
                raise GitHubError(self.reread_error)
            return {"object": {"sha": self.reread_main}}
        if self.fail_writes and method in ("POST", "DELETE"):
            self.fail_writes -= 1
            from coordlib.gh import GitHubError
            raise GitHubError("HTTP 502 (injected)")
        self.writes.append((method, path, body))
        match = re.search(r"issues/(\d+)/comments$", path)
        if method == "POST" and match:
            self.issue_comments.setdefault(int(match.group(1)), []).append(
                {"created_at": snapshot.iso(NOW), "body": body["body"]})
        return {"html_url": "h"} if method == "POST" else None

    def rest_paginated(self, path):
        match = re.search(r"actions/workflows/ci\.yml/runs\?branch=([^&]+)", path)
        if match:
            return [{"workflow_runs": self.runs.get(urllib.parse.unquote(match.group(1)), [])}]
        match = re.search(r"actions/runs/(\d+)/jobs", path)
        if match:
            return [{"jobs": self.jobs.get(int(match.group(1)), [])}]
        if "ci.yml/runs?head_sha=" in path:
            return [{"workflow_runs": self.main_runs}]
        if "ci.yml/runs?event=workflow_dispatch" in path:
            return [{"workflow_runs": self.dispatches_today}]
        if "actions/runs?branch=" in path:
            branch = urllib.parse.unquote(path.split("branch=")[1].split("&")[0])
            return [{"workflow_runs": self.branch_runs.get(branch, [])}]
        match = re.search(r"issues/(\d+)/comments", path)
        if match:
            return self.issue_comments.get(int(match.group(1)), [])
        raise AssertionError(f"unexpected paginated call {path}")

    def dispatch_workflow(self, workflow, ref, inputs=None):
        self.dispatched.append((workflow, ref, inputs))

    def posted(self, event):
        out = []
        for method, path, body in self.writes:
            if method == "POST" and path.endswith("/comments"):
                payload = marker.decode(body["body"])
                if payload and payload.get("event") == event:
                    out.append(payload["pr"])
        return out


def sh(cwd, *args, env=None):
    full = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@e", **(env or {}))
    return subprocess.check_output(["git", "-C", cwd, *args], env=full, stderr=subprocess.PIPE).decode().strip()


class StepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="coord-train-")
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.origin = os.path.join(root, "origin.git")
        self.work = os.path.join(root, "work")
        self.clone = os.path.join(root, "clone")
        subprocess.check_call(["git", "init", "-q", "--bare", "-b", "main", self.origin])
        subprocess.check_call(["git", "init", "-q", "-b", "main", self.work])
        self.write("a.txt", "line 1\nline 2\nline 3\n")
        self.write("b.txt", "b\n")
        self.commit("base")
        sh(self.work, "remote", "add", "origin", self.origin)
        sh(self.work, "push", "-q", "origin", "main")
        self.main = sh(self.work, "rev-parse", "HEAD")
        # The workflow's clone: blob-less, no checkout. A plain-path clone would
        # silently ignore --filter, so go through file:// and let the origin serve it.
        sh(self.origin, "config", "uploadpack.allowFilter", "true")
        sh(self.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        subprocess.check_call(["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
                               "file://" + self.origin, self.clone])

    def write(self, path, text):
        with open(os.path.join(self.work, path), "w") as handle:
            handle.write(text)

    def commit(self, message):
        sh(self.work, "add", "-A")
        sh(self.work, "commit", "-q", "-m", message)
        return sh(self.work, "rev-parse", "HEAD")

    def pr_branch(self, number, path, text):
        sh(self.work, "checkout", "-q", "-b", f"pr{number}", self.main)
        self.write(path, text)
        head = self.commit(f"pr {number}")
        sh(self.work, "push", "-q", "origin", f"HEAD:refs/pull/{number}/head")
        sh(self.work, "checkout", "-q", "main")
        return head

    def members(self, name):
        log = sh(self.origin, "log", "--first-parent", "--max-count=10", f"--format={train.LOG_FORMAT}", name)
        return train.parse_batch(log, name)[1]

    def step(self, fake, mode="live", now=None, sleep=None):
        return train_step.Step(fake, train_step.Git(self.clone), mode, now=now or snapshot.now_utc(),
                               log=lambda line: None, sleep=sleep or (lambda seconds: None))

    def queue(self, fake, number, head, minutes_ago):
        fake.issue_comments[number] = [{"created_at": snapshot.iso(NOW - dt.timedelta(minutes=minutes_ago)),
                                        "body": marker.compose("added", {"kind": "train-add", "pr": number, "head": head})}]

    def test_build_batches_ready_prs_and_removes_dropped_ones(self):
        h1 = self.pr_branch(1, "b.txt", "b from 1\n")
        h2 = self.pr_branch(2, "c.txt", "c\n")
        h3 = self.pr_branch(3, "d.txt", "d\n")
        h4 = self.pr_branch(4, "e.txt", "e\n")
        nodes = [ready_node(1, h1, "pr1"), ready_node(2, h2, "pr2"),
                 ready_node(3, h3, "pr3", files=(".github/workflows/x.yml",)),
                 ready_node(4, h4, "pr4")]
        nodes[3]["reactions"] = {"nodes": []}  # no Codex approval: held
        fake = FakeGitHub(nodes, self.main)
        for number, head, minutes in ((2, h2, 50), (1, h1, 40), (3, h3, 30), (4, h4, 20)):
            self.queue(fake, number, head, minutes)
        lines = self.step(fake).run()

        branches = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
        self.assertEqual(len(branches.splitlines()), 1, lines)
        tip = sh(self.origin, "rev-parse", branches)
        self.assertEqual(self.members(branches), [{"pr": 2, "sha": h2}, {"pr": 1, "sha": h1}], "queue order")
        self.assertIn("Train-Batch: " + branches, sh(self.origin, "log", "-1", "--format=%B", tip))
        self.assertEqual(sh(self.origin, "show", f"{tip}:b.txt"), "b from 1")
        self.assertEqual(fake.dispatched, [("ci.yml", branches, {"lanes": "changed"})])
        self.assertEqual(sorted(fake.posted("testing")), [1, 2])
        self.assertEqual(fake.posted("removed"), [3])
        self.assertIn(("DELETE", "repos/{repo}/issues/3/labels/train", None), fake.writes)
        self.assertTrue(any("#4 held: no Codex" in line for line in lines), lines)

    def test_a_pr_that_conflicts_with_an_earlier_member_waits_for_the_next_batch(self):
        h1 = self.pr_branch(1, "a.txt", "line 1\nONE\nline 3\n")
        h2 = self.pr_branch(2, "a.txt", "line 1\nTWO\nline 3\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1"), ready_node(2, h2, "pr2")], self.main)
        self.queue(fake, 1, h1, 20)
        self.queue(fake, 2, h2, 10)
        lines = self.step(fake).run()
        name = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
        tip = sh(self.origin, "rev-parse", name)
        self.assertEqual([m["pr"] for m in self.members(name)], [1])
        self.assertTrue(any("#2 held: it does not merge cleanly on top of #1" in line for line in lines), lines)
        self.assertEqual(fake.posted("removed"), [], "a conflict with a batch-mate is not the PR's fault")

    def test_the_clone_is_partial_and_a_content_merge_fetches_what_it_needs(self):
        self.assertEqual(sh(self.clone, "config", "remote.origin.promisor"), "true")
        h1 = self.pr_branch(1, "a.txt", "ONE\nline 2\nline 3\n")
        h2 = self.pr_branch(2, "a.txt", "line 1\nline 2\nTHREE\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1"), ready_node(2, h2, "pr2")], self.main)
        self.queue(fake, 1, h1, 20)
        self.queue(fake, 2, h2, 10)
        lines = self.step(fake).run()
        name = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
        self.assertTrue(name, lines)
        self.assertEqual(sh(self.origin, "show", f"{name}:a.txt"), "ONE\nline 2\nTHREE",
                         "both edits to one file, merged without a checkout")

    def build_batch(self, fake_nodes_heads):
        """Build a batch with the step itself, return (fake, name, tip)."""
        nodes = [ready_node(n, h, f"pr{n}") for n, h in fake_nodes_heads]
        fake = FakeGitHub(nodes, self.main)
        for index, (number, head) in enumerate(fake_nodes_heads):
            self.queue(fake, number, head, 50 - index)
        self.step(fake).run()
        sh(self.clone, "fetch", "-q", "origin", "+refs/heads/train/*:refs/remotes/origin/train/*")
        name = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
        tip = sh(self.origin, "rev-parse", name)
        fake.writes.clear()
        fake.dispatched.clear()
        return fake, name, tip

    def run_on(self, fake, name, tip, **fields):
        run = {"id": 77, "head_sha": tip, "status": "completed", "run_attempt": 1, "html_url": "run-url",
               "event": "workflow_dispatch", "actor": {"login": "github-actions[bot]"}}
        run.update(fields)
        fake.runs[name] = [run]
        return run

    def test_only_the_trains_own_dispatch_gates_a_batch(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        # Someone's green narrow dispatch at the same tip, newer than any train run.
        self.run_on(fake, name, tip, id=99, conclusion="success", actor={"login": "alex-bridges"})
        lines = self.step(fake, now=snapshot.now_utc()).run()
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes), lines)
        self.assertEqual(fake.posted("merged"), [])

    def test_green_batch_fast_forwards_main_to_the_tested_commit(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        h2 = self.pr_branch(2, "d.txt", "d\n")
        fake, name, tip = self.build_batch([(1, h1), (2, h2)])
        self.run_on(fake, name, tip, conclusion="success")
        here = {"full_name": "o/r"}
        fake.branch_runs["pr1"] = [{"id": 501, "status": status, "head_repository": here} for status in
                                   ("queued", "in_progress", "requested", "waiting", "pending")] + [
                                  {"id": 507, "status": "completed", "head_repository": here},
                                  {"id": 508, "status": "queued", "head_repository": {"full_name": "someone/fork"}}]
        self.step(fake).run()
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes)
        cancelled = [path for method, path, _ in fake.writes if path.endswith("/cancel")]
        self.assertEqual(len(cancelled), 5, "every run that has not completed, in any waiting state")
        self.assertFalse(any("/508/" in path for path in cancelled), "a fork's run with the same branch name is not ours")
        self.assertEqual(sorted(fake.posted("merged")), [1, 2])
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        # GitHub marks a PR merged when its head becomes reachable from main.
        for head in (h1, h2):
            subprocess.check_call(["git", "-C", self.origin, "merge-base", "--is-ancestor", head, tip])

    def test_green_batch_in_dry_run_leaves_main_alone(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake, mode="dry-run").run()
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))
        self.assertEqual(fake.posted("green-dry-run"), [1])
        self.assertIn(("DELETE", "repos/{repo}/issues/1/labels/train", None), fake.writes)

    def test_green_batch_is_rebuilt_when_a_member_moved_or_main_moved(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.nodes[0]["headRefOid"] = "f" * 40
        lines = self.step(fake).run()
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))
        self.assertTrue(any("moved" in line for line in lines), lines)

        fake, name, tip = self.build_batch_after_main_moves(h1)
        self.run_on(fake, name, tip, conclusion="success")
        lines = self.step(fake).run()
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))
        self.assertTrue(any("main moved" in line for line in lines), lines)

    def build_batch_after_main_moves(self, h1):
        sh(self.origin, "update-ref", "-d", sh(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads/train/"))
        fake, name, tip = self.build_batch([(1, h1)])
        self.write("z.txt", "someone merged by hand\n")
        sh(self.work, "checkout", "-q", "main")
        self.commit("hand merge")
        sh(self.work, "push", "-q", "origin", "main")
        return fake, name, tip

    def test_red_batch_reruns_once_then_splits_or_ejects(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        h2 = self.pr_branch(2, "d.txt", "d\n")
        fake, name, tip = self.build_batch([(1, h1), (2, h2)])
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=1)
        self.step(fake).run()
        self.assertIn(("POST", "repos/{repo}/actions/runs/77/rerun-failed-jobs", None), fake.writes)

        fake.writes.clear()
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        fake.jobs[77] = [{"name": "CoreKit release gate", "conclusion": "failure"}]
        self.step(fake).run()
        solo = [body for method, path, body in fake.writes
                if method == "POST" and path.startswith("repos/{repo}/issues/") and path.endswith("/labels")]
        self.assertEqual(solo, [{"labels": ["train:solo"]}, {"labels": ["train:solo"]}])
        self.assertEqual(sorted(fake.posted("split")), [1, 2])
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))

        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        self.step(fake).run()
        self.assertEqual(fake.posted("ejected"), [1])
        self.assertIn(("DELETE", "repos/{repo}/issues/1/labels/train", None), fake.writes)

    def test_clean_up_interrupted_after_main_moved_is_finished_by_the_next_step(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        h2 = self.pr_branch(2, "d.txt", "d\n")
        fake, name, tip = self.build_batch([(1, h1), (2, h2)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.fail_writes = 1  # the first write after the fast-forward fails
        lines = self.step(fake).run()
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes)
        self.assertNotIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes,
                         "the batch branch stays so the clean-up can resume")
        self.assertTrue(any("clean-up incomplete" in line for line in lines), lines)
        first_round = sorted(fake.posted("merged"))
        # What the real PATCH did: main is now the batch tip. The merged PRs are closed.
        sh(self.origin, "update-ref", "refs/heads/main", tip)
        fake.nodes = []
        fake.writes.clear()
        lines = self.step(fake).run()
        self.assertTrue(any("main already contains" in line for line in lines), lines)
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes), "main is not moved twice")
        self.assertEqual(sorted(first_round + fake.posted("merged")), [1, 2], "one receipt per PR, none twice")
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)

    def test_an_ambiguous_main_update_never_throws_away_the_batch(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        # 1. The update WAS applied, the response was lost: finish the merge.
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.patch_error, fake.reread_main = "HTTP 502 (response lost)", tip
        self.step(fake).run()
        self.assertEqual(fake.posted("merged"), [1])
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        # 2. Main cannot even be re-read: keep the batch branch for the next step.
        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.patch_error, fake.reread_error = "HTTP 502", "HTTP 502 again"
        lines = self.step(fake).run()
        self.assertNotIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        self.assertTrue(any("keeping the batch" in line for line in lines), lines)
        # 3. The update really failed (main elsewhere): rebuild.
        fake.writes.clear()
        fake.reread_error, fake.reread_main = "", self.main
        self.step(fake).run()
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        self.assertEqual(fake.posted("merged"), [])

    def test_the_final_check_reads_fresh_pr_state(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        dequeued = ready_node(1, h1, "pr1", labels=())  # someone ran `train remove` mid-step
        calls = []
        original = fake.graphql
        def graphql(query, variables=None):
            calls.append(1)
            data = original(query, variables)
            if len(calls) > 1:
                data["repository"]["pullRequests"]["nodes"] = [dequeued]
            return data
        fake.graphql = graphql
        lines = self.step(fake).run()
        self.assertGreaterEqual(len(calls), 2, "the PRs are read again before main moves")
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes), lines)

    def test_one_failed_cancel_does_not_stop_the_others_and_keeps_the_batch(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        here = {"full_name": "o/r"}
        fake.branch_runs["pr1"] = [{"id": n, "status": "queued", "head_repository": here} for n in (601, 602, 603)]
        original = fake.rest
        def rest(path, method="GET", body=None):
            if path.endswith("/601/cancel"):
                from coordlib.gh import GitHubError
                raise GitHubError("HTTP 502")
            if path.endswith("/602/cancel"):
                from coordlib.gh import GitHubError
                raise GitHubError("HTTP 409 Cannot cancel a workflow run that is completed")
            return original(path, method, body)
        fake.rest = rest
        lines = self.step(fake).run()
        self.assertIn(("POST", "repos/{repo}/actions/runs/603/cancel", None), fake.writes, "the rest still cancelled")
        self.assertNotIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes,
                         "a failed cancel keeps the batch for the next step")
        self.assertTrue(any("601" in line for line in lines), lines)
        self.assertFalse(any("602" in line for line in lines), "409: the run finished, nothing to retry")

    def test_a_person_s_train_branch_is_never_touched(self):
        sh(self.work, "push", "-q", "origin", f"{self.main}:refs/heads/train/fix-cache")
        fake = FakeGitHub([], self.main)
        lines = self.step(fake).run()
        self.assertFalse(any(method == "DELETE" for method, _, _ in fake.writes), fake.writes)
        self.assertTrue(any("train/fix-cache is not a batch" in line for line in lines), lines)

    def test_a_run_the_train_does_not_recognise_is_never_dispatched_over(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, status="in_progress", actor={"login": "someone-else[bot]"})
        for minutes in (30, 45, 60, 75):
            self.step(fake, now=snapshot.now_utc() + dt.timedelta(minutes=minutes)).run()
        self.assertEqual(fake.dispatched, [], "no repeat dispatch on top of an existing run, however many ticks")

    def test_the_daily_budget_stops_new_batches_and_redispatches(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1")], self.main)
        self.queue(fake, 1, h1, 20)
        fake.dispatches_today = [{"head_branch": f"train/2026092{i}"} for i in range(train.DAILY_RUNS_DEFAULT)]
        lines = self.step(fake).run()
        self.assertEqual(fake.dispatched, [])
        self.assertTrue(any("daily budget" in line for line in lines), lines)
        self.assertEqual(sh(self.origin, "for-each-ref", "refs/heads/train/"), "", "no batch branch built either")

    def test_a_red_main_stops_batches_and_is_never_blamed_on_prs(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1")], self.main)
        self.queue(fake, 1, h1, 20)
        fake.main_runs = [{"id": 900, "status": "completed", "conclusion": "failure", "html_url": "main-run"}]
        fake.jobs[900] = [{"name": "Android", "conclusion": "failure"}]
        lines = self.step(fake).run()
        self.assertEqual(fake.dispatched, [])
        self.assertTrue(any("is red" in line for line in lines), lines)
        # A batch that went red in the same job main is red in: nobody is ejected.
        fake.main_runs = []
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        fake.jobs[77] = [{"name": "Android", "conclusion": "failure"}]
        fake.main_runs = [{"id": 900, "status": "completed", "conclusion": "failure", "html_url": "main-run"}]
        fake.jobs[900] = [{"name": "Android", "conclusion": "failure"}]
        lines = self.step(fake).run()
        self.assertEqual(fake.posted("ejected"), [])
        self.assertTrue(any("red on main too" in line for line in lines), lines)

    def test_dry_run_reports_green_even_when_main_moved(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        self.write("z.txt", "a hand merge\n")
        sh(self.work, "checkout", "-q", "main")
        self.commit("hand merge")
        sh(self.work, "push", "-q", "origin", "main")
        self.step(fake, mode="dry-run").run()
        self.assertEqual(fake.posted("green-dry-run"), [1])
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))

    def test_a_run_only_finishing_its_bookkeeping_is_waited_for(self):
        # ci.yml's verdict job wakes the train as its last step, so the step can start
        # before GitHub marks the run completed.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        run = self.run_on(fake, name, tip, status="in_progress", conclusion=None)
        fake.jobs[77] = [{"name": "Select lanes", "status": "completed"}, {"name": "CI verdict", "status": "in_progress"}]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            run.update(status="completed", conclusion="success")
            fake.jobs[77] = [dict(job, status="completed") for job in fake.jobs[77]]

        self.step(fake, sleep=sleep).run()
        self.assertEqual(slept, [train_step.SETTLE_POLL_SECONDS])
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes)

    def test_a_run_with_real_work_left_is_not_waited_for(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, status="in_progress", conclusion=None)
        fake.jobs[77] = [{"name": "CoreKit release gate", "status": "in_progress"},
                         {"name": "CI verdict", "status": "queued"}]
        slept = []
        self.step(fake, sleep=slept.append).run()
        self.assertEqual(slept, [], "a gate still running is an ordinary wait, not a poll")
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))

    def settle_calls(self, jobs, finishes_after_sleep=True):
        """How many times settle sleeps for a batch run with these jobs listed."""
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        run = self.run_on(fake, name, tip, status="in_progress", conclusion=None)
        fake.jobs[77] = jobs
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if finishes_after_sleep:
                run.update(status="completed", conclusion="success")

        self.step(fake, sleep=sleep).run()
        return len(slept)

    def test_settle_waits_when_every_job_has_finished(self):
        done = {"name": "CoreKit release gate", "status": "completed"}
        self.assertEqual(self.settle_calls([done, {"name": "CI verdict", "status": "completed"},
                                            {"name": "Wake the merge train", "status": "completed"}]), 1)

    def test_settle_waits_while_only_the_wake_job_runs(self):
        done = {"name": "CoreKit release gate", "status": "completed"}
        self.assertEqual(self.settle_calls([done, {"name": "CI verdict", "status": "completed"},
                                            {"name": "Wake the merge train", "status": "in_progress"}]), 1)

    def test_settle_never_waits_for_a_final_job_that_has_not_started(self):
        # A verdict queued behind THIS step on the one light runner can never start
        # while the step waits.
        self.assertEqual(self.settle_calls([{"name": "CI verdict", "status": "queued"}]), 0)

    def test_settle_never_waits_before_any_job_is_listed(self):
        self.assertEqual(self.settle_calls([]), 0)

    def test_the_aggregate_verdict_is_never_a_shared_failure(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        fake.jobs[77] = [{"name": "CoreKit release gate", "conclusion": "failure"},
                         {"name": "CI verdict", "conclusion": "failure"}]
        fake.main_runs = [{"id": 900, "status": "completed", "conclusion": "failure", "html_url": "main-run"}]
        fake.jobs[900] = [{"name": "Android", "conclusion": "failure"}, {"name": "CI verdict", "conclusion": "failure"}]
        self.step(fake).run()
        self.assertEqual(fake.posted("ejected"), [1], "main red in Android does not excuse a red release gate")

    def test_a_closed_batch_starts_the_next_step_and_a_build_does_not(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])  # build_batch clears `dispatched`
        self.assertNotIn("merge-train.yml", [d[0] for d in fake.dispatched], "building never self-wakes")
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        fake.jobs[77] = [{"name": "App-owned SwiftPM packages", "conclusion": "failure"},
                         {"name": "CI verdict", "conclusion": "failure"}]
        self.step(fake).run()
        self.assertIn(("merge-train.yml", "main", None), fake.dispatched)
        ejected = [body for method, path, body in fake.writes if method == "POST" and path.endswith("/comments")
                   and "Removed from the merge train" in (body or {}).get("body", "")]
        self.assertTrue(ejected and "App-owned SwiftPM packages" in ejected[0]["body"])
        self.assertNotIn("CI verdict", ejected[0]["body"].split("<!--")[0], "the aggregate verdict is not a failed lane")

    def test_the_train_creates_the_carve_out_labels(self):
        self.assertIn(train.NEEDS_ALEX_LABEL, train.LABELS)
        self.assertIn(train.ALEX_APPROVED_LABEL, train.LABELS)

    def test_branch_names_are_encoded_in_run_queries(self):
        fake = FakeGitHub([], self.main)
        seen = []
        original = fake.rest_paginated
        fake.rest_paginated = lambda path: (seen.append(path), original(path))[1]
        self.step(fake).cancel_branch_runs("feature#1&x")
        self.assertIn("branch=feature%231%26x&", seen[0])

    def test_a_missing_run_waits_briefly_then_dispatches_again(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.step(fake, now=snapshot.now_utc()).run()
        self.assertEqual(fake.dispatched, [], "just built: the dispatched run has not registered yet")
        self.step(fake, now=snapshot.now_utc() + dt.timedelta(minutes=30)).run()
        self.assertEqual(fake.dispatched, [("ci.yml", name, {"lanes": "changed"})])


if __name__ == "__main__":
    unittest.main()

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
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coordlib import marker, snapshot, train, train_step  # noqa: E402
from test_coord import BOT, NOW, check, pr_node, reaction  # noqa: E402


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
        self.assertEqual(train.eligibility(dict(pr, labels=["train", "needs-alex", "alex-approved"]))[0], train.READY)
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": True}))[1],
                         "Codex is re-reviewing it")
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": False}))[0], train.HOLD)
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": False},
                                                labels=["train", "codex-unavailable"]))[0], train.READY)
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

    def test_run_action(self):
        self.assertEqual(train.run_action(None, 2), "dispatch")
        self.assertEqual(train.run_action({"status": "in_progress"}, 2), "wait")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "success"}, 2), "merge")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 1}, 2), "rerun")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 2}, 2), "split")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "failure", "run_attempt": 2}, 1), "eject")
        self.assertEqual(train.run_action({"status": "completed", "conclusion": "cancelled"}, 1), "abandon")

    def test_parse_members(self):
        log = ("c3\tc2 p3\tMerge pull request #30 from o/c\n"
               "c2\tc1 p2\tMerge pull request #20 from o/b\n"
               "c1\tm0 x\tSome other merge\n")
        self.assertEqual(train.parse_members(log), [{"pr": 20, "sha": "p2"}, {"pr": 30, "sha": "p3"}])


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
        self.calls = 0

    def graphql(self, query, variables=None):
        return {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": self.main}},
                               "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                "nodes": self.nodes}}}

    def rest(self, path, method="GET", body=None):
        self.writes.append((method, path, body))
        return {"html_url": "h"} if method == "POST" else None

    def rest_paginated(self, path):
        match = re.search(r"actions/workflows/ci\.yml/runs\?branch=([^&]+)", path)
        if match:
            return [{"workflow_runs": self.runs.get(match.group(1), [])}]
        match = re.search(r"actions/runs/(\d+)/jobs", path)
        if match:
            return [{"jobs": self.jobs.get(int(match.group(1)), [])}]
        if "actions/runs?branch=" in path:
            return [{"workflow_runs": []}]
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

    def step(self, fake, mode="live", now=None):
        return train_step.Step(fake, train_step.Git(self.clone), mode, now=now or snapshot.now_utc(),
                               log=lambda line: None)

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
        log = sh(self.origin, "log", "--first-parent", "--merges", "--format=%H%x09%P%x09%s", f"{self.main}..{tip}")
        self.assertEqual(train.parse_members(log), [{"pr": 2, "sha": h2}, {"pr": 1, "sha": h1}], "queue order")
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
        members = train.parse_members(sh(self.origin, "log", "--first-parent", "--merges",
                                         "--format=%H%x09%P%x09%s", f"{self.main}..{tip}"))
        self.assertEqual([m["pr"] for m in members], [1])
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
        run = {"id": 77, "head_sha": tip, "status": "completed", "run_attempt": 1, "html_url": "run-url"}
        run.update(fields)
        fake.runs[name] = [run]
        return run

    def test_green_batch_fast_forwards_main_to_the_tested_commit(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        h2 = self.pr_branch(2, "d.txt", "d\n")
        fake, name, tip = self.build_batch([(1, h1), (2, h2)])
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake).run()
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes)
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

    def test_a_missing_run_waits_briefly_then_dispatches_again(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.step(fake, now=snapshot.now_utc()).run()
        self.assertEqual(fake.dispatched, [], "just built: the dispatched run has not registered yet")
        self.step(fake, now=snapshot.now_utc() + dt.timedelta(minutes=30)).run()
        self.assertEqual(fake.dispatched, [("ci.yml", name, {"lanes": "changed"})])


if __name__ == "__main__":
    unittest.main()

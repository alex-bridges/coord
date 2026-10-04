"""Tests for the merge train. Run: python3 -m unittest discover -s Tools/coord/tests

The pure rules are tested directly. The step is tested against REAL git — a bare
"origin" with main and refs/pull/N/head, and a clone as the step's repository —
and an in-memory GitHub that records every write, so each branch of the state
machine is exercised end to end without the network.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coordlib import marker, snapshot, train, train_step  # noqa: E402
from coordlib.gh import GitHubError  # noqa: E402
from test_coord import (ALEX, BOT, NOW, REAL_FAILURE, REPO, check, codex_summary, coord_comment, pr_node,  # noqa: E402
                        isolate_from_rest_env, reaction, refused_check)


def setUpModule():
    isolate_from_rest_env()  # the fakes answer GraphQL; see test_coord.REST_ENV (#1085)

# The repository checkout these tests run in: the light/heavy tests use its REAL lane
# selector and rules, so a change to either is exercised here too.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
HEAVY_PATH = "Packages/CoreKit/Sources/A.swift"
LIGHT_PATH = ".claude/skills/x.md"  # the docs lane only, and not one of train.ALEX_ONLY_PATHS
# Who posted an attestation record, and whether it counts (snapshot.trusted_attestation):
# only the repository's owner, never a bot or another user (Codex P1 on #1054).
ATTESTERS = ((ALEX, True),
             ({"login": "chatgpt-codex-connector[bot]", "type": "Bot"}, False),
             ({"login": "inisa-sidechain[bot]", "type": "Bot"}, False),
             ({"login": "mallory", "type": "User"}, False))
FAKE_OWNER = {"login": "o", "type": "User"}  # the owner of FakeGitHub's repository, o/r


class CodexRequestTests(unittest.TestCase):
    def test_request_starts_on_its_own_line_without_crossing_blank_lines(self):
        pattern = train_step._CODEX_REQUEST
        self.assertIsNone(pattern.search("\n" * 65536))
        self.assertIsNone(pattern.search("\n  \n@codex\nreview"))
        self.assertIsNotNone(pattern.search("a comment\n  @codex review please"))


def compose_record(text, payload):
    return marker.compose(text, dict({"v": 1, "agent": "codex@test:train"}, **payload))


def checkout_file(path: str) -> str:
    with open(os.path.join(REPO_ROOT, path)) as handle:
        return handle.read()


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
        self.assertEqual(train.eligibility(dict(pr, unansweredFindings=2))[1], "2 unanswered Codex findings on head " + str(pr["head"])[:9])
        self.assertEqual(train.eligibility(dict(pr, ci={"failing": ["Android"], "pending": [], "verdict": "failure"}))[0],
                         train.HOLD)
        self.assertEqual(train.eligibility(dict(pr, ci={"failing": [], "pending": ["x"], "verdict": "pending"}))[0],
                         train.READY)

    def unreviewed(self, **fields):
        """A summary with no Codex 👍, no labels but `train`, and `fields` on top."""
        return dict(dict(self.summary(), codex={"approved": False, "reviewing": False}, labels=["train"]), **fields)

    def test_a_clean_review_by_any_agent_at_the_head_counts_without_a_label(self):
        pr = self.unreviewed()
        self.assertEqual(train.eligibility(dict(pr, reviewedHead=pr["head"])), (train.READY, "ready"))

    def test_a_clean_review_of_an_older_head_does_not_count(self):
        pr = self.unreviewed(reviewedHead="0" * 40)
        verdict, hold, reason = train.assess(pr)
        self.assertEqual((verdict, hold), (train.HOLD, train.HOLD_NO_REVIEW), "a new push needs a new review")
        self.assertIn("covers 000000000, an older head", reason)

    def test_no_codex_thumbs_up_and_no_recorded_review_holds(self):
        verdict, hold, reason = train.assess(self.unreviewed())
        self.assertEqual((verdict, hold), (train.HOLD, train.HOLD_NO_REVIEW))
        self.assertIn("no clean review of it recorded", reason)
        self.assertIn("coord train review 1 --head <full-reviewed-sha> --by <agent>", reason)
        self.assertIn("current head aaaaaaaaa", reason, "the held head is named, outside the command")
        self.assertNotIn("--head aaaaaaa", reason, "never pre-filled with the current head")
        self.assertEqual(train.assess(self.unreviewed(head=None))[0], train.HOLD, "no head and no record is not a match")

    # A recorded clean review of the head while Codex re-reviews it: test_train_clean_review (#1181).

    def test_a_record_of_either_kind_sets_the_reviewed_head(self):
        head = "a" * 40
        for kind, labels in (("review-clean", ["train"]),
                             # Recorded by `substitute-review` before 2026-09-26, with the old label.
                             ("review-substitute", ["train", "codex-unavailable"])):
            with self.subTest(kind=kind):
                pr = self.unreviewed(labels=labels)
                snapshot.attach_head_records([pr], [coord_comment(1, 1, 5, {"kind": kind, "pr": 1, "head": head})], REPO)
                self.assertEqual(pr["reviewedHead"], head)
                self.assertEqual(train.eligibility(pr), (train.READY, "ready"))

    def test_the_newest_record_of_either_kind_wins(self):
        pr = self.unreviewed()
        snapshot.attach_head_records([pr], [
            coord_comment(1, 1, 30, {"kind": "review-clean", "pr": 1, "head": pr["head"]}),
            coord_comment(2, 1, 10, {"kind": "review-substitute", "pr": 1, "head": "0" * 40})], REPO)
        self.assertEqual(train.eligibility(pr)[0], train.HOLD, "the newer record names an older head")

    def test_only_the_owner_s_records_attest_in_the_snapshot(self):
        head = "a" * 40
        for author, counts in ATTESTERS:
            with self.subTest(author=author["login"]):
                review = self.unreviewed()
                snapshot.attach_head_records([review], [coord_comment(
                    1, 1, 5, {"kind": "review-clean", "pr": 1, "head": head}, author)], REPO)
                self.assertEqual(train.eligibility(review)[0], train.READY if counts else train.HOLD)
                # Alex's approval: the same code path, and the same rule.
                approval = dict(self.summary(), labels=["train", "needs-alex", "alex-approved"])
                snapshot.attach_head_records([approval], [coord_comment(
                    1, 1, 5, {"kind": "alex-approve", "pr": 1, "head": head}, author)], REPO)
                self.assertEqual(train.assess(approval)[:2], (train.READY, "") if counts else
                                 (train.HOLD, train.HOLD_NEEDS_ALEX))

    def test_a_record_counts_only_on_the_pr_it_names(self):
        # A trusted record posted on #5 that names #7 is not a record on #7 (the step
        # reads only #7's own comments, so the snapshot must agree).
        for kind, labels in (("review-clean", ["train"]), ("alex-approve", ["train", "needs-alex", "alex-approved"])):
            with self.subTest(kind=kind):
                pr = self.unreviewed(number=7, labels=labels)
                if kind == "alex-approve":
                    pr["codex"] = {"approved": True}
                snapshot.attach_head_records([pr], [coord_comment(
                    1, 5, 5, {"kind": kind, "pr": 7, "head": pr["head"]})], REPO)
                self.assertEqual((pr["reviewedHead"], pr["alexApprovedHead"]), (None, None))
                self.assertEqual(train.eligibility(pr)[0], train.HOLD)

    def test_an_untrusted_record_never_displaces_a_trusted_one(self):
        pr = self.unreviewed()
        snapshot.attach_head_records([pr], [
            coord_comment(1, 1, 30, {"kind": "review-clean", "pr": 1, "head": pr["head"]}),
            coord_comment(2, 1, 10, {"kind": "review-clean", "pr": 1, "head": "0" * 40}, ATTESTERS[2][0])], REPO)
        self.assertEqual(pr["reviewedHead"], pr["head"])

    def summary_with_checks(self, *checks):
        node = ready_node(1, "a" * 40, "b")
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [check("CI verdict")] + list(checks)
        return snapshot.summarize_pr(node)

    def test_checks_github_never_started_neither_hold_nor_count(self):
        # #1026: both plugin probes refused by the Actions budget before any step ran.
        refused = [refused_check("Linux plugin probe (x86_64)"),
                   refused_check("Windows plugin probe (dynamic runtime)")]
        pr = self.summary_with_checks(*refused)
        self.assertEqual(pr["ci"]["not_run"], ["Linux plugin probe (x86_64)", "Windows plugin probe (dynamic runtime)"])
        self.assertEqual(train.eligibility(pr), (train.READY, "ready"))
        # Not run is neutral, not green: the other conditions still decide.
        self.assertEqual(train.eligibility(dict(pr, isDraft=True))[0], train.HOLD)
        self.assertEqual(train.eligibility(dict(pr, codex={"approved": False, "reviewing": False}))[0], train.HOLD)

    def test_a_real_failure_beside_a_never_started_check_still_holds(self):
        pr = self.summary_with_checks(
            refused_check("Linux plugin probe (x86_64)"),
            check("Android", conclusion="FAILURE", annotations=[REAL_FAILURE]))
        self.assertEqual(train.eligibility(pr), (train.HOLD, "CI is red at its head: Android"))
        unannotated = self.summary_with_checks(
            refused_check("Linux plugin probe (x86_64)"),
            check("Android", conclusion="FAILURE"))
        self.assertEqual(train.eligibility(unannotated), (train.HOLD, "CI is red at its head: Android"))


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

    def test_plan_names_no_overtake_without_classes(self):
        chosen = train.plan([{"pr": 1}], {1: (train.READY, "")}, {1: []})
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([1], None))

    def test_parse_batch_reads_only_this_batchs_merges(self):
        log = ("c3\tc2 p3\ttrain/B\tMerge pull request #30 from o/c\n"
               "c2\tc1 p2\ttrain/B\tMerge pull request #20 from o/b\n"
               "c1\tm0 p1\ttrain/A\tMerge pull request #10 from o/a\n")  # an EARLIER batch, already on main
        self.assertEqual(train.parse_batch(log, "train/B"), ("c1", [{"pr": 20, "sha": "p2"}, {"pr": 30, "sha": "p3"}]))
        self.assertEqual(train.parse_batch(log, "train/C"), (None, []))


L, H = train.LIGHT, train.HEAVY


class LightPriorityPlanTests(unittest.TestCase):
    """Bounded light priority (Alex, 2026-09-25): at most one light batch ahead of ready
    heavy work, and a batch never mixes the two."""

    def plan(self, order, classes, overtaken=(), solo=(), verdicts=None, cap=3):
        queue = [{"pr": n} for n in order]
        verdicts = verdicts or {n: (train.READY, "") for n in order}
        labels = {n: (["train:solo"] if n in solo else []) for n in order}
        return train.plan(queue, verdicts, labels, cap=cap, classes=classes, overtaken=overtaken)

    def test_one_class_plans_exactly_as_before(self):
        order = [1, 2, 3, 4, 5, 6]
        verdicts = {1: (train.READY, ""), 2: (train.HOLD, "h"), 3: (train.READY, ""), 4: (train.DROP, "d"),
                    5: (train.READY, ""), 6: (train.READY, "")}
        for solo in ((), (1,), (3,), (1, 3)):
            before = self.plan(order, None, solo=solo, verdicts=verdicts)
            for kind in (L, H):
                classes = {n: kind for n in order}
                for overtaken in ((), (1, 3, 5)):
                    now = self.plan(order, classes, overtaken=overtaken, solo=solo, verdicts=verdicts)
                    self.assertEqual(now, before, (kind, solo, overtaken))
                    self.assertIsNone(now["overtakes"])

    def test_one_light_batch_overtakes_then_heavy_goes_while_light_waits(self):
        classes = {1: H, 2: L, 3: H, 4: L, 5: L, 6: L, 7: L, 8: H}
        first = self.plan([1, 2, 3, 4, 5, 6, 7], classes)
        self.assertEqual((first["batch"], first["overtakes"]), ([2, 4, 5], 1),
                         "one light batch goes ahead of the oldest ready heavy PR")
        # That batch left the queue; the step recorded the overtake on #1.
        second = self.plan([1, 3, 6, 7], classes, overtaken=[1])
        self.assertEqual((second["batch"], second["overtakes"]), ([1, 3], None),
                         "heavy goes next, although light PRs are still waiting")
        # Heavy work had its turn; the next oldest heavy PR allows one light batch again.
        third = self.plan([6, 7, 8], classes)
        self.assertEqual((third["batch"], third["overtakes"]), ([6, 7], 8))

    def test_any_overtaken_ready_heavy_pr_gives_heavy_the_turn(self):
        # #3 was overtaken; #1, older and held back then, is ready again. Heavy goes
        # (both of them), not a second light batch.
        chosen = self.plan([1, 2, 3], {1: H, 2: L, 3: H}, overtaken=[3])
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([1, 3], None))

    def test_a_batch_never_mixes_classes(self):
        chosen = self.plan([1, 2, 3], {1: L, 2: H, 3: L})
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([1, 3], 2))
        chosen = self.plan([1, 2, 3], {1: L, 2: H, 3: L}, overtaken=[2])
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([2], None),
                         "a light PR queued before the overtaken one still waits: heavy has the turn")

    def test_solo_applies_within_the_chosen_class(self):
        chosen = self.plan([1, 2, 3], {1: H, 2: L, 3: L}, solo=(2,))
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([2], 1))
        chosen = self.plan([1, 2, 3, 4], {1: H, 2: L, 3: H, 4: L}, solo=(3,), overtaken=[1])
        self.assertEqual(chosen["batch"], [1], "a solo PR behind the front is skipped, as before")

    def test_held_prs_neither_overtake_nor_are_overtaken(self):
        verdicts = {1: (train.HOLD, "h"), 2: (train.READY, "")}
        chosen = self.plan([1, 2], {2: L}, verdicts=verdicts)
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([2], None), "no ready heavy work to overtake")

    def test_an_unclassified_ready_pr_is_heavy(self):
        chosen = self.plan([1, 2], {2: L})
        self.assertEqual((chosen["batch"], chosen["overtakes"]), ([2], 1))
        chosen = self.plan([1, 2], {2: L}, overtaken=[1])
        self.assertEqual(chosen["batch"], [1])


class LaneClassifierTests(unittest.TestCase):
    """The classifier runs the REAL selector and rules from this checkout."""

    def classify(self, *paths):
        return train.LaneClassifier(checkout_file).classify(list(paths))

    def test_ci_infrastructure_is_heavy(self):
        for path in (".github/scripts/select-ci-lanes.py", ".github/ci-lanes.json",
                     ".github/actions/attach/action.yml", ".github/workflows/ci.yml"):
            kind, reason = self.classify(path)
            self.assertEqual(kind, H, path)
            self.assertIn("CI graph itself changed", reason, path)

    def test_engine_package_and_app_changes_are_heavy(self):
        for path in (HEAVY_PATH, "Packages/CoreKit/Package.swift", "Packages/DesignKit/Sources/A.swift",
                     "Apps/AppBeta/AppBeta/App.swift"):
            self.assertEqual(self.classify(path)[0], H, path)

    def test_docs_tools_and_prose_are_light(self):
        for path in ("Docs/agent-coordination.md", LIGHT_PATH, "Packages/CoreKit/Docs/Plan.md",
                     "Packages/CoreKit/Sources/CoreKitCore/CoreKitCore.docc/A.md", "Apps/AppBeta/README.md",
                     "Apps/AppAlpha/Marketing/Screenshots/shot.png"):
            self.assertEqual(self.classify(path)[0], L, path)

    def test_one_heavy_path_makes_the_pr_heavy(self):
        kind, reason = self.classify("Docs/agent-coordination.md", LIGHT_PATH, HEAVY_PATH)
        self.assertEqual(kind, H)
        self.assertIn("mk4-debug", reason)

    def test_an_unknown_or_empty_diff_is_heavy(self):
        classifier = train.LaneClassifier(checkout_file)
        self.assertIsNone(classifier.error)
        self.assertEqual(classifier.classify(None)[0], H)
        self.assertEqual(classifier.classify([])[0], H)

    def test_a_selector_that_does_not_load_makes_everything_heavy(self):
        def missing(path):
            raise FileNotFoundError(path)

        def broken_rules(path):
            return "{not json" if path == train.LANE_RULES else checkout_file(path)

        def rules_missing_a_lane(path):
            if path != train.LANE_RULES:
                return checkout_file(path)
            rules = json.loads(checkout_file(path))
            del rules["lanes"]["mk4"]
            return json.dumps(rules)

        for show in (missing, lambda path: "def (", broken_rules, rules_missing_a_lane):
            classifier = train.LaneClassifier(show)
            self.assertIsNotNone(classifier.error)
            self.assertEqual(classifier.classify([LIGHT_PATH])[0], H)

    def test_a_failure_while_selecting_is_heavy(self):
        classifier = train.LaneClassifier(checkout_file)

        def explode(*args):
            raise RuntimeError("new selector code")

        classifier.selector = type("Selector", (), {"Selection": classifier.selector.Selection,
                                                    "select_for_paths": staticmethod(explode)})
        kind, reason = classifier.classify([LIGHT_PATH])
        self.assertEqual(kind, H)
        self.assertIn("lane selection failed", reason)


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
        self.fail_held = ""       # every POST of a `held` notice raises GitHubError(this)
        self.fail_comment_reads = ""  # every read of a PR's comments raises GitHubError(this)
        self.login = train_step.BOT_NAME  # who GitHub says posted what the step posts
        self.issue_states: Dict[int, str] = {}  # GET issues/N answers {"state": this}
        self.pull_bodies: Dict[int, str] = {}   # GET pulls/N answers {"body": this}

    def graphql(self, query, variables=None):
        return {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": self.main}},
                               "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                "nodes": self.nodes}}}

    def rest(self, path, method="GET", body=None):
        from coordlib.gh import GitHubError
        if method == "PATCH" and self.patch_error:
            self.writes.append((method, path, body))
            raise GitHubError(self.patch_error)
        issue_read = re.search(r"issues/(\d+)$", path)
        if method == "GET" and issue_read:
            state = self.issue_states.get(int(issue_read.group(1)))
            if state == "missing":
                from coordlib.gh import NotFound
                raise NotFound("HTTP 404")
            if state == "open-pr":
                return {"state": "open", "pull_request": {"url": "u"}}
            return {"state": state}
        pull_read = re.search(r"pulls/(\d+)$", path)
        if method == "GET" and pull_read and int(pull_read.group(1)) in self.pull_bodies:
            return {"body": self.pull_bodies[int(pull_read.group(1))], "head": {"ref": "x"}}
        if path.endswith("git/ref/heads/main"):
            if self.reread_error:
                raise GitHubError(self.reread_error)
            return {"object": {"sha": self.reread_main}}
        if self.fail_writes and method in ("POST", "DELETE"):
            self.fail_writes -= 1
            from coordlib.gh import GitHubError
            raise GitHubError("HTTP 502 (injected)")
        match = re.search(r"issues/(\d+)/comments$", path)
        if self.fail_held and method == "POST" and match \
                and (marker.decode(body["body"]) or {}).get("event") == "held":
            raise GitHubError(self.fail_held)
        self.writes.append((method, path, body))
        if method == "POST" and match:
            # GitHub names the token's user as the author: the bot for the Actions train,
            # alex-bridges for a step run by hand (set `login`).
            self.issue_comments.setdefault(int(match.group(1)), []).append(
                {"created_at": (marker.decode(body["body"]) or {}).get("at", snapshot.iso(NOW)),
                 "body": body["body"], "user": {"login": self.login}})
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
        if "issues/comments?since=" in path:
            return [dict(comment, issue_url=f"https://api.github.com/repos/o/r/issues/{number}")
                    for number, comments in self.issue_comments.items() for comment in comments]
        match = re.search(r"issues/(\d+)/comments", path)
        if match:
            if self.fail_comment_reads:
                from coordlib.gh import GitHubError
                raise GitHubError(self.fail_comment_reads)
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
        full = os.path.join(self.work, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
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

    def step(self, fake, mode="live", now=None, sleep=None, agent=train_step.TRAIN_AGENT):
        return train_step.Step(fake, train_step.Git(self.clone), mode, now=now or snapshot.now_utc(), agent=agent,
                               log=lambda line: None, sleep=sleep or (lambda seconds: None))

    def queue(self, fake, number, head, minutes_ago):
        fake.issue_comments[number] = [{"created_at": snapshot.iso(NOW - dt.timedelta(minutes=minutes_ago)),
                                        "body": compose_record("added", {"kind": "train-add", "pr": number, "head": head})}]

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

    def test_forged_batch_tree_cannot_fast_forward_main(self):
        h1 = self.pr_branch(1, "c.txt", "reviewed\n")
        fake, name, tip = self.build_batch([(1, h1)])
        message = sh(self.origin, "log", "-1", "--format=%B", tip)
        # Keep the train's subject, trailer, base, and reviewed PR parent, but
        # replace the generated tree with one containing unreviewed content.
        self.write("unreviewed.txt", "injected\n")
        sh(self.work, "add", "unreviewed.txt")
        tree = sh(self.work, "write-tree")
        forged = sh(self.work, "commit-tree", tree, "-p", self.main, "-p", h1, "-m", message)
        sh(self.work, "push", "-q", "--force", "origin", f"{forged}:refs/heads/{name}")
        sh(self.clone, "fetch", "-q", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}")
        self.run_on(fake, name, forged, conclusion="success")

        lines = self.step(fake).run()
        self.assertFalse(any(method == "PATCH" and path.endswith("heads/main") for method, path, _ in fake.writes), lines)
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        self.assertTrue(any("commit chain or merge trees" in line for line in lines), lines)

    def test_batch_with_extra_first_parent_commit_cannot_fast_forward_main(self):
        h1 = self.pr_branch(1, "c.txt", "reviewed\n")
        fake, name, tip = self.build_batch([(1, h1)])
        message = sh(self.origin, "log", "-1", "--format=%B", tip)
        sh(self.work, "fetch", "-q", "origin", f"refs/heads/{name}")
        self.write("unreviewed.txt", "injected\n")
        sh(self.work, "add", "unreviewed.txt")
        tree = sh(self.work, "write-tree")
        forged = sh(self.work, "commit-tree", tree, "-p", tip, "-m", message)
        sh(self.work, "push", "-q", "--force", "origin", f"{forged}:refs/heads/{name}")
        sh(self.clone, "fetch", "-q", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}")
        self.run_on(fake, name, forged, conclusion="success")

        lines = self.step(fake).run()
        self.assertFalse(any(method == "PATCH" and path.endswith("heads/main") for method, path, _ in fake.writes), lines)
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        self.assertTrue(any("commit chain or merge trees" in line for line in lines), lines)

    def closed_issues(self, fake):
        return [path.rsplit("/", 1)[1] for method, path, body in fake.writes
                if method == "PATCH" and re.search(r"issues/\d+$", path) and body == {"state": "closed",
                                                                                     "state_reason": "completed"}]

    @staticmethod
    def review_record(head, author=FAKE_OWNER):
        """A clean review of `head` as `coord train review` posts it, authored by `author`."""
        return {"created_at": snapshot.iso(NOW), "user": dict(author), "body": compose_record("reviewed", {
            "kind": "review-clean", "v": 1, "agent": "claude@h:w", "by": "claude@h:reviewer", "pr": 1, "head": head})}

    def queue_reviewed(self, head, reviewed_head, author=FAKE_OWNER):
        """PR 1 queued at `head` with no Codex 👍 and no label but `train`, and a clean
        review recorded by `coord train review` naming `reviewed_head`."""
        node = ready_node(1, head, "pr1")
        node["reactions"] = {"nodes": []}
        fake = FakeGitHub([node], self.main)
        self.queue(fake, 1, head, 20)
        fake.issue_comments[1].append(self.review_record(reviewed_head, author))
        return fake

    def test_a_bot_s_record_at_the_head_is_held(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        lines = self.step(self.queue_reviewed(h1, h1, {"login": "inisa-sidechain[bot]", "type": "Bot"})).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), lines)
        self.assertTrue(any("#1 held: no Codex" in line and "no clean review" in line for line in lines), lines)

    def test_the_final_check_ignores_a_bot_s_record(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])  # built on Codex's 👍
        self.run_on(fake, name, tip, conclusion="success")
        node = ready_node(1, h1, "pr1")
        node["reactions"] = {"nodes": []}  # the 👍 is gone; only a bot's record remains
        fake.nodes = [node]
        fake.issue_comments[1].append(self.review_record(h1, {"login": "chatgpt-codex-connector[bot]", "type": "Bot"}))
        lines = self.step(fake).run()
        self.assertFalse(any(method == "PATCH" and path.endswith("heads/main") for method, path, _ in fake.writes), lines)
        self.assertTrue(any("#1 is no longer ready" in line for line in lines), lines)

    def test_the_final_check_trusts_the_same_authors_as_the_snapshot(self):
        head = "a" * 40
        for kind, labels in (("review-clean", ("train",)), ("review-substitute", ("train",)),
                             ("alex-approve", ("train", "needs-alex", "alex-approved"))):
            for author, counts in ATTESTERS:
                with self.subTest(kind=kind, author=author["login"]):
                    node = ready_node(1, head, "pr1", labels=labels)
                    if kind != "alex-approve":
                        node["reactions"] = {"nodes": []}  # the record is its only review
                    payload = {"kind": kind, "pr": 1, "head": head}
                    fake = FakeGitHub([node], self.main)
                    fake.repo = REPO  # trusted_attesters(REPO) is {alex-bridges}
                    fake.issue_comments[1] = [{"created_at": snapshot.iso(NOW), "user": dict(author),
                                               "body": compose_record("record", payload)}]
                    fresh = snapshot.summarize_pr(node)
                    self.step(fake).train_added(fresh)
                    cached = snapshot.summarize_pr(node)
                    snapshot.attach_head_records([cached], [coord_comment(1, 1, 5, payload, author)], REPO)
                    self.assertEqual(train.assess(fresh)[0], train.READY if counts else train.HOLD)
                    self.assertEqual(train.assess(cached), train.assess(fresh))

    def test_a_recorded_clean_review_lands_without_codex_or_a_label(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake = self.queue_reviewed(h1, h1)
        lines = self.step(fake).run()
        name, tip = self.batch_on_origin()
        self.assertTrue(name, lines)
        self.assertEqual([m["pr"] for m in self.members(name)], [1])
        sh(self.clone, "fetch", "-q", "origin", "+refs/heads/train/*:refs/remotes/origin/train/*")
        self.run_on(fake, name, tip, conclusion="success")
        lines = self.step(fake).run()  # the final check reads the record again
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes, lines)

    def test_a_recorded_clean_review_of_an_older_head_is_held(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        lines = self.step(self.queue_reviewed(h1, "0" * 40)).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), lines)
        self.assertTrue(any("#1 held: no Codex" in line and "an older head" in line for line in lines), lines)

    def test_a_merged_batch_closes_the_issues_its_prs_close(self):
        # pr_node names #55 in closingIssuesReferences; GitHub does not close it for a ref update.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        fake.issue_states = {55: "open"}
        self.run_on(fake, name, tip, conclusion="success")
        lines = self.step(fake).run()
        self.assertEqual(self.closed_issues(fake), ["55"])
        receipt = [marker.decode(body["body"]) for method, path, body in fake.writes
                   if method == "POST" and path.endswith("issues/55/comments")]
        self.assertEqual([(r["event"], r["pr"], r["issue"]) for r in receipt], [("closed-issue", 1, 55)])
        self.assertTrue(any("closed #55" in line for line in lines), lines)

    def test_an_issue_already_closed_is_left_alone_and_dry_run_closes_nothing(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        fake.issue_states = {55: "closed"}
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake).run()
        self.assertEqual(self.closed_issues(fake), [])
        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")
        sh(self.origin, "update-ref", "refs/heads/main", sh(self.origin, "rev-parse", f"{tip}^1"))
        fake, name, tip = self.build_batch([(1, h1)])
        fake.issue_states = {55: "open"}
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake, mode="dry-run").run()
        self.assertEqual(self.closed_issues(fake), [])

    def test_a_later_step_finishing_the_batch_reads_the_merged_prs_body(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.fail_writes = 1  # the first write after the fast-forward fails: clean-up resumes next step
        self.step(fake).run()
        sh(self.origin, "update-ref", "refs/heads/main", tip)
        fake.nodes = []  # merged: no longer an open PR
        fake.pull_bodies = {1: "Fixes the thing.\n\nCloses #56"}
        fake.issue_states = {56: "open"}
        fake.writes.clear()
        self.step(fake).run()
        self.assertEqual(self.closed_issues(fake), ["56"])

    def test_closing_is_best_effort_and_never_touches_prs_or_other_repositories(self):
        # Substitute review of #1050: a permanent error must not stall the train; a PR number
        # or another repository's issue must never be closed.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        fake.nodes[0]["closingIssuesReferences"] = {"nodes": [
            {"number": 55}, {"number": 56}, {"number": 57},
            {"number": 58, "repository": {"nameWithOwner": "someone/else"}},
            {"number": 59, "repository": {"nameWithOwner": "O/R"}}]}
        fake.issue_states = {55: "missing", 56: "open-pr", 57: "open", 58: "open", 59: "open"}
        self.run_on(fake, name, tip, conclusion="success")
        lines = self.step(fake).run()
        self.assertEqual(self.closed_issues(fake), ["57", "59"])
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes,
                      "a failed close does not keep the batch branch")
        self.assertTrue(any("could not close #55" in line for line in lines), lines)
        order = [(method, path) for method, path, _ in fake.writes if path.startswith("repos/{repo}/issues/57")]
        self.assertEqual(order, [("PATCH", "repos/{repo}/issues/57"), ("POST", "repos/{repo}/issues/57/comments")],
                         "closed first, then the receipt")

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

    def test_an_ejection_reaches_the_adder_and_its_fallback(self):
        # Codex on #1157: advance() hands the PR dicts open_prs() returned straight to
        # recipients(), which never read the train-add marker, so the notice went to the
        # conductor alone. The ejected record must name the adder and its fallback.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        fake.issue_comments[1] = [{"created_at": snapshot.iso(NOW - dt.timedelta(minutes=50)), "body": compose_record(
            "added", {"kind": "train-add", "pr": 1, "head": h1, "agent": "worker@x", "fallback": "lead@y"})}]
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=2)
        fake.jobs[77] = [{"name": "CoreKit release gate", "conclusion": "failure"}]
        self.step(fake).run()
        ejected = [marker.decode(body["body"]) for method, path, body in fake.writes
                   if method == "POST" and path.endswith("/comments")
                   and (marker.decode(body["body"]) or {}).get("event") == "ejected"]
        self.assertEqual([payload["to"] for payload in ejected], [["worker@x", "lead@y", train.CONDUCTOR]])

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
        # Match the message, never a bare run id: the output also carries the batch
        # name (train/YYYYMMDD-HHMMSS) and a short SHA, either of which can contain
        # "602" (a batch built at 19:36:02 is train/…-193602: CI red on #1094).
        self.assertTrue(any("could not cancel run 601 " in line for line in lines), lines)
        self.assertFalse(any("could not cancel run 602 " in line for line in lines),
                         "409: the run finished, nothing to retry")

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

    def test_a_build_never_self_wakes(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1")], self.main)
        self.queue(fake, 1, h1, 20)
        self.step(fake).run()
        self.assertEqual([d[0] for d in fake.dispatched], ["ci.yml"], "only the batch's CI, never the next step")

    def test_no_wake_when_the_batch_branch_cannot_be_deleted(self):
        # The self-wake is bounded only because it needs a REAL close; a delete that keeps
        # failing must not wake a step per wake.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="cancelled")
        original = fake.rest
        def rest(path, method="GET", body=None):
            if method == "DELETE" and "/git/refs/heads/" in path:
                from coordlib.gh import GitHubError
                raise GitHubError("HTTP 502")
            return original(path, method, body)
        fake.rest = rest
        self.step(fake).run()
        self.assertNotIn("merge-train.yml", [d[0] for d in fake.dispatched])

    def test_a_closed_batch_starts_the_next_step(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
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
        self.assertIn(train.REVIEWED_LABEL, train.LABELS)
        self.assertNotIn("codex-unavailable", train.LABELS, "retired: the train no longer reads it")

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

    # MARK: - Held notices (#1034)

    AGENT = "claude-cloud@abc12345:INISA"
    H1, H2 = "1" * 40, "2" * 40

    def add(self, fake, number, head, minutes_ago, agent=AGENT):
        """Append a train-add record, as `coord train add` does; earlier comments stay."""
        payload = {"kind": "train-add", "pr": number, "head": head}
        payload["agent"] = agent  # None deliberately represents an invalid old marker.
        fake.issue_comments.setdefault(number, []).append(
            {"created_at": snapshot.iso(NOW - dt.timedelta(minutes=minutes_ago)),
             "body": compose_record("added", payload)})

    def held(self, fake, number):
        """The held notices on PR `number` as GitHub keeps them: [(payload, text)]."""
        out = []
        for comment in fake.issue_comments.get(number, []):
            payload = marker.decode(comment["body"])
            if payload and payload.get("kind") == "train" and payload.get("event") == "held":
                out.append((payload, marker.strip(comment["body"])))
        return out

    def held_node(self, number, head, hold, *failing):
        """A queued PR node held for `hold` (one of train.HOLD_CLASSES, from eligibility)."""
        labels = ("train", "needs-alex") if hold == train.HOLD_NEEDS_ALEX else ("train",)
        node = ready_node(number, head, f"pr{number}", labels=labels)
        if hold == train.HOLD_NO_REVIEW:
            node["reactions"] = {"nodes": []}
        elif hold == train.HOLD_DRAFT:
            node["isDraft"] = True
        elif hold == train.HOLD_CI_RED:
            node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = (
                [check("CI verdict")] + [check(name, conclusion="FAILURE") for name in failing or ("Android",)])
        elif hold == train.HOLD_UNANSWERED_FINDINGS:
            node["reviewThreads"] = {"totalCount": 1, "nodes": [{"isResolved": False, "comments": {
                "totalCount": 1, "nodes": [{"author": {"login": BOT}, "body": "P1: a finding"}]}}]}
        elif hold == train.HOLD_PARTIAL_DATA:
            node["reviewThreads"] = {"totalCount": 1, "nodes": [{"isResolved": True, "comments": {
                "totalCount": 60, "nodes": [{"author": {"login": "alex-bridges"}, "body": "a long thread"}]}}]}
        return node

    def test_a_held_pr_is_told_once_addressed_to_its_agent_and_the_conductor(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX),
                           self.held_node(2, self.H2, train.HOLD_CI_RED)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.add(fake, 2, self.H2, 20, agent=None)  # a record from before train-add named its agent
        self.step(fake).run()
        self.assertEqual(sorted(fake.posted("held")), [1, 2], "one comment per held PR, no more")
        [(payload, text)] = self.held(fake, 1)
        self.assertEqual(payload["to"], [self.AGENT, "conductor@*"])
        self.assertEqual((payload["head"], payload["hold"], payload["pr"]), (self.H1, "needs-alex", 1))
        self.assertIn("waits for Alex", text, "the reason")
        self.assertIn("coord train approve 1 --head <full-sha-Alex-approved>", text, "the action that clears it")
        self.assertIn(f"at head `{self.H1[:9]}`", text, "the held head is named, outside the command")
        self.assertNotIn(f"--head {self.H1[:7]}", text, "never pre-filled with the current head")
        [(payload, _)] = self.held(fake, 2)
        self.assertEqual(payload["to"], ["conductor@*"], "no agent on record: the conductor still hears")

    def test_the_same_hold_is_never_told_twice_not_even_after_a_restart(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.step(fake).run()
        fake.writes.clear()
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [], "the next step, same state")
        # A new runner: new objects, nothing carried over but what GitHub keeps.
        restarted = FakeGitHub(copy.deepcopy(fake.nodes), self.main)
        restarted.issue_comments = copy.deepcopy(fake.issue_comments)
        self.step(restarted, now=snapshot.now_utc() + dt.timedelta(days=1)).run()
        self.assertEqual(restarted.posted("held"), [], "read back from the PR's comments")
        self.assertEqual(len(self.held(restarted, 1)), 1)

    def test_a_new_head_is_a_new_notice(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.step(fake).run()
        # The worker pushes and adds the new head: still needs-alex, at a head Alex has not seen.
        fake.nodes = [self.held_node(1, self.H2, train.HOLD_NEEDS_ALEX)]
        self.add(fake, 1, self.H2, 10)
        self.step(fake).run()
        self.step(fake).run()
        self.assertEqual([(p["head"], p["hold"]) for p, _ in self.held(fake, 1)],
                         [(self.H1, "needs-alex"), (self.H2, "needs-alex")])

    def test_a_different_hold_at_the_same_head_is_a_new_notice(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NO_REVIEW)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.step(fake).run()
        fake.nodes = [self.held_node(1, self.H1, train.HOLD_CI_RED)]  # Codex 👍 arrives; CI is red
        self.step(fake).run()
        self.step(fake).run()
        self.assertEqual([(p["head"], p["hold"]) for p, _ in self.held(fake, 1)],
                         [(self.H1, "no-review"), (self.H1, "ci-red")])

    def test_only_holds_alex_must_clear_mention_him(self):
        holds = [train.HOLD_NEEDS_ALEX, train.HOLD_PARTIAL_DATA, train.HOLD_CI_RED, train.HOLD_NO_REVIEW,
                 train.HOLD_DRAFT, train.HOLD_UNANSWERED_FINDINGS]
        fake = FakeGitHub([self.held_node(n, str(n) * 40, hold) for n, hold in enumerate(holds, start=1)], self.main)
        for number in range(1, len(holds) + 1):
            self.add(fake, number, str(number) * 40, 30 - number)
        self.step(fake).run()
        for number, hold in enumerate(holds, start=1):
            [(payload, text)] = self.held(fake, number)
            self.assertEqual(payload["hold"], hold)
            self.assertEqual("@alex-bridges" in text, hold in train.OWNER_HOLDS, hold)
        self.assertEqual(train.OWNER_HOLDS, {"needs-alex", "partial-data"})

    def test_no_notice_pre_fills_the_current_head_into_an_attestation(self):
        # Copying a pre-filled current head would attest whatever is there now, not the
        # head that was reviewed or approved (review of #1054 at 7d9213923).
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NO_REVIEW),
                           self.held_node(2, self.H2, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.add(fake, 2, self.H2, 20)
        self.step(fake).run()
        for number, head, command in ((1, self.H1, "coord train review 1 --head <full-reviewed-sha> --by <agent>"),
                                      (2, self.H2, "coord train approve 2 --head <full-sha-Alex-approved>")):
            with self.subTest(pr=number):
                [(_, text)] = self.held(fake, number)
                self.assertIn(command, text)
                self.assertIn(f"at head `{head[:9]}`", text, "the held head is still named, for comparison")
                self.assertNotIn(f"--head {head[:7]}", text)

    def test_new_wording_or_a_later_step_is_not_a_new_notice(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_CI_RED, "Android")], self.main)
        self.add(fake, 1, self.H1, 30)
        self.step(fake).run()
        # A second check fails at the same head: the reason's text changes, its class does not.
        fake.nodes = [self.held_node(1, self.H1, train.HOLD_CI_RED, "Android", "Linux plugin probe (x86_64)")]
        lines = self.step(fake, now=snapshot.now_utc() + dt.timedelta(hours=3)).run()
        self.assertTrue(any("Android, Linux plugin probe" in line for line in lines), lines)
        self.assertEqual([p["hold"] for p, _ in self.held(fake, 1)], ["ci-red"])
        # needs-alex in its other wording (an approval of an older head) is the same hold.
        stale = self.held_node(2, self.H2, train.HOLD_NEEDS_ALEX)
        fake.nodes = [stale]
        self.add(fake, 2, self.H2, 20)
        self.step(fake).run()
        stale["labels"]["nodes"].append({"name": "alex-approved"})
        fake.issue_comments[2].append({"created_at": snapshot.iso(NOW), "user": FAKE_OWNER, "body": compose_record(
            "approved", {"kind": "alex-approve", "pr": 2, "head": self.H1})})
        lines = self.step(fake).run()
        self.assertTrue(any("does not name the current head" in line for line in lines), lines)
        self.assertEqual([p["hold"] for p, _ in self.held(fake, 2)], ["needs-alex"])

    def test_every_eligibility_hold_has_a_class_and_an_action(self):
        for hold in train.HOLD_CLASSES:
            self.assertIn(hold, train.HOLD_ACTIONS)
        base = snapshot.summarize_pr(ready_node(1, "a" * 40, "b"))
        cases = [(dict(base, isDraft=True), "draft"), (dict(base, incomplete=["x"]), "partial-data"),
                 (dict(base, labels=["train", "needs-alex"]), "needs-alex"),
                 (dict(base, codex={"reviewing": True}), "no-review"),
                 (dict(base, codex={}), "no-review"),
                 (dict(base, codex={}, reviewedHead="0" * 40), "no-review"),
                 (dict(base, unansweredFindings=1), "unanswered-findings"),
                 (dict(base, ci={"failing": ["Android"]}), "ci-red")]
        for pr, hold in cases:
            verdict, got, reason = train.assess(pr)
            self.assertEqual((verdict, got), (train.HOLD, hold), reason)
            self.assertEqual(train.eligibility(pr), (verdict, reason))
        self.assertEqual(train.assess(base), (train.READY, "", "ready"))

    def test_a_conflict_found_while_building_is_told(self):
        h1 = self.pr_branch(1, "a.txt", "line 1\nONE\nline 3\n")
        h2 = self.pr_branch(2, "a.txt", "line 1\nTWO\nline 3\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1"), ready_node(2, h2, "pr2")], self.main)
        self.add(fake, 1, h1, 20)
        self.add(fake, 2, h2, 10)
        self.step(fake).run()
        self.assertEqual([(p["head"], p["hold"], p["to"]) for p, _ in self.held(fake, 2)],
                         [(h2, "conflict", [self.AGENT, "conductor@*"])])
        self.assertEqual(self.held(fake, 1), [])

    def test_a_head_that_moves_while_building_is_told_once(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1")], self.main)
        self.add(fake, 1, h1, 20)
        sh(self.work, "checkout", "-q", "pr1")
        self.write("c.txt", "c, pushed again\n")
        moved = self.commit("pr 1, a later push")
        sh(self.work, "push", "-q", "-f", "origin", f"HEAD:refs/pull/1/head")
        sh(self.work, "checkout", "-q", "main")
        self.step(fake).run()
        self.step(fake).run()
        self.assertEqual([(p["head"], p["hold"]) for p, _ in self.held(fake, 1)], [(moved, "head-moved")])

    def test_only_the_trains_own_held_record_counts_as_told(self):
        # Every agent posts as alex-bridges and can write any marker (review of #1037).
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        record = {"kind": "train", "v": 1, "event": "held", "pr": 1, "head": self.H1, "hold": "needs-alex"}
        forged = [(dict(record, agent="claude@Eidolon:wt"), "alex-bridges"),  # another agent's marker
                  (dict(record, agent="train@actions"), "alex-bridges"),      # the train's name, not the bot
                  (dict(record, agent="claude@Eidolon:wt"), train_step.BOT_NAME)]
        for payload, login in forged:
            fake.issue_comments[1].append({"created_at": snapshot.iso(NOW), "user": {"login": login},
                                           "body": compose_record("held?", payload)})
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [1], "none of those is the train's own notice")
        fake.writes.clear()
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [], "the train's own one is")

    def test_only_the_trains_own_receipt_stops_a_second_receipt(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.issue_comments[1].append({"created_at": snapshot.iso(NOW), "user": {"login": "alex-bridges"},
                                       "body": compose_record("merged?", {"kind": "train", "agent": "train@actions",
                                                                         "event": "merged", "pr": 1, "batch": name})})
        self.step(fake).run()
        self.assertEqual(fake.posted("merged"), [1])

    HAND = "claude@Eidolon:INISA"  # `coord train step` run by hand: posts as alex-bridges under its own id

    def test_hand_runs_never_repeat_what_the_actions_train_said(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [1])
        fake.writes.clear()
        fake.login = "alex-bridges"
        self.step(fake, agent=self.HAND).run()
        self.step(fake, agent=self.HAND).run()
        self.assertEqual(fake.posted("held"), [], "two hand runs after the Actions train")

    def test_the_actions_train_repeats_a_hand_runs_notice_at_most_once(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        fake.login = "alex-bridges"
        self.step(fake, agent=self.HAND).run()
        self.step(fake, agent=self.HAND).run()
        self.assertEqual(fake.posted("held"), [1], "a hand run counts its own notice")
        fake.writes.clear()
        fake.login = train_step.BOT_NAME
        self.step(fake).run()
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [1], "the Actions train: once, then its own counts")
        self.assertEqual([p["agent"] for p, _ in self.held(fake, 1)], [self.HAND, "train@actions"])

    def test_hand_runs_never_repeat_their_own_receipt(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.login = "alex-bridges"
        self.step(fake, mode="dry-run", agent=self.HAND).run()
        self.step(fake, mode="dry-run", agent=self.HAND).run()  # the batch branch is still there
        self.assertEqual(fake.posted("green-dry-run"), [1])

    def test_a_train_marker_from_another_login_never_silences_the_actions_train(self):
        fake = FakeGitHub([self.held_node(1, self.H1, train.HOLD_NEEDS_ALEX)], self.main)
        self.add(fake, 1, self.H1, 30)
        fake.issue_comments[1].append({"created_at": snapshot.iso(NOW), "user": {"login": "alex-bridges"},
                                       "body": compose_record("held?", {
                                           "kind": "train", "agent": "train@actions", "event": "held", "pr": 1,
                                           "head": self.H1, "hold": "needs-alex"})})
        self.step(fake).run()
        self.assertEqual(fake.posted("held"), [1])

    # Errors the Actions token's REST calls raise (gh.py turns each into GitHubError).
    NOTICE_FAILURES = [
        "HTTP 403 POST https://api.github.com/repos/o/r/issues/2/comments: You have exceeded a secondary rate limit. "
        "Please wait a few minutes before you try again.",
        "HTTP 403 POST https://api.github.com/repos/o/r/issues/2/comments: API rate limit exceeded for installation ID 1.",
        "HTTP 502 POST https://api.github.com/repos/o/r/issues/2/comments: Bad Gateway",
        "POST https://api.github.com/repos/o/r/issues/2/comments: The read operation timed out",
    ]

    def drop_batches(self):
        for ref in sh(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads/train/").splitlines():
            sh(self.origin, "update-ref", "-d", ref)

    def test_a_failed_notice_never_changes_what_build_decides(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        for error in self.NOTICE_FAILURES:
            with self.subTest(error=error):
                self.drop_batches()
                fake = FakeGitHub([ready_node(1, h1, "pr1"), self.held_node(2, self.H2, train.HOLD_NEEDS_ALEX)],
                                  self.main)
                self.add(fake, 1, h1, 30)
                self.add(fake, 2, self.H2, 20)
                fake.fail_held = error
                lines = self.step(fake).run()
                name = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
                self.assertEqual([m["pr"] for m in self.members(name)], [1], lines)
                self.assertEqual(fake.dispatched, [("ci.yml", name, {"lanes": "changed"})], "dispatched once")
                self.assertEqual(fake.posted("testing"), [1])
                self.assertEqual(self.held(fake, 2), [])
                self.assertTrue(any("could not post the held notice" in line for line in lines), lines)

    def test_a_failed_notice_never_changes_what_advance_decides(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        for error in self.NOTICE_FAILURES:
            with self.subTest(error=error):
                self.drop_batches()
                fake, name, tip = self.build_batch([(1, h1)])
                self.run_on(fake, name, tip, conclusion="success")
                fake.nodes = [ready_node(1, h1, "pr1", labels=("train", "needs-alex"))]
                fake.fail_held = error
                lines = self.step(fake).run()
                self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes), lines)
                self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
                self.assertEqual(self.held(fake, 1), [])

    def test_a_failed_read_for_the_notice_never_stops_the_rebuild(self):
        # The head-moved member arrives fresh from open_prs(), so notify_held reads its
        # comments itself; that read failing must not skip deleting the batch.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        for error in [failure.replace("POST", "GET") for failure in self.NOTICE_FAILURES]:
            with self.subTest(error=error):
                self.drop_batches()
                fake, name, tip = self.build_batch([(1, h1)])
                self.run_on(fake, name, tip, conclusion="success")
                fake.nodes[0]["headRefOid"] = "f" * 40
                fake.fail_comment_reads = error
                lines = self.step(fake).run()
                self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes), lines)
                self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
                self.assertTrue(any("could not post the held notice" in line for line in lines), lines)

    def test_advance_deletes_the_batch_before_it_notifies(self):
        # Whatever a notice does, even fail in a way nobody catches, the rebuild is done.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.nodes = [ready_node(1, h1, "pr1", labels=("train", "needs-alex"))]
        original = fake.rest

        def rest(path, method="GET", body=None):
            if method == "POST" and (marker.decode((body or {}).get("body")) or {}).get("event") == "held":
                raise KeyError("an unexpected failure")
            return original(path, method, body)

        fake.rest = rest
        with self.assertRaises(KeyError):
            self.step(fake).run()
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)

    def test_a_green_batch_member_that_is_no_longer_ready_is_told(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="success")
        fake.nodes = [ready_node(1, h1, "pr1", labels=("train", "needs-alex"))]  # labelled during the batch's CI
        self.step(fake).run()
        self.step(fake).run()
        self.assertFalse(any(method == "PATCH" for method, _, _ in fake.writes))
        [(payload, text)] = self.held(fake, 1)
        self.assertEqual((payload["head"], payload["hold"], payload["to"]), (h1, "needs-alex", ["codex@test:train", "conductor@*"]))
        self.assertIn("@alex-bridges", text)

    # MARK: Bounded light priority

    def install_selector(self):
        """Put this checkout's real lane selector and rules on the fake main. The step's
        clone is blob-less, so reading them exercises the on-demand fetch too."""
        for path in (train.LANE_SELECTOR, train.LANE_RULES):
            self.write(path, checkout_file(path))
        self.main = self.commit("the CI lane selector")
        sh(self.work, "push", "-q", "origin", "main")

    def restart(self, fake, nodes):
        """A new step on a new clone, with only what GitHub keeps: the PRs and comments."""
        self.clone = os.path.join(self.tmp.name, f"clone-{len(os.listdir(self.tmp.name))}")
        subprocess.check_call(["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
                               "file://" + self.origin, self.clone])
        fresh = FakeGitHub(nodes, self.main)
        fresh.issue_comments = {n: list(c) for n, c in fake.issue_comments.items()}
        return fresh

    def land(self, name, tip):
        """What a green live batch leaves behind: main at the tip, no batch branch."""
        sh(self.origin, "update-ref", "refs/heads/main", tip)
        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")
        sh(self.work, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        self.main = tip

    def batch_on_origin(self):
        name = sh(self.origin, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/train/")
        return name, (sh(self.origin, "rev-parse", name) if name else "")

    def test_one_light_batch_goes_ahead_of_heavy_then_heavy_goes_even_after_a_restart(self):
        self.install_selector()
        h1 = self.pr_branch(1, HEAVY_PATH, "heavy\n")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        h3 = self.pr_branch(3, "Docs/y.md", "prose\n")
        nodes = {1: ready_node(1, h1, "pr1", files=(HEAVY_PATH,)), 2: ready_node(2, h2, "pr2", files=(LIGHT_PATH,)),
                 3: ready_node(3, h3, "pr3", files=("Docs/y.md",))}
        fake = FakeGitHub(list(nodes.values()), self.main)
        for number, head, minutes in ((1, h1, 50), (2, h2, 40), (3, h3, 30)):
            self.queue(fake, number, head, minutes)
        lines = self.step(fake).run()
        self.assertTrue(any("#1 is heavy: it selects" in line for line in lines), lines)
        self.assertTrue(any("#2 is light: it selects docs" in line for line in lines), lines)
        name, tip = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [2, 3], "light only, ahead of the older heavy #1")
        self.assertEqual(fake.posted("overtaken"), [1])

        # Green: main moves, the light PRs merge, another light PR arrives.
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake).run()
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes)
        self.land(name, tip)
        h4 = self.pr_branch(4, ".claude/skills/z.md", "light again\n")
        fresh = self.restart(fake, [nodes[1], ready_node(4, h4, "pr4", files=(".claude/skills/z.md",))])
        self.queue(fresh, 4, h4, 5)

        # A minute later, as a real next build is: a batch built in the same second as the
        # landed one would share its train/<stamp> name, and parse_batch would read that
        # batch's merges (now on main) as this one's.
        lines = self.step(fresh, now=snapshot.now_utc() + dt.timedelta(minutes=1)).run()
        name, tip = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [1],
                         f"heavy has its turn although light #4 is ready: {lines}")
        self.assertEqual(fresh.posted("overtaken"), [], "no second overtake")

    def test_the_overtake_holds_when_the_light_batch_never_lands(self):
        # A merge-commit trailer would be lost here: the batch is deleted, not merged.
        self.install_selector()
        h1 = self.pr_branch(1, HEAVY_PATH, "heavy\n")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        nodes = [ready_node(1, h1, "pr1", files=(HEAVY_PATH,)), ready_node(2, h2, "pr2", files=(LIGHT_PATH,))]
        fake = FakeGitHub(nodes, self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        self.step(fake).run()
        name, tip = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [2])
        self.run_on(fake, name, tip, conclusion="timed_out")
        self.step(fake).run()  # abandoned: the batch branch is deleted, main never moved
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")

        lines = self.step(self.restart(fake, nodes), now=snapshot.now_utc() + dt.timedelta(minutes=1)).run()
        name, tip = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [1], lines)

    def test_a_new_train_add_gets_its_own_overtake(self):
        fake = FakeGitHub([], self.main)
        def record(kind, **fields):
            return {"created_at": snapshot.iso(NOW), "user": {"login": train_step.BOT_NAME},
                    "body": compose_record("x", dict(fields, kind=kind, pr=1))}
        added = record("train-add", head="a")
        overtaken = record("train", event="overtaken", batch="train/1", agent=train_step.TRAIN_AGENT)
        for comments, expected in (([added, overtaken], "train/1"), ([added, overtaken, added], None), ([added], None)):
            fake.issue_comments[1] = comments
            pr = {"number": 1}
            self.step(fake).train_added(pr)
            self.assertEqual(pr["overtakenBy"], expected)

    def test_only_the_trains_own_overtake_record_counts(self):
        # Every agent posts as alex-bridges and can write any marker (own_record, #1037): a
        # forged record must not give heavy its turn early, and the train's own must hold.
        self.install_selector()
        h1 = self.pr_branch(1, HEAVY_PATH, "heavy\n")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=(HEAVY_PATH,)),
                           ready_node(2, h2, "pr2", files=(LIGHT_PATH,))], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        record = {"kind": "train", "v": 1, "event": train.OVERTAKEN_EVENT, "pr": 1, "batch": "train/forged"}
        for agent, login in (("claude@Eidolon:wt", "alex-bridges"),          # another agent's marker
                             (train_step.TRAIN_AGENT, "alex-bridges"),        # the train's name, not the bot
                             ("claude@Eidolon:wt", train_step.BOT_NAME)):     # the bot, not the train's name
            fake.issue_comments[1].append({"created_at": snapshot.iso(NOW), "user": {"login": login},
                                           "body": compose_record("overtaken?", dict(record, agent=agent))})
        self.step(fake).run()
        name, tip = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [2], "no forged record gives heavy its turn")
        self.assertEqual(fake.posted("overtaken"), [1], "the train records its own overtake")
        # That light batch never lands; the train's own record still gives heavy the turn.
        self.run_on(fake, name, tip, conclusion="timed_out")
        self.step(fake).run()
        sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")
        lines = self.step(fake, now=snapshot.now_utc() + dt.timedelta(minutes=1)).run()
        name, _ = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [1], lines)

    def test_the_overtake_is_recorded_before_the_batch_is_pushed(self):
        self.install_selector()
        h1 = self.pr_branch(1, HEAVY_PATH, "heavy\n")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=(HEAVY_PATH,)),
                           ready_node(2, h2, "pr2", files=(LIGHT_PATH,))], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        fake.fail_writes = 1  # the record is this step's first write
        with self.assertRaises(GitHubError):
            self.step(fake).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), "no record, no light batch")
        self.assertEqual(fake.dispatched, [])

    def test_the_prs_own_selector_is_never_run(self):
        self.install_selector()
        sentinel = os.path.join(self.tmp.name, "pr-code-ran")
        h1 = self.pr_branch(1, train.LANE_SELECTOR,
                            f"open({sentinel!r}, 'w').close()\nLIGHT_LANES = HEAVY_LANES = ()\n")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=(train.LANE_SELECTOR,)),
                           ready_node(2, h2, "pr2", files=(LIGHT_PATH,))], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        lines = self.step(fake).run()
        self.assertFalse(os.path.exists(sentinel), "the step executed the PR's selector")
        self.assertTrue(any("#1 is heavy" in line and "CI graph itself changed" in line for line in lines), lines)
        name, _ = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [2])

    def test_a_rename_counts_both_sides_as_the_selector_does(self):
        # GitHub lists a rename by its new path only; ci.yml's diff (--no-renames) has both.
        self.write(HEAVY_PATH, "moved out of the engine\n")
        self.install_selector()
        sh(self.work, "checkout", "-q", "-b", "pr1", self.main)
        os.makedirs(os.path.join(self.work, "Docs"))
        sh(self.work, "mv", HEAVY_PATH, "Docs/A.md")
        h1 = self.commit("pr 1")
        sh(self.work, "push", "-q", "origin", "HEAD:refs/pull/1/head")
        sh(self.work, "checkout", "-q", "main")
        h2 = self.pr_branch(2, LIGHT_PATH, "light\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=("Docs/A.md",)),
                           ready_node(2, h2, "pr2", files=(LIGHT_PATH,))], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        lines = self.step(fake).run()
        self.assertTrue(any("#1 is heavy" in line for line in lines), lines)

    def test_a_head_the_pr_ref_no_longer_points_at_is_heavy(self):
        # GitHub's snapshot names an older head than refs/pull/1/head: the diff at that
        # head is light, but it is not the PR any more, so the train does not trust it.
        self.install_selector()
        old = self.pr_branch(1, LIGHT_PATH, "light\n")
        sh(self.work, "checkout", "-q", "pr1")
        self.write(HEAVY_PATH, "then heavy\n")
        self.commit("pr 1, pushed again")
        sh(self.work, "push", "-q", "origin", "HEAD:refs/pull/1/head")
        sh(self.work, "checkout", "-q", "main")
        h2 = self.pr_branch(2, "Docs/y.md", "prose\n")
        fake = FakeGitHub([ready_node(1, old, "pr1", files=(LIGHT_PATH,)),
                           ready_node(2, h2, "pr2", files=("Docs/y.md",))], self.main)
        self.queue(fake, 1, old, 50)
        self.queue(fake, 2, h2, 40)
        lines = self.step(fake).run()
        self.assertTrue(any("#1 is heavy: its diff is unknown or empty" in line for line in lines), lines)
        self.assertEqual(fake.posted("overtaken"), [1])

    def test_a_pr_whose_head_cannot_be_fetched_is_heavy(self):
        self.install_selector()
        sh(self.work, "checkout", "-q", "-b", "pr1", self.main)
        self.write(LIGHT_PATH, "light, never pushed\n")
        h1 = self.commit("pr 1")  # only in the work repository: origin has no refs/pull/1/head
        sh(self.work, "checkout", "-q", "main")
        h2 = self.pr_branch(2, "Docs/y.md", "prose\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=(LIGHT_PATH,)),
                           ready_node(2, h2, "pr2", files=("Docs/y.md",))], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        lines = self.step(fake).run()
        self.assertTrue(any("#1 is heavy: its diff is unknown or empty" in line for line in lines), lines)
        name, _ = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [2])

    def test_the_selector_missing_from_main_keeps_the_old_order(self):
        # Every ready PR counts as heavy, so one class is ready: the queue order decides.
        h1 = self.pr_branch(1, "c.txt", "c\n")
        h2 = self.pr_branch(2, "d.txt", "d\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1"), ready_node(2, h2, "pr2")], self.main)
        self.queue(fake, 1, h1, 50)
        self.queue(fake, 2, h2, 40)
        lines = self.step(fake).run()
        self.assertTrue(any("every ready PR counts as heavy" in line for line in lines), lines)
        name, _ = self.batch_on_origin()
        self.assertEqual([m["pr"] for m in self.members(name)], [1, 2])
        self.assertEqual(fake.posted("overtaken"), [])

    def test_full_receipt_keeps_exact_base_heads_lanes_and_attempt_after_cleanup(self):
        self.install_selector()
        h1 = self.pr_branch(1, LIGHT_PATH, "light\n")
        fake, name, tip = self.build_batch([(1, h1)])
        plan = train.read_plan(sh(self.origin, "log", "-1", "--format=%B", tip), name)
        self.assertEqual(plan["base"], self.main)
        self.assertEqual(plan["members"], [{"pr": 1, "head": h1, "queuedAt": snapshot.iso(NOW - dt.timedelta(minutes=50))}])
        self.assertEqual((plan["lanePlan"]["class"], plan["lanePlan"]["lanes"]), (train.LIGHT, ["docs"]))
        self.run_on(fake, name, tip, conclusion="success", run_attempt=2)
        self.step(fake).run()
        receipts = [marker.decode(comment["body"])["receipt"] for comment in fake.issue_comments[1]
                    if (marker.decode(comment["body"]) or {}).get("event") == "merged"]
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual((receipt["base"], receipt["tip"], receipt["members"]), (self.main, tip, plan["members"]))
        self.assertEqual((receipt["run"]["id"], receipt["run"]["attempt"], receipt["outcome"]), (77, 2, "merged"))
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)

    def test_exhausted_heavy_budget_keeps_a_red_batch_without_starting_a_retry(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        fake, name, tip = self.build_batch([(1, h1)])
        self.run_on(fake, name, tip, conclusion="failure", run_attempt=1)
        fake.dispatches_today = [{"id": number, "head_branch": f"train/{number}", "run_attempt": 2}
                                 for number in range(1, 7)]
        lines = self.step(fake).run()
        self.assertTrue(any("daily budget of heavy CI attempts" in line for line in lines), lines)
        self.assertFalse(any(path.endswith("rerun-failed-jobs") for _, path, _ in fake.writes))
        self.assertEqual(fake.posted("ci-requested"), [])
        self.assertEqual(sh(self.origin, "rev-parse", name), tip, "the queued retry can resume after reset")

    def test_docs_batch_can_start_when_heavy_attempts_are_exhausted(self):
        self.install_selector()
        h1 = self.pr_branch(1, LIGHT_PATH, "light\n")
        fake = FakeGitHub([ready_node(1, h1, "pr1", files=(LIGHT_PATH,))], self.main)
        self.queue(fake, 1, h1, 20)
        fake.dispatches_today = [{"id": number, "head_branch": f"train/{number}", "run_attempt": 2}
                                 for number in range(1, 7)]
        with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_RUNS": "", "COORD_TRAIN_DAILY_LIGHT_ATTEMPTS": "12"}):
            self.step(fake).run()
        self.assertEqual(len(fake.dispatched), 1)
        reservation = next(marker.decode(comment["body"]) for comment in fake.issue_comments[1]
                           if (marker.decode(comment["body"]) or {}).get("event") == "ci-requested")
        self.assertEqual(reservation["receipt"]["lanePlan"]["class"], train.LIGHT)

    def test_lost_dispatches_stop_at_the_reserved_attempt_cap(self):
        h1 = self.pr_branch(1, "c.txt", "c\n")
        with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_HEAVY_ATTEMPTS": "2", "COORD_TRAIN_DAILY_RUNS": ""}):
            fake, name, tip = self.build_batch([(1, h1)])
            future = snapshot.now_utc() + dt.timedelta(minutes=30)
            self.step(fake, now=future).run()
            lines = self.step(fake, now=future + dt.timedelta(minutes=15)).run()
        self.assertEqual(fake.dispatched, [("ci.yml", name, {"lanes": "changed"})], "only one redispatch after the first request")
        self.assertTrue(any("daily budget" in line for line in lines), lines)
        self.assertEqual(sh(self.origin, "rev-parse", name), tip)


if __name__ == "__main__":
    unittest.main()

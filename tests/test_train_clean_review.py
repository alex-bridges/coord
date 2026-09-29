"""A recorded clean review of the exact head releases the Codex-pending hold (#1181).

Alex, 2026-09-29: "Yes, clean review suffices." Until then the train held a queued PR on
`no-review` ("Codex is re-reviewing it") whenever Codex showed 👀 on it, even with a clean
review of that exact head recorded by `coord train review`, and even with Alex's approval
of the head recorded; #1075, #1094 and #1068 all waited there. Now that record lets the
PR go ahead as it would on Codex's 👍. What stays the same:
- a clean review of an older head, or none, still waits for Codex;
- Codex findings on the head still hold it (`unanswered-findings`), and findings that
  land while it is in a batch take it out at the final check before main moves, which
  is what the train already did with findings on a queued head;
- Alex-approval holds, conflict and head-moved drops, sidechain `changes` verdicts and
  red CI are untouched.

Run: python3 -m unittest tests.test_train_clean_review (from Tools/coord).

The step runs against REAL git and test_train's in-memory GitHub, as in test_train.
Its StepTests helpers are borrowed, not inherited, so its tests do not run twice.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_train as base  # noqa: E402
from coordlib import snapshot, train, train_step  # noqa: E402
from test_coord import BOT, NOW, codex_summary, isolate_from_rest_env, reaction  # noqa: E402


def setUpModule():
    isolate_from_rest_env()  # the fakes answer GraphQL; see test_coord.REST_ENV (#1085)


HEAD = "a" * 40
OLDER = "0" * 40
IN_PROGRESS = "⏳ **In progress**"
# One Codex review thread nobody has answered: what Codex leaves when it finishes a head
# with findings (a COMMENTED review, no 👍).
FINDING = {"totalCount": 1, "nodes": [{"isResolved": False, "comments": {
    "totalCount": 1, "nodes": [{"author": {"login": BOT}, "body": "P1: a finding"}]}}]}


def pending_node(number, head, branch, **overrides):
    """A PR node as GitHub shows it while Codex re-reviews `head`: its 👍 removed, 👀 on
    the PR, and its summary naming `head` as in progress, not completed."""
    node = base.ready_node(number, head, branch, **overrides)
    node["reactions"] = {"nodes": [reaction("EYES", 5)]}
    node["comments"] = {"totalCount": 1, "nodes": [codex_summary(head, status=IN_PROGRESS)]}
    return node


class RuleTests(unittest.TestCase):
    """`train.assess` on a queued PR Codex is re-reviewing."""

    def pr(self, **fields):
        pr = snapshot.summarize_pr(pending_node(1, HEAD, "b"))
        pr.update(fields)
        return pr

    def test_the_fixture_is_codex_pending(self):
        codex = self.pr()["codex"]
        self.assertEqual((codex["reviewing"], codex["approved"]), (True, False))

    def test_a_clean_review_of_the_head_releases_the_wait_for_codex(self):
        self.assertEqual(train.assess(self.pr(reviewedHead=HEAD)), (train.READY, "", "ready"))

    def test_a_clean_review_of_an_older_head_still_waits_for_codex(self):
        self.assertEqual(train.assess(self.pr(reviewedHead=OLDER)),
                         (train.HOLD, train.HOLD_NO_REVIEW, "Codex is re-reviewing it"))

    def test_no_clean_review_still_waits_for_codex(self):
        self.assertEqual(train.assess(self.pr()), (train.HOLD, train.HOLD_NO_REVIEW, "Codex is re-reviewing it"))

    def test_no_head_and_no_record_is_not_a_match(self):
        self.assertEqual(train.assess(self.pr(head=None, reviewedHead=None))[:2], (train.HOLD, train.HOLD_NO_REVIEW))

    def test_findings_on_the_head_still_hold_a_cleanly_reviewed_pr(self):
        verdict, hold, reason = train.assess(self.pr(reviewedHead=HEAD, unansweredFindings=1))
        self.assertEqual((verdict, hold), (train.HOLD, train.HOLD_UNANSWERED_FINDINGS), reason)
        self.assertEqual(reason, "1 unanswered Codex finding on head " + HEAD[:9])

    def test_alex_s_approval_alone_is_not_a_review(self):
        # An ALEX_ONLY_PATHS change, approved at the head by Alex, still waits for Codex
        # unless a clean review of the head is recorded; with one it goes ahead.
        approved = dict(self.pr(files=["Tools/coord/coordlib/train.py"]),
                        labels=["train", "alex-approved"], alexApprovedHead=HEAD)
        self.assertEqual(train.assess(approved)[:2], (train.HOLD, train.HOLD_NO_REVIEW))
        self.assertEqual(train.assess(dict(approved, reviewedHead=HEAD)), (train.READY, "", "ready"))

    def test_every_other_hold_and_drop_is_unchanged(self):
        """Each case also carries a clean review of the head: it releases the wait for
        Codex and nothing else."""
        reviewed = self.pr(reviewedHead=HEAD)
        holds = [
            ("needs-alex, not approved", dict(reviewed, labels=["train", "needs-alex"]), train.HOLD_NEEDS_ALEX),
            ("needs-alex, an older head approved",
             dict(reviewed, labels=["train", "needs-alex", "alex-approved"], alexApprovedHead=OLDER),
             train.HOLD_NEEDS_ALEX),
            ("an Alex-only path, not approved", dict(reviewed, files=["Tools/coord/coordlib/train.py"]),
             train.HOLD_NEEDS_ALEX),
            ("a sidechain changes verdict at the head", dict(reviewed, sidechainBlockedHeads=[HEAD]),
             train.HOLD_UNANSWERED_FINDINGS),
            ("red CI", dict(reviewed, ci={"failing": ["Android"], "pending": [], "verdict": "failure"}),
             train.HOLD_CI_RED),
            ("a draft", dict(reviewed, isDraft=True), train.HOLD_DRAFT),
            ("partial data", dict(reviewed, incomplete=["reviewThreads"]), train.HOLD_PARTIAL_DATA),
        ]
        for name, pr, hold in holds:
            with self.subTest(name):
                self.assertEqual(train.assess(pr)[:2], (train.HOLD, hold))
        drops = [
            ("a conflict with main", dict(reviewed, mergeable="CONFLICTING"), {}),
            ("a head moved since it was added", reviewed, {"head_at_add": OLDER}),
            ("a workflow change", dict(reviewed, files=[".github/workflows/ci.yml"]), {}),
            ("a fork", dict(reviewed, fromFork=True), {}),
        ]
        for name, pr, kwargs in drops:
            with self.subTest(name):
                self.assertEqual(train.assess(pr, **kwargs)[0], train.DROP)

    def test_codex_is_not_asked_again_for_a_head_it_is_reviewing(self):
        # Unchanged (#1101): the review is under way, so the train posts no `@codex review`.
        pr = dict(self.pr(reviewedHead=HEAD), headPushedAt=NOW, reviewRequests=[], readdedAtNewHead=True)
        self.assertIsNone(train.review_request_due(pr, NOW))


class StepTests(unittest.TestCase):
    """The step, end to end: build a batch, then the final check before main moves."""

    setUp = base.StepTests.setUp
    write = base.StepTests.write
    commit = base.StepTests.commit
    pr_branch = base.StepTests.pr_branch
    queue = base.StepTests.queue
    run_on = base.StepTests.run_on
    members = base.StepTests.members
    held = base.StepTests.held
    batch_on_origin = base.StepTests.batch_on_origin
    review_record = staticmethod(base.StepTests.review_record)

    def step(self, fake, mode="live"):
        self.lines = []
        return train_step.Step(fake, train_step.Git(self.clone), mode, now=NOW, log=self.lines.append,
                               sleep=lambda seconds: None)

    def queue_pending(self, reviewed_head=None, findings=False):
        """PR 1 queued at its head while Codex re-reviews it, with a clean review recorded
        by `coord train review` naming `reviewed_head` (none when None)."""
        head = self.pr_branch(1, "c.txt", "c\n")
        node = pending_node(1, head, "pr1")
        if findings:
            node["reviewThreads"] = dict(FINDING)
        fake = base.FakeGitHub([node], self.main)
        self.queue(fake, 1, head, 20)
        if reviewed_head is not None:
            fake.issue_comments[1].append(self.review_record(head if reviewed_head == "head" else reviewed_head))
        return fake, head, node

    def main_moved(self, fake):
        return any(method == "PATCH" and path.endswith("heads/main") for method, path, _ in fake.writes)

    def test_a_pending_codex_review_with_a_clean_review_of_the_head_is_batched_and_merged(self):
        fake, head, _ = self.queue_pending(reviewed_head="head")
        self.step(fake).run()
        name, tip = self.batch_on_origin()
        self.assertTrue(name, self.lines)
        self.assertEqual(self.members(name), [{"pr": 1, "sha": head}])
        self.assertEqual(fake.posted("testing"), [1])
        self.assertEqual(self.held(fake, 1), [], "nothing held it")
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake).run()  # the final check re-reads the PR: still 👀, still reviewed
        self.assertIn(("PATCH", "repos/{repo}/git/refs/heads/main", {"sha": tip, "force": False}), fake.writes,
                      self.lines)
        self.assertEqual(fake.posted("merged"), [1])

    def test_a_clean_review_of_an_older_head_stays_held(self):
        fake, head, _ = self.queue_pending(reviewed_head=OLDER)
        self.step(fake).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), self.lines)
        self.assertIn("#1 held: Codex is re-reviewing it", self.lines)
        [(payload, _)] = self.held(fake, 1)
        self.assertEqual((payload["head"], payload["hold"]), (head, train.HOLD_NO_REVIEW))

    def test_no_clean_review_stays_held(self):
        fake, head, _ = self.queue_pending()
        self.step(fake).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), self.lines)
        self.assertIn("#1 held: Codex is re-reviewing it", self.lines)
        [(payload, _)] = self.held(fake, 1)
        self.assertEqual((payload["head"], payload["hold"]), (head, train.HOLD_NO_REVIEW))

    def test_findings_on_the_head_are_told_and_keep_it_out_of_a_batch(self):
        fake, head, _ = self.queue_pending(reviewed_head="head", findings=True)
        self.step(fake).run()
        self.assertEqual(self.batch_on_origin(), ("", ""), self.lines)
        [(payload, text)] = self.held(fake, 1)
        self.assertEqual((payload["head"], payload["hold"], payload["to"]),
                         (head, train.HOLD_UNANSWERED_FINDINGS, ["codex@test:train", "conductor@*"]))
        self.assertIn("1 unanswered Codex finding", text)

    def test_findings_that_land_during_the_batch_take_it_out_and_are_told(self):
        """Codex finishes the head with a finding while the batch's CI runs. The final
        check reads the PR again: main does not move, the batch is rebuilt without it, and
        whoever added it (and the conductor) hears why: the train's rule for findings on
        a queued head, the same as before the change."""
        fake, head, node = self.queue_pending(reviewed_head="head")
        self.step(fake).run()
        name, tip = self.batch_on_origin()
        self.assertTrue(name, self.lines)
        node["reactions"] = {"nodes": []}  # Codex is done with the head: 👀 off, no 👍
        node["comments"] = {"totalCount": 1, "nodes": [codex_summary(head)]}
        node["reviewThreads"] = dict(FINDING)
        self.run_on(fake, name, tip, conclusion="success")
        self.step(fake).run()
        self.assertFalse(self.main_moved(fake), self.lines)
        self.assertIn(("DELETE", f"repos/{{repo}}/git/refs/heads/{name}", None), fake.writes)
        self.assertEqual(fake.posted("rebuilt"), [1])
        self.assertTrue(any("#1 is no longer ready (1 unanswered Codex finding" in line for line in self.lines),
                        self.lines)
        [(payload, _)] = self.held(fake, 1)
        self.assertEqual((payload["head"], payload["hold"], payload["to"]),
                         (head, train.HOLD_UNANSWERED_FINDINGS, ["codex@test:train", "conductor@*"]))
        base.sh(self.origin, "update-ref", "-d", f"refs/heads/{name}")  # the DELETE the fake only recorded
        self.step(fake).run()  # the next build holds it again and says nothing twice
        self.assertEqual(self.batch_on_origin(), ("", ""), self.lines)
        self.assertIn("#1 held: 1 unanswered Codex finding on head " + head[:9], self.lines)
        self.assertEqual(len(self.held(fake, 1)), 1)


if __name__ == "__main__":
    unittest.main()

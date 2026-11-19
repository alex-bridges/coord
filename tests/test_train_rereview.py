"""The train asks Codex again for a queued head nobody reviewed (#1101).

Run: python3 -m unittest tests.test_train_rereview (from Tools/coord).

The step runs against REAL git and test_train's in-memory GitHub, as in test_train.
Its StepTests helpers are borrowed, not inherited, so its tests do not run twice.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_train as base  # noqa: E402
from coordlib import marker, snapshot, train, train_step  # noqa: E402
from test_coord import ALEX, NOW, reaction, t  # noqa: E402
import example_config  # noqa: E402

example_config.use()  # example-repo workflow/job/lane names are config, not defaults


def setUpModule():
    example_config.use()


def requests(fake):
    """The `@codex review` comments the step posted: (pr, head in the marker, body)."""
    out = []
    for method, path, body in fake.writes:
        if method == "POST" and path.endswith("/comments") and "@codex review" in body["body"]:
            payload = marker.decode(body["body"]) or {}
            out.append((int(path.split("/")[-2]), payload.get("head"), body["body"]))
    return out


class RereviewStepTests(unittest.TestCase):
    setUp = base.StepTests.setUp
    write = base.StepTests.write
    commit = base.StepTests.commit
    pr_branch = base.StepTests.pr_branch
    queue = base.StepTests.queue

    def step(self, fake, mode="live", **kwargs):
        return train_step.Step(fake, train_step.Git(self.clone), mode, now=NOW, log=lambda line: None,
                               sleep=lambda seconds: None, **kwargs)

    def unreviewed(self, pushed_minutes_ago=60, queued_minutes_ago=20):
        """PR 1 queued at its head, which Codex never reviewed: no 👍, no summary naming
        it, no recorded clean review. GitHub first saw the head `pushed_minutes_ago`."""
        head = self.pr_branch(1, "c.txt", "c\n")
        node = base.ready_node(1, head, "pr1")
        node["reactions"] = {"nodes": []}
        node["comments"] = {"totalCount": 0, "nodes": []}
        node["commits"]["nodes"][0]["commit"]["checkSuites"] = {"nodes": [{"createdAt": t(pushed_minutes_ago)}]}
        fake = base.FakeGitHub([node], self.main)
        self.queue(fake, 1, head, queued_minutes_ago)
        return fake, head, node

    def test_an_unreviewed_head_gets_one_request_per_head(self):
        fake, head, _ = self.unreviewed()
        lines = self.step(fake).run()
        self.assertEqual([(pr, h) for pr, h, _ in requests(fake)], [(1, head)], lines)
        body = requests(fake)[0][2]
        self.assertTrue(body.startswith("@codex review"), body)
        self.assertIn(head[:9], body)
        self.assertTrue(any("#1: asked Codex to review" in line for line in lines), lines)
        self.step(fake).run()  # its own request is on the PR now
        self.assertEqual(len(requests(fake)), 1)

    def test_a_dry_run_asks_too(self):
        fake, head, _ = self.unreviewed()
        self.step(fake, mode="dry-run").run()
        self.assertEqual([(pr, h) for pr, h, _ in requests(fake)], [(1, head)])

    def test_a_head_younger_than_the_threshold_is_left_alone(self):
        fake, _, _ = self.unreviewed(pushed_minutes_ago=29)
        self.step(fake).run()
        self.assertEqual(requests(fake), [])

    def test_a_request_anyone_made_for_this_head_is_enough(self):
        fake, _, _ = self.unreviewed()
        fake.issue_comments[1].append({"created_at": t(10), "body": "@codex review", "user": dict(ALEX)})
        self.step(fake).run()
        self.assertEqual(requests(fake), [])

    def test_a_request_made_before_the_head_was_pushed_does_not_count(self):
        fake, head, _ = self.unreviewed()
        fake.issue_comments[1].append({"created_at": t(90), "body": "@codex review", "user": dict(ALEX)})
        self.step(fake).run()
        self.assertEqual([(pr, h) for pr, h, _ in requests(fake)], [(1, head)])

    def test_a_head_codex_is_reviewing_is_left_alone(self):
        fake, _, node = self.unreviewed()
        node["reactions"] = {"nodes": [reaction("EYES", 5)]}
        self.step(fake).run()
        self.assertEqual(requests(fake), [])

    def test_a_head_codex_reviewed_with_findings_is_left_alone(self):
        fake, head, node = self.unreviewed()
        node["comments"] = {"totalCount": 1, "nodes": [base.codex_summary(head)]}
        self.step(fake).run()
        self.assertEqual(requests(fake), [])

    def test_a_pr_re_added_at_a_new_head_is_asked_for_at_once(self):
        fake, head, _ = self.unreviewed(pushed_minutes_ago=2, queued_minutes_ago=1)
        old = "0" * 40
        fake.issue_comments[1].insert(0, {"created_at": t(120), "body": base.compose_record(
            "added", {"kind": "train-add", "pr": 1, "head": old})})
        lines = self.step(fake).run()
        self.assertEqual([(pr, h) for pr, h, _ in requests(fake)], [(1, head)], lines)
        self.assertIn("added again", requests(fake)[0][2])

    def test_a_first_add_of_a_young_head_waits_for_the_threshold(self):
        fake, _, _ = self.unreviewed(pushed_minutes_ago=2, queued_minutes_ago=1)
        self.step(fake).run()
        self.assertEqual(requests(fake), [])

    def test_the_request_goes_through_the_requester(self):
        fake, head, _ = self.unreviewed()
        as_alex = base.FakeGitHub([], self.main)
        self.step(fake, requester=as_alex).run()
        self.assertEqual(requests(fake), [])
        self.assertEqual([(pr, h) for pr, h, _ in requests(as_alex)], [(1, head)])


class ReviewRequestRuleTests(unittest.TestCase):
    HEAD = "b" * 40

    def pr(self, **fields):
        pr = {"number": 1, "head": self.HEAD, "isDraft": False, "codex": {},
              "headPushedAt": NOW - dt.timedelta(minutes=45), "reviewRequests": [], "readdedAtNewHead": False}
        pr.update(fields)
        return pr

    def test_due_after_the_threshold_only(self):
        self.assertTrue(train.review_request_due(self.pr(), NOW))
        self.assertIsNone(train.review_request_due(
            self.pr(headPushedAt=NOW - dt.timedelta(minutes=29)), NOW))
        self.assertIsNone(train.review_request_due(self.pr(headPushedAt=None), NOW))

    def test_never_for_a_head_that_is_reviewed_or_being_reviewed(self):
        for fields in ({"codex": {"approved": True}}, {"codex": {"reviewing": True}},
                       {"codex": {"reviewedCommit": HEAD_PREFIX}}, {"reviewedHead": "b" * 40},
                       {"sidechainBlockedHeads": ["b" * 40]}, {"isDraft": True}):
            with self.subTest(fields=fields):
                self.assertIsNone(train.review_request_due(self.pr(**fields), NOW))

    def test_a_request_after_the_push_counts_one_before_does_not(self):
        after = NOW - dt.timedelta(minutes=40)
        before = NOW - dt.timedelta(minutes=50)
        self.assertIsNone(train.review_request_due(self.pr(reviewRequests=[after]), NOW))
        self.assertTrue(train.review_request_due(self.pr(reviewRequests=[before]), NOW))

    def test_a_re_add_at_a_new_head_is_due_at_once(self):
        young = NOW - dt.timedelta(minutes=1)
        self.assertTrue(train.review_request_due(self.pr(headPushedAt=young, readdedAtNewHead=True), NOW))
        self.assertIsNone(train.review_request_due(
            self.pr(headPushedAt=young, readdedAtNewHead=True, reviewRequests=[NOW]), NOW))


HEAD_PREFIX = "b" * 7


if __name__ == "__main__":
    unittest.main()

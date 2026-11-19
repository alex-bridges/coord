"""#1148: a queued PR held for new Codex findings reaches a live owner."""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coordlib import marker, train, train_step  # noqa: E402
import inisa_config  # noqa: E402

inisa_config.use()  # INISA workflow/job/lane names are config, not defaults


def setUpModule():
    inisa_config.use()

HEAD = "a" * 40
NOW = dt.datetime(2026, 9, 28, 4, 0, tzinfo=dt.timezone.utc)


def pr(**extra):
    base = {"number": 7, "base": "main", "head": HEAD, "files": ["README.md"], "labels": [train.TRAIN_LABEL],
            "codex": {"approved": False, "reviewing": False}, "unansweredFindings": 0, "ci": {}}
    base.update(extra)
    return base


class FakeGitHub:
    repo = "o/r"

    def __init__(self, comments):
        self.comments = comments
        self.posted = []

    def rest_paginated(self, path):
        return list(self.comments)

    def rest(self, path, method="GET", body=None):
        self.posted.append(marker.decode(body["body"]))
        self.comments.append({"user": {"login": train_step.BOT_NAME}, "body": body["body"]})
        return {}


def record(payload, login="alex-bridges"):
    return {"user": {"login": login}, "created_at": "2026-09-28T00:00:00Z", "body": marker.compose("x", payload)}


class HoldClassTests(unittest.TestCase):
    def test_findings_without_thumbs_up_are_not_waiting_for_review(self):
        verdict, hold, reason = train.assess(pr(unansweredFindings=2))
        self.assertEqual((verdict, hold), (train.HOLD, train.HOLD_UNANSWERED_FINDINGS))
        self.assertIn(HEAD[:9], reason)

    def test_no_findings_still_waits_for_review(self):
        self.assertEqual(train.assess(pr())[1], train.HOLD_NO_REVIEW)


class NoticeTests(unittest.TestCase):
    def step(self, comments):
        gh = FakeGitHub(comments)
        return gh, train_step.Step(gh, None, "dry-run", now=NOW, log=lambda _: None)

    def test_new_findings_after_no_review_notice_notify_owner_and_fallback_once(self):
        comments = [
            record({"kind": "train-add", "v": 1, "agent": "worker@x", "pr": 7, "head": HEAD, "fallback": "lead@y"}),
            record({"kind": "train", "v": 1, "agent": train_step.TRAIN_AGENT, "event": "held", "pr": 7,
                    "head": HEAD, "hold": train.HOLD_NO_REVIEW}, login=train_step.BOT_NAME),
        ]
        gh, step = self.step(comments)
        queued = pr(unansweredFindings=1)
        for _ in range(2):  # two steps at the same head: one notice
            queued.pop("heldNotices", None)
            _, hold, reason = train.assess(queued, HEAD)
            step.notify_held(queued, HEAD, hold, reason)
        held = [p for p in gh.posted if p.get("event") == "held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["hold"], train.HOLD_UNANSWERED_FINDINGS)
        self.assertEqual(held[0]["to"], ["worker@x", "lead@y", train.CONDUCTOR])


if __name__ == "__main__":
    unittest.main()

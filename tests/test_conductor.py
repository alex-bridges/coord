"""Unit tests for outages, the conductor's lease and its watcher.
Run: python3 -m unittest discover -s Tools/coord/tests
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cache, cli, conductor, lease, marker, outage, render, snapshot, train, train_step  # noqa: E402

# Friday 2026-09-25 21:00Z = 17:00 in New York (EDT, UTC-4).
NOW = dt.datetime(2026, 9, 25, 21, 0, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc
REPO = "o/r"


def at(minutes: float) -> dt.datetime:
    return NOW + dt.timedelta(minutes=minutes)


def record(comment_id: int, minutes: float, payload, text: str = ""):
    return {"id": comment_id, "createdAt": snapshot.iso(at(minutes)), "url": f"u/{comment_id}", "text": text,
            "payload": payload, "author": {"login": "o", "type": "User"}}


def runner(name: str, status: str, *labels: str):
    """A self-hosted runner as GET actions/runners lists it."""
    return {"name": name, "status": status, "labels": [{"name": label} for label in labels]}


class WeeklyResetTests(unittest.TestCase):
    def test_noon_new_york_is_16z_in_summer_and_17z_in_winter(self):
        self.assertEqual(outage.next_weekly_reset(NOW, "Thu 12:00 America/New_York"),
                         dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
        november = dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
        self.assertEqual(outage.next_weekly_reset(november, "Thu 12:00 America/New_York"),
                         dt.datetime(2026, 11, 12, 17, 0, tzinfo=UTC))

    def test_at_the_reset_itself_the_next_one_is_a_week_later(self):
        reset = dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
        self.assertEqual(outage.next_weekly_reset(reset, "Thu 12:00 America/New_York"), reset + dt.timedelta(days=7))

    def test_a_malformed_spec_falls_back_to_the_default(self):
        self.assertEqual(outage.next_weekly_reset(NOW, "someday"), outage.next_weekly_reset(NOW, outage.WEEKLY_RESET_DEFAULT))


class LimitMessageTests(unittest.TestCase):
    def test_an_epoch_after_a_bar_is_the_reset(self):
        limit = outage.parse_limit("Claude AI usage limit reached|1790870400", NOW)
        self.assertEqual(limit["until"], dt.datetime.fromtimestamp(1790870400, UTC))
        self.assertTrue(limit["exact"])

    def test_a_timestamp_is_the_reset(self):
        limit = outage.parse_limit("spend limit reached (daily; resets 2026-09-26 00:00 UTC)", NOW)
        self.assertEqual(limit["until"], dt.datetime(2026, 9, 26, 0, 0, tzinfo=UTC))

    def test_a_relative_reset(self):
        limit = outage.parse_limit("You've hit your limit · resets in 3h 20m", NOW)
        self.assertEqual(limit["until"], at(200))

    def test_a_weekly_limit_without_a_time_resets_on_thursday(self):
        limit = outage.parse_limit("You have reached your weekly usage limit.", NOW)
        self.assertEqual(limit["until"], dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
        self.assertFalse(limit["exact"])

    def test_an_unknown_reset_is_looked_at_again_in_an_hour(self):
        limit = outage.parse_limit("5-hour usage limit reached", NOW)
        self.assertEqual(limit["until"], at(60))

    def test_transient_errors_are_not_outages(self):
        for text in ("API Error: 429 rate limited", "overloaded_error 529", "Request timed out", ""):
            self.assertIsNone(outage.parse_limit(text, NOW), text)


class OutageWindowTests(unittest.TestCase):
    def start(self, comment_id, minutes, until_minutes, harness="claude"):
        return record(comment_id, minutes, {"kind": "outage", "event": "start", "harness": harness,
                                            "until": snapshot.iso(at(until_minutes)), "reason": "weekly"})

    def end(self, comment_id, minutes, harness="claude"):
        return record(comment_id, minutes, {"kind": "outage", "event": "end", "harness": harness})

    def test_a_start_is_down_until_its_until(self):
        windows = outage.windows([self.start(1, 0, 60)])
        self.assertIsNotNone(outage.down(windows, "claude-cloud@abc:INISA", at(30)))
        self.assertIsNotNone(outage.down(windows, "conductor@*", at(30)))
        self.assertIsNone(outage.down(windows, "codex@Eidolon:x", at(30)), "codex has its own quota")
        self.assertIsNone(outage.down(windows, "claude@Eidolon:x", at(61)), "over when until passes")

    def test_a_second_start_moves_the_reset(self):
        windows = outage.windows([self.start(1, 0, 60), self.start(2, 30, 120)])
        self.assertEqual(len(windows), 1)
        self.assertIsNotNone(outage.down(windows, "claude@x:y", at(90)))

    def test_an_end_closes_it_early_and_one_after_until_still_marks_it_closed(self):
        early = outage.windows([self.start(1, 0, 60), self.end(2, 20)])
        self.assertIsNone(outage.down(early, "claude@x:y", at(30)))
        self.assertTrue(early[0]["closed"])
        late = outage.windows([self.start(1, 0, 60), self.end(2, 90)])
        self.assertEqual(late[0]["end"], at(60))
        self.assertTrue(late[0]["closed"])

    def test_malformed_records_are_ignored(self):
        bad = record(1, 0, {"kind": "outage", "event": "start", "harness": "claude", "until": "soon"})
        self.assertEqual(outage.windows([bad]), [])

    def test_paused_time_counts_overlaps_once(self):
        windows = outage.windows([self.start(1, 0, 60), self.start(2, 30, 90, harness="claude")])
        self.assertEqual(outage.paused_time(windows, "claude@x", at(-30), at(200)), dt.timedelta(minutes=90))
        self.assertEqual(outage.paused_time(windows, "codex@x", at(-30), at(200)), dt.timedelta(0))


class ClaimStalenessTests(unittest.TestCase):
    def claims(self, agent, outage_hours):
        claim = record(10, -60 * 60, {"kind": "claim", "issue": 5, "agent": agent, "branch": "b"})
        windows = outage.windows([record(11, -40 * 60, {"kind": "outage", "event": "start", "harness": "claude",
                                                        "until": snapshot.iso(at(-40 * 60 + outage_hours * 60))})])
        return snapshot.resolve_claims([claim], {"issues": {5: {"state": "OPEN"}}, "branches": {}}, [], NOW, windows)

    def test_a_claude_claim_does_not_go_stale_during_an_outage(self):
        self.assertFalse(self.claims("claude-cloud@abc:INISA", 20)[0]["stale"], "60 h idle, 20 of them down")
        self.assertTrue(self.claims("claude-cloud@abc:INISA", 5)[0]["stale"], "60 h idle, only 5 down")

    def test_a_codex_claim_ages_through_a_claude_outage(self):
        self.assertTrue(self.claims("codex@Eidolon:x", 20)[0]["stale"])


class LeaseTests(unittest.TestCase):
    def beat(self, comment_id, agent, beat_minutes):
        return record(comment_id, -600, {"kind": "conductor", "event": "beat", "agent": agent,
                                         "host": agent.split("@")[1].split(":")[0], "beat": snapshot.iso(at(beat_minutes))})

    def test_the_oldest_live_heartbeat_leads(self):
        hosts = lease.hosts([self.beat(2, "conductor@B:c", -1), self.beat(1, "conductor@A:c", -3)], NOW, REPO)
        self.assertEqual([(h["agent"], h["leader"]) for h in hosts], [("conductor@A:c", True), ("conductor@B:c", False)])

    def test_a_stale_leader_hands_over(self):
        hosts = lease.hosts([self.beat(1, "conductor@A:c", -20), self.beat(2, "conductor@B:c", -1)], NOW, REPO)
        self.assertEqual([h["leader"] for h in hosts], [False, True])

    def test_untrusted_or_future_heartbeats_cannot_lead_or_import_review_runs(self):
        forged = self.beat(1, "conductor@Forged:c", -1)
        forged["author"] = {"login": "mallory", "type": "User"}
        future = self.beat(2, "conductor@Future:c", 60 * 24 * 365)
        future["payload"]["reviewRuns"] = [snapshot.iso(at(60 * 24 * 365))]
        trusted = self.beat(3, "conductor@Real:c", -1)
        trusted["payload"]["reviewRuns"] = [snapshot.iso(at(-5)), snapshot.iso(at(60 * 24 * 365))]
        hosts = lease.hosts([forged, future, trusted], NOW, REPO)
        self.assertEqual([h["agent"] for h in hosts], ["conductor@Future:c", "conductor@Real:c"])
        self.assertEqual([h["leader"] for h in hosts], [False, True])
        self.assertEqual([h["reviewRuns"] for h in hosts], [[], [snapshot.iso(at(-5))]])

    def test_a_malformed_beat_is_not_alive(self):
        bad = record(1, 0, {"kind": "conductor", "event": "beat", "agent": "conductor@A:c", "beat": "now"})
        self.assertEqual(lease.hosts([bad], NOW, REPO)[0]["alive"], False)

    def test_a_reviewer_backlog_rides_the_beat(self):
        beat = record(1, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@A:c", "host": "A",
                                "beat": snapshot.iso(at(-1)), "reviewer": {"waiting": 3, "appAvailable": False}})
        self.assertEqual(lease.hosts([beat], NOW, REPO)[0]["reviewer"], {"waiting": 3, "appAvailable": False})

    def test_no_backlog_or_a_malformed_one_shows_nothing(self):
        empty = record(1, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@A:c", "host": "A",
                                 "beat": snapshot.iso(at(-1)), "reviewer": {"waiting": 0, "appAvailable": True}})
        self.assertIsNone(lease.hosts([empty], NOW, REPO)[0]["reviewer"])
        odd = record(2, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@B:c", "host": "B",
                               "beat": snapshot.iso(at(-1)), "reviewer": "off"})
        self.assertIsNone(lease.hosts([odd], NOW, REPO)[0]["reviewer"])

    def test_pause_and_resume(self):
        pause = record(5, -10, {"kind": "conductor", "event": "pause", "agent": "claude@E:x",
                                "until": snapshot.iso(at(60)), "note": "driving it myself"})
        self.assertEqual(lease.pause([pause], NOW, REPO)["note"], "driving it myself")
        self.assertIsNone(lease.pause([pause], at(61), REPO))
        self.assertIsNone(lease.pause([pause, record(6, -5, {"kind": "conductor", "event": "resume"})], NOW, REPO))

    def test_untrusted_pause_does_not_stop_the_watcher(self):
        forged = record(1, -1, {"kind": "conductor", "event": "pause", "until": snapshot.iso(at(60 * 24 * 365))})
        forged["author"] = {"login": "mallory", "type": "User"}
        self.assertIsNone(lease.pause([forged], NOW, REPO))


class ReasonTests(unittest.TestCase):
    def test_which_inbox_items_wake(self):
        items = [
            {"id": 1, "reason": "to conductor@*", "from": "a", "on": 7, "text": "please look", "url": "u1"},
            {"id": 2, "reason": "to all", "from": "a", "on": 8, "text": "fyi", "url": "u2"},
            {"id": 3, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "held", "url": "u3"},
            {"id": 4, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "held", "url": "u4"},
            {"id": 5, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "out", "url": "u5"},
            {"id": 6, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "git unavailable", "url": "u6"},
        ]
        payloads = {3: {"kind": "train", "event": "held", "hold": "no-review"},
                    4: {"kind": "train", "event": "held", "hold": "ci-red"},
                    5: {"kind": "train", "event": "ejected"},
                    6: {"kind": "train", "event": "held", "hold": "merge-check"}}
        wakes = {r["key"]: r["wake"] for r in conductor.message_reasons(items, payloads)}
        self.assertEqual(wakes, {"comment:1": True, "comment:2": False, "comment:3": False,
                                 "comment:4": True, "comment:5": True, "comment:6": True})

    def test_only_the_newest_completed_run_counts(self):
        red = {"id": 2, "status": "completed", "conclusion": "failure", "name": "CI", "event": "push", "head_sha": "abc"}
        green = {"id": 3, "status": "completed", "conclusion": "success"}
        running = {"id": 4, "status": "in_progress", "conclusion": None}
        self.assertEqual([r["key"] for r in conductor.run_reasons([red, running], "main-red", "CI")], ["main-red:2:1"])
        self.assertEqual(conductor.run_reasons([red, green], "main-red", "CI"), [])

    def test_a_runner_counts_after_ten_minutes_offline(self):
        state = {}
        runners = [{"name": "Tycho", "status": "offline"}]
        self.assertEqual(conductor.runner_reasons(runners, state, NOW), [])
        self.assertEqual(len(conductor.runner_reasons(runners, state, at(11))), 1)
        conductor.runner_reasons([{"name": "Tycho", "status": "online"}], state, at(12))
        self.assertEqual(state["runnerOffline"], {})

    def test_disk_has_a_margin_before_it_counts_as_recovered(self):
        config = make_config("/tmp")
        state = {}
        self.assertEqual(conductor.disk_reasons(22, state, config, NOW), [])
        self.assertEqual(len(conductor.disk_reasons(18, state, config, NOW)), 1)
        self.assertEqual(len(conductor.disk_reasons(22, state, config, at(5))), 1, "still inside the margin")
        self.assertEqual(conductor.disk_reasons(26, state, config, at(10)), [])

    def test_attempts_escalation_and_reminders(self):
        state = {}
        disk = conductor.reason("disk-low:T:x", "disk-low", "low")
        for _ in range(conductor.MAX_ATTEMPTS):
            todo, exhausted = conductor.pending([disk], state, NOW)
            self.assertEqual(todo, [disk])
            conductor.mark(state, todo, NOW, done=False)
        self.assertEqual(conductor.pending([disk], state, NOW), ([], [disk]))
        state = {}
        conductor.mark(state, [disk], NOW, done=True)
        self.assertEqual(conductor.pending([disk], state, at(60)), ([], []))
        self.assertEqual(conductor.pending([disk], state, at(25 * 60))[0], [disk], "a persisting condition comes back")

    def test_timeout_and_infrastructure_receipts_wake_and_go_to_alex(self):
        items = [{"id": n, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "t", "url": f"u{n}"}
                 for n in (1, 2, 3)]
        payloads = {1: {"kind": "train", "event": "timed-out"}, 2: {"kind": "train", "event": "infra"},
                    3: {"kind": "train", "event": "ejected"}}
        found = {r["key"]: (r["wake"], r["escalate"]) for r in conductor.message_reasons(items, payloads)}
        self.assertEqual(found, {"comment:1": (True, "train:timed-out"), "comment:2": (True, "train:infra"),
                                 "comment:3": (True, "")})

    def test_a_parked_runner_is_never_offline(self):
        state = {}
        routed = conductor.routing_labels({"MK4_RUNNER_MACOS": "tycho-macos", "MK4_RUNNER_MACOS_LIGHT": "Tycho-Light"})
        parked = runner("Eidolon-heavy", "offline", "self-hosted", "macOS", "eidolon")
        routed_offline = runner("Tycho-2", "offline", "self-hosted", "tycho-light")
        conductor.runner_reasons([parked, routed_offline], state, NOW, routed)
        found = conductor.runner_reasons([parked, routed_offline], state, at(11), routed)
        self.assertEqual([r["summary"].split()[1] for r in found], ["Tycho-2"], "labels match case-insensitively")
        unknown = {}
        conductor.runner_reasons([parked], unknown, NOW, None)
        self.assertEqual(len(conductor.runner_reasons([parked], unknown, at(11), None)), 1, "routing unknown: as before")
        self.assertIsNone(conductor.routing_labels({}), "no routing variable set: nothing is parked")

    def test_a_routing_label_no_online_runner_carries(self):
        state = {}
        variables = {"MK4_RUNNER_MACOS": "tycho-macos", "MK4_RUNNER_MACOS_LIGHT": "xcode-27",
                     "MK4_RUNNER_LINUX_ARM64": "tycho-linux"}
        runners = [runner("Tycho", "online", "self-hosted", "Tycho-macOS"), runner("Tycho-linux", "offline", "tycho-linux")]
        self.assertEqual(conductor.routing_reasons(variables, runners, state, NOW), [], "not before ten minutes")
        found = conductor.routing_reasons(variables, runners, state, at(11))
        self.assertEqual(sorted(r["key"].split(":")[1] for r in found),
                         ["MK4_RUNNER_LINUX_ARM64=tycho-linux", "MK4_RUNNER_MACOS_LIGHT=xcode-27"],
                         "an offline runner's labels do not count")
        self.assertTrue(all(r["kind"] == "routing" and not r["repair"] for r in found))
        variables["MK4_RUNNER_MACOS_LIGHT"] = "tycho-macos"
        conductor.routing_reasons(variables, runners, state, at(12))
        self.assertEqual(list(state["routingMissing"]), ["MK4_RUNNER_LINUX_ARM64=tycho-linux"], "fixed: forgotten")

    def test_control_jobs_queued_over_fifteen_minutes(self):
        state = {}
        queued = {"name": "CI verdict", "status": "queued", "labels": ["self-hosted", "inisa-control"],
                  "created_at": snapshot.iso(at(-16)), "html_url": "j/1"}
        self.assertEqual(conductor.control_queued_reasons([dict(queued, created_at=snapshot.iso(at(-14)))],
                                                          "inisa-control", state, NOW), [])
        self.assertEqual(conductor.control_queued_reasons([dict(queued, status="in_progress"),
                                                           dict(queued, labels=["tycho-light"])],
                                                          "inisa-control", state, NOW), [])
        found = conductor.control_queued_reasons([queued], "inisa-control", state, NOW)
        self.assertEqual([(r["kind"], r["url"], r["repair"]) for r in found], [("control-queued", "j/1", True)])
        self.assertEqual(conductor.control_queued_reasons([queued], "inisa-control", state, at(5))[0]["key"], found[0]["key"])
        conductor.control_queued_reasons([], "inisa-control", state, at(6))
        self.assertNotIn("controlQueuedSince", state)

    def test_a_runner_failing_fast_while_another_could_take_its_jobs(self):
        def failure(job_id, seconds=41, ended=-10, runner_name="Eidolon-heavy", labels=("tycho-macos",), run_id=None):
            """A failed job; each in its own run unless `run_id` says otherwise."""
            return {"id": job_id, "run_id": run_id or 600 + job_id, "name": f"job {job_id}", "conclusion": "failure",
                    "runner_name": runner_name, "labels": list(labels),
                    "started_at": snapshot.iso(at(ended) - dt.timedelta(seconds=seconds)),
                    "completed_at": snapshot.iso(at(ended))}

        runners = [runner("Tycho", "online", "self-hosted", "tycho-macos"),
                   runner("Eidolon-heavy", "online", "self-hosted", "tycho-macos")]
        three = [failure(1), failure(2), failure(3)]
        state = {}
        found = conductor.poisoned_runners(three, runners, state, NOW)
        self.assertEqual([alert["key"] for alert in found], [f"runner-poisoned:Eidolon-heavy:{snapshot.iso(NOW)}"])
        self.assertIn("Tycho", found[0]["text"])
        self.assertEqual(conductor.poisoned_runners(three, runners, state, at(5))[0]["key"], found[0]["key"], "one episode")
        across_runs = [dict(failure(n), name="Android") for n in (11, 12, 13)]
        self.assertEqual(len(conductor.poisoned_runners(across_runs, runners, {}, NOW)), 1,
                         "one job failing fast in three runs (Android on the laptop, 2026-10-08) counts three times")
        reruns = [dict(failure(n, run_id=601), name="Android") for n in (11, 12, 13)]
        for jobs, why in ((three[:2] + [three[0]], "the same job twice is one job"),
                          (reruns, "one job re-run three times within its run is one job"),
                          (three[:2] + [failure(3, seconds=95)], "a job that ran 95 s is not fast"),
                          (three[:2] + [failure(3, ended=-70)], "outside the hour"),
                          (three[:2] + [dict(failure(3), conclusion="cancelled")], "only failures")):
            with self.subTest(why):
                self.assertEqual(conductor.poisoned_runners(jobs, runners, {}, NOW), [])
        self.assertEqual(conductor.poisoned_runners(three, runners[1:], {}, NOW), [], "nowhere else to go")
        self.assertEqual(conductor.poisoned_runners(three, [dict(runners[0], status="offline")] + runners[1:], {}, NOW), [])
        conductor.poisoned_runners([], runners, state, at(70))
        self.assertEqual(state["runnerPoisoned"], {}, "recovered: a later episode is a new one")

    def test_when_the_train_is_asleep(self):
        ready = [{"pr": 7, "verdict": train.READY}]
        held = [{"pr": 8, "verdict": train.HOLD}]
        old = [{"status": "completed", "run_started_at": snapshot.iso(at(-25))}]
        batch = {"branch": "train/1", "tip": "t", "run": {"status": "completed"}}
        self.assertIn("1 ready PR(s)", conductor.train_asleep(ready + held, [], old, {}, NOW))
        self.assertIn("train/1", conductor.train_asleep(held, [batch], old, {}, NOW))
        self.assertIn("train/1", conductor.train_asleep([], [dict(batch, run=None)], [], {}, NOW), "a lost dispatch")
        for queue, batches, runs, state, why in (
                (held, [], old, {}, "nothing ready"),
                (ready, [], [{"status": "completed", "run_started_at": snapshot.iso(at(-19))}], {}, "a recent step"),
                (ready, [], old + [{"status": "queued", "created_at": snapshot.iso(at(-40))}], {}, "a step waits to start"),
                (ready, [dict(batch, run={"status": "in_progress"})], old, {}, "its CI wakes the train"),
                (ready, [], old, {"trainWokenAt": snapshot.iso(at(-5))}, "the watcher just woke it")):
            with self.subTest(why):
                self.assertIsNone(conductor.train_asleep(queue, batches, runs, state, NOW))

    def test_stuck_train_entries(self):
        pr = {"number": 7, "head": "a" * 40, "url": "u", "isDraft": True, "labels": ["train"]}
        snap = {"prs": [pr], "train": [{"pr": 7, "addedAt": snapshot.iso(at(-13 * 60)), "headAtAdd": "a" * 40}]}
        with mock.patch.object(conductor.train, "eligibility", return_value=(conductor.train.HOLD, "a draft")):
            self.assertEqual([r["key"] for r in conductor.stuck_reasons(snap, NOW)], [f"stuck:7:{'a' * 12}"])
            snap["train"][0]["addedAt"] = snapshot.iso(at(-60))
            self.assertEqual(conductor.stuck_reasons(snap, NOW), [])


class OutcomeTests(unittest.TestCase):
    def result(self, text, is_error=False):
        return json.dumps({"type": "result", "is_error": is_error, "result": text, "total_cost_usd": 0.04,
                           "usage": {"input_tokens": 18, "cache_read_input_tokens": 19319}})

    def test_success_and_its_summary(self):
        out = conductor.outcome(0, self.result("did things\nSUMMARY: answered #12"), "", NOW)
        self.assertEqual((out["ok"], out["summary"], out["escalate"], out["limit"]), (True, "answered #12", False, None))
        self.assertEqual(out["usage"]["cache_read_input_tokens"], 19319)

    def test_escalate(self):
        out = conductor.outcome(0, self.result("ESCALATE: main is red in the render lane"), "", NOW)
        self.assertTrue(out["escalate"])

    def test_a_refused_run_is_an_outage(self):
        out = conductor.outcome(1, self.result("You have reached your weekly usage limit", is_error=True), "", NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["limit"]["until"], dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC))
        out = conductor.outcome(1, "", "Claude AI usage limit reached|1790870400", NOW)
        self.assertIsNotNone(out["limit"])

    def test_a_good_run_that_mentions_limits_is_not_an_outage(self):
        out = conductor.outcome(0, self.result("SUMMARY: told #9 the usage limit resets Thursday"), "", NOW)
        self.assertIsNone(out["limit"])


# MARK: - The watcher, against a fake GitHub


def make_config(state_dir, agent="conductor@Tycho:conductor"):
    return conductor.Config(home=state_dir, state_dir=state_dir, claude="claude", agent=agent,
                            host=agent.split("@")[1].split(":")[0], update_checkout=False)


class FakeGitHub:
    repo, owner, name = "o/r", "o", "r"

    def __init__(self):
        self.issue_comments = []   # raw, on the conductor issue (900)
        self.repo_comments = []    # raw, anywhere (issues/comments)
        self.main_runs = []
        self.runners = [{"name": "Tycho", "status": "online"}]
        self.next_id = 1000
        self.calls = []
        self.dispatches = []
        self.dispatch_error = None
        self.variables = {}          # GET actions/variables; None answers 403
        self.train_runs = []         # merge-train.yml runs, newest first
        self.recent_runs = []        # GET actions/runs (every workflow)
        self.run_jobs = {}           # run id -> its jobs
        self.batch_refs = []         # train/* branches: (name, tip)
        self.batch_runs = {}         # train/* branch -> its ci.yml runs

    def transport(self):
        return "fake"

    def raw(self, body, minutes, comment_id=None):
        if comment_id is None:
            self.next_id += 1
            comment_id = self.next_id
        payload = marker.decode(body) or {}
        author = {"login": "o", "type": "User"} if payload.get("kind") == "conductor" else {"login": "alex-bridges"}
        return {"id": comment_id, "created_at": snapshot.iso(at(minutes)), "html_url": f"c/{comment_id}", "body": body,
                "issue_url": "https://api/repos/o/r/issues/900", "user": author}

    def post(self, number, text, payload, minutes):
        comment = self.raw(marker.compose(text, payload), minutes)
        comment["issue_url"] = f"https://api/repos/o/r/issues/{number}"
        (self.issue_comments if number == 900 else self.repo_comments).append(comment)
        return comment

    def rest(self, path, method="GET", body=None):
        self.calls.append((method, path))
        if "labels=coord:conductor" in path:
            return [{"number": 900}]
        if method == "POST" and path.endswith("issues/900/comments"):
            comment = self.raw(body["body"], self.clock_minutes)
            self.issue_comments.append(comment)
            return comment
        if method == "PATCH" and "/issues/comments/" in path:
            comment_id = int(path.rsplit("/", 1)[1])
            for comment in self.issue_comments:
                if comment["id"] == comment_id:
                    comment["body"] = body["body"]
                    return comment
            raise conductor.GitHubError("HTTP 404")
        if "ci.yml/runs?branch=main" in path:
            return {"workflow_runs": self.main_runs}
        if "merge-train.yml/runs" in path:
            return {"workflow_runs": self.train_runs}
        if path.endswith("actions/runners?per_page=100"):
            return {"runners": self.runners}
        if path.startswith("repos/{repo}/actions/variables"):
            if self.variables is None:
                raise conductor.GitHubError("HTTP 403: Resource not accessible by personal access token")
            return {"variables": [{"name": name, "value": value} for name, value in self.variables.items()]}
        if path == "repos/{repo}/actions/runs?per_page=100":
            return {"workflow_runs": self.recent_runs}
        if "/jobs?filter=all" in path:
            jobs = self.run_jobs.get(int(path.split("actions/runs/")[1].split("/")[0]), [])
            if isinstance(jobs, Exception):
                raise jobs
            return {"jobs": jobs}
        if path == "repos/{repo}/git/matching-refs/heads/train":
            return [{"ref": f"refs/heads/{name}", "object": {"sha": tip}} for name, tip in self.batch_refs]
        raise AssertionError(f"unexpected call {method} {path}")

    def dispatch_workflow(self, workflow, ref, inputs=None):
        if self.dispatch_error:
            raise self.dispatch_error
        self.dispatches.append((workflow, ref, inputs))

    def rest_paginated(self, path):
        self.calls.append(("GET", path))
        if "issues/900/comments" in path:
            return list(self.issue_comments)
        if path.startswith("repos/{repo}/issues/comments"):
            # Like GitHub, only comments at or after `since` (review of #1049: a fake that
            # returned everything hid a request the cursor had moved past).
            since = path.split("since=", 1)[1].split("&", 1)[0] if "since=" in path else ""
            return [c for c in self.repo_comments if not since or (c.get("updated_at") or c["created_at"]) >= since]
        if path.startswith("repos/{repo}/pulls/comments"):
            return []
        if "ci.yml/runs?branch=train" in path:  # a batch's gate runs (cli.train_batches), never main's
            branch = path.split("branch=", 1)[1].split("&", 1)[0].replace("%2F", "/")
            return [{"workflow_runs": self.batch_runs.get(branch, [])}]
        raise AssertionError(f"unexpected paginated call {path}")

    def records(self, kind, event=None):
        out = []
        for comment in self.issue_comments:
            payload = marker.decode(comment["body"])
            if payload and payload.get("kind") == kind and (event is None or payload.get("event") == event):
                out.append(payload)
        return out


def claude_result(text, code=0, is_error=False, stderr=""):
    stdout = json.dumps({"type": "result", "is_error": is_error, "result": text, "total_cost_usd": 0.05,
                         "usage": {"input_tokens": 10}, "session_id": "s"})
    return subprocess.CompletedProcess([], code, stdout, stderr)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"COORD_CACHE_DIR": os.path.join(self.tmp.name, "cache")})
        self.env.start()
        self.gh = FakeGitHub()
        self.config = make_config(os.path.join(self.tmp.name, "state"))
        self.runs = []
        self.replies = []
        self.snap = mock.patch.object(cli, "get_snapshot", return_value={"prs": [], "claims": [], "train": []})
        self.snap.start()

    def tearDown(self):
        self.snap.stop()
        self.env.stop()
        self.tmp.cleanup()

    def runner(self, cmd, cwd, env, timeout):
        self.runs.append({"model": cmd[cmd.index("--model") + 1], "prompt": cmd[2], "agent": env["COORD_AGENT"]})
        return self.replies.pop(0) if self.replies else claude_result("SUMMARY: done")

    def tick(self, minutes, config=None, **kwargs):
        self.gh.clock_minutes = minutes
        watcher = conductor.Watcher(config or self.config, self.gh, now=at(minutes), runner=self.runner,
                                    disk=lambda path: 100.0)
        return watcher.tick(**kwargs)

    def message(self, minutes, to="conductor@*", text="the train looks stuck"):
        self.gh.post(55, f"**From** `claude@E:x`\n\n{text}", {"kind": "msg", "v": 1, "agent": "claude@E:x", "to": [to]},
                     minutes)

    def test_an_idle_tick_beats_and_wakes_nobody(self):
        self.tick(0)
        self.assertEqual(self.runs, [])
        beats = self.gh.records("conductor", "beat")
        self.assertEqual(len(beats), 1)
        self.tick(5)
        self.assertEqual(len(self.gh.records("conductor", "beat")), 1, "the heartbeat is edited, not reposted")
        self.assertEqual(self.gh.records("conductor", "beat")[0]["beat"], snapshot.iso(at(5)))

    def test_a_message_wakes_the_triage_model_once(self):
        self.tick(0)
        self.message(1)
        self.tick(5)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["model"], conductor.TRIAGE_MODEL)
        self.assertIn("the train looks stuck", self.runs[0]["prompt"])
        self.assertEqual(self.runs[0]["agent"], "conductor@Tycho:conductor")
        self.tick(10)
        self.assertEqual(len(self.runs), 1, "handled")

    def test_a_broadcast_waits_for_the_next_wake(self):
        self.tick(0)
        self.message(1, to="all", text="rebasing main")
        self.tick(5)
        self.assertEqual(self.runs, [])
        self.message(6)
        self.tick(10)
        self.assertIn("rebasing main", self.runs[0]["prompt"])

    def test_red_main_wakes_the_repair_model_and_escalation_reruns(self):
        self.gh.main_runs = [{"id": 7, "status": "completed", "conclusion": "failure", "name": "CI", "event": "push",
                              "head_sha": "abc", "html_url": "r/7"}]
        self.tick(0)
        self.assertEqual([r["model"] for r in self.runs], [conductor.REPAIR_MODEL])
        self.runs.clear()
        self.message(1)
        self.replies = [claude_result("ESCALATE: this needs a code fix"), claude_result("SUMMARY: fixed")]
        self.tick(20)
        self.assertEqual([r["model"] for r in self.runs], [conductor.TRIAGE_MODEL, conductor.REPAIR_MODEL])
        self.assertIn("this needs a code fix", self.runs[1]["prompt"])

    def test_a_usage_limit_posts_an_outage_and_the_backlog_waits_for_the_reset(self):
        self.tick(0)
        self.message(1)
        self.replies = [claude_result("You've hit your limit · resets in 2h", code=1, is_error=True)]
        self.tick(5)
        outages = self.gh.records("outage", "start")
        self.assertEqual([o["until"] for o in outages], [snapshot.iso(at(125))])
        self.message(10, text="are you there")
        self.tick(30)
        self.assertEqual(len(self.runs), 1, "no run while down")
        self.tick(130)
        self.assertEqual(len(self.runs), 2)
        self.assertIn("the train looks stuck", self.runs[1]["prompt"])
        self.assertIn("are you there", self.runs[1]["prompt"])
        self.assertIn("out of usage", self.runs[1]["prompt"])
        self.assertEqual(len(self.gh.records("outage", "end")), 1)
        self.message(131)
        self.tick(135)
        self.assertEqual(len(self.gh.records("outage", "end")), 1, "the end is posted once")

    def test_a_standby_leaves_github_to_the_leader(self):
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "v": 1, "event": "beat", "agent": "conductor@Old:c",
                                                     "beat": snapshot.iso(at(-1))}), -600, comment_id=1)
        self.gh.issue_comments.append(leader)
        self.message(-1)
        self.tick(0)
        self.assertEqual(self.runs, [])
        self.assertFalse(any("issues/comments?since" in path for _, path in self.gh.calls), "no inbox read")
        self.assertIn("standby", self.gh.issue_comments[-1]["body"])

    def test_outage_handoff_notifies_once_per_new_reason_without_consuming_mail(self):
        self.tick(0)
        self.message(1)
        cursor = cache.load_cursor(self.gh.repo, self.config.agent)
        self.gh.post(900, "Claude unavailable", {"kind": "outage", "v": 1, "event": "start",
                     "agent": self.config.agent, "harness": "claude", "until": snapshot.iso(at(120))}, 2)
        self.tick(5)
        self.tick(10)
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1)
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(cache.load_cursor(self.gh.repo, self.config.agent), cursor)
        self.assertEqual(conductor.load_state(self.config).get("handled"), {})
        self.message(11, text="new failed job")
        self.tick(15, wake=False)
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 2)
        alerts = [c["body"] for c in self.gh.issue_comments
                  if (marker.decode(c["body"]) or {}).get("event") == "escalate"]
        self.assertIn("new failed job", alerts[-1])
        self.assertNotIn("the train looks stuck", alerts[-1])
        self.tick(125)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("the train looks stuck", self.runs[0]["prompt"])
        self.assertIn("new failed job", self.runs[0]["prompt"])

    def test_limit_hit_hands_off_in_the_same_tick(self):
        self.tick(0)
        self.message(1)
        self.replies = [claude_result("You've hit your limit · resets in 2h", code=1, is_error=True)]
        self.tick(5)
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1)
        self.tick(10)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1)

    def test_outage_standby_does_not_handoff_the_leaders_work(self):
        self.gh.post(900, "leader", {"kind": "conductor", "v": 1, "event": "beat",
                     "agent": "conductor@Old:c", "beat": snapshot.iso(at(-1))}, -600)
        self.gh.post(900, "Claude unavailable", {"kind": "outage", "v": 1, "event": "start",
                     "agent": self.config.agent, "harness": "claude", "until": snapshot.iso(at(120))}, -1)
        self.message(-1)
        self.tick(0)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.gh.records("conductor", "escalate"), [])

    def test_state_loss_reuses_only_its_own_heartbeat_comment(self):
        payload = {"kind": "conductor", "v": 1, "event": "beat", "agent": self.config.agent,
                   "beat": snapshot.iso(at(-1))}
        forged = self.gh.raw(marker.compose("beat", payload), -2, comment_id=1)
        forged["user"] = {"login": "mallory", "type": "User"}
        own = self.gh.raw(marker.compose("beat", payload), -1, comment_id=2)
        self.gh.issue_comments = [forged, own]
        watcher = conductor.Watcher(self.config, self.gh, now=NOW)
        watcher.records = conductor.coord_comments(self.gh.issue_comments)
        watcher.issue = 900
        watcher.state["beatComment"] = 1
        watcher.beat(False, {})
        self.assertIn(("PATCH", "repos/{repo}/issues/comments/2"), self.gh.calls)
        self.assertNotIn(("PATCH", "repos/{repo}/issues/comments/1"), self.gh.calls)

    def test_forged_saved_beat_id_posts_once_then_reuses_the_new_comment(self):
        payload = {"kind": "conductor", "v": 1, "event": "beat", "agent": self.config.agent,
                   "beat": snapshot.iso(at(-1))}
        forged = self.gh.raw(marker.compose("beat", payload), -1, comment_id=1)
        forged["user"] = {"login": "mallory", "type": "User"}
        self.gh.issue_comments = [forged]
        self.gh.clock_minutes = 0
        watcher = conductor.Watcher(self.config, self.gh, now=NOW)
        watcher.records = conductor.coord_comments(self.gh.issue_comments)
        watcher.issue = 900
        watcher.state["beatComment"] = 1
        watcher.beat(False, {})
        watcher.beat(True, {})
        self.assertEqual([method for method, _ in self.gh.calls], ["POST", "PATCH"])
        self.assertEqual(self.gh.calls[1][1], "repos/{repo}/issues/comments/1001")
        self.assertEqual(len(self.gh.issue_comments), 2)

    def test_a_standby_takes_over_when_the_leader_goes_quiet(self):
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "v": 1, "event": "beat", "agent": "conductor@Old:c",
                                                     "leader": True, "beat": snapshot.iso(at(-30)),
                                                     "seenThrough": snapshot.iso(at(-40))}), -600, comment_id=1)
        self.gh.issue_comments.append(leader)
        self.message(-35, text="already handled by the old leader")
        self.message(-5, text="new since then")
        self.tick(0)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("new since then", self.runs[0]["prompt"])
        self.assertIn("already handled by the old leader", self.runs[0]["prompt"],
                      "the adopted cursor is 40 min old: a 35-min-old message is after it")

    def test_a_pause_and_the_daily_cap_hold_wakes(self):
        self.tick(0)
        self.gh.post(900, "paused", {"kind": "conductor", "v": 1, "event": "pause", "agent": "claude@E:x",
                                     "until": snapshot.iso(at(60))}, 1)
        self.message(2)
        lines = self.tick(5)
        self.assertEqual(self.runs, [])
        self.assertTrue(any("paused" in line for line in lines))
        capped = make_config(self.config.state_dir)
        capped.max_wakes_per_day = 0
        self.tick(70, config=capped)
        self.tick(75, config=capped)
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1, "the cap is escalated once")

    def test_three_failed_runs_escalate_to_alex(self):
        self.tick(0)
        self.message(1)
        self.replies = [claude_result("boom", code=1, is_error=True)] * 3
        for minutes in (5, 10, 15, 20):
            self.tick(minutes)
        self.assertEqual(len(self.runs), 3)
        escalations = self.gh.records("conductor", "escalate")
        self.assertEqual(len(escalations), 1)
        self.assertEqual([(w, ref, inputs["issue"]) for w, ref, inputs in self.gh.dispatches],
                         [("notify-alex.yml", "main", "900")], "the escalation reaches Alex's phone")

    def test_an_escalation_stands_when_notify_alex_cannot_be_dispatched(self):
        self.gh.dispatch_error = conductor.GitHubError("HTTP 404 (not on main yet)")
        self.tick(0)
        self.message(1)
        self.replies = [claude_result("boom", code=1, is_error=True)] * 3
        for minutes in (5, 10, 15, 20):
            lines = self.tick(minutes)
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1)
        self.assertTrue(any("board only" in line for line in lines))

    def test_1_a_standby_run_is_told_to_leave_the_inbox_and_its_cursor_is_not_adopted(self):
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "v": 1, "event": "beat", "agent": "conductor@Old:c",
                                                     "leader": True, "beat": snapshot.iso(at(-1))}), -600, comment_id=1)
        self.gh.issue_comments.append(leader)
        watcher = conductor.Watcher(self.config, self.gh, now=at(0), runner=self.runner, disk=lambda path: 5.0)
        self.gh.clock_minutes = 0
        watcher.tick()
        self.assertEqual(len(self.runs), 1)
        self.assertIn("STANDBY", self.runs[0]["prompt"])
        self.assertIn('"leader":false', self.gh.issue_comments[-1]["body"].replace(" ", ""))

    def test_1_a_leader_ignores_a_standbys_newer_cursor(self):
        self.tick(0)
        mine = cli.cache.load_cursor("o/r", self.config.agent)
        standby = self.gh.raw(marker.compose("beat", {"kind": "conductor", "v": 1, "event": "beat", "agent": "conductor@B:c",
                                                      "leader": False, "beat": snapshot.iso(at(4)),
                                                      "seenThrough": snapshot.iso(at(4))}), 1)
        self.gh.issue_comments.append(standby)
        self.tick(5)
        self.assertEqual(cli.cache.load_cursor("o/r", self.config.agent), mine)

    def test_6_the_inbox_is_read_back_at_most_48_hours(self):
        self.tick(0)
        cli.cache.store_cursor("o/r", self.config.agent, snapshot.iso(at(-5 * 24 * 60)), [])
        self.tick(5)
        since = [p for _, p in self.gh.calls if p.startswith("repos/{repo}/issues/comments?since=")][-1]
        self.assertGreaterEqual(since.split("since=")[1].split("&")[0], snapshot.iso(at(5 - 48 * 60)))

    def test_7_repeated_unknown_limits_edit_one_outage_record(self):
        self.tick(0)
        self.message(1)
        self.replies = [claude_result("5-hour usage limit reached", code=1, is_error=True)] * 2
        self.tick(5)
        self.tick(70)
        starts = self.gh.records("outage", "start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["until"], snapshot.iso(at(130)))

    def test_a_modified_guard_in_the_checkout_stops_wakes_and_is_escalated_once(self):
        self.tick(0)
        self.message(1)
        dirty = lambda args: (0, " M .claude/hooks/conductor-guard.py\n") if args[0] == "status" else (0, "")
        for minutes in (5, 10):
            self.gh.clock_minutes = minutes
            conductor.Watcher(self.config, self.gh, now=at(minutes), runner=self.runner, disk=lambda p: 100.0,
                              git=dirty).tick()
        self.assertEqual(self.runs, [])
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 1)

    def test_a_standbys_run_beats_as_standby(self):
        watcher = conductor.Watcher(self.config, self.gh, now=at(0), runner=self.runner, disk=lambda p: 100.0)
        watcher.state, watcher.issue = {}, 900
        self.gh.clock_minutes = 0
        stop = conductor.threading.Event()
        with mock.patch.object(conductor, "BEAT_DURING_RUN", dt.timedelta(seconds=0.01)):
            thread = conductor.threading.Thread(target=watcher._beat_while_running, args=(stop, {}, False))
            thread.start()
            conductor.threading.Event().wait(0.1)
            stop.set()
            thread.join()
        self.assertEqual([b["leader"] for b in self.gh.records("conductor", "beat")], [False])

    def receipt(self, minutes, event):
        self.gh.post(12, f"Merge train: `train/x` {event} again on attempt 4", {
            "kind": "train", "v": 1, "agent": "train@actions", "event": event, "pr": 12, "batch": "train/x",
            "to": ["conductor@*"]}, minutes)

    def test_timeout_and_infrastructure_receipts_wake_and_reach_alex_the_first_time(self):
        self.tick(0)
        self.receipt(1, "infra")
        self.tick(5)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("`train/x` infra again on attempt 4", self.runs[0]["prompt"])
        escalations = self.gh.records("conductor", "escalate")
        self.assertEqual(len(escalations), 1)
        self.assertIn("notify-alex.yml", [workflow for workflow, _, _ in self.gh.dispatches])
        self.receipt(6, "infra")
        self.receipt(7, "timed-out")
        self.tick(10)
        self.assertEqual(len(self.runs), 2, "each receipt still wakes the conductor")
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 2, "Alex hears once per kind: the timeout")
        self.receipt(25 * 60, "infra")
        self.tick(25 * 60 + 5)
        self.assertEqual(len(self.gh.records("conductor", "escalate")), 3, "and again a day later")

    def asleep(self):
        """A batch whose CI finished and no merge-train step for an hour: the train is asleep."""
        tip = "f" * 40
        self.gh.batch_refs = [("train/20261008-010101", tip)]
        self.gh.batch_runs = {"train/20261008-010101": [
            {"id": 3, "head_sha": tip, "event": "workflow_dispatch", "actor": {"login": "github-actions[bot]"},
             "status": "completed", "conclusion": "success", "run_attempt": 1, "html_url": "r/3"}]}
        self.gh.train_runs = [{"id": 40, "status": "completed", "conclusion": "success",
                               "run_started_at": snapshot.iso(at(-60)), "created_at": snapshot.iso(at(-60))}]

    def test_an_asleep_train_is_dispatched_once(self):
        self.asleep()
        lines = self.tick(0)
        self.assertEqual(self.gh.dispatches, [("merge-train.yml", "main", None)])
        self.assertEqual(self.runs, [], "an action, not a wake")
        self.assertTrue(any("dispatched merge-train.yml" in line for line in lines), lines)
        self.tick(16)
        self.assertEqual(len(self.gh.dispatches), 1, "not again within twenty minutes")
        self.gh.train_runs.insert(0, {"id": 41, "status": "completed", "run_started_at": snapshot.iso(at(25))})
        self.tick(32)
        self.assertEqual(len(self.gh.dispatches), 1, "a step that started recently")
        self.gh.batch_runs["train/20261008-010101"][0].update(status="in_progress", conclusion=None)
        self.tick(70)
        self.assertEqual(len(self.gh.dispatches), 1, "a batch's running CI wakes the train itself")

    def test_a_dry_run_never_wakes_the_train(self):
        self.asleep()
        lines = self.tick(0, dry_run=True)
        self.assertEqual(self.gh.dispatches, [])
        self.assertTrue(any("would dispatch merge-train.yml" in line for line in lines), lines)

    def test_a_parked_runner_raises_nothing(self):
        self.gh.variables = {"MK4_RUNNER_MACOS": "tycho-macos"}
        self.gh.runners = [runner("Tycho", "online", "self-hosted", "tycho-macos"),
                           runner("Eidolon-heavy", "offline", "self-hosted", "macOS", "eidolon")]
        for minutes in (0, 15, 30):
            self.tick(minutes)
        self.assertEqual(self.runs, [])

    def test_a_routing_label_on_no_online_runner_wakes_the_conductor(self):
        self.gh.variables = {"MK4_RUNNER_MACOS": "tycho-macos", "MK4_RUNNER_MACOS_LIGHT": "xcode-27"}
        self.gh.runners = [runner("Tycho", "online", "self-hosted", "tycho-macos")]
        self.tick(0)
        self.assertEqual(self.runs, [])
        self.tick(15)
        self.assertEqual([r["model"] for r in self.runs], [conductor.TRIAGE_MODEL])
        self.assertIn("[routing] repository variable MK4_RUNNER_MACOS_LIGHT routes jobs to the runner label `xcode-27`",
                      self.runs[0]["prompt"])

    def test_unreadable_variables_skip_the_routing_checks_with_one_note(self):
        self.gh.variables = None
        self.gh.runners = [runner("Eidolon-heavy", "offline", "self-hosted", "eidolon")]
        first = self.tick(0)
        self.assertEqual(sum("cannot read the repository variables" in line for line in first), 1)
        later = self.tick(15)
        self.assertFalse(any("cannot read the repository variables" in line for line in later))
        self.assertEqual([r["model"] for r in self.runs], [conductor.REPAIR_MODEL], "unknown routing: offline counts")
        self.assertIn("[runner-offline]", self.runs[0]["prompt"])

    def test_control_jobs_queued_wake_the_repair_model(self):
        self.gh.variables = {"MK4_RUNNER_CONTROL": "inisa-control"}
        self.gh.runners = [runner("Tycho-control-1", "online", "self-hosted", "inisa-control")]
        control = {"id": 1, "name": "CI verdict", "status": "queued", "labels": ["self-hosted", "inisa-control"],
                   "created_at": snapshot.iso(at(-20)), "html_url": "j/1"}
        self.gh.recent_runs = [
            {"id": 501, "status": "in_progress", "event": "workflow_dispatch", "path": ".github/workflows/ci.yml",
             "created_at": snapshot.iso(at(-30)), "updated_at": snapshot.iso(at(-20))},
            {"id": 502, "status": "queued", "event": "pull_request", "path": ".github/workflows/ci.yml",
             "created_at": snapshot.iso(at(-30)), "updated_at": snapshot.iso(at(-30))}]
        self.gh.run_jobs = {501: [control], 502: [dict(control, id=2)]}
        self.tick(0)
        self.assertEqual([r["model"] for r in self.runs], [conductor.REPAIR_MODEL])
        self.assertIn("[control-queued] 1 job(s) for the control runners (`inisa-control`)", self.runs[0]["prompt"])
        self.assertNotIn(("GET", "repos/{repo}/actions/runs/502/jobs?filter=all&per_page=100"), self.gh.calls,
                         "pull-request CI never targets the control runners")

    def fast_android_failures(self, runs):
        """Android failing in 41 s on Eidolon-heavy, once per (run id, attempt) in `runs`, while
        Tycho, online, carries the same label."""
        self.gh.variables = {"MK4_RUNNER_MACOS": "tycho-macos"}
        self.gh.runners = [runner("Tycho", "online", "self-hosted", "tycho-macos"),
                           runner("Eidolon-heavy", "online", "self-hosted", "tycho-macos")]
        self.gh.recent_runs = [{"id": run_id, "status": "completed", "conclusion": "failure", "event": "workflow_dispatch",
                                "path": ".github/workflows/ci.yml", "created_at": snapshot.iso(at(-50)),
                                "updated_at": snapshot.iso(at(-5))} for run_id in sorted({run_id for run_id, _ in runs})]
        self.gh.run_jobs = {}
        for n, (run_id, attempt) in enumerate(runs, 1):
            self.gh.run_jobs.setdefault(run_id, []).append(
                {"id": n, "run_id": run_id, "run_attempt": attempt, "name": "Android / Android arm64 tests + APK + ELF closure",
                 "conclusion": "failure", "runner_name": "Eidolon-heavy", "labels": ["tycho-macos"],
                 "started_at": snapshot.iso(at(-10 * n)), "completed_at": snapshot.iso(at(-10 * n) + dt.timedelta(seconds=41))})

    def test_a_poisoned_runner_goes_to_alex_once_and_wakes_nobody(self):
        # As on 2026-10-08: the same job failing fast on the laptop in three different batches.
        self.fast_android_failures([(601, 1), (602, 1), (603, 1)])
        self.tick(0)
        self.tick(15)
        self.assertEqual(self.runs, [])
        escalations = [c for c in self.gh.issue_comments if (marker.decode(c["body"]) or {}).get("event") == "escalate"]
        self.assertEqual(len(escalations), 1)
        self.assertIn("Runner Eidolon-heavy may be poisoned: it failed 3 jobs", escalations[0]["body"])

    def test_one_job_rerun_three_times_on_one_runner_is_not_poisoning(self):
        self.fast_android_failures([(601, 1), (601, 2), (601, 3)])
        self.tick(0)
        self.assertEqual(self.gh.records("conductor", "escalate"), [])

    def test_a_failed_new_read_skips_its_check_not_the_slow_tick(self):
        self.gh.variables = {"MK4_RUNNER_MACOS": "tycho-macos", "MK4_RUNNER_MACOS_LIGHT": "xcode-27"}
        self.gh.runners = [runner("Tycho", "online", "self-hosted", "tycho-macos")]
        self.gh.recent_runs = [{"id": 701, "status": "in_progress", "event": "workflow_dispatch",
                                "path": ".github/workflows/ci.yml", "created_at": snapshot.iso(at(-30)),
                                "updated_at": snapshot.iso(at(-1))}]
        self.gh.run_jobs = {701: conductor.GitHubError("HTTP 404 GET actions/runs/701/jobs: Not Found")}
        self.asleep()
        lines = self.tick(0)
        self.assertEqual(sum("could not read recent Actions jobs" in line for line in lines), 1, lines)
        self.assertEqual(self.gh.dispatches, [("merge-train.yml", "main", None)], "the other checks still ran")
        state = conductor.load_state(self.config)
        self.assertEqual(state.get("lastSlow"), snapshot.iso(at(0)), "the slow tick counts as done")
        self.assertIn("MK4_RUNNER_MACOS_LIGHT=xcode-27", state.get("routingMissing") or {}, "the routing check ran")
        self.gh.calls.clear()
        self.tick(5)
        self.assertFalse(any("actions/runs?per_page=100" in path for _, path in self.gh.calls),
                         "the next fast tick does not repeat the slow reads")
        self.tick(15)
        self.assertEqual([r["model"] for r in self.runs], [conductor.TRIAGE_MODEL])
        self.assertIn("[routing]", self.runs[0]["prompt"], "its reason arrives on schedule")

    def test_unreadable_train_branches_skip_only_the_asleep_check(self):
        self.asleep()
        original = self.gh.rest

        def rest(path, method="GET", body=None):
            if "matching-refs" in path:
                raise conductor.GitHubError("HTTP 502 Bad Gateway")
            return original(path, method, body)

        self.gh.rest = rest
        lines = self.tick(0)
        self.assertTrue(any("could not read the train/* branches" in line for line in lines), lines)
        self.assertEqual(self.gh.dispatches, [])
        self.assertEqual(conductor.load_state(self.config).get("lastSlow"), snapshot.iso(at(0)))

    def test_dry_run_writes_nothing(self):
        self.message(-1)
        self.tick(0, dry_run=True)
        self.assertEqual(self.runs, [])
        self.assertFalse(any(method != "GET" for method, _ in self.gh.calls))
        self.assertFalse(os.path.exists(conductor.state_path(self.config)))


class BoardTests(unittest.TestCase):
    def test_the_board_shows_an_outage_and_the_conductor(self):
        windows = outage.windows([record(1, -10, {"kind": "outage", "event": "start", "harness": "claude",
                                                  "until": snapshot.iso(at(120)), "reason": "weekly usage limit"})])
        beat = record(2, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@Tycho:conductor",
                                "host": "Tycho", "beat": snapshot.iso(at(-2)), "disk": 27,
                                "lastWake": {"at": snapshot.iso(at(-30)), "summary": "answered #12"}})
        snap = {"repo": "o/r", "main": {"sha": "m" * 40}, "generatedAt": snapshot.iso(NOW), "prs": [], "claims": [],
                "overlaps": [], "runs": {"running": [], "queued": []}, "train": [],
                "outages": [outage.serialize(w) for w in windows],
                "conductor": {"hosts": lease.hosts([beat], NOW, REPO), "pause": None, "escalations": []}}
        text = render.board(snap, "codex@E:x", 0)
        self.assertIn("OUTAGE  claude agents are down until", text)
        self.assertIn("conductor@Tycho:conductor (leader) · beat 2m ago · disk 27 GB free · last wake 30m ago: answered #12",
                      text)
        brief = render.brief(snap, "codex@E:x")
        self.assertIn("conductor down (outage)", brief)

    def test_an_old_cached_board_without_the_new_keys_still_renders(self):
        snap = {"repo": "o/r", "main": {}, "generatedAt": snapshot.iso(NOW), "prs": [], "claims": [], "overlaps": [],
                "runs": {"running": [], "queued": []}, "train": []}
        self.assertIn("no watcher running", render.board(snap, "me", 0))
        self.assertNotIn("conductor", render.brief(snap, "me").split("\n")[0])

    def test_the_board_shows_a_stalled_reviewer_backlog(self):
        """The reviewer piling up requests with the app unavailable (#1092) shows on the
        board, not only in the watcher log only this host can read."""
        beat = record(2, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@Tycho:conductor",
                                "host": "Tycho", "beat": snapshot.iso(at(-1)),
                                "reviewer": {"waiting": 19, "appAvailable": False}})
        snap = {"repo": "o/r", "main": {}, "generatedAt": snapshot.iso(NOW), "prs": [], "claims": [], "overlaps": [],
                "runs": {"running": [], "queued": []}, "train": [], "outages": [],
                "conductor": {"hosts": lease.hosts([beat], NOW, REPO), "pause": None, "escalations": []}}
        text = render.board(snap, "codex@E:x", 0)
        self.assertIn("reviewer off (app not available on Tycho), 19 waiting", text)

    def test_the_board_shows_a_running_reviewer(self):
        beat = record(2, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@Tycho:conductor",
                                "host": "Tycho", "beat": snapshot.iso(at(-1)),
                                "reviewer": {"waiting": 1, "appAvailable": True}})
        snap = {"repo": "o/r", "main": {}, "generatedAt": snapshot.iso(NOW), "prs": [], "claims": [], "overlaps": [],
                "runs": {"running": [], "queued": []}, "train": [], "outages": [],
                "conductor": {"hosts": lease.hosts([beat], NOW, REPO), "pause": None, "escalations": []}}
        self.assertIn("reviewer on, 1 waiting", render.board(snap, "codex@E:x", 0))


class ReviewRegressionTests(unittest.TestCase):
    """The adversarial review of 7635f365 (Fable 5.1), one test per finding."""

    def test_2_a_far_future_time_in_the_output_is_not_the_reset(self):
        weekly = dt.datetime(2026, 10, 1, 16, 0, tzinfo=UTC)
        table = outage.parse_limit("usage limit reached\n| run | 9999999999 |", NOW)
        self.assertLessEqual(table["until"], weekly, "the epoch is on another line")
        same_line = outage.parse_limit("usage limit reached; resets 2099-01-01T00:00Z", NOW)
        self.assertEqual(same_line["until"], weekly, "clamped to the weekly reset")

    def test_3_malformed_records_do_not_crash_the_board(self):
        beat = record(2, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@T:c", "beat": snapshot.iso(at(-1)),
                                "lastWake": "oops", "disk": "full", "pending": [1]})
        odd_wake = record(3, -600, {"kind": "conductor", "event": "beat", "agent": "conductor@U:c",
                                    "beat": snapshot.iso(at(-1)), "lastWake": {"at": 123}})
        pause = record(4, -5, {"kind": "conductor", "event": "pause", "until": snapshot.iso(at(60)), "note": 5})
        down = record(5, -5, {"kind": "outage", "event": "start", "harness": "claude", "until": snapshot.iso(at(60)),
                              "reason": ["x"]})
        comments = [beat, odd_wake, pause, down]
        snap = {"repo": "o/r", "main": {}, "generatedAt": snapshot.iso(NOW), "prs": [], "claims": [], "overlaps": [],
                "runs": {"running": [], "queued": []}, "train": [],
                "outages": [outage.serialize(w) for w in outage.windows(comments)],
                "conductor": {"hosts": lease.hosts(comments, NOW, REPO), "pause": lease.pause(comments, NOW, REPO),
                              "escalations": lease.escalations(comments, NOW, REPO)}}
        text = render.board(json.loads(json.dumps(snap)), "me", 0)
        self.assertIn("PAUSED", text)
        self.assertIn("OUTAGE", text)

    def test_4_a_rerun_that_fails_again_is_a_new_reason(self):
        first = {"id": 9, "run_attempt": 1, "status": "completed", "conclusion": "failure"}
        second = dict(first, run_attempt=2)
        self.assertNotEqual(conductor.run_reasons([first], "main-red", "CI")[0]["key"],
                            conductor.run_reasons([second], "main-red", "CI")[0]["key"])

    def test_5_the_conductors_own_controls_wait_for_alex(self):
        pr = {"number": 7, "base": "main", "head": "h" * 40, "labels": [], "files": [".claude/agents/conductor.md"],
              "codex": {"approved": True}, "ci": {}}
        verdict, hold, reason = train.assess(pr)
        self.assertEqual((verdict, hold), (train.HOLD, train.HOLD_NEEDS_ALEX))
        approved = dict(pr, labels=[train.ALEX_APPROVED_LABEL], alexApprovedHead="h" * 40)
        self.assertEqual(train.assess(approved)[0], train.READY)
        self.assertEqual(train.assess(dict(pr, files=["Docs/agent-coordination.md"]))[0], train.READY)
        for guarded in ("Tools/coord/coordlib/cli.py", "CLAUDE.md", ".claude/hooks/conductor-guard.py"):
            self.assertEqual(train.assess(dict(pr, files=[guarded]))[1], train.HOLD_NEEDS_ALEX, guarded)
        self.assertEqual(train.assess(dict(pr, files=["Apps/AppAlpha/CLAUDE.md"]))[0], train.READY,
                         "only the root CLAUDE.md is loaded into a conductor run")

    def test_5_the_run_is_denied_the_dangerous_commands(self):
        command = conductor.model_command(make_config("/tmp"), "p", "sonnet")
        settings = json.loads(command[command.index("--settings") + 1])
        for rule in ("Bash(gh pr merge:*)", "Bash(git push --force:*)", "Bash(Tools/coord/coord train approve:*)"):
            self.assertIn(rule, settings["permissions"]["deny"])

    def test_10_a_timeout_stops_the_whole_process_group(self):
        started = dt.datetime.now()
        done = conductor.default_runner(["/bin/sh", "-c", "sleep 30 & sleep 30"], "/tmp", dict(os.environ), 1)
        self.assertEqual(done.returncode, 124)
        self.assertLess((dt.datetime.now() - started).total_seconds(), 10)


GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".claude", "hooks", "conductor-guard.py")


class GuardHookTests(unittest.TestCase):
    """.claude/hooks/conductor-guard.py: the substitute reviews of 7afafab1 and 4d3b200b."""

    def run_guard(self, tool, data, agent="conductor@Tycho:conductor", cwd="/tmp/home", project="/tmp/home", raw=None,
                  full=False):
        env = {k: v for k, v in os.environ.items() if k not in ("COORD_AGENT", "CONDUCTOR_WAKE", "CLAUDE_PROJECT_DIR")}
        env["HOME"] = os.path.expanduser("~")
        if project:
            env["CLAUDE_PROJECT_DIR"] = project
        if agent:
            env["COORD_AGENT"] = agent
        payload = raw if raw is not None else json.dumps({"tool_name": tool, "tool_input": data, "cwd": cwd})
        result = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env)
        return result if full else result.returncode

    def test_moving_main_merging_and_approving_are_blocked(self):
        for command in ("gh api -X PUT repos/example-owner/example-repo/pulls/1/merge", "git push origin --force --dry-run HEAD",
                        "./Tools/coord/coord train approve 1 --note x", "Tools/coord/coord train approve 1",
                        "git push origin +HEAD:main", "git push origin HEAD:main", "git push -uf origin x",
                        "gh pr merge 5 --squash", "gh pr -R example-owner/example-repo merge 5", "gh pr --repo o/r merge 5",
                        "bash -c 'gh pr merge 5'", "sh -c \"git push origin main\"", "eval 'coord train approve 3'",
                        "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=x", "gh api repos/o/r/merges -f base=main",
                        "gh variable set X", "gh secret list", "sudo rm -rf /", "launchctl list", "cd x && sudo ls",
                        "gh auth token", "gh workflow disable ci.yml"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_ordinary_work_and_prose_are_allowed(self):
        for command in ("git push -u origin conductor/fix-cache", "git push origin --delete conductor/old",
                        "gh pr create --fill", "Tools/coord/coord train add 5", "gh run rerun 123 --failed",
                        "git push origin conductor/main-red-fix", "Tools/coord/coord inbox > /tmp/inbox.txt",
                        "gh pr view 5 --json mergeable,mergedAt", "gh api repos/o/r/git/ref/heads/main",
                        "gh api repos/o/r/pulls/5/merge", "git commit -m 'docs: never push main by hand'",
                        "Tools/coord/coord conductor escalate 'PR 7 needs you: run coord train approve 7'",
                        "gh pr comment 7 -b 'waiting for coord train approve 7'", "grep -rn 'train approve' Docs/",
                        "grep -n sudo Tools/coord/conductor/install.sh", "cat .claude/agents/conductor.md",
                        "cat .claude/hooks/README.md 2>/dev/null", "git diff origin/main -- .claude/settings.json",
                        "cat ~/.claude/settings.json", "cd ../conductor-fix && sed -i '' s/a/b/ .claude/hooks/conductor-guard.py",
                        "python3 -m unittest discover -s Tools/coord/tests"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_its_own_controls_cannot_be_written_but_a_worktrees_can(self):
        for path in ("/tmp/home/.claude/settings.json", "/tmp/home/.claude/agents/conductor.md",
                     "/tmp/home/Tools/coord/coordlib/conductor.py", "/tmp/home/CLAUDE.md", "Tools/coord/coord",
                     "~/.claude/settings.json", "~/.claude/hooks/x.sh"):
            self.assertEqual(self.run_guard("Write", {"file_path": path}), 2, path)
        for path in ("/tmp/wt/.claude/hooks/conductor-guard.py", "/tmp/wt/Tools/coord/coordlib/conductor.py",
                     "/tmp/home/Docs/agent-coordination.md", "~/.claude/projects/x/memory/MEMORY.md"):
            self.assertEqual(self.run_guard("Edit", {"file_path": path}), 0, path)
        for command in ("sed -i '' s/a/b/ .claude/hooks/conductor-guard.py", "echo x > CLAUDE.md",
                        "cp /tmp/x /tmp/home/Tools/coord/coordlib/conductor.py", "rm -rf Tools/coord",
                        "echo {} > ~/.claude/settings.json"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_silent_outside_the_conductor_and_closed_inside_it(self):
        self.assertEqual(self.run_guard("Bash", {"command": "gh pr merge 5"}, agent=""), 0)
        self.assertEqual(self.run_guard("Bash", {}, raw="not json"), 2)
        self.assertEqual(self.run_guard("Bash", {}, raw=json.dumps({"tool_name": "Bash", "tool_input": []})), 0,
                         "no command is nothing to run")
        self.assertEqual(self.run_guard("Write", {"file_path": "CLAUDE.md"}, project=None), 2,
                         "without CLAUDE_PROJECT_DIR the call's cwd is the project")

    # The rules #1046 adds (the GitHub App, #1035). Each reads the raw command, quoted strings
    # included: a quoted script is caught, and so is prose that names the command.

    def test_every_line_of_mains_guard_is_here_in_order(self):
        # #1046 adds to main's guard and changes none of it: every line of main's guard
        # (tests/legacy_guard/conductor-guard-59e25cc6f.py, main at 59e25cc6f) is in the guard, in
        # order. Main's lines are pinned here; test_guard_differential's
        # test_nothing_mains_guard_blocks_is_allowed_now shows, over its corpus, that the
        # additions only add refusals (an inserted line could still allow).
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy_guard", "conductor-guard-59e25cc6f.py")
        with open(path, "rb") as handle:
            data = handle.read()
        blob = __import__("hashlib").sha1(b"blob %d\0" % len(data) + data).hexdigest()
        self.assertEqual(blob, "d0441c4ecc072255a06b189566b7afbbef524f5b", "the copy is main's guard at 59e25cc6f")
        with open(GUARD, encoding="utf-8") as handle:
            lines = iter(handle.read().split("\n"))
        for number, line in enumerate(data.decode("utf-8").split("\n"), 1):
            self.assertTrue(any(candidate == line for candidate in lines), f"main's line {number} missing: {line!r}")

    def test_coord_token_exec_is_blocked_in_every_form(self):
        # #1035 review (P3-8): a conductor run must never get a GitHub App token in a shell.
        for command in ("Tools/coord/coord token exec -- gh pr list",
                        "Tools/coord/coord token exec --kind ops -- sh -c 'echo $GH_TOKEN'",
                        "./Tools/coord/coord token exec --kind read -- env",
                        "/Users/dev/Projects/INISA/Tools/coord/coord token exec -- true",
                        "python3 Tools/coord/coord token exec -- true", "coord token  exec -- true",
                        "sh -c 'Tools/coord/coord token exec --kind ops -- env'",
                        "bash -c \"coord token exec -- gh api user\"", "eval 'coord token exec -- true'",
                        "cd /tmp && Tools/coord/coord token exec -- true",
                        "true; Tools/coord/coord token exec -- true", "x=1 && coord token exec -- true",
                        # coord's top-level --repo before the command (round three, P3-1)
                        "Tools/coord/coord --repo example-owner/example-repo token exec -- env",
                        "Tools/coord/coord --repo=example-owner/example-repo token exec --kind ops -- env",
                        "sh -c 'coord --repo o/r token exec -- env'",
                        # Round twelve's review (P3-1): every spelling of coord's options that
                        # argparse takes, before the command and between its two words.
                        "Tools/coord/coord --rep o/r token exec -- env", "Tools/coord/coord --re=o/r token exec -- env",
                        "Tools/coord/coord --r o/r token exec -- env",
                        "Tools/coord/coord --repo a/b --repo o/r token exec -- env",
                        "Tools/coord/coord --repo= token exec -- env", "Tools/coord/coord -- token exec -- env",
                        "Tools/coord/coord token -- exec -- env", "Tools/coord/coord --repo o/r -- token -- exec -- env",
                        # The delta review of 2d13f6b1e (P2-A): argparse runs a quoted or escaped
                        # subcommand word as the word, so any such spelling is refused.
                        "coord 'token' exec -- env", 'coord "token" exec -- env', "coord t''oken exec -- env",
                        "coord tok\\en exec -- env", "coord token 'exec' -- env", "coord $'token' exec -- env",
                        "sh -c 'coord \"token\" exec'",
                        # The raw command is read: prose that names the command is refused too.
                        "gh pr comment 5 -b 'never run coord token exec here'"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("Tools/coord/coord token status", "grep -n 'token exec' Docs/agent-coordination.md",
                        "Tools/coord/coord --repo example-owner/example-repo token status",
                        "Tools/coord/coord --repo example-owner/example-repo board", "Tools/coord/coord --rep o/r board",
                        "Tools/coord/coord -- token status", "Tools/coord/coord send --on 1 'the token exec docs'"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)
        self.assertEqual(self.run_guard("Bash", {"command": "Tools/coord/coord token exec -- true"}, agent=""), 0,
                         "outside the conductor the guard is silent")

    def test_train_approve_with_repo_is_blocked(self):
        # A second rule beside main's `coord train approve` rule, which is unchanged: the same
        # command with at least one of coord's options in it, in any spelling argparse takes.
        for command in ("Tools/coord/coord --repo example-owner/example-repo train approve 5 --note x",
                        "coord --repo=o/r train approve 5", "Tools/coord/coord --rep o/r train approve 5 --note x",
                        "Tools/coord/coord --re=o/r train approve 5 --note x",
                        "Tools/coord/coord --r o/r train approve 5 --note x",
                        "Tools/coord/coord --repo a/b --repo o/r train approve 5 --note x",
                        "Tools/coord/coord --repo= train approve 5 --note x",
                        "Tools/coord/coord -- train approve 5 --note x", "Tools/coord/coord train -- approve 5 --note x",
                        "Tools/coord/coord --repo o/r 'train' approve 5 --note x",
                        "Tools/coord/coord --repo o/r train 'approve' 5 --note x", "coord 'train' approve 5"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("Tools/coord/coord --repo o/r train status", "Tools/coord/coord --rep o/r train add 5",
                        "Tools/coord/coord -- train status"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_a_quoted_or_escaped_coord_subcommand_is_refused(self):
        # The shell's quoting is not decoded; no real command quotes its subcommand. The quotes
        # of a later argument are not the subcommand's.
        for command in ("coord 'board'", "coord --repo 'o/r' board", "coord $CMD", "coord train \"status\"",
                        "coord \\\n'token' exec"):
            result = self.run_guard("Bash", {"command": command}, full=True)
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("a coord subcommand spelled with quotes or escapes", result.stderr, command)
        for command in ("coord send 'a quoted message'", 'coord send --on 5 "x"',
                        "Tools/coord/coord send --to 'conductor@*' 'I am on main-red'",
                        "Tools/coord/coord conductor escalate \"PR 7 needs you\"",
                        "Tools/coord/coord claim 991 --note 'on it'", "Tools/coord/coord --repo o/r board",
                        # A newline ends the coord command; the next line's quotes are its own.
                        "Tools/coord/coord board\n\"$CONDUCTOR_STATE_DIR/run.sh\""):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_the_keychain_is_blocked(self):
        # The app's private key: `security find-generic-password -s example-coord-github-app -w`
        # prints it without a prompt to any process running as Alex. `security` runs the FIRST
        # command in its table that its word is a prefix of (`security help` lists the table):
        # `f` is find-generic-password, `find-i` find-internet-password (before find-identity),
        # `du` dump-keychain, `e` export. -i and -p read commands from stdin.
        for command in ("security find-generic-password -s example-coord-github-app -w",
                        "/usr/bin/security find-generic-password -s 'example-coord-github-app' -a x -w",
                        "security find-internet-password -s github.com -w", "security dump-keychain -d",
                        "security export -k login.keychain -o /tmp/k.p12", "sh -c 'security find-generic-password -w'",
                        "x=$(security find-generic-password -s example-coord-github-app -w)",
                        "cd /tmp && security find-generic-password -ga inisa", "security find-g -s x -w",
                        "security find-generic -w", "security dump-k",
                        # Round twelve's review (P2-1): any prefix, and interactive mode.
                        "security f -s x -w", "security fi -s x -w", "security find -s x -w", "security find- -s x -w",
                        "security du -d", "security dump -d", "security dump- -d",
                        "security e -k login.keychain -t privKeys -o /tmp/k", "security expor -t privKeys",
                        "security find-i -w", "security find-k", "security export-smartcard",
                        "/usr/bin/security f -a client-id -w", "security -i", "security -q -i", "security -p 'k> '",
                        "printf 'find-generic-password -w\\n' | security -i", "security -v find-g -s x",
                        "security -- find-generic-password -w",
                        # The command word is found past redirections and quotes; one built at run
                        # time is refused.
                        "security 2>/dev/null f -w", "security > /dev/null find-generic-password -w",
                        "security &>/dev/null f -w", "security f>/tmp/o -w", "security 'f' -w", "security \"find\" -w",
                        "security $CMD -w", "security $(echo f) -w", "security `echo f` -w", "security f* -w",
                        "security {find-generic-password,x}", "(security f -w)", "true && security du -d",
                        # The delta review of 2d13f6b1e (P2-B): escapes are not decoded, so `$` or a
                        # backslash in a word up to the command word is refused.
                        "security $'\\x64ump-keychain' -d", "security $'\\x66ind-generic-password' -w",
                        "security $'\\145xport' -k login.keychain -t privKeys -o /tmp/k", "security -$X",
                        "security \\-i", "security fin\\d -w",
                        # The raw command is read: prose that names the command is refused too.
                        "grep -n 'security find-generic-password' Docs/agent-coordination.md",
                        "gh pr comment 5 -b 'the key is read with security find-generic-password'"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("security list-keychains", "security show-keychain-info",
                        "grep -rn security Tools/coord/coordlib/apptoken.py", "security find-certificate -a",
                        "security find-identity -v -p codesigning", "security verify-cert -c x.pem", "security help",
                        "security -h", "security dump-trust-settings", "gh pr comment 5 -b 'the security fix landed'",
                        "echo securityexport"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_a_security_the_shell_assembles_is_refused(self):
        # Codex on a774ba186 (P1), the third review in a row whose P1 is a shell spelling: with
        # every quote, backslash and `$` deleted, the command names `security` more often than
        # it does as written, so the shell assembles the program name. Refused whatever follows.
        for command in ('"security" dump-keychain -d', "secu\\rity dump-keychain", "'security' du", 's"ec"urity f -w',
                        '/usr/bin/"security" e', "sec$'u'rity dump-keychain", "\\security dump-keychain",
                        "\"security\" $'\\x64ump-keychain'", "'security' list-keychains",
                        # The cost: prose that quotes the name.
                        "git commit -q -m 'fix the \"security\" wording'"):
            result = self.run_guard("Bash", {"command": command}, full=True)
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("`security` spelled with quotes or escapes", result.stderr, command)
            self.assertIn("write such text with the Write tool", result.stderr, command)
        for command in ("security list-keychains", "grep -rn security Tools/coord/coordlib/apptoken.py",
                        "gh pr comment 5 -b 'the security fix landed'", "echo securityexport"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_the_token_cache_is_blocked(self):
        # Codex on 141d38f62 (P1): a path has spellings no pattern keeps up with, so the rule is
        # co-occurrence, like apptoken's: `inisa-coord` and `cache` anywhere in the case-folded command.
        for command in ("cat ~/Library/Caches/inisa-coord/ops.json", "cat \"$HOME/Library/Caches/inisa-coord/read.json\"",
                        "python3 -c 'print(open(\"/Users/dev/Library/Caches/inisa-coord/ops.json\").read())'",
                        "ls -la ~/Library/Caches/inisa-coord", "cp ~/Library/Caches/inisa-coord/ops.json /tmp/",
                        "cat ~/library/caches/INISA-COORD/ops.json", "cat ~/Library//Caches/inisa-coord/read.json",
                        "cat ~/Library/Caches/./inisa-coord/ops.json",
                        "cat ~/Library/Caches/INISA-COORD/ops.json", "cd ~/Library/Caches && cat inisa-coord/ops.json",
                        # The cost, as for apptoken: any command that names both, the budget cache and
                        # prose included. The way out is the Write tool.
                        "cat ~/.cache/inisa-coord/alex-bridges_INISA/budget.json"):
            result = self.run_guard("Bash", {"command": command}, full=True)
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("`inisa-coord` and `cache` in one command", result.stderr, command)
            self.assertIn("write such text with the Write tool", result.stderr, command)
        # The repo's own name beside a cache word is not the token cache (#1051, Alex: narrow the
        # rule): the false positives of #1046's fresh review. The last is the conductor's disk-low
        # `du -xhd1` of the CI trees and caches. A glob (`inisa*`) now passes: the spelling class
        # #1051 accepts.
        for command in ("ls ~/Library/Caches", "Tools/coord/coord token status", "cat ~/.cache/pip/x.json",
                        "git -C /Users/dev/Projects/INISA diff --cached", "gh cache list --repo example-owner/example-repo",
                        "gh api repos/example-owner/example-repo/actions/caches",
                        "python3 /Users/dev/Projects/INISA/scripts/test-ci-cache-preservation.py",
                        "du -xhd1 ~/actions-runner*/_work/INISA ~/Library/Caches 2>/dev/null | sort -h",
                        "cat ~/Library/Caches/inisa*/ops.json"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_the_keychain_service_name_is_blocked_however_it_is_called(self):
        for command in ("python3 -c \"import subprocess; subprocess.run(['security', 'find-generic-password', "
                        "'-s', 'example-coord-github-app', '-w'])\"",
                        "'security' find-generic-password -s example-coord-github-app -w",
                        "env x=1 bash -c \"security find-generic-password -s example-coord-github-app\"",
                        # Matched without case, which fails closed whether or not the Keychain's
                        # lookup ignores it.
                        "python3 -c \"import subprocess; subprocess.run(['security', 'find-generic-password', "
                        "'-s', 'Example-Coord-GitHub-App'])\""):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        self.assertEqual(self.run_guard("Bash", {"command": "Tools/coord/coord token status"}), 0)

    def test_python_and_apptoken_in_one_command_are_blocked(self):
        # Round eight of the #1046 review: the rule is co-occurrence on the raw command. Two
        # rounds of parsing pipelines and heredocs failed open, so nothing is parsed.
        heredoc = "<<'EOF'\nimport coordlib.apptoken\nEOF"
        for command in ("python3 -c 'from coordlib import apptoken; print(apptoken.AppTokens)'",
                        "cd Tools/coord && python3 -c \"import coordlib.apptoken as a\"",
                        "python3 -B -c 'from coordlib.apptoken import AppTokens'",
                        "/usr/bin/python3 -c 'import coordlib.apptoken'",
                        "python3 - <<'EOF'\nfrom coordlib import apptoken\nEOF",
                        "python3 -c 'import sys; from coordlib import apptoken'",
                        "true && python3 -c 'from coordlib.apptoken import AppTokens'",
                        "echo 'from coordlib import apptoken' | python3 -",
                        "printf 'import coordlib.apptoken\\n' | python3 -",
                        "cat <<EOF | python3 -\nfrom coordlib import apptoken\nEOF",
                        "echo 'import coordlib.apptoken' | python3", "python3 <<EOF\nimport coordlib.apptoken\nEOF",
                        "echo 'import coordlib.apptoken' | /usr/bin/python3 -B -",
                        "python3 -c 'print(\"it'\\''s\"); import coordlib.apptoken'",
                        "python3 -c $'print(\\'x\\'); import coordlib.apptoken'",
                        "timeout 30 python3 - " + heredoc, "( python3 - " + heredoc + "\n)",
                        "{ python3 - " + heredoc + "\n}", "/usr/bin/env python3 - " + heredoc,
                        "cd Tools/coord && timeout 60 python3 - <<'EOF'\nfrom coordlib.apptoken import AppTokens\nEOF",
                        "python3 - 2>&1 " + heredoc, "python3 -X utf8 - " + heredoc, "python3 /dev/stdin " + heredoc,
                        "echo 'import coordlib.apptoken' | python3 - 2>&1",
                        "echo 'import coordlib.apptoken' | nice -n 5 python3",
                        "echo 'import coordlib.apptoken' | env GREETING='hi there' python3",
                        "bash <<'X'\npython3 - <<'Y'\nimport coordlib.apptoken\nY\nX",
                        "python3 -c 'import sys; exec(sys.stdin.read())' " + heredoc,
                        # Round seven's parser took these `<<`s for heredocs, and missed what followed.
                        "echo \"<<X\"\npython3 - " + heredoc, "true # <<X\npython3 - " + heredoc,
                        "echo $((1<<2))\npython3 - " + heredoc, "cat <<$'Z'\nhi\nZ\npython3 - " + heredoc,
                        "python3 - <<'EOF'\r\nimport coordlib.apptoken\r\nEOF\r\n",
                        # The test module's name is exempt, nothing else in the command is.
                        "python3 -m unittest test_apptoken; python3 - <<'Y'\nimport coordlib.apptoken\nY",
                        "echo 'import coordlib.apptoken' | /opt/homebrew/bin/python3t -",
                        # macOS's filesystem ignores case: these run python3.
                        "Python3 - <<'Y'\nimport coordlib.apptoken\nY",
                        "/usr/bin/PYTHON3 -c 'import coordlib.apptoken'",
                        # Matched on the NFKC, case-folded command. Python reads identifiers in NFKC,
                        # and PYTHONCASEOK=1 imports ignore case.
                        "python3 -c 'print(AppTokens)'", "PYTHONCASEOK=1 python3 -c 'import coordlib.APPTOKEN as t'",
                        "python3 -c 'import coordlib.\uff41pptoken'",
                        "python3 -c 'import coordlib.\U0001d41a\U0001d429\U0001d429\U0001d42d\U0001d428\U0001d424\U0001d41e\U0001d427'",
                        # The exemption is the test module's name only.
                        "python3 -c 'import coordlib.xtest_apptoken'"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_prose_that_names_python_and_apptoken_is_refused_with_the_way_out(self):
        # The cost of not parsing, accepted in round eight: it fails closed, and says what to do.
        for command in ("python3 -c \"print(1)\"; git log -- Tools/coord/coordlib/apptoken.py",
                        "python3 -c 'print(1)' | grep -c apptoken", "grep -rn apptoken Tools/coord | grep python3",
                        "gh pr view 5 --json files | python3 -m json.tool | grep apptoken",
                        "git commit -q -F - <<'EOF'\ncoord: don't let apptoken run from python3 stdin\nEOF",
                        "gh pr create --title x --body-file - <<'EOF'\nThe guard's apptoken check for python3 stdin.\nEOF",
                        "Tools/coord/coord send --on 1046 'the apptoken test'\\''s python3 run is green'"):
            result = self.run_guard("Bash", {"command": command}, full=True)
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("write it with the Write tool (for example under $CONDUCTOR_STATE_DIR) and pass the file "
                          "(--body-file / -F", result.stderr, command)
        # Every shell way to write such text is refused, so the Write tool is the way: the guard
        # checks only its path. The state directory is the watcher's default (conductor.Config:
        # $XDG_STATE_HOME or ~/.local/state, then inisa-conductor).
        note = os.path.join(os.path.expanduser("~"), ".local", "state", "inisa-conductor", "note.md")
        for path in (note, "$CONDUCTOR_STATE_DIR/note.md"):
            self.assertEqual(self.run_guard("Write", {"file_path": path, "content": "python3 cannot import apptoken"}), 0)
        for command in (f"gh pr create --title x --body-file {note}", f"git commit -q -F {note}",
                        f"Tools/coord/coord send --on 1046 \"$(cat {note})\"",
                        "gh pr create --title x --body-file \"$CONDUCTOR_STATE_DIR/note.md\""):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_the_test_runner_and_the_conductors_own_commands_are_allowed(self):
        for command in ("python3 -m unittest test_apptoken", "python3 -m unittest discover -s Tools/coord/tests",
                        "cd Tools/coord/tests && python3 -W ignore -m unittest test_apptoken 2>&1 | tail -3",
                        "grep -rn apptoken Tools/coord", "Tools/coord/coord send --on 1 'the apptoken test passed'",
                        "grep -n AppTokens Tools/coord/coordlib/apptoken.py", "python3 -c 'print(1)'",
                        "git log --oneline -- Tools/coord/coordlib/apptoken.py",
                        # .claude/agents/conductor.md
                        "cat \"$CONDUCTOR_STATE_DIR/notes.md\"", "Tools/coord/coord inbox", "Tools/coord/coord board",
                        "Tools/coord/coord board --json | python3 -m json.tool | head -50",
                        "Tools/coord/coord send --on 1046 'the train is moving again'",
                        "Tools/coord/coord send --to 'conductor@*' 'I am on main-red'", "Tools/coord/coord claim 991",
                        "Tools/coord/coord train add 1046", "Tools/coord/coord conductor escalate \"PR 7 needs you\"",
                        "Tools/coord/coord conductor pause --for 2h", "gh run view 123456 --log-failed | tail -80",
                        "tail -50 ~/Library/Logs/inisa/ci-health.log", "du -xhd1 /Users/dev 2>/dev/null | sort -h",
                        "git worktree add -b conductor/fix-x ../conductor-fix-x origin/main",
                        "gh pr diff 1046 -- Tools/coord/coordlib/apptoken.py | head -100"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_a_very_long_command_is_refused_quickly(self):
        # _QUOTED rescans to the end for each unclosed quote: uncapped, 100,000 characters of
        # this take ~20 s, past the hook's timeout, which lets the call through.
        env = {k: v for k, v in os.environ.items() if k not in ("COORD_AGENT", "CONDUCTOR_WAKE")}
        env.update({"COORD_AGENT": "conductor@Tycho:conductor", "CLAUDE_PROJECT_DIR": "/tmp/home"})
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 'echo "' + 'a\\"' * 66665}, "cwd": "/tmp/home"})
        result = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("longer than 20000 characters", result.stderr)
        self.assertIn("write long text with the Write tool", result.stderr)
        self.assertEqual(self.run_guard("Bash", {"command": "echo " + "a" * 19000}), 0, "under the cap: checked as usual")

    # The watchdog: the verdict comes from a forked child, and a late one is a refusal.

    PADDED = "gh pr merge 5 # " + "git push " * 780  # the review's input, 7,036 characters

    def run_patched_guard(self, code, payload):
        """The guard with `code` run just before main(), in a child with a 15 s cap."""
        env = {k: v for k, v in os.environ.items() if k not in ("COORD_AGENT", "CONDUCTOR_WAKE")}
        env.update({"COORD_AGENT": "conductor@Tycho:conductor", "CLAUDE_PROJECT_DIR": "/tmp/home"})
        with tempfile.TemporaryDirectory() as folder:
            patched = os.path.join(folder, "conductor-guard.py")
            with open(GUARD, encoding="utf-8") as handle:
                source = handle.read()
            anchor = '\nif __name__ == "__main__":'
            self.assertEqual(source.count(anchor), 1)
            with open(patched, "w", encoding="utf-8") as handle:
                handle.write(source.replace(anchor, f"\n{code}\n" + anchor))
            start = time.monotonic()
            result = subprocess.run([sys.executable, patched], input=payload, capture_output=True, text=True, env=env,
                                    timeout=15)
            return result, time.monotonic() - start

    def test_a_slow_check_is_refused_in_time(self):
        # Main's push rule is cubic in `git push` repeats (unchanged here): the review's padded
        # input runs for about half a minute. The hook's 10 s timeout would let the call through,
        # so the watchdog refuses it at DEADLINE_SECONDS (7).
        start = time.monotonic()
        result = self.run_guard("Bash", {"command": self.PADDED}, full=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not decide within 7 s", result.stderr)
        self.assertLess(time.monotonic() - start, 9.5, "inside the hook's 10 s timeout")
        result = self.run_guard("Bash", {"command": "gh pr merge 5"}, full=True)
        self.assertIn("merging a PR by hand", result.stderr, "a quick verdict is the real one")

    def test_a_crashed_or_hung_checker_is_a_refusal(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/tmp/home"})
        # A child killed before it answers: no verdict is a refusal, at once.
        result, took = self.run_patched_guard(
            "def verdict(payload, env):\n    os.kill(os.getpid(), signal.SIGKILL)", payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not check this call (the check exited without a verdict)", result.stderr)
        self.assertLess(took, 3)
        # A child that never answers: refused at DEADLINE_SECONDS, not left to the hook's timeout.
        result, took = self.run_patched_guard("def verdict(payload, env):\n    time.sleep(60)", payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not decide within 7 s", result.stderr)
        self.assertLess(took, 9.5)

    def test_the_parent_never_waits_for_a_stuck_child(self):
        # A failing select (or a child stuck in I/O) must not keep the parent waiting past the
        # hook's timeout: it kills the child and refuses at once.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": self.PADDED}, "cwd": "/tmp/home"})
        result, took = self.run_patched_guard(
            "def _broken(*args):\n    raise ValueError('select failed')\nselect.select = _broken", payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not check this call (ValueError)", result.stderr)
        self.assertLess(took, 5, "the slow child was killed, not waited for")

    def test_without_fork_the_guard_refuses(self):
        # Without a checker there is no deadline to keep, so it refuses.
        guard = self.load_guard(GUARD)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/tmp/home"}
        for name, error in (("fork", OSError("no fork")), ("pipe", OSError("no pipe")), ("fork", AttributeError("fork"))):
            with mock.patch.object(guard.os, name, side_effect=error):
                why = guard.decide(payload, {"CLAUDE_PROJECT_DIR": "/tmp/home"})
            self.assertIn(f"could not start its checker ({error.__class__.__name__})", why)
        result = self.run_guard("Bash", {}, raw=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": 5}),
                                full=True)
        self.assertEqual(result.returncode, 2, "an error in the checking child still refuses")
        self.assertIn("could not check this call (TypeError)", result.stderr)

    def test_every_rule_this_pr_adds_is_fast_on_its_own_keywords(self):
        # Each rule #1046 adds, on inputs built from its own keywords and punctuation up to the
        # cap; each rule's keywords must match it (so its inputs engage it). One input over 2 CPU
        # seconds for the rule alone fails. Each rule runs in its own child, one at a time, judged
        # on the child's CPU time, not the wall clock (Codex on 2d13f6b1e). Main's rules are not
        # this PR's to time (Codex on 141d38f62: its gh api rule alone passed 2 CPU s on Linux);
        # the watchdog covers a slow verdict (test_a_slow_check_is_refused_in_time).
        import guard_timing
        guard = self.load_guard(GUARD)
        names = guard_timing.own_rules(guard)
        self.assertEqual(set(names), {"token exec", "train approve", "quoted subcommand", "security", "RAW_RULES[0]",
                                      "assembled security", "token cache", "apptoken", "cap"})
        report, failures = guard_timing.measure_in_children(GUARD, only=names)
        self.assertEqual(failures, [])
        self.assertEqual([rule["rule"] for rule in report], names)
        for rule in report:
            self.assertGreater(rule["inputs"], 1, rule)

    def test_the_timing_test_catches_a_cubic_rule(self):
        # The review's known-cubic rule, put into a copy, must be reported.
        import guard_timing
        with tempfile.TemporaryDirectory() as folder:
            cubic = os.path.join(folder, "conductor-guard.py")
            with open(GUARD, encoding="utf-8") as handle:
                source = handle.read()
            anchor = '\nif __name__ == "__main__":'
            rule = 'RULES.insert(0, (r"\\bgh\\s+api\\b[^\\n;&|]*\\bapi\\b[^\\n;&|]*--force", "cubic"))'
            with open(cubic, "w", encoding="utf-8") as handle:
                handle.write(source.replace(anchor, "\n" + rule + "\n" + anchor))
            report, failures = guard_timing.measure_in_children(cubic, timeout=4, only=["RULES[0]"])
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("RULES[0]", failures[0])
        self.assertTrue("slower than" in failures[0] or "over the" in failures[0], failures)

    @staticmethod
    def load_guard(path):
        spec = __import__("importlib.util").util.spec_from_file_location("guard_" + str(abs(hash(path))), path)
        guard = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        return guard

    def test_the_deny_list_names_token_exec(self):
        self.assertIn("Bash(* token exec*)", conductor.DENY)

    def test_the_deny_list_keeps_the_read_tool_out_of_the_token_cache(self):
        # The guard sees Bash only; a conductor run's Read tool is kept out by --settings deny.
        self.assertIn("Read(~/Library/Caches/inisa-coord/**)", conductor.DENY)
        self.assertIn("Read(//Users/*/Library/Caches/inisa-coord/**)", conductor.DENY)

    def test_the_guard_protects_exactly_what_the_train_holds_for_alex(self):
        spec = __import__("importlib.util").util.spec_from_file_location("guard", GUARD)
        guard = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        self.assertEqual(guard.CONTROL_PATHS, train.ALEX_ONLY_PATHS)


class TrainRecipientTests(unittest.TestCase):
    def test_ejections_and_removals_reach_the_adder_and_the_conductor(self):
        self.assertEqual(train_step.Step.recipients(None, {"trainAddedBy": "codex@E:x"}), ["codex@E:x", "conductor@*"])
        self.assertEqual(train_step.Step.recipients(None, None), ["conductor@*"], "no adder on record")


class DurationTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(conductor.parse_duration("90m"), dt.timedelta(minutes=90))
        self.assertEqual(conductor.parse_duration("2h"), dt.timedelta(hours=2))
        with self.assertRaises(ValueError):
            conductor.parse_duration("soon")


if __name__ == "__main__":
    unittest.main()

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

from coordlib import cli, conductor, lease, marker, outage, render, snapshot, train, train_step  # noqa: E402

# Friday 2026-09-25 21:00Z = 17:00 in New York (EDT, UTC-4).
NOW = dt.datetime(2026, 9, 25, 21, 0, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


def at(minutes: float) -> dt.datetime:
    return NOW + dt.timedelta(minutes=minutes)


def record(comment_id: int, minutes: float, payload, text: str = ""):
    return {"id": comment_id, "createdAt": snapshot.iso(at(minutes)), "url": f"u/{comment_id}", "text": text,
            "payload": payload}


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
        hosts = lease.hosts([self.beat(2, "conductor@B:c", -1), self.beat(1, "conductor@A:c", -3)], NOW)
        self.assertEqual([(h["agent"], h["leader"]) for h in hosts], [("conductor@A:c", True), ("conductor@B:c", False)])

    def test_a_stale_leader_hands_over(self):
        hosts = lease.hosts([self.beat(1, "conductor@A:c", -20), self.beat(2, "conductor@B:c", -1)], NOW)
        self.assertEqual([h["leader"] for h in hosts], [False, True])

    def test_a_malformed_beat_is_not_alive(self):
        bad = record(1, 0, {"kind": "conductor", "event": "beat", "agent": "conductor@A:c", "beat": "now"})
        self.assertEqual(lease.hosts([bad], NOW)[0]["alive"], False)

    def test_pause_and_resume(self):
        pause = record(5, -10, {"kind": "conductor", "event": "pause", "agent": "claude@E:x",
                                "until": snapshot.iso(at(60)), "note": "driving it myself"})
        self.assertEqual(lease.pause([pause], NOW)["note"], "driving it myself")
        self.assertIsNone(lease.pause([pause], at(61)))
        self.assertIsNone(lease.pause([pause, record(6, -5, {"kind": "conductor", "event": "resume"})], NOW))


class ReasonTests(unittest.TestCase):
    def test_which_inbox_items_wake(self):
        items = [
            {"id": 1, "reason": "to conductor@*", "from": "a", "on": 7, "text": "please look", "url": "u1"},
            {"id": 2, "reason": "to all", "from": "a", "on": 8, "text": "fyi", "url": "u2"},
            {"id": 3, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "held", "url": "u3"},
            {"id": 4, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "held", "url": "u4"},
            {"id": 5, "reason": "to conductor@*", "from": "train@actions", "on": 9, "text": "out", "url": "u5"},
        ]
        payloads = {3: {"kind": "train", "event": "held", "hold": "no-review"},
                    4: {"kind": "train", "event": "held", "hold": "ci-red"},
                    5: {"kind": "train", "event": "ejected"}}
        wakes = {r["key"]: r["wake"] for r in conductor.message_reasons(items, payloads)}
        self.assertEqual(wakes, {"comment:1": True, "comment:2": False, "comment:3": False,
                                 "comment:4": True, "comment:5": True})

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

    def transport(self):
        return "fake"

    def raw(self, body, minutes, comment_id=None):
        if comment_id is None:
            self.next_id += 1
            comment_id = self.next_id
        return {"id": comment_id, "created_at": snapshot.iso(at(minutes)), "html_url": f"c/{comment_id}", "body": body,
                "issue_url": "https://api/repos/o/r/issues/900", "user": {"login": "alex-bridges"}}

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
        if "ci.yml/runs" in path:
            return {"workflow_runs": self.main_runs}
        if "merge-train.yml/runs" in path:
            return {"workflow_runs": []}
        if path.endswith("actions/runners?per_page=100"):
            return {"runners": self.runners}
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
            return list(self.repo_comments)
        if path.startswith("repos/{repo}/pulls/comments"):
            return []
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
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "event": "beat", "agent": "conductor@Old:c",
                                                     "beat": snapshot.iso(at(-1))}), -600, comment_id=1)
        self.gh.issue_comments.append(leader)
        self.message(-1)
        self.tick(0)
        self.assertEqual(self.runs, [])
        self.assertFalse(any("issues/comments?since" in path for _, path in self.gh.calls), "no inbox read")
        self.assertIn("standby", self.gh.issue_comments[-1]["body"])

    def test_a_standby_takes_over_when_the_leader_goes_quiet(self):
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "event": "beat", "agent": "conductor@Old:c",
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
        self.gh.post(900, "paused", {"kind": "conductor", "event": "pause", "agent": "claude@E:x",
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
        leader = self.gh.raw(marker.compose("beat", {"kind": "conductor", "event": "beat", "agent": "conductor@Old:c",
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
        standby = self.gh.raw(marker.compose("beat", {"kind": "conductor", "event": "beat", "agent": "conductor@B:c",
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
                "conductor": {"hosts": lease.hosts([beat], NOW), "pause": None, "escalations": []}}
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
                "conductor": {"hosts": lease.hosts(comments, NOW), "pause": lease.pause(comments, NOW),
                              "escalations": lease.escalations(comments, NOW)}}
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
                        "gh auth token", "gh workflow disable ci.yml",
                        # Round nine: a word ends at the end of its segment, a `)` or a backtick.
                        "git push origin main;echo ok", "git push origin main&& echo", "(git push origin main)",
                        # Round ten: the shell's view. Continuations are joined, redirections are not
                        # words, and program names and paths ignore case.
                        "git push origin \\\nmain", "git push \\\n-f", "git push origin \\\n+HEAD:x",
                        "git push origin HEAD:\\\nrefs/heads/main", "gh pr \\\n  merge 5",
                        "Tools/coord/coord train \\\n  approve 5",
                        "git push origin main>/dev/null", "git push origin main</dev/null", "git push origin main>&2",
                        "git push origin refs/heads/main>/tmp/o", "gh>/dev/null pr merge 5", "gh api -X>/dev/null PUT r/pulls/1/merge",
                        "bash>/dev/null -c 'gh pr merge 5'", "eval>/dev/null 'gh pr merge 5'", "cd x &&>&2 sudo ls",
                        "GIT push --force", "Git push origin main", "GH pr merge 5", "GH api -X PUT repos/o/r/pulls/5/merge",
                        "GH secret list", "GH auth token", "SUDO ls", "LAUNCHCTL list", "ENV sudo ls",
                        "BASH -c 'gh pr merge 5'"):
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
                     "~/.claude/settings.json", "~/.claude/hooks/x.sh",
                     # Round ten: macOS resolves paths without case.
                     "/tmp/home/.CLAUDE/settings.json", "~/.CLAUDE/hooks/x.sh", "/tmp/home/tools/COORD/coord"):
            self.assertEqual(self.run_guard("Write", {"file_path": path}), 2, path)
        for path in ("/tmp/wt/.claude/hooks/conductor-guard.py", "/tmp/wt/Tools/coord/coordlib/conductor.py",
                     "/tmp/home/Docs/agent-coordination.md", "~/.claude/projects/x/memory/MEMORY.md"):
            self.assertEqual(self.run_guard("Edit", {"file_path": path}), 0, path)
        for command in ("sed -i '' s/a/b/ .claude/hooks/conductor-guard.py", "echo x > CLAUDE.md",
                        "cp /tmp/x /tmp/home/Tools/coord/coordlib/conductor.py", "rm -rf Tools/coord",
                        "echo {} > ~/.claude/settings.json",
                        # Round ten: case, redirections and continuations.
                        "RM -rf Tools/coord", "echo x > .CLAUDE/settings.json", "sed</dev/null -i '' s/a/b/ CLAUDE.md",
                        "cd /tmp/home>/dev/null && echo x > .claude/agents/conductor.md", "rm -rf \\\nTools/coord"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_silent_outside_the_conductor_and_closed_inside_it(self):
        self.assertEqual(self.run_guard("Bash", {"command": "gh pr merge 5"}, agent=""), 0)
        self.assertEqual(self.run_guard("Bash", {}, raw="not json"), 2)
        self.assertEqual(self.run_guard("Bash", {}, raw=json.dumps({"tool_name": "Bash", "tool_input": []})), 0,
                         "no command is nothing to run")
        self.assertEqual(self.run_guard("Write", {"file_path": "CLAUDE.md"}, project=None), 2,
                         "without CLAUDE_PROJECT_DIR the call's cwd is the project")

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
                        "sh -c 'coord --repo o/r token exec -- env'"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("Tools/coord/coord token status", "gh pr comment 5 -b 'never run coord token exec here'",
                        "grep -n 'token exec' Docs/agent-coordination.md",
                        "Tools/coord/coord --repo example-owner/example-repo token status",
                        "Tools/coord/coord --repo example-owner/example-repo board"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)
        self.assertEqual(self.run_guard("Bash", {"command": "Tools/coord/coord token exec -- true"}, agent=""), 0,
                         "outside the conductor the guard is silent")

    def test_train_approve_with_repo_is_blocked(self):
        for command in ("Tools/coord/coord --repo example-owner/example-repo train approve 5 --note x",
                        "coord --repo=o/r train approve 5"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_the_keychain_is_blocked(self):
        # The app's private key: `security find-generic-password -s example-coord-github-app -w`
        # prints it without a prompt to any process running as Alex.
        for command in ("security find-generic-password -s example-coord-github-app -w",
                        "/usr/bin/security find-generic-password -s 'example-coord-github-app' -a x -w",
                        "security find-internet-password -s github.com -w", "security dump-keychain -d",
                        "security export -k login.keychain -o /tmp/k.p12", "sh -c 'security find-generic-password -w'",
                        "x=$(security find-generic-password -s example-coord-github-app -w)",
                        "cd /tmp && security find-generic-password -ga inisa"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("security list-keychains", "security show-keychain-info",
                        "grep -n 'security find-generic-password' Docs/agent-coordination.md",
                        "gh pr comment 5 -b 'the key is read with security find-generic-password'",
                        "grep -rn security Tools/coord/coordlib/apptoken.py"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_the_token_cache_is_blocked(self):
        for command in ("cat ~/Library/Caches/inisa-coord/ops.json", "cat \"$HOME/Library/Caches/inisa-coord/read.json\"",
                        "python3 -c 'print(open(\"/Users/dev/Library/Caches/inisa-coord/ops.json\").read())'",
                        "ls -la ~/Library/Caches/inisa-coord", "cp ~/Library/Caches/inisa-coord/ops.json /tmp/"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("ls ~/Library/Caches", "cat ~/.cache/inisa-coord/alex-bridges_INISA/budget.json",
                        "Tools/coord/coord token status"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

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
                        # Round nine: matched on the NFKC, case-folded command. Python reads
                        # identifiers in NFKC, and PYTHONCASEOK=1 imports ignore case.
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
        # Round nine (P3-3): every shell way to write such text is refused, so the Write tool is
        # the way: the guard checks only its path. The state directory is the watcher's default
        # (conductor.Config: $XDG_STATE_HOME or ~/.local/state, then inisa-conductor).
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

    def test_the_guard_decides_in_time_or_refuses(self):
        # Round nine (P1): the hook's timeout lets a call through, so a slow check must refuse.
        # The reviewer's input: `gh pr merge` then `git push` repeated, cubic for the old push rule.
        command = "gh pr merge 5 # " + "git push " * 780  # 7,036 characters
        env = {k: v for k, v in os.environ.items() if k not in ("COORD_AGENT", "CONDUCTOR_WAKE")}
        env.update({"COORD_AGENT": "conductor@Tycho:conductor", "CLAUDE_PROJECT_DIR": "/tmp/home"})
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp/home"})
        start = time.monotonic()
        result = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("merging a PR by hand", result.stderr, "the push rule is linear: the verdict is the real one")
        self.assertLess(time.monotonic() - start, 3)
        # The same input with the old cubic push rule put back first: the watchdog refuses at
        # DEADLINE_SECONDS (7), whatever regex is slow.
        result, took = self.run_patched_guard(self.SLOW_RULE, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not decide within 7 s", result.stderr)
        self.assertLess(took, 9.5, "well inside the hook's 10 s timeout")

    SLOW_RULE = "RULES.insert(0, (re.compile(LEGACY_PUSH), 'old'))"

    def run_patched_guard(self, code, payload):
        """The guard with `code` run before main(), LEGACY_PUSH being 4f7d4a4fd's cubic push rule."""
        legacy = self.load_guard(os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy_guard",
                                              "conductor-guard.py"))
        env = {k: v for k, v in os.environ.items() if k not in ("COORD_AGENT", "CONDUCTOR_WAKE")}
        env.update({"COORD_AGENT": "conductor@Tycho:conductor", "CLAUDE_PROJECT_DIR": "/tmp/home"})
        with tempfile.TemporaryDirectory() as folder:
            patched = os.path.join(folder, "conductor-guard.py")
            with open(GUARD, encoding="utf-8") as handle:
                source = handle.read()
            anchor = '\nif __name__ == "__main__":'
            self.assertEqual(source.count(anchor), 1)
            with open(patched, "w", encoding="utf-8") as handle:
                handle.write(source.replace(anchor, f"\nLEGACY_PUSH = {legacy.RULES[0][0]!r}\n{code}\n" + anchor))
            start = time.monotonic()
            result = subprocess.run([sys.executable, patched], input=payload, capture_output=True, text=True, env=env,
                                    timeout=15)
            return result, time.monotonic() - start

    def test_the_parent_never_waits_for_a_stuck_child(self):
        # Round ten (P3-1): a failing select (or a child stuck in I/O) must not keep the parent
        # waiting past the hook's timeout: it kills the child and refuses at once.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr view 5 # " + "git push " * 780},
                              "cwd": "/tmp/home"})
        result, took = self.run_patched_guard(
            self.SLOW_RULE + "\ndef _broken(*args):\n    raise ValueError('select failed')\nselect.select = _broken", payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not check this call (ValueError)", result.stderr)
        self.assertLess(took, 5, "the slow child was killed, not waited for")

    def test_without_fork_the_guard_refuses(self):
        # Round ten (P3-2): without a checker there is no deadline to keep, so it refuses.
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

    def test_every_rule_is_fast_on_its_own_keywords(self):
        # Rounds nine and ten (P1, P2-3): each rule of RULES and RAW_RULES, the apptoken and write
        # checks and the helpers every verdict runs, on inputs built from its own keywords and
        # punctuation up to the cap. Each rule's keywords must match it (so its inputs engage it),
        # and each rule runs in its own child with a 10 s timeout, reported as a failure.
        import guard_timing
        guard = self.load_guard(GUARD)
        report, failures = guard_timing.measure_in_children(GUARD, timeout=10)
        self.assertEqual(failures, [])
        self.assertEqual([rule["rule"] for rule in report], list(guard_timing.rules(guard)))
        for rule in report:
            self.assertGreater(rule["inputs"], 1, rule)

    def test_the_timing_test_catches_a_cubic_rule(self):
        # Round ten (P2-3): the review's known-cubic rule, put into a copy, must be reported.
        import guard_timing
        with tempfile.TemporaryDirectory() as folder:
            cubic = os.path.join(folder, "conductor-guard.py")
            with open(GUARD, encoding="utf-8") as handle:
                source = handle.read()
            anchor = '\nif __name__ == "__main__":'
            rule = 'RULES.insert(0, (re.compile(r"\\bgh\\s+api\\b[^\\n;&|]*\\bapi\\b[^\\n;&|]*--force"), "cubic"))'
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

    def test_the_keychain_service_name_is_blocked_however_it_is_called(self):
        for command in ("python3 -c \"import subprocess; subprocess.run(['security', 'find-generic-password', "
                        "'-s', 'example-coord-github-app', '-w'])\"",
                        "'security' find-generic-password -s example-coord-github-app -w",
                        "env x=1 bash -c \"security find-generic-password -s example-coord-github-app\""):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        self.assertEqual(self.run_guard("Bash", {"command": "Tools/coord/coord token status"}), 0)

    def test_the_deny_list_names_token_exec(self):
        self.assertIn("Bash(* token exec*)", conductor.DENY)

    def test_the_deny_list_keeps_the_read_tool_out_of_the_token_cache(self):
        # The guard sees Bash only; a conductor run's Read tool is kept out by --settings deny.
        self.assertIn("Read(~/Library/Caches/inisa-coord/**)", conductor.DENY)
        self.assertIn("Read(//Users/*/Library/Caches/inisa-coord/**)", conductor.DENY)

    def test_a_view_is_added_never_substituted(self):
        # Round eleven (P1-1, P1-2): each normalisation is a view the rules read beside the raw
        # command, and each is escape-aware. The metamorphic test cannot see these: the rewrites
        # that make them change the meaning.
        for command in (
                # An escaped backslash, then a newline: not a continuation.
                "echo \\\\\ncoord train approve 5", "echo \\\\\ncoord token exec -- env",
                "echo \\\\\nsecurity find-generic-password -w", 'echo "a\\\\\n" ; gh pr merge 5 ; echo "b"',
                'echo "a\\\\\n" ; git push origin main ; echo "b"', 'echo "a\\\\\n" ; coord train approve 5 ; echo "b"',
                'echo "a\\\\\n" ; sudo ls ; echo "b"', 'echo "a\\\\\n" ; rm -rf Tools/coord ; echo "b"',
                # An escaped `>` or `<`: a literal character, then `|` or `&`.
                "echo x\\>|gh pr merge 5", "echo x\\<&sudo ls", "echo x\\>&coord train approve 5",
                "true\\>|git push origin main", "echo x\\>|security find-generic-password -w",
                "push\\<&sudo --force-f}-f"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)

    def test_the_linear_quote_remover_is_the_regexs(self):
        # Round eleven: the views run executable_text twice, so its quadratic `_QUOTED.sub` is
        # now one pass. It must give the regex's result exactly.
        guard = self.load_guard(GUARD)
        random = __import__("random").Random(11)
        alphabet = ["'", '"', "\\", "\n", "a", " ", "\\'", '\\"', "\\\\", "\\\n", "$", "(", "x"]
        for _ in range(20000):
            text = "".join(random.choice(alphabet) for _ in range(random.randint(0, 16)))
            self.assertEqual(guard._unquoted(text), guard._QUOTED.sub("''", text), repr(text))
        start = time.monotonic()
        guard._unquoted('"' + 'a\\"' * 6663)
        self.assertLess(time.monotonic() - start, 0.1, "one pass over an unclosed quote full of escapes")

    def test_a_view_is_added_and_the_raw_command_always_read(self):
        # Round eleven (Principle C): the raw command and its executable text come first; the
        # joined command, and its text without redirections, are added beside them.
        guard = self.load_guard(GUARD)
        command = "gh pr view 5 \\\n  --json title >/tmp/x"
        commands, texts = guard._views(command)
        self.assertEqual(commands, [command, "gh pr view 5   --json title >/tmp/x"])
        self.assertEqual(texts[0], guard.executable_text(command))
        self.assertEqual(texts[1:], ["gh pr view 5   --json title >/tmp/x", "gh pr view 5   --json title  "])

    def test_the_rest_of_the_shells_spellings_are_blocked(self):
        for command in (
                # P2-1: a space between a redirection and its target; zsh's {fd}.
                "gh > /dev/null pr merge 5", "coord 2> /dev/null train approve 5", "bash > /dev/null -c 'gh pr merge 5'",
                "eval > /dev/null 'gh pr merge 5'", "gh <<< x secret list", "gh >| f auth token", "gh &> f pr merge 5",
                "gh 3> f pr merge 5", "gh {fd}>x pr merge 5", "bash > \"o u t\" -c 'gh pr merge 5'",
                ">|\tsudo  launchctl<<<refs/heads/main",
                # P2-2: every place a command starts.
                "true\nlaunchctl list", "true\nrm -rf Tools/coord", "bash -c 'sudo ls'", "bash -c 'rm -rf Tools/coord'",
                "  sudo ls", "{ sudo ls; }", "! sudo ls", "if true; then sudo ls; fi", "X=1 sudo ls", "command sudo ls",
                "nice sudo ls", "nice -n 5 sudo ls", "timeout 5 sudo ls", "caffeinate sudo ls", "arch -arm64 sudo ls",
                "stdbuf -o0 sudo ls",
                # P3: nine redirections, a shell's options before -c, security's prefixes.
                "bash >a >a >a >a >a >a >a >a >a -c 'gh pr merge 5'", "bash -x -c 'gh pr merge 5'",
                "bash -c -- 'gh pr merge 5'", "bash --norc -c 'gh pr merge 5'", "sh -e -c 'sudo ls'",
                "bash -o pipefail -c 'gh pr merge 5'", "security find-g -s x -w", "security find-generic -w",
                "security dump-k",
                # zsh's `=name` runs the program found on PATH; `\name` is the name, escaped.
                "=sudo ls", "\tsudo\n=launchctl list", "=coord train approve 5", "=security find-generic-password -w",
                "\\sudo ls", "\\coord token exec -- env", "=rm -rf Tools/coord",
                # zsh runs `()cmd` at once (an anonymous function).
                "()sudo ls", "true&()sudo ls", "() coord train approve 5", "()security find-generic-password -w",
                "()rm -rf Tools/coord"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 2, command)
        for command in ("gh pr diff 5 > /tmp/d.txt", "Tools/coord/coord board --brief 2> /dev/null",
                        "cat notes.md | grep -n sudo", "gh pr view 5 --json title < /dev/null", "echo 'x' >> /tmp/log"):
            self.assertEqual(self.run_guard("Bash", {"command": command}), 0, command)

    def test_a_word_ends_at_every_shell_metacharacter(self):
        # Round ten (P2-1): the push rule on its own, before redirections are taken out.
        push = self.load_guard(GUARD).RULES[0][0]
        for metacharacter in " \t;&|()<>`":
            self.assertTrue(push.search(f"git push origin main{metacharacter}x"), metacharacter)
        self.assertFalse(push.search("git push origin mainline"))

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

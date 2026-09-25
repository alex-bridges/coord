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
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cli, conductor, lease, marker, outage, render, snapshot, train_step  # noqa: E402

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
        self.assertEqual([r["key"] for r in conductor.run_reasons([red, running], "main-red", "CI")], ["main-red:2"])
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
                                                     "beat": snapshot.iso(at(-30)),
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

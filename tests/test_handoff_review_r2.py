"""Review round 2 of the handoff relay: one test (or more) per finding. Each fails on the
commit under review (round 1) and passes after the fix. No GitHub writes, no agent runs."""

import contextlib
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from coordlib import EXIT_OK, cache, cli, doctor, marker, relay, snapshot
from coordlib.gh import GitHub, GitHubError
from test_handoff_review import DRIVER, FakeRelay, ReviewCase, T0, WATCHER, UUID
from test_handoff_wake import RECIPIENT, RESTARTED, SENDER

MAILBOX = 1001
COORD_APP = "my-coord-app[bot]"


class AppConfigCase(ReviewCase):
    def setUp(self):
        super().setUp()
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"coord_app_login": COORD_APP, "reviewer_bot_login": "my-bot[bot]"}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        patcher = mock.patch.dict(os.environ, {"COORD_CONFIG": handle.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def by_app(self, payload, text="the work"):
        comment = self.api.add(MAILBOX, marker.compose(text, payload), snapshot.iso(snapshot.now_utc()))
        comment["user"] = {"login": COORD_APP, "type": "Bot"}
        return comment["id"]


class R2_1_TrustTheCoordApp(AppConfigCase):
    def test_r2_1_a_handoff_posted_by_the_configured_coord_app_counts(self):
        self.registered()
        handoff_id = self.by_app({"kind": "msg", "v": 1, "agent": SENDER, "to": [RECIPIENT], "handoff": True})
        code, out, _ = self.run_cli(["handoff", "status", str(handoff_id)], SENDER)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("NOT taken", out)
        self.watch(T0)
        self.assertEqual(len(self.fake.calls), 1)
        code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT)
        self.assertIn("took handoff", out)

    def test_r2_1_an_ack_posted_by_the_coord_app_counts_everywhere(self):
        handoff_id, _ = self.hand_off()
        self.by_app({"kind": "msg", "v": 1, "agent": RECIPIENT, "to": [SENDER], "ack": handoff_id}, "took")
        code, out, _ = self.run_cli(["handoff", "status", str(handoff_id)], SENDER)
        self.assertIn(f"taken by {RECIPIENT}", out)

    def test_r2_1_the_mailbox_trusts_the_coord_app(self):
        messages = [{"on": 77, "payload": {"kind": "msg", "v": 1, "agent": SENDER, "to": ["all"]},
                     "author": {"login": COORD_APP, "type": "Bot"}}]
        self.assertEqual(cli.mailbox_numbers(messages, "o/r"), {77})

    def test_r2_1_doctor_flags_a_local_app_login_the_repository_does_not_trust(self):
        gh = GitHub("o/r", transport="http")
        gh.app = None
        gh.app_mode = types.SimpleNamespace(config={"app_login": "another-app[bot]"}, error=None, on=False)
        report = doctor.inspect(gh)
        self.assertTrue(any("another-app[bot]" in w and "coord_app_login" in w for w in report.get("warnings") or []))
        gh.app_mode = types.SimpleNamespace(config={"app_login": COORD_APP}, error=None, on=False)
        self.assertEqual(doctor.inspect(gh).get("warnings"), [])


class R2_2_TakeNeverLosesAHandoff(ReviewCase):
    def test_r2_2_a_failed_re_read_after_the_ack_keeps_the_handoff(self):
        handoff_id, _ = self.hand_off()
        original = self.api.rest_paginated
        reads = []

        def second_read_fails(path):
            if f"/issues/{MAILBOX}/comments?since=" in path:
                reads.append(path)
                if len(reads) == 2:
                    raise GitHubError("HTTP 502")
            return original(path)

        with mock.patch.object(self.api, "rest_paginated", side_effect=second_read_fails):
            code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("take over PR #42", out)
        self.assertEqual(cache.load_acked("o/r")[str(handoff_id)]["by"], RECIPIENT)

    def test_r2_2_an_ack_post_that_lands_then_raises_is_still_ours_on_the_retry(self):
        handoff_id, _ = self.hand_off()
        original = self.api.rest

        def lost_response(path, method="GET", body=None):
            result = original(path, method, body)
            if method == "POST" and '"ack"' in (body or {}).get("body", ""):
                raise GitHubError("timed out reading the response")
            return result

        with mock.patch.object(self.api, "rest", side_effect=lost_response):
            code, _, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertNotEqual(code, EXIT_OK)
        code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("take over PR #42", out)
        self.assertNotIn("do no new work", out)


class R2_3_ReadLag(ReviewCase):
    def test_r2_3_a_lagging_read_does_not_make_the_first_taker_stand_down(self):
        handoff_id, _ = self.hand_off()
        original = self.api.rest_paginated
        posted = {}

        def lagging(path):
            rows = original(path)
            if "mine" in posted and f"/issues/{MAILBOX}/comments?since=" in path:
                if "racer" not in posted:  # B posts after A, and A's read lists B's ack but not A's
                    racer = self.api.add(MAILBOX, marker.compose("took", {"kind": "msg", "v": 1, "agent": RESTARTED,
                                                                          "to": [SENDER], "ack": handoff_id}),
                                         snapshot.iso(snapshot.now_utc()))
                    posted["racer"] = racer["id"]
                rows = [c for c in original(path) if c["id"] != posted["mine"]]
            return rows

        original_rest = self.api.rest

        def note_post(path, method="GET", body=None):
            result = original_rest(path, method, body)
            if method == "POST":
                posted["mine"] = result["id"]
            return result

        with mock.patch.object(self.api, "rest_paginated", side_effect=lagging), \
                mock.patch.object(self.api, "rest", side_effect=note_post):
            code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertEqual(code, EXIT_OK)
        self.assertGreater(posted["racer"], posted["mine"])
        self.assertIn("took handoff", out)


class R2_4_BadRelayConfiguration(ReviewCase):
    def test_r2_4_a_bad_model_is_reported_never_counted_never_given_up(self):
        self.registered()
        handoff_id, _ = self.hand_off()
        self.env["COORD_RELAY_MODEL"] = "claude-haiku-5-5[1m]"
        lines = []
        for hours in (0, 1, 3, 9):
            lines += self.watch(T0 + dt.timedelta(hours=hours))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.give_ups(handoff_id), [])
        self.assertIn("configuration error", "\n".join(lines))
        state = relay.load_state(self.state_dir)["handoffs"]
        self.assertEqual(state.get(str(handoff_id), {}).get("attempts", 0), 0)


class R2_5_WorkingFolder(ReviewCase):
    def test_r2_5_the_relay_working_folder_is_never_inside_a_work_tree(self):
        self.registered()
        self.hand_off()
        tree = tempfile.mkdtemp()
        os.mkdir(os.path.join(tree, ".git"))
        tmp = os.path.join(tree, ".tmp")
        os.mkdir(tmp)
        with mock.patch.object(tempfile, "tempdir", tmp):
            self.watch(T0)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsNone(relay.inside_work_tree(self.fake.calls[0]["cwd"]))


DRIVER_HUP = DRIVER


@unittest.skipIf(os.name == "nt", "SIGHUP is POSIX")
class R2_6_Sighup(unittest.TestCase):
    def test_r2_6_sighup_to_the_watcher_kills_the_running_relay(self):
        work = tempfile.mkdtemp()
        driver = os.path.join(work, "driver.py")
        with open(driver, "w") as handle:
            handle.write(DRIVER_HUP)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        watcher = subprocess.Popen([sys.executable, driver, root, work], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        pidfile = os.path.join(work, "relay.pid")
        for _ in range(100):
            if os.path.exists(pidfile):
                with open(pidfile) as handle:
                    if handle.read().strip():
                        break
            time.sleep(0.1)
        with open(pidfile) as handle:
            relay_pid = int(handle.read())
        watcher.send_signal(signal.SIGHUP)
        watcher.wait(timeout=10)
        alive = True
        for _ in range(20):
            try:
                os.kill(relay_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
        if alive:
            os.kill(relay_pid, signal.SIGKILL)
        self.assertFalse(alive, "the relay outlived the watcher after SIGHUP")


class R2_6_Smaller(ReviewCase):
    def test_r2_6_dry_run_messages_say_what_a_real_pass_would_do(self):
        self.registered()
        handoff_id, _ = self.hand_off()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(relay.state_path(self.state_dir), "w") as handle:
            handle.write("{not json")
        lines = "\n".join(self.watch(T0, dry_run=True))
        self.assertNotIn("was unreadable; set aside", lines)
        self.assertIn("a real pass sets it aside", lines)
        with open(relay.state_path(self.state_dir), "w") as handle:
            json.dump({"version": 1, "handoffs": {str(handoff_id): 5}}, handle)
        lines = "\n".join(self.watch(T0, dry_run=True))
        self.assertIn("a real pass drops them", lines)
        self.assertFalse(os.path.exists(relay.state_path(self.state_dir) + ".bad"))

    def test_r2_6_a_session_id_in_the_registry_reaches_no_wake_line(self):
        os.makedirs(cache.cache_dir("o/r"), exist_ok=True)
        with open(cache.sessions_path("o/r"), "w") as handle:
            json.dump({"version": 1, "agents": {RECIPIENT: {"name": UUID, "at": None}}}, handle)
        _, out = self.hand_off()
        self.assertNotIn(UUID, out)
        self.assertIn("no session on this machine is registered", out)

    def test_r2_6_watch_json_is_always_json(self):
        with mock.patch.dict(os.environ, self.env):
            code, out, _ = self.run_cli(["handoff", "watch", "--json"], WATCHER)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["status"], "ok")

    @unittest.skipIf(os.name == "nt", "HOME decides ~ on POSIX")
    def test_r2_6_a_home_folder_that_is_a_work_tree_gets_a_way_out(self):
        home = tempfile.mkdtemp()
        os.mkdir(os.path.join(home, ".git"))
        with mock.patch.dict(os.environ, {"HOME": home}):
            with self.assertRaises(ValueError) as caught:
                relay.state_dir("o/r", {})
        self.assertIn("outside your home folder", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

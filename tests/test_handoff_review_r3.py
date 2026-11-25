"""Review round 3 of the handoff relay (coord issue #4): one test (or more) per finding.
Each fails at the merge of #3 and passes after the fix. No GitHub writes, no agent runs."""

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

from coordlib import EXIT_OK, cache, doctor, marker, snapshot
from coordlib.gh import GitHub, GitHubError
from test_handoff_review import DRIVER, ReviewCase
from test_handoff_wake import RECIPIENT, RESTARTED, SENDER

MAILBOX = 1001


class RaceCase(ReviewCase):
    """B (RECIPIENT) takes; A (RESTARTED, the other recipient) acks first, right after B's check."""

    def racing_take(self, handoff_id, re_read_error=None, a_records_locally=False, args=()):
        original = self.api.rest_paginated
        reads = []

        def reads_hook(path):
            if f"/issues/{MAILBOX}/comments?since=" not in path:
                return original(path)
            reads.append(path)
            rows = original(path)
            if len(reads) == 1:  # B's check saw no ack; now A acks (a smaller id than B's ack)
                a = self.api.add(MAILBOX, marker.compose("took", {"kind": "msg", "v": 1, "agent": RESTARTED,
                                                                  "to": [SENDER], "ack": handoff_id}),
                                 snapshot.iso(snapshot.now_utc()))
                if a_records_locally:
                    cache.remember_acked("o/r", handoff_id, {"id": a["id"], "by": RESTARTED,
                                                             "at": a["created_at"], "url": a["html_url"]})
            elif len(reads) == 2 and re_read_error is not None:
                raise re_read_error
            return rows

        with mock.patch.object(self.api, "rest_paginated", side_effect=reads_hook):
            return self.run_cli(["handoff", "take", str(handoff_id), *args], RECIPIENT, board=False)

    def two_recipient_handoff(self):
        code, _, err = self.run_cli(["send", "--handoff", "--to", RECIPIENT, "--to", RESTARTED, "x"], SENDER)
        self.assertEqual(code, EXIT_OK, err)
        return self.api.comments[-1]["id"]


class R3_1_RaceMeetsAFailedReRead(RaceCase):
    def test_r3_1_t1_a_failed_re_read_is_unconfirmed_and_the_rerun_stands_down(self):
        handoff_id = self.two_recipient_handoff()
        code, out, err = self.racing_take(handoff_id, GitHubError("HTTP 502"), args=("--json",))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["status"], "unconfirmed")
        self.assertIn("UNCONFIRMED", err)
        code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertIn(f"already taken by {RESTARTED}", out)
        self.assertNotIn("The work:", out)

    def test_r3_1_t2_stopped_before_the_re_read_the_retry_asks_github(self):
        handoff_id = self.two_recipient_handoff()
        with self.assertRaises(KeyboardInterrupt):
            self.racing_take(handoff_id, KeyboardInterrupt())
        code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertIn(f"already taken by {RESTARTED}", out)
        self.assertNotIn("The work:", out)
        self.assertEqual(cache.load_acked("o/r")[str(handoff_id)]["by"], RESTARTED)

    def test_r3_1_our_own_ack_replaces_this_machines_record(self):
        handoff_id = self.two_recipient_handoff()
        self.racing_take(handoff_id, GitHubError("HTTP 502"), a_records_locally=True)
        self.assertEqual(cache.load_acked("o/r")[str(handoff_id)]["by"], RECIPIENT)

    def test_r3_1_already_yours_covers_a_finished_handoff(self):
        handoff_id, _ = self.hand_off()
        self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT)
        code, out, _ = self.run_cli(["handoff", "take", str(handoff_id)], RECIPIENT, board=False)
        self.assertIn("If you have finished it, there is nothing to do", out)


@unittest.skipIf(os.name == "nt", "POSIX signal dispositions")
class R3_2_InheritedIgnore(unittest.TestCase):
    def run_ignoring(self, signum):
        work = tempfile.mkdtemp()
        driver = os.path.join(work, "driver.py")
        with open(driver, "w") as handle:
            handle.write(DRIVER)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        watcher = subprocess.Popen([sys.executable, driver, root, work], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, preexec_fn=lambda: signal.signal(signum, signal.SIG_IGN))
        pidfile = os.path.join(work, "relay.pid")
        for _ in range(100):
            if os.path.exists(pidfile):
                with open(pidfile) as handle:
                    if handle.read().strip():
                        break
            time.sleep(0.1)
        with open(pidfile) as handle:
            relay_pid = int(handle.read())
        watcher.send_signal(signum)
        time.sleep(1.0)
        watcher_alive = watcher.poll() is None
        try:
            os.kill(relay_pid, 0)
            relay_alive = True
        except ProcessLookupError:
            relay_alive = False
        watcher.send_signal(signal.SIGTERM)
        watcher.wait(timeout=10)
        try:
            os.kill(relay_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return watcher_alive, relay_alive

    def test_r3_2_an_inherited_sighup_ignore_is_left_alone(self):
        self.assertEqual(self.run_ignoring(signal.SIGHUP), (True, True))

    def test_r3_2_an_inherited_sigint_ignore_is_left_alone(self):
        self.assertEqual(self.run_ignoring(signal.SIGINT), (True, True))


class R3_3_AppLogins(ReviewCase):
    def configure(self, values):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(values, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        patcher = mock.patch.dict(os.environ, {"COORD_CONFIG": handle.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def handoff_by(self, login, account):
        comment = self.api.add(MAILBOX, marker.compose("w", {"kind": "msg", "v": 1, "agent": SENDER,
                                                             "to": [RECIPIENT], "handoff": True}),
                               snapshot.iso(snapshot.now_utc()))
        comment["user"] = {"login": login, "type": account}
        return comment["id"]

    def test_r3_3_an_app_login_without_the_bot_suffix_trusts_nobody(self):
        self.configure({"coord_app_login": "my-coord-app"})
        handoff_id = self.handoff_by("my-coord-app", "User")
        code, _, _ = self.run_cli(["handoff", "status", str(handoff_id)], SENDER)
        self.assertNotEqual(code, EXIT_OK)

    def test_r3_3_app_logins_compare_without_case(self):
        self.configure({"coord_app_login": "My-Coord-App[bot]"})
        handoff_id = self.handoff_by("my-coord-app[bot]", "Bot")
        code, out, _ = self.run_cli(["handoff", "status", str(handoff_id)], SENDER)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("NOT taken", out)

    def test_r3_3_doctor_notes_app_mode_without_app_login_and_a_login_without_bot(self):
        self.configure({"coord_app_login": "my-coord-app"})
        gh = GitHub("o/r", transport="http")
        gh.app = None
        gh.app_mode = types.SimpleNamespace(config={}, error=None, on=True)
        warnings = doctor.inspect(gh).get("warnings") or []
        self.assertTrue(any("no app_login" in w for w in warnings), warnings)
        self.assertTrue(any("without the [bot] suffix" in w for w in warnings), warnings)


if __name__ == "__main__":
    unittest.main()

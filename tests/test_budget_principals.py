"""Budgets per principal (#1035). Run: python3 -m unittest discover -s Tools/coord/tests

The user principal (Alex's counter) keeps #1033's rule, and test_budget.py, unchanged,
is its witness. These tests cover what is new: the app installation's two concurrent
counters (the sum of each window's maximum), the file format and its migration, readings
recorded under the principal whose token made the call, and gating on that principal
without ever spilling onto the other.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import INSTALLATION, REPO, AppTestCase, rate_headers  # noqa: E402
from coordlib import EXIT_BUDGET, EXIT_OK, budget, cache, cli  # noqa: E402
from coordlib.gh import GitHub  # noqa: E402

APP = budget.app_principal(INSTALLATION)

# The probe of 2026-09-25 (#1035): 25 sequential calls. Calls 1-9 reported one reset with
# used 32..40, calls 10-25 a reset 3 s later with used 23..38. Their sum of maxima, 78,
# roughly matched the REST calls the probes had made.
RESET_A_OFFSET = 1800
USED_A = list(range(32, 41))   # 9 readings
USED_B = list(range(23, 39))   # 16 readings


def measured(reset_a: int, order: str):
    a = [(reset_a, used) for used in USED_A]
    b = [(reset_a + 3, used) for used in USED_B]
    if order == "sequential":
        return a + b
    out = []  # interleaved: consecutive calls alternate between the counters (P1)
    for index in range(max(len(a), len(b))):
        out += a[index:index + 1] + b[index:index + 1]
    return out


class PrincipalTestCase(AppTestCase):
    def feed(self, reset: int, used: int, principal: str = APP, resource: str = "core", limit: int = 5000):
        return budget.record(REPO, rate_headers(used, reset, resource, limit), resource, principal)

    def estimate(self, principal: str = APP, resource: str = "core"):
        return budget.current(REPO, resource, principal=principal)


class TwoCounterTests(PrincipalTestCase):
    def test_the_measured_sequence_sums_the_maxima_of_both_counters(self):
        for order in ("sequential", "interleaved"):
            with self.subTest(order=order):
                cache.store_principal(REPO, APP, {})
                reset_a = int(time.time()) + RESET_A_OFFSET
                for reset, used in measured(reset_a, order):
                    self.feed(reset, used)
                reading = self.estimate()
                self.assertEqual(reading["used"], 40 + 38)
                self.assertEqual(budget.left(reading), 5000 - 78)
                self.assertEqual(sorted((w["reset"], w["used"]) for w in reading["windows"]),
                                 [(reset_a, 40), (reset_a + 3, 38)])

    def test_the_latest_reading_alone_would_be_wrong(self):
        # What #1033's user rule would say for this principal: the last reading only.
        reset_a = int(time.time()) + RESET_A_OFFSET
        for reset, used in measured(reset_a, "sequential"):
            self.feed(reset, used)
        self.assertNotEqual(budget.left(self.estimate()), 5000 - 38)

    def test_a_lower_reading_never_lowers_the_estimate(self):
        reset_a = int(time.time()) + RESET_A_OFFSET
        seen = []
        for reset, used in measured(reset_a, "interleaved"):
            self.feed(reset, used)
            seen.append(self.estimate()["used"])
        self.assertEqual(seen, sorted(seen), "the estimate went down between two calls")
        self.feed(reset_a, 12)  # a stale or reordered reading for counter A
        self.feed(reset_a + 3, 1)
        self.assertEqual(self.estimate()["used"], 78)

    def test_windows_whose_reset_has_passed_drop_out(self):
        now = int(time.time())
        self.feed(now + 1800, 40)
        stored = cache.load_principal(REPO, APP)["core"]
        stored["windows"].append({"reset": now - 5, "used": 4000, "limit": 5000, "at": now - 3000})
        cache.store_principal(REPO, APP, {"core": stored})
        self.assertEqual(self.estimate()["used"], 40, "a passed window does not count")
        self.feed(now + 1800, 41)
        resets = [w["reset"] for w in cache.load_principal(REPO, APP)["core"]["windows"]]
        self.assertEqual(resets, [now + 1800], "the next record drops it from the file")

    def test_a_window_counts_for_an_hour_at_most(self):
        now = time.time()
        far = int(now) + 10 * 3600
        cache.store_principal(REPO, APP, {"core": {"windows": [
            {"reset": far, "used": 4900, "limit": 5000, "at": now - 3601}]}})
        self.assertIsNone(self.estimate())

    def test_every_live_window_is_one_budget_with_the_same_floors(self):
        now = int(time.time())
        self.feed(now + 1000, 2400)
        self.feed(now + 1003, 2400)  # 200 left in all, though each counter reads 2,600 remaining
        reading = self.estimate()
        self.assertEqual(budget.left(reading), 200)
        self.assertEqual(budget.decision("train add", reading)[0], budget.REFUSE)
        self.assertEqual(budget.decision("board", reading)[0], budget.LOW)
        self.assertIn("GitHub API budget low: 200 left on the GitHub App until", budget.low_words(reading))
        self.assertEqual(budget.until(reading), now + 1000, "the first window's end frees budget first")

    def test_a_malformed_app_entry_is_no_reading(self):
        now = int(time.time())
        for stored in ({"windows": "x"}, {"windows": [None, 3]}, {"windows": [{"reset": "1", "used": 5, "at": now}]},
                       {"windows": [{"reset": now + 60, "used": True, "at": now}]}, "text", None,
                       {"windows": [{"reset": now + 60, "used": 5, "at": float("nan")}]}):
            with self.subTest(stored=stored):
                cache.store_principal(REPO, APP, {"core": stored} if stored is not None else {})
                self.assertIsNone(self.estimate())
                self.assertEqual(budget.decision("train add", self.estimate()), (budget.OK, ""))

    def test_graphql_is_its_own_resource(self):
        now = int(time.time())
        self.feed(now + 1800, 70, resource="graphql")
        self.feed(now + 1803, 9, resource="graphql")
        self.assertEqual(self.estimate(resource="graphql")["used"], 79)
        self.assertIsNone(self.estimate(resource="core"))


class UserPrincipalTests(PrincipalTestCase):
    def test_the_user_principal_keeps_the_latest_reading(self):
        # #1033's rule, unchanged: one counter, the latest reading wins, no maximum kept.
        now = int(time.time())
        self.feed(now + 1800, 4000, principal=budget.USER)
        self.feed(now + 1800, 3000, principal=budget.USER)
        reading = budget.current(REPO)
        self.assertEqual(reading["used"], 3000)
        self.assertNotIn("windows", reading)
        self.assertEqual(budget.left(reading), 2000)

    def test_app_exhaustion_never_touches_the_user_principal(self):
        now = int(time.time())
        self.feed(now + 1800, 1000, principal=budget.USER)
        before = cache.load_budget(REPO)
        self.feed(now + 1800, 2500)
        self.feed(now + 1803, 2500)  # the app is spent
        self.assertEqual(budget.left(self.estimate()), 0)
        self.assertEqual(cache.load_budget(REPO), before, "Alex's reading did not move")
        self.assertEqual(budget.decision("train add", budget.current(REPO))[0], budget.OK)

    def test_user_exhaustion_never_touches_the_app_principal(self):
        now = int(time.time())
        self.feed(now + 1800, 50)
        self.feed(now + 1800, 5000, principal=budget.USER)
        self.assertEqual(budget.left(budget.current(REPO)), 0)
        self.assertEqual(budget.left(self.estimate()), 4950)


class FileFormatTests(PrincipalTestCase):
    def write_raw(self, data, path=None):
        path = path or cache.budget_path(REPO)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def read_raw(self, path=None):
        with open(path or cache.budget_path(REPO), encoding="utf-8") as handle:
            return json.load(handle)

    def test_alex_in_budget_json_as_1033_writes_it_and_the_app_in_a_file_of_its_own(self):
        # Codex on 2d13f6b1e (P2): a coord from before #1035 replaces budget.json without a
        # lock, so the app's readings there could be lost or rolled back. They live in
        # budget-app.json, which that coord never opens.
        now = int(time.time())
        old = {"core": {"remaining": 1234, "limit": 5000, "used": 3766, "reset": now + 1800, "at": now}}
        self.write_raw(old)  # a file #1033 wrote
        self.assertEqual(cache.load_budget(REPO), old)
        self.assertEqual(budget.left(budget.current(REPO)), 1234)
        self.assertEqual(cache.load_principal(REPO, APP), {})
        self.feed(now + 1800, 40)
        self.assertEqual(self.read_raw(), old, "an app reading never touches budget.json")
        app = self.read_raw(cache.app_budget_path(REPO))
        self.assertEqual(set(app), {APP})
        self.assertEqual(app[APP]["core"]["windows"][0]["used"], 40)
        self.feed(now + 1800, 100, principal=budget.USER)
        self.assertEqual(set(self.read_raw()), {"core"}, "Alex's file holds only his resources")
        self.assertEqual(self.estimate()["used"], 40, "writing Alex's reading keeps the app's")
        self.feed(now + 1800, 7, principal=budget.app_principal(999))  # after a reinstall
        self.assertEqual(set(self.read_raw(cache.app_budget_path(REPO))), {APP, budget.app_principal(999)})
        self.assertEqual(self.estimate()["used"], 40, "each app principal keeps its own")

    def test_app_keys_an_earlier_head_left_in_budget_json_are_ignored_and_dropped(self):
        # Earlier heads of #1046 kept the app under "app:<id>" in budget.json. No merged coord
        # did: they are ignored (not moved), and the next write of Alex's readings drops them.
        now = int(time.time())
        reading = {"remaining": 900, "limit": 5000, "used": 4100, "reset": now + 1800, "at": now}
        self.write_raw({"core": reading, APP: {"core": {"windows": [{"used": 4000, "reset": now + 1800, "at": now}]}}})
        self.assertIsNone(self.estimate(), "not the app's reading any more")
        self.assertEqual(cache.load_budget(REPO), {"core": reading})
        self.feed(now + 1800, 101, principal=budget.USER)
        self.assertEqual(set(self.read_raw()), {"core"})

    def test_the_first_drafts_nested_file_heals(self):
        now = int(time.time())
        reading = {"remaining": 900, "limit": 5000, "used": 4100, "reset": now + 1800, "at": now}
        nested = {"core": dict(reading)}
        for _ in range(60):  # what alternating the draft's writer with an old coord produced
            nested = {"user": nested, "version": 2, "core": dict(reading), APP: {"core": {"windows": []}}}
        self.write_raw(nested)
        self.assertEqual(budget.current(REPO)["used"], 4100)
        self.feed(now + 1800, 4200, principal=budget.USER)
        self.assertEqual(set(self.read_raw()), {"core"})
        self.assertLess(os.path.getsize(cache.budget_path(REPO)), 1024)

    def test_a_file_too_deep_to_parse_fails_open_and_is_replaced(self):
        # Deep enough that json.load raises RecursionError on every Python this runs on:
        # 3.9 gives up near 1,000 levels, 3.14's C parser only between 100,000 and 400,000.
        depth = 1_000_000
        content = '{"user":' * depth + "{}" + "}" * depth
        with self.assertRaises(RecursionError, msg="the fixture must really be too deep to parse"):
            json.loads(content)
        os.makedirs(os.path.dirname(cache.budget_path(REPO)), exist_ok=True)
        with open(cache.budget_path(REPO), "w", encoding="utf-8") as handle:
            handle.write(content)
        self.assertIsNone(budget.current(REPO))
        self.assertIsNone(self.estimate())
        self.assertEqual(budget.decision("train add", budget.current(REPO)), (budget.OK, ""))
        now = int(time.time())
        self.feed(now + 1800, 50, principal=budget.USER)
        self.assertEqual(budget.current(REPO)["used"], 50)
        self.assertLess(os.path.getsize(cache.budget_path(REPO)), 1024)

    def test_a_malformed_file_fails_open(self):
        for data in ({"version": 2, "user": "x"}, {APP: [1]}, {APP: {"core": "x"}}, {"core": 3}, [1], "x"):
            for path in (cache.budget_path(REPO), cache.app_budget_path(REPO)):
                with self.subTest(data=data, path=os.path.basename(path)):
                    self.write_raw(data, path)
                    self.assertIsNone(budget.current(REPO))
                    self.assertIsNone(self.estimate())


class LockTests(PrincipalTestCase):
    """record() reads, merges and writes budget.json inside cache.budget_lock, so two agents
    recording at once cannot lose each other's window maximum (review of #1046, X14). A
    spy, not a race: deterministic."""

    def test_the_read_modify_write_happens_inside_the_lock(self):
        import contextlib
        events = []
        real_load, real_store = cache.load_principal, cache.store_principal

        @contextlib.contextmanager
        def spy_lock(repo, timeout_seconds=5.0, principal=budget.USER):
            events.append(f"lock {principal}")
            yield
            events.append("unlock")

        self.patch(cache, "budget_lock", spy_lock)
        self.patch(cache, "load_principal", lambda *a: events.append("load") or real_load(*a))
        self.patch(cache, "store_principal", lambda *a: events.append("store") or real_store(*a))
        for principal in (APP, budget.USER):
            with self.subTest(principal=principal):
                events.clear()
                self.feed(int(time.time()) + 1800, 10, principal=principal)
                self.assertEqual(events, [f"lock {principal}", "load", "store", "unlock"])

    def test_the_lock_is_a_real_flock_on_a_file_in_the_cache(self):
        import fcntl
        for principal, name in ((budget.USER, "budget.lock"), (APP, "budget-app.lock")):
            with self.subTest(principal=principal), cache.budget_lock(REPO, principal=principal):
                fd = os.open(os.path.join(cache.cache_dir(REPO), name), os.O_RDWR)
                self.addCleanup(os.close, fd)
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class LegacyWriterTests(PrincipalTestCase):
    """The coord from before #1035 (tests/legacy_coordlib, a frozen copy) and this one on
    ONE cache directory, turn about: the file stays small and both read what they wrote."""

    def test_old_and_new_writers_share_the_file(self):
        from legacy_coordlib import budget as old_budget
        from legacy_coordlib import cache as old_cache
        now = int(time.time())
        sizes = []
        for turn in range(250):
            user_used = 1000 + turn
            old_budget.record(REPO, rate_headers(user_used, now + 1800, "core"), "core")
            self.feed(now + 1800 + turn % 2 * 3, 20 + turn)  # the app's two counters, alternating
            budget.record(REPO, rate_headers(user_used + 1, now + 1800, "graphql"), "graphql", budget.USER)
            old_budget.record(REPO, rate_headers(user_used + 2, now + 1800, "graphql"), "graphql")
            sizes.append(os.path.getsize(cache.budget_path(REPO)))
            self.assertEqual(old_budget.current(REPO)["used"], user_used, turn)
            self.assertEqual(budget.current(REPO)["used"], user_used, turn)
            self.assertEqual(budget.current(REPO, "graphql")["used"], user_used + 2, turn)
            self.assertEqual(self.estimate()["used"], (20 + turn) + (19 + turn if turn else 0), turn)
        self.assertLess(max(sizes), 2048, "the file does not grow with alternation")
        self.assertEqual(set(old_cache.load_budget(REPO)), {"core", "graphql"}, "budget.json is #1033's")

    def test_a_legacy_writer_that_replaces_budget_json_cannot_touch_the_apps_readings(self):
        # Codex on 2d13f6b1e (P2): a coord from before #1035 takes no lock. It loads
        # budget.json, the app records a reading, then it writes what it loaded back.
        from legacy_coordlib import budget as old_budget
        from legacy_coordlib import cache as old_cache
        now = int(time.time())
        old_budget.record(REPO, rate_headers(1000, now + 1800, "core"), "core")
        self.feed(now + 1800, 30)
        loaded = old_cache.load_budget(REPO)
        self.feed(now + 1800, 60)  # the app records between the legacy load and store
        loaded["core"] = dict(loaded["core"], used=1001)
        old_cache.store_budget(REPO, loaded)
        self.assertEqual(self.estimate()["used"], 60, "not lost, not rolled back to 30")
        os.remove(cache.budget_path(REPO))  # or the file is deleted, or replaced by anything
        self.assertEqual(self.estimate()["used"], 60)


class FakeApp:
    """A token source for GitHub (apptoken.AppTokens' interface) that hands out fixed tokens."""

    def __init__(self, server, installation=INSTALLATION):
        self.server = server
        self.installation = installation
        self.config = {"app_login": "inisa-sidechain[bot]", "client_id": "c", "repo": REPO}
        self.issued = {}

    def token(self, kind):
        if kind not in self.issued:
            token = f"ghs_fake{kind}token0000"
            self.server.tokens[token] = kind
            self.issued[kind] = token
        return self.issued[kind]

    def installation_id(self):
        return self.installation


class RecordingTests(PrincipalTestCase):
    def test_an_app_call_is_recorded_under_the_app_and_a_user_call_under_user(self):
        GitHub(REPO, transport="http", app=FakeApp(self.server)).rest("repos/{repo}/issues/comments?per_page=1")
        self.assertEqual(cache.load_budget(REPO), {}, "the app's reading is not Alex's")
        self.assertEqual(self.estimate()["used"], self.server.used)
        user = GitHub(REPO, transport="http", token="user-token")
        self.server.tokens["user-token"] = "user"
        user.rest("repos/{repo}/issues/comments?per_page=1")
        self.assertEqual(budget.current(REPO)["used"], self.server.used)
        self.assertEqual(len(self.estimate()["windows"]), 1, "the user call added nothing to the app")

    def test_the_gh_probe_records_under_user(self):
        GitHub(REPO, runner=self.runner).probe_budget()
        self.assertEqual(budget.current(REPO)["used"], 1000)
        self.assertEqual(cache.load_principal(REPO, APP), {})


class GateTests(PrincipalTestCase):
    """cli.gate reads the principal the command spends."""

    def gate(self, argv, gh):
        return cli.gate(cli.parser().parse_args(argv), gh)

    def test_app_mode_gates_on_the_app_and_never_probes_alex(self):
        now = int(time.time())
        self.feed(now + 1800, 2400)
        self.feed(now + 1803, 2450)  # 150 left on the app
        self.feed(now + 1800, 10, principal=budget.USER)  # Alex has plenty
        gh = GitHub(REPO, app=FakeApp(self.server))
        code, out, err = self.run_cli(["train", "add", "101"], gh)
        self.assertEqual(code, EXIT_BUDGET)
        self.assertIn("GitHub API budget low: 150 left on the GitHub App", err)
        self.assertEqual(self.runner.calls, [], "no gh probe, no call on Alex's identity")
        self.assertEqual(self.server.requests, [])

    def test_an_exhausted_app_serves_the_board_from_cache_and_never_spills(self):
        now = int(time.time())
        self.feed(now + 1800, 2500)
        self.feed(now + 1803, 2500)
        self.seed_snapshot()
        gh = GitHub(REPO, app=FakeApp(self.server))
        code, out, err = self.run_cli(["board"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("GitHub API budget low: 0 left on the GitHub App", err)
        self.assertIn("showing the snapshot from 60m ago", err)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.server.requests, [])
        self.assertIn("API (app): 0 left", out.splitlines()[0])

    def test_user_identity_commands_gate_on_alex(self):
        now = int(time.time())
        self.feed(now + 1800, 2500)
        self.feed(now + 1803, 2500)  # the app is spent; Alex is not
        self.feed(now + 1800, 100, principal=budget.USER)
        gh = GitHub(REPO, app=FakeApp(self.server), transport="gh")
        code, _, err = self.run_cli(["train", "approve", "101", "--note", "Alex said so"], gh)
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(self.server.requests, [], "an attestation never goes through the app")

    def test_no_installation_yet_is_no_reading(self):
        app = FakeApp(self.server, installation=None)
        self.assertIsNone(GitHub(REPO, app=app).principal())
        self.assertIsNone(self.gate(["train", "add", "7"], GitHub(REPO, app=app)))

    def run_cli(self, argv, gh):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(cli.parser().parse_args(argv), gh)
        return code, out.getvalue(), err.getvalue()


class HeaderTests(PrincipalTestCase):
    def test_the_user_line_is_unchanged_and_the_app_line_shows_both(self):
        now = int(time.time())
        self.assertEqual(budget.header_words(REPO), "API: no current reading")
        self.feed(now + 1800, 1000, principal=budget.USER)
        self.feed(now + 1800, 30)
        self.feed(now + 1803, 20)
        clock = budget.clock(now + 1800)
        self.assertEqual(budget.header_words(REPO), f"API: 4000 left (resets {clock})")
        self.assertEqual(budget.header_words(REPO, principal=APP),
                         f"API (app): 4950 left (resets {clock}) · Alex: 4000 left (resets {clock})")
        self.assertEqual(budget.header_words(REPO, principal=None),
                         f"API (app): no current reading · Alex: 4000 left (resets {clock})")


if __name__ == "__main__":
    unittest.main()

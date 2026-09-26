"""Tests for the GitHub API budget guard. Run: python3 -m unittest discover -s Tools/coord/tests

The reading (budget.py, recorded by gh.py from real response headers or a `gh api -i`
probe), the guard (cli.gate / cli.run) and the SessionStart hook. Every GitHub here is
local: HTTPS through a fake `urlopen`, `gh` through a fake runner, and a transport that
fails the test on any call where a command must spend nothing.
"""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import EXIT_BUDGET, EXIT_NOT_FOUND, EXIT_OK, budget, cache, cli  # noqa: E402
from coordlib import gh as ghmodule  # noqa: E402
from coordlib.gh import GitHub, GitHubError  # noqa: E402

REPO = "o/r"
BILLING_RESET = int(time.time()) + 3600


def probe_output(remaining: int, reset: int = BILLING_RESET, status: str = "200 OK", limit: int = 5000) -> str:
    """`gh api -i` output, as recorded from a real probe on 2026-09-25 (CRLF, canonical case)."""
    return (f"HTTP/2.0 {status}\r\nContent-Type: application/json\r\nX-Ratelimit-Limit: {limit}\r\n"
            f"X-Ratelimit-Remaining: {remaining}\r\nX-Ratelimit-Reset: {reset}\r\n"
            f"X-Ratelimit-Resource: core\r\nX-Ratelimit-Used: {limit - remaining}\r\n\r\n"
            '{"id":100000001,"full_name":"o/r"}')


class BudgetTestCase(unittest.TestCase):
    """A private cache directory, default floors, and not in Actions."""

    ENV = ("COORD_CACHE_DIR", "COORD_BUDGET_READ_FLOOR", "COORD_BUDGET_HARD_FLOOR", "COORD_BUDGET_USER_LIMIT",
           "GITHUB_ACTIONS", "COORD_AGENT")

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in self.ENV}
        for name in self.ENV:
            os.environ.pop(name, None)
        self.cache_root = tempfile.mkdtemp()
        os.environ["COORD_CACHE_DIR"] = self.cache_root
        os.environ["COORD_AGENT"] = "claude@test:budget"

    def tearDown(self):
        shutil.rmtree(self.cache_root, ignore_errors=True)
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # MARK: fixtures

    def seed(self, remaining, reset=None, age=0.0, resource="core", limit=5000, used=None):
        readings = cache.load_budget(REPO)
        readings[resource] = {"remaining": remaining, "limit": limit, "used": limit - remaining if used is None else used,
                              "reset": BILLING_RESET if reset is None else reset, "at": time.time() - age}
        cache.store_budget(REPO, readings)

    def write_budget_file(self, content):
        """budget.json exactly as given: a hand edit, a half-written file, another format."""
        os.makedirs(os.path.dirname(cache.budget_path(REPO)), exist_ok=True)
        with open(cache.budget_path(REPO), "w", encoding="utf-8") as handle:
            handle.write(content if isinstance(content, str) else json.dumps(content))

    def seed_snapshot(self, age_seconds=3600):
        then = time.time() - age_seconds
        cache.store_snapshot(REPO, {
            "schema": 1, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(then)), "repo": REPO,
            "main": {"sha": "f" * 40}, "prs": [], "claims": [], "overlaps": [],
            "train": [], "runs": {"running": [], "queued": []}, "messages": [], "apiCalls": 9,
        })
        os.utime(cache.snapshot_path(REPO), (then, then))

    def silent_http(self):
        """An HTTPS GitHub that fails the test on any call."""
        gh = GitHub(REPO, transport="http", token="t")

        def refuse(method, url, body=None):
            raise AssertionError(f"no API call expected, got {method} {url}")

        gh._http = refuse
        return gh

    def run_cli(self, argv, gh):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.run(cli.parser().parse_args(argv), gh)
        return code, out.getvalue(), err.getvalue()


class FakeResponse:
    def __init__(self, body, headers):
        self._body = json.dumps(body).encode()
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def rate_headers(remaining, resource, reset=BILLING_RESET, limit=5000):
    return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Used": str(limit - remaining), "X-RateLimit-Reset": str(reset),
            "X-RateLimit-Resource": resource}


class RecordingTests(BudgetTestCase):
    def with_urlopen(self, fake):
        original = ghmodule.urllib.request.urlopen
        ghmodule.urllib.request.urlopen = fake
        self.addCleanup(setattr, ghmodule.urllib.request, "urlopen", original)

    def test_headers_are_recorded_per_resource(self):
        def urlopen(request, timeout=None):
            if request.full_url.endswith("/graphql"):
                return FakeResponse({"data": {"viewer": {}}}, rate_headers(4900, "graphql"))
            return FakeResponse({"number": 1}, rate_headers(4000, "core"))

        self.with_urlopen(urlopen)
        gh = GitHub(REPO, transport="http", token="t")
        gh.rest("repos/{repo}/issues/1")
        gh.graphql("query { viewer { login } }")
        readings = cache.load_budget(REPO)
        self.assertEqual((readings["core"]["remaining"], readings["core"]["reset"]), (4000, BILLING_RESET))
        self.assertEqual((readings["graphql"]["remaining"], readings["graphql"]["reset"]), (4900, BILLING_RESET))
        self.assertEqual(budget.header_words(REPO),
                         f"API: 4000 left (resets {budget.clock(BILLING_RESET)}), GraphQL 4900 left")

    def test_the_403_of_an_exhausted_budget_is_recorded(self):
        def urlopen(request, timeout=None):
            headers = email.message.Message()
            for name, value in rate_headers(0, "core").items():
                headers[name] = value
            error = urllib.error.HTTPError(request.full_url, 403, "rate limit exceeded", headers,
                                           io.BytesIO(b'{"message":"API rate limit exceeded"}'))
            self.addCleanup(error.close)
            raise error

        self.with_urlopen(urlopen)
        with self.assertRaises(GitHubError):
            GitHub(REPO, transport="http", token="t").rest("repos/{repo}/issues/1")
        self.assertEqual(cache.load_budget(REPO)["core"]["remaining"], 0)

    def test_a_response_without_budget_headers_keeps_the_reading(self):
        self.seed(1234)
        self.with_urlopen(lambda request, timeout=None: FakeResponse({}, {}))
        GitHub(REPO, transport="http", token="t").rest("repos/{repo}/issues/1")
        self.assertEqual(cache.load_budget(REPO)["core"]["remaining"], 1234)

    def test_the_actions_token_does_not_stand_in_for_the_shared_budget(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self.with_urlopen(lambda request, timeout=None: FakeResponse({}, rate_headers(900, "core")))
        GitHub(REPO, transport="http", token="t").rest("repos/{repo}/issues/1")
        self.assertEqual(cache.load_budget(REPO), {})

    def test_the_probe_reads_gh_api_i_even_when_the_call_is_refused(self):
        calls = []

        def runner(args, stdin):
            calls.append(list(args))
            code, status = (0, "200 OK") if len(calls) == 1 else (1, "403 Forbidden")
            return subprocess.CompletedProcess(args, code, probe_output(4913 if code == 0 else 0, status=status),
                                               "gh: API rate limit exceeded (HTTP 403)" if code else "")

        gh = GitHub(REPO, runner=runner)
        self.assertEqual(gh.probe_budget()["remaining"], 4913)
        self.assertEqual(calls, [["gh", "api", "-i", "repos/o/r"]], "one call, never the rate_limit endpoint")
        self.assertEqual(gh.probe_budget()["remaining"], 0, "a refused probe still carries the reading")
        self.assertEqual(cache.load_budget(REPO)["core"]["remaining"], 0)
        self.assertEqual(gh.calls, 2)


class ProbeTests(BudgetTestCase):
    def gh_transport(self):
        self.probes = []

        def runner(args, stdin):
            if list(args[:3]) != ["gh", "api", "-i"]:
                raise AssertionError(f"only the probe may run here, got {args}")
            self.probes.append(list(args))
            return subprocess.CompletedProcess(args, 0, probe_output(4321), "")

        return GitHub(REPO, runner=runner)

    def gate(self, argv, gh):
        return cli.gate(cli.parser().parse_args(argv), gh)

    def test_no_reading_on_the_gh_transport_probes_once(self):
        gh = self.gh_transport()
        self.assertIsNone(self.gate(["board"], gh))
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(budget.current(REPO)["remaining"], 4321)

    def test_a_fresh_reading_is_not_probed_and_a_stale_one_is(self):
        gh = self.gh_transport()
        self.seed(3000, age=budget.PROBE_AFTER_SECONDS - 5)
        self.gate(["train", "status"], gh)
        self.assertEqual(self.probes, [], "fresh: no probe")
        self.seed(3000, age=budget.PROBE_AFTER_SECONDS + 5)
        self.gate(["train", "status"], gh)
        self.assertEqual(len(self.probes), 1, "stale: one probe")

    def test_the_https_transport_never_probes(self):
        gh = self.silent_http()
        self.assertIsNone(self.gate(["board"], gh), "no reading: full budget")
        self.seed(3000, age=3600)
        self.assertIsNone(self.gate(["inbox"], gh))
        self.assertEqual(gh.calls, 0)

    def test_ungated_commands_never_probe(self):
        gh = self.gh_transport()
        for argv in (["send", "hi", "--on", "7"], ["train", "step"], ["whoami"]):
            self.assertIsNone(self.gate(argv, gh), argv)
        self.assertEqual(self.probes, [])


class ResetTests(BudgetTestCase):
    def test_a_reading_whose_reset_has_passed_is_a_full_budget(self):
        self.seed(0, reset=int(time.time()) - 5)
        self.assertIsNone(budget.current(REPO))
        self.assertEqual(budget.decision("claim", budget.current(REPO)), (budget.OK, ""))
        self.assertIsNone(cli.gate(cli.parser().parse_args(["claim", "5", "--branch", "b"]), self.silent_http()))
        self.assertEqual(budget.header_words(REPO), "API: no current reading")


class ReadFloorTests(BudgetTestCase):
    """Below COORD_BUDGET_READ_FLOOR: the cached snapshot at any age, a warning, no call."""

    def setUp(self):
        super().setUp()
        self.seed(1000)
        self.warning = f"coord: GitHub API budget low: 1000 left until {budget.clock(BILLING_RESET)}"

    def test_board_serves_the_cache_whatever_its_age(self):
        self.seed_snapshot(age_seconds=3600)
        gh = self.silent_http()
        for argv in (["board"], ["board", "--fresh"], ["board", "--max-age", "5"]):
            code, out, err = self.run_cli(argv, gh)
            self.assertEqual(code, EXIT_OK, argv)
            self.assertEqual(err.strip(), f"{self.warning}; showing the snapshot from 60m ago", argv)
            self.assertIn("o/r board", out)
            self.assertIn(f"API: 1000 left (resets {budget.clock(BILLING_RESET)})", out.splitlines()[0])
        self.assertEqual(gh.calls, 0)

    def test_the_brief_serves_the_cache(self):
        self.seed_snapshot()
        code, out, err = self.run_cli(["board", "--brief", "--max-age", "300"], self.silent_http())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.startswith("coord (claude@test:budget): "), out)
        self.assertIn("showing the snapshot from 60m ago", err)

    def test_train_status_shows_the_cached_queue_and_reads_no_batch(self):
        self.seed_snapshot()
        gh = self.silent_http()
        code, out, err = self.run_cli(["train", "status"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("batch: not read (API budget low)", out)
        self.assertIn("queue: empty", out)
        self.assertIn("showing the snapshot from 60m ago", err)
        self.assertEqual(gh.calls, 0)

    def test_inbox_counts_from_the_snapshot_and_marks_nothing_read(self):
        self.seed_snapshot()
        gh = self.silent_http()
        code, out, err = self.run_cli(["inbox"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("0 new in that snapshot", out)
        self.assertIsNone(cache.load_cursor(REPO, "claude@test:budget"))
        self.assertEqual(gh.calls, 0)

    def test_ci_dispatches_but_does_not_read_the_queue_back(self):
        requested = []
        gh = GitHub(REPO, transport="http", token="t")
        gh._http = lambda method, url, body=None: (requested.append((method, url)), (None, {}))[1]
        code, out, _ = self.run_cli(["ci", "mk4", "--ref", "feature/x"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(requested, [("POST", "repos/o/r/actions/workflows/ci.yml/dispatches")])
        self.assertIn("Not reading the Tycho queue back", out)

    def test_an_allowed_write_does_not_take_the_fallback_away(self):
        # `send` invalidates the cached board; the next low-budget read must still find it.
        self.seed_snapshot()
        gh = GitHub(REPO, transport="http", token="t")
        gh._http = lambda method, url, body=None: ({"html_url": "h"}, {})
        self.assertEqual(self.run_cli(["send", "stopping: budget low", "--on", "7"], gh)[0], EXIT_OK)
        self.assertIsNone(cache.load_snapshot(REPO, 300), "invalidated: no max-age serves it")
        code, out, err = self.run_cli(["board"], self.silent_http())
        self.assertEqual(code, EXIT_OK)
        self.assertIn("showing the snapshot from 60m ago", err)
        # Its age from its own timestamp, not the aged file: the invalidated file's mtime is the epoch.
        # generatedAt is whole seconds and the test takes time to run, so allow the same minute the
        # "60m ago" line above allows, rather than an exact 3600.
        age = re.search(r"cached (\d+)s", out.splitlines()[0])
        self.assertIsNotNone(age, out.splitlines()[0])
        self.assertTrue(3600 <= int(age.group(1)) < 3660, f"its age from its own timestamp, not the aged file: {age.group(1)}s")

    def test_above_the_floor_nothing_is_held_back(self):
        self.seed(budget.READ_FLOOR_DEFAULT)
        for command in ("board", "inbox", "train status", "ci", "claim"):
            self.assertEqual(budget.decision(command, budget.current(REPO))[0], budget.OK, command)

    def test_the_floor_is_an_environment_variable(self):
        os.environ["COORD_BUDGET_READ_FLOOR"] = "900"
        self.assertEqual(budget.decision("board", budget.current(REPO))[0], budget.OK)


class NoSnapshotTests(BudgetTestCase):
    def test_no_snapshot_refuses_with_its_own_exit_code(self):
        self.seed(1000)
        gh = self.silent_http()
        for argv in (["board"], ["board", "--brief"], ["train", "status"], ["inbox"]):
            code, out, err = self.run_cli(argv, gh)
            self.assertEqual(code, EXIT_BUDGET, argv)
            self.assertEqual(out, "")
            self.assertEqual(err.strip(), f"coord: GitHub API budget low: 1000 left until "
                                          f"{budget.clock(BILLING_RESET)}; no cached snapshot on this machine to show")
        self.assertEqual(gh.calls, 0)
        self.assertEqual(EXIT_BUDGET, 5)
        self.assertNotEqual(EXIT_BUDGET, EXIT_NOT_FOUND, "4 already means not found")


class HardFloorTests(BudgetTestCase):
    """Below COORD_BUDGET_HARD_FLOOR every write but `send` is refused before any call."""

    def setUp(self):
        super().setUp()
        self.seed(100)

    def test_writes_are_refused(self):
        gh = self.silent_http()
        for argv in (["claim", "5", "--branch", "feature/x"], ["release", "5"], ["train", "add", "7"],
                     ["train", "remove", "7"], ["train", "substitute-review", "7", "--note", "n"],
                     ["train", "review", "7", "--by", "x", "--note", "n"],
                     ["train", "approve", "7", "--note", "n"], ["ci", "mk4", "--ref", "feature/x"]):
            code, out, err = self.run_cli(argv, gh)
            self.assertEqual(code, EXIT_BUDGET, argv)
            self.assertIn(f"GitHub API budget low: 100 left until {budget.clock(BILLING_RESET)}; not running", err)
        self.assertEqual(gh.calls, 0)

    def test_send_is_always_allowed(self):
        posted = []
        gh = GitHub(REPO, transport="http", token="t")
        gh._http = lambda method, url, body=None: (posted.append((method, url)), ({"html_url": "h"}, {}))[1]
        code, out, _ = self.run_cli(["send", "the budget is low; stopping", "--on", "7"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(posted, [("POST", "repos/o/r/issues/7/comments")])

    def test_reads_below_the_hard_floor_still_serve_the_cache(self):
        self.seed_snapshot()
        code, _, err = self.run_cli(["board"], self.silent_http())
        self.assertEqual(code, EXIT_OK)
        self.assertIn("showing the snapshot", err)

    def test_between_the_floors_writes_run(self):
        self.seed(budget.HARD_FLOOR_DEFAULT)
        for command in ("claim", "train add", "release"):
            self.assertEqual(budget.decision(command, budget.current(REPO))[0], budget.OK, command)


class SharedCounterTests(BudgetTestCase):
    """The cloud proxy's token reports Limit 15000 while Alex's gh reports 5000, and Used
    is one counter shared by both (probed 2026-09-25 06:07Z). What is left is the lower of
    Remaining and COORD_BUDGET_USER_LIMIT (5000) minus Used."""

    def setUp(self):
        super().setUp()
        self.until = budget.clock(BILLING_RESET)

    def assert_spent(self, gh):
        """Used 5000 of the shared 5000: writes refused, reads cached-only, header says 0.
        (On gh the first gated command is what probes, so the checks start with one.)"""
        code, _, err = self.run_cli(["train", "add", "7"], gh)
        self.assertEqual(code, EXIT_BUDGET)
        self.assertIn(f"GitHub API budget low: 0 left until {self.until}; not running `coord train add`", err)
        self.assertEqual(budget.left(budget.current(REPO)), 0)
        self.assertEqual(budget.header_words(REPO), f"API: 0 left (resets {self.until})")
        self.seed_snapshot()
        code, out, err = self.run_cli(["board"], gh)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("showing the snapshot from 60m ago", err)
        self.assertIn(f"API: 0 left (resets {self.until})", out.splitlines()[0])

    def test_https_headers_of_a_15000_token_with_the_shared_5000_spent(self):
        original = ghmodule.urllib.request.urlopen
        ghmodule.urllib.request.urlopen = lambda request, timeout=None: FakeResponse(
            {}, rate_headers(10000, "core", limit=15000))
        try:
            GitHub(REPO, transport="http", token="t").rest("repos/{repo}/issues/1")
        finally:
            ghmodule.urllib.request.urlopen = original
        self.assertEqual({k: cache.load_budget(REPO)["core"][k] for k in ("remaining", "limit", "used")},
                         {"remaining": 10000, "limit": 15000, "used": 5000})
        gh = self.silent_http()
        self.assert_spent(gh)
        self.assertEqual(gh.calls, 0)

    def test_the_gh_probe_of_a_15000_token_with_the_shared_5000_spent(self):
        probes = []

        def runner(args, stdin):
            if list(args[:3]) != ["gh", "api", "-i"]:
                raise AssertionError(f"only the probe may run here, got {args}")
            probes.append(list(args))
            return subprocess.CompletedProcess(args, 0, probe_output(10000, limit=15000), "")

        self.assert_spent(GitHub(REPO, runner=runner))
        self.assertEqual(len(probes), 1, "one probe; its reading serves the next command too")

    def test_between_the_floors_on_a_15000_token(self):
        self.seed(11400, limit=15000, used=3600)  # 1,400 of the shared 5,000 left
        reading = budget.current(REPO)
        self.assertEqual(budget.left(reading), 1400)
        self.assertEqual(budget.decision("board", reading)[0], budget.LOW)
        self.assertEqual(budget.decision("train add", reading)[0], budget.OK)

    def test_the_user_limit_is_an_environment_variable(self):
        self.seed(10000, limit=15000, used=5000)
        os.environ["COORD_BUDGET_USER_LIMIT"] = "15000"
        self.assertEqual(budget.left(budget.current(REPO)), 10000)
        self.assertEqual(budget.decision("train add", budget.current(REPO))[0], budget.OK)

    def test_left_never_goes_below_zero(self):
        self.seed(8000, limit=15000, used=7000)  # 2,000 past the shared 5,000
        self.assertEqual(budget.left(budget.current(REPO)), 0)
        self.assertEqual(budget.header_words(REPO), f"API: 0 left (resets {self.until})")
        self.assertIn("budget low: 0 left", budget.decision("train add", budget.current(REPO))[1])

    def test_left_never_exceeds_remaining(self):
        # A 5,000 token read 1,234 left with the user limit raised: Remaining is the bound.
        self.seed(1234, limit=5000, used=3766)
        os.environ["COORD_BUDGET_USER_LIMIT"] = "15000"
        self.assertEqual(budget.left(budget.current(REPO)), 1234)
        os.environ.pop("COORD_BUDGET_USER_LIMIT")
        # A token with a smaller limit than the user's: its own Remaining is the bound.
        self.seed(200, limit=1000, used=800)
        self.assertEqual(budget.left(budget.current(REPO)), 200)
        self.assertEqual(budget.decision("train add", budget.current(REPO))[0], budget.REFUSE)

    def test_used_is_derived_from_the_limit_when_absent_and_nothing_is_recorded_without_either(self):
        derived = budget.reading_from_headers({"X-RateLimit-Limit": "15000", "X-RateLimit-Remaining": "10000",
                                               "X-RateLimit-Reset": str(BILLING_RESET)})
        self.assertEqual(derived[1]["used"], 5000)
        self.assertIsNone(budget.reading_from_headers({"X-RateLimit-Remaining": "10000",
                                                       "X-RateLimit-Reset": str(BILLING_RESET)}))


class BadShapeTests(BudgetTestCase):
    """A budget.json of the wrong shape is no reading: nothing crashes, the guard fails
    open, and on gh the next probe replaces it."""

    def shapes(self):
        now = time.time()
        good = {"remaining": 100, "limit": 5000, "used": 4900, "reset": BILLING_RESET, "at": now}
        return [
            '{"core": "x"}', '[1, 2]', '"text"', 'not json{', {"core": None},
            {"core": {k: v for k, v in good.items() if k != "remaining"}},
            {"core": {k: v for k, v in good.items() if k != "used"}},
            {"core": dict(good, remaining="100")}, {"core": dict(good, used="4900")},
            {"core": dict(good, reset=str(BILLING_RESET))}, {"core": dict(good, at="now")},
            {"core": dict(good, remaining=True)},
        ]

    def test_every_gated_command_and_whoami_fail_open(self):
        for shape in self.shapes():
            self.write_budget_file(shape)
            gh = self.silent_http()
            for argv in (["board"], ["claim", "5", "--branch", "b"], ["train", "add", "7"]):
                self.assertIsNone(cli.gate(cli.parser().parse_args(argv), gh), (shape, argv))
            code, out, _ = self.run_cli(["whoami"], gh)
            self.assertEqual(code, EXIT_OK, shape)
            self.assertIn("apiBudget: API: no current reading", out, shape)
            self.assertEqual(budget.header_words(REPO), "API: no current reading", shape)
            self.assertEqual(gh.calls, 0)

    def test_the_gh_transport_probes_a_bad_reading_away(self):
        for shape in self.shapes():
            self.write_budget_file(shape)
            runner = lambda args, stdin: subprocess.CompletedProcess(args, 0, probe_output(4321), "")  # noqa: E731
            self.assertIsNone(cli.gate(cli.parser().parse_args(["board"]), GitHub(REPO, runner=runner)), shape)
            self.assertEqual(budget.current(REPO)["remaining"], 4321, shape)

    def test_a_nan_infinite_or_future_at_is_no_reading(self):
        # Only a hand edit writes these; each would defeat the one-hour cap (min(reset, nan)
        # is reset) and keep the gh probe from ever refreshing the reading.
        now = time.time()
        far = int(now) + 10 * 3600
        # Also integers past float range (309+ digits), which a float conversion cannot
        # take (OverflowError), and a time before the epoch.
        for at in (float("nan"), float("inf"), float("-inf"), now + 10 * 3600, 10 ** 400, -10 ** 400, -1):
            label = str(at)[:20]
            self.write_budget_file('{"core": {"remaining": 100, "limit": 5000, "used": 4900, '
                                   f'"reset": {far}, "at": {json.dumps(at)}}}}}')
            raw = cache.load_budget(REPO)["core"]
            if at == at:  # NaN is the one value unequal to itself
                self.assertEqual(raw["at"], at, f"the file holds exactly this `at`: {label}")
            self.assertFalse(budget.valid(raw), label)
            self.assertIsNone(budget.current(REPO), label)
            self.assertTrue(budget.needs_probe(raw), label)
            self.assertIsNone(cli.gate(cli.parser().parse_args(["train", "add", "7"]), self.silent_http()), label)
            code, out, _ = self.run_cli(["whoami"], self.silent_http())
            self.assertEqual(code, EXIT_OK, label)
            self.assertIn("apiBudget: API: no current reading", out, label)
        # A few seconds of clock skew between writers is still a reading.
        self.seed(100, age=-(budget.AT_SKEW_SECONDS - 30))
        self.assertIsNotNone(budget.current(REPO))

    def test_a_far_future_reset_holds_for_an_hour_at_most(self):
        far = int(time.time()) + 10 * 3600
        self.seed(100, reset=far)
        reading = budget.current(REPO)
        self.assertEqual(budget.until(reading), reading["at"] + budget.WINDOW_SECONDS)
        self.assertIn(f"until {budget.clock(reading['at'] + budget.WINDOW_SECONDS)}", budget.low_words(reading))
        self.seed(100, reset=far, age=budget.WINDOW_SECONDS + 1)
        self.assertIsNone(budget.current(REPO), "an hour-old reading holds nothing back")
        self.assertIsNone(cli.gate(cli.parser().parse_args(["claim", "5", "--branch", "b"]), self.silent_http()))


class TrainStepTests(BudgetTestCase):
    def test_train_step_is_never_gated(self):
        self.seed(0)
        stepped = []

        def runner(args, stdin):
            raise AssertionError(f"train step must not probe, got {args}")

        args = cli.parser().parse_args(["train", "step"])
        args.func = lambda args, gh: stepped.append(True) or EXIT_OK
        self.assertEqual(cli.run(args, GitHub(REPO, runner=runner)), EXIT_OK)
        self.assertEqual(stepped, [True])
        self.assertEqual(budget.decision("train step", budget.current(REPO)), (budget.OK, ""))


class HookTests(BudgetTestCase):
    """.claude/hooks/coord-board.sh passes the budget line on and still exits 0."""

    HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".claude", "hooks", "coord-board.sh")

    def run_hook(self):
        bin_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bin_dir, True)
        for directory in ("/usr/bin", "/bin"):
            for name in os.listdir(directory):
                target = os.path.join(bin_dir, name)
                if name != "gh" and not os.path.exists(target):
                    os.symlink(os.path.join(directory, name), target)
        python = shutil.which("python3")
        if python and not os.path.exists(os.path.join(bin_dir, "python3")):
            os.symlink(python, os.path.join(bin_dir, "python3"))
        # No gh and a token: the HTTPS transport, which has nothing to probe; the API URL
        # is a closed local port, so any call would fail rather than reach GitHub.
        env = {"PATH": bin_dir, "HOME": os.environ.get("HOME", "/tmp"), "GH_TOKEN": "dummy",
               "GITHUB_API_URL": "http://127.0.0.1:9", "COORD_REPO": REPO, "COORD_CACHE_DIR": self.cache_root,
               "COORD_AGENT": "claude@test:hook"}
        result = subprocess.run(["bash", self.HOOK], input='{"source":"startup"}', capture_output=True,
                                text=True, env=env, timeout=60)
        return result.returncode, result.stdout

    def setUp(self):
        super().setUp()
        if not os.path.exists(self.HOOK) or not shutil.which("bash"):
            self.skipTest("the hook or bash is not available")
        self.seed(42)

    def test_a_refusal_is_one_line_and_exit_0(self):
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        message = json.loads(out)["systemMessage"]
        self.assertEqual(message, f"GitHub API budget low: 42 left until {budget.clock(BILLING_RESET)}; "
                                  "no cached snapshot on this machine to show")

    def test_a_served_snapshot_leads_with_the_warning(self):
        self.seed_snapshot()
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"].splitlines()
        self.assertTrue(context[0].startswith("GitHub API budget low: 42 left until"), context[0])
        self.assertTrue(context[0].endswith("showing the snapshot from 60m ago"), context[0])
        self.assertTrue(context[1].startswith("coord (claude@test:hook): "), context[1])


if __name__ == "__main__":
    unittest.main()

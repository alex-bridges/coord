"""CI accounting witnesses: retries, lost requests, runner time and sparse history."""

import copy
import datetime as dt
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import marker, snapshot, train, train_metrics as metrics
from coordlib.gh import GitHubError
import example_config  # noqa: E402

example_config.use()  # example-repo workflow/job/lane names are config, not defaults


def setUpModule():
    example_config.use()

NOW = dt.datetime(2026, 9, 27, 14, tzinfo=dt.timezone.utc)
SINCE = NOW - dt.timedelta(hours=24)
BASE, HEAD, TIP = "a" * 40, "b" * 40, "c" * 40
BUDGET_ENV = {"COORD_TRAIN_DAILY_RUNS": "", "COORD_TRAIN_DAILY_LIGHT_ATTEMPTS": "",
              "COORD_TRAIN_DAILY_HEAVY_ATTEMPTS": ""}


def plan(batch="train/one", kind=train.LIGHT):
    return {"version": 1, "batch": batch, "base": BASE, "batchCap": 3,
            "builtAt": snapshot.iso(NOW - dt.timedelta(minutes=5)),
            "members": [{"pr": 1, "head": HEAD, "queuedAt": snapshot.iso(NOW - dt.timedelta(minutes=15))}],
            "lanePlan": {"class": kind, "lanes": ["docs"] if kind == train.LIGHT else ["core-release"],
                         "source": "main", "at": BASE}}


def run(number=1, count=1, kind=train.LIGHT, batch="train/one"):
    return {"id": number, "head_branch": batch, "head_sha": TIP, "run_attempt": count,
            "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
            "created_at": snapshot.iso(NOW - dt.timedelta(minutes=4)),
            "actor": {"login": train.GATE_ACTOR}, "head_commit": {"message": train.plan_trailer(plan(batch, kind))}}


def event(name, batch="train/one", kind=train.LIGHT, **fields):
    payload = {"kind": "train", "v": 1, "agent": "train@actions", "pr": 1, "event": name,
               "batch": batch, "receipt": dict(plan(batch, kind), tip=TIP, outcome=name,
                                               run={"id": 1, "attempt": 1, "status": "completed", "conclusion": "success"})}
    payload.update(fields)
    return {"payload": payload, "createdAt": snapshot.iso(NOW), "author": {"login": train.GATE_ACTOR}}


def request(identifier, action="dispatch", count=1, run_id=None, **fields):
    return event("ci-requested", request={"id": identifier, "action": action, "attempt": count},
                 run=run_id, **fields)


def job(number, start, end, runner="heavy", **fields):
    value = {"id": number, "status": "completed", "conclusion": "success", "runner_name": runner,
             "started_at": snapshot.iso(NOW + dt.timedelta(minutes=start)),
             "completed_at": snapshot.iso(NOW + dt.timedelta(minutes=end))}
    value.update(fields)
    return value


class FakeGitHub:
    repo = "o/r"

    def __init__(self, runs=(), records=(), jobs=None):
        self.runs, self.records, self.jobs = list(runs), list(records), jobs or {}
        self.calls, self.paths = 0, []
        self.total = len(runs)

    def rest_paginated(self, path):
        self.calls += 1
        self.paths.append(path)
        if "workflows/ci.yml/runs?" in path:
            return [{"workflow_runs": self.runs, "total_count": self.total}]
        if "issues/comments?" in path:
            return [{"id": index, "created_at": record["createdAt"], "user": record["author"],
                     "issue_url": "https://api.github.com/repos/o/r/issues/1",
                     "body": marker.compose("event", record["payload"])} for index, record in enumerate(self.records, 1)]
        match = re.search(r"actions/runs/(\d+)/jobs\?filter=all", path)
        if match:
            value = self.jobs.get(int(match[1]), [])
            if isinstance(value, Exception):
                raise value
            return [{"jobs": value}]
        raise AssertionError("unexpected read " + path)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, BUDGET_ENV)
        patch.start()
        self.addCleanup(patch.stop)

    def test_retry_attempts_exhaust_heavy_but_do_not_spend_light(self):
        gh = FakeGitHub([run(n, count=2, kind=train.HEAVY, batch=f"train/{n}") for n in range(1, 7)])
        self.assertEqual(metrics.budget_remaining(gh, NOW, train.HEAVY), 0)
        self.assertEqual(metrics.budget_remaining(gh, NOW, train.LIGHT), 12)
        self.assertEqual(gh.calls, 4, "two cohort reads per decision, never jobs or per-run lookups")

    def test_explicit_legacy_cap_remains_combined_and_additional(self):
        gh = FakeGitHub([run(count=2, kind=train.HEAVY)])
        with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_RUNS": "2"}):
            self.assertEqual(metrics.budget_remaining(gh, NOW, train.LIGHT), 0)
        with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_RUNS": "99"}):
            self.assertEqual(metrics.budget_remaining(gh, NOW, train.HEAVY), 10)
        with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_HEAVY_ATTEMPTS": "3"}):
            self.assertEqual(metrics.budget_remaining(gh, NOW, train.HEAVY), 1)

    def test_registered_dispatch_and_retry_are_not_double_counted(self):
        events = [request("first"), request("retry", action="rerun", count=2, run_id=1)]
        self.assertEqual(metrics.budget_usage([run(count=2)], events, SINCE), {"light": 2, "heavy": 0})

    def test_a_refused_retry_cannot_hide_behind_an_unrelated_run(self):
        events = [request("first"), request("retry", action="rerun", count=2, run_id=1)]
        self.assertEqual(metrics.budget_usage([run(1), run(2)], events, SINCE), {"light": 3, "heavy": 0})

    def test_each_unregistered_request_remains_charged_but_duplicate_records_do_not(self):
        first, retry = request("first"), request("redispatch", action="redispatch")
        self.assertEqual(metrics.budget_usage([], [first, first, retry], SINCE), {"light": 2, "heavy": 0})
        self.assertEqual(metrics.budget_usage([run()], [first, retry], SINCE), {"light": 2, "heavy": 0})

    def test_a_rerun_of_an_old_run_counts_from_the_recent_request(self):
        self.assertEqual(metrics.budget_usage([], [request("retry", action="rerun", count=2, run_id=99)], SINCE),
                         {"light": 1, "heavy": 0})

    def test_a_duplicate_retry_request_only_matches_one_observed_attempt(self):
        records = [request("first"), request("r1", "rerun", 2, 1), request("r2", "rerun", 2, 1)]
        self.assertEqual(metrics.budget_usage([run(count=2)], records, SINCE), {"light": 3, "heavy": 0})

    def test_unknown_or_malformed_class_charges_heavy(self):
        old = run(count=2)
        old.pop("head_commit")
        bad = request("bad")
        bad["payload"]["receipt"] = "invalid"
        self.assertEqual(metrics.budget_usage([old], [bad], SINCE), {"light": 0, "heavy": 3})

    def test_untrusted_or_out_of_window_reservations_do_not_consume_a_pool(self):
        forged, old = request("forged"), request("old")
        forged["author"]["login"] = "someone"
        old["createdAt"] = snapshot.iso(SINCE - dt.timedelta(seconds=1))
        self.assertEqual(metrics.budget_usage([], [forged, old], SINCE), {"light": 0, "heavy": 0})

    def test_truncated_or_unreadable_ledgers_never_authorize_an_attempt(self):
        gh = FakeGitHub([run()])
        gh.total = 1001
        self.assertEqual(metrics.budget_remaining(gh, NOW, train.LIGHT), 0)
        with mock.patch.object(gh, "rest_paginated", side_effect=GitHubError("offline")):
            with self.assertRaises(GitHubError):
                metrics.budget_remaining(gh, NOW, train.LIGHT)

    def test_invalid_config_is_actionable_before_any_api_read(self):
        gh = FakeGitHub()
        for value in ("mistyped", "-1"):
            with mock.patch.dict(os.environ, {"COORD_TRAIN_DAILY_LIGHT_ATTEMPTS": value}):
                with self.assertRaisesRegex(metrics.BudgetConfigurationError, "COORD_TRAIN_DAILY_LIGHT_ATTEMPTS"):
                    metrics.budget_remaining(gh, NOW, train.LIGHT)
        self.assertEqual(gh.calls, 0)


class ReceiptAndReportTests(unittest.TestCase):
    def test_plan_validation_rejects_ambiguous_or_bad_external_fields(self):
        good = plan()
        self.assertEqual(train.read_plan(train.plan_trailer(good), "train/one"), good)
        self.assertIsNone(train.read_plan(train.plan_trailer(good), "train/other"))
        self.assertIsNone(train.read_plan(train.plan_trailer(good) + "\n" + train.plan_trailer(good)))
        for field, value in (("members", [1]), ("lanePlan", []), ("base", "oops"), ("version", True)):
            self.assertIsNone(train.read_plan(train.plan_trailer(dict(good, **{field: value}))))
        broken = copy.deepcopy(good)
        broken["lanePlan"]["lanes"] = None
        self.assertEqual(metrics.plan_class(broken), train.HEAVY)

    def test_trailer_json_rejects_duplicates_nonfinite_values_and_deep_extensions(self):
        raw = json.dumps(plan())
        malformed = [raw[:-1] + ', "version": 1}', raw[:-1] + ', "extension": NaN}',
                     raw[:-1] + ', "extension": 1e400}',
                     raw[:-1] + ', "extension": ' + '[' * 40 + '0' + ']' * 40 + '}',
                     '[' * 2000 + '0' + ']' * 2000]
        for value in malformed:
            with self.subTest(value=value[:80]):
                self.assertIsNone(train.read_plan("Train-Plan: " + value))

    def test_runner_minutes_sum_job_intervals_across_retries_and_overlap(self):
        jobs = [job(1, 0, 10), job(2, 0, 10), job(3, 30, 35, runner="light"), job(1, 0, 10)]
        total = metrics.job_minutes(jobs)
        self.assertEqual(total["minutes"], 25, "not the 35-minute pipeline span, nor overlap collapsed")
        self.assertEqual(total["byRunner"], {"heavy": 20, "light": 5})
        self.assertTrue(total["complete"])
        jobs += [job(4, 0, 0, status="queued", completed_at=None), {"id": 5, "conclusion": "skipped"}]
        total = metrics.job_minutes(jobs)
        self.assertEqual((total["minutes"], total["missingJobs"], total["complete"]), (25, 1, False))
        self.assertFalse(metrics.job_minutes([])["complete"])

    def test_report_joins_receipts_and_exposes_real_cost_and_event_counts(self):
        records = [event("testing"), event("split"), event("split"),
                   event("held", head=HEAD, hold="needs-owner"), event("held", head=HEAD, hold="needs-owner"),
                   event("merged")]
        gh = FakeGitHub([run(count=2)], records, {1: [job(1, 0, 10), job(2, 15, 20)]})
        report = metrics.report(gh, now=NOW)
        self.assertEqual((report["schema"], report["status"], report["apiCalls"]), (1, "ok", 3))
        total = report["totals"]
        self.assertEqual((total["measuredRunnerMinutes"], total["retryAttempts"], total["splitBatches"], total["mergedPRs"]), (15, 1, 1, 1))
        self.assertEqual(total["holdNoticesByClass"], {"needs-owner": 1})
        self.assertAlmostEqual(total["meanBatchOccupancy"], 1 / 3)
        self.assertEqual(total["meanQueueWaitMinutes"], 10)
        self.assertTrue(any("filter=all" in path for path in gh.paths), "failed attempts are included")
        self.assertIn("not pipeline elapsed time", metrics.render(report))

    def test_historical_missing_receipts_and_job_failures_are_partial_not_zero_claims(self):
        old = run()
        old.pop("head_commit")
        error = GitHubError("HTTP 403 token ghs_abc123PRIVATE")
        report = metrics.report(FakeGitHub([old], jobs={1: error}), now=NOW)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"]["runsMissingPlans"], 1)
        self.assertIsNone(report["totals"]["meanQueueWaitMinutes"])
        self.assertIn("unavailable", metrics.render(report))
        self.assertNotIn("ghs_abc123PRIVATE", metrics.render(report))

    def test_an_initial_receipt_does_not_pretend_to_be_a_terminal_outcome(self):
        report = metrics.report(FakeGitHub([run()], [event("testing")], {1: [job(1, 0, 1)]}), now=NOW)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"]["completedRunsMissingOutcomes"], 1)
        self.assertEqual(report["totals"]["mergedPRs"], 0)

    def test_timeout_and_infrastructure_receipts_are_terminal_outcomes(self):
        for outcome in ("timed-out", "infra"):
            with self.subTest(outcome=outcome):
                report = metrics.report(FakeGitHub([run()], [event(outcome)], {1: [job(1, 0, 1)]}), now=NOW)
                self.assertEqual(report["coverage"]["completedRunsMissingOutcomes"], 0)
                self.assertTrue(report["runs"][0]["terminalReceipt"])

    def test_a_terminal_receipt_is_bound_to_its_exact_run(self):
        report = metrics.report(FakeGitHub([run(1), run(2)], [event("merged")],
                                          {1: [job(1, 0, 1)], 2: [job(2, 0, 1)]}), now=NOW)
        self.assertEqual(report["coverage"]["completedRunsMissingOutcomes"], 1)
        self.assertIsNone(report["runs"][0]["receipt"])

    def test_one_timing_api_error_stops_further_reads(self):
        gh = FakeGitHub([run(1), run(2)], jobs={2: GitHubError("HTTP 403 rate limited")})
        report = metrics.report(gh, now=NOW)
        self.assertEqual(gh.calls, 3)
        self.assertEqual(report["coverage"]["timedRuns"], 0)
        self.assertEqual(report["status"], "partial")

    def test_report_caps_job_reads_without_hiding_unmeasured_runs(self):
        gh = FakeGitHub([run(n, batch=f"train/{n}") for n in range(1, 4)], jobs={n: [job(n, 0, 1)] for n in range(1, 4)})
        with mock.patch.object(metrics, "MAX_TIMED_RUNS", 2):
            report = metrics.report(gh, now=NOW)
        self.assertEqual((report["totals"]["runs"], report["coverage"]["timedRuns"], report["apiCalls"]), (3, 2, 4))
        self.assertEqual(report["status"], "partial")
        self.assertEqual([row["id"] for row in report["runs"] if row["timing"] is None], [1])

    def test_invalid_window_makes_no_requests(self):
        gh = FakeGitHub()
        for days in (0, 31, True):
            with self.assertRaises(ValueError):
                metrics.report(gh, days, NOW)
        self.assertEqual(gh.calls, 0)


if __name__ == "__main__":
    unittest.main()

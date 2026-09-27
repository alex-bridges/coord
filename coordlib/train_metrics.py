"""Bounded, on-demand train accounting from receipts and Actions job timestamps.

No polling and no per-PR history crawl. One repository comment stream joins the
workflow-run list; job timings are read only for an explicit report. A run's elapsed
wall time is never called runner time. Missing old receipts/timings remain missing,
and there is no invented counterfactual estimate of CI time saved.
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import urllib.parse
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import snapshot, train
from .gh import GitHub, GitHubError, redact

MAX_TIMED_RUNS = 100
TERMINAL_OUTCOMES = frozenset({"merged", "green-dry-run", "split", "ejected", "abandoned", "rebuilt"})


class BudgetConfigurationError(ValueError):
    """Invalid attempt limit; no read, reservation or dispatch should follow it."""


def moment(value: Any) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def train_record(record: Dict[str, Any]) -> bool:
    payload = record.get("payload") or {}
    return (payload.get("kind") == "train" and payload.get("agent") == "train@actions"
            and (record.get("author") or {}).get("login") == train.GATE_ACTOR)


def recent_runs(gh: GitHub, since: dt.datetime) -> Tuple[List[Dict[str, Any]], bool]:
    pages = gh.rest_paginated(
        f"repos/{{repo}}/actions/workflows/{train.CI_WORKFLOW}/runs?event=workflow_dispatch"
        f"&created={urllib.parse.quote('>=' + snapshot.iso(since), safe='')}&per_page=100")
    listed = [run for page in pages for run in page.get("workflow_runs", [])]
    # This filtered GitHub endpoint caps results at 1,000 even with pagination.
    complete = len(listed) < 1000 and all(page.get("total_count", len(listed)) <= len(listed) for page in pages)
    return [run for run in listed if (run.get("head_branch") or "").startswith(train.BATCH_PREFIX)], complete


def run_plan(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return train.read_plan((run.get("head_commit") or {}).get("message") or "", run.get("head_branch"))


def plan_class(plan: Optional[Dict[str, Any]]) -> str:
    if not train.valid_plan(plan):
        return train.HEAVY
    lanes = (plan or {}).get("lanePlan") or {}
    return train.LIGHT if lanes.get("class") == train.LIGHT and isinstance(lanes.get("lanes"), list) else train.HEAVY


def attempts(run: Dict[str, Any]) -> int:
    count = run.get("run_attempt")
    return count if type(count) is int and count > 0 else 1


def budget_usage(runs: Sequence[Dict[str, Any]], records: Sequence[Dict[str, Any]],
                 since: dt.datetime) -> Dict[str, int]:
    """Conservative attempts in the rolling window, without reading each run's jobs.

    Recent-created runs contribute all their attempts. Durable pre-request records
    also cover reruns of older runs, and requests whose API response/run was lost.
    A dispatch reservation matches one distinct train run at its batch/tip. A retry
    matches only its run ID and target attempt, once. Unmatched reservations remain
    charged, including refused requests; uncertainty never widens a limit.
    """
    observed: Dict[str, int] = defaultdict(int)
    reserved: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    observed_runs: Dict[str, Dict[int, Dict[str, Any]]] = defaultdict(dict)
    classes: Dict[str, List[str]] = defaultdict(list)
    seen = set()
    for index, run in enumerate(runs):
        batch = run.get("head_branch") or ""
        if not batch.startswith(train.BATCH_PREFIX):
            continue
        key = run.get("id", (batch, index))
        if key in seen:
            continue
        seen.add(key)
        observed[batch] += attempts(run)
        if type(run.get("id")) is int:
            observed_runs[batch][run["id"]] = run
        classes[batch].append(plan_class(run_plan(run)))
    for record in records:
        payload = record.get("payload") or {}
        when = moment(record.get("createdAt"))
        request = payload.get("request") or {}
        batch = payload.get("batch")
        if not train_record(record) or payload.get("event") != "ci-requested" or not when or when < since \
                or not isinstance(batch, str) or not batch.startswith(train.BATCH_PREFIX) \
                or not isinstance(request, dict) or not isinstance(request.get("id"), str):
            continue
        receipt = payload.get("receipt")
        receipt = receipt if train.valid_plan(receipt, batch) else {}
        reserved[batch][request["id"]] = dict(request, run=payload.get("run"),
                                              tip=receipt.get("tip"))
        classes[batch].append(plan_class(receipt))
    used = {train.LIGHT: 0, train.HEAVY: 0}
    for batch in observed.keys() | reserved.keys():
        kind = train.LIGHT if classes[batch] and all(c == train.LIGHT for c in classes[batch]) else train.HEAVY
        unmatched, matched = 0, set()
        for request in reserved[batch].values():
            match = None
            if request.get("action") == "rerun":
                run_id, attempt = request.get("run"), request.get("attempt")
                run = observed_runs[batch].get(run_id) if type(run_id) is int else None
                if run and type(attempt) is int and 1 < attempt <= attempts(run):
                    match = (run_id, attempt)
            else:
                for run_id, run in observed_runs[batch].items():
                    if (run_id, 1) not in matched and request.get("tip") == run.get("head_sha") \
                            and train.is_gate_run(run, run.get("head_sha")):
                        match = (run_id, 1)
                        break
            if match is None or match in matched:
                unmatched += 1
            else:
                matched.add(match)
        used[kind] += observed[batch] + unmatched
    return used


def budget_remaining(gh: GitHub, now: dt.datetime, kind: str) -> int:
    def limit(variable: str) -> int:
        value = os.environ.get(variable)
        try:
            parsed = int(value) if value else train.DAILY_RUNS_DEFAULT
            if parsed < 0:
                raise ValueError("negative count")
            return parsed
        except ValueError as error:
            raise BudgetConfigurationError(f"{variable} must be a nonnegative attempt count") from error

    kind = train.LIGHT if kind == train.LIGHT else train.HEAVY
    class_limit = limit("COORD_TRAIN_DAILY_" + kind.upper() + "_ATTEMPTS")
    combined = limit("COORD_TRAIN_DAILY_RUNS") if os.environ.get("COORD_TRAIN_DAILY_RUNS") else None
    since = now - dt.timedelta(hours=24)
    runs, complete = recent_runs(gh, since)
    if not complete:
        return 0  # never spend against a truncated ledger
    records = snapshot.fetch_coord_comments(gh, since)
    used = budget_usage(runs, records, since)
    remaining = class_limit - used[kind]
    if combined is not None:
        remaining = min(remaining, combined - sum(used.values()))
    return remaining


def job_minutes(jobs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum actual job intervals across all attempts, once per job id.

    GET .../jobs?filter=all includes earlier attempts. Queued time between jobs is
    excluded; concurrent jobs add independently. Incomplete/missing timings make the
    sum a measured lower bound. A skipped job used no runner.
    """
    seen, by_runner = set(), defaultdict(float)
    missing, measured = 0, 0
    for index, job in enumerate(jobs):
        key = job.get("id", ("missing-id", index))
        if key in seen:
            continue
        seen.add(key)
        if job.get("conclusion") == "skipped":
            continue
        start, end = moment(job.get("started_at")), moment(job.get("completed_at"))
        if job.get("status") != "completed" or not start or not end or end < start:
            missing += 1
            continue
        by_runner[job.get("runner_name") or "unknown runner"] += (end - start).total_seconds() / 60
        measured += 1
    return {"minutes": sum(by_runner.values()), "byRunner": dict(by_runner), "measuredJobs": measured,
            "missingJobs": missing, "complete": bool(jobs) and missing == 0}


def report(gh: GitHub, days: int = 7, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """A creation-window cohort report, with explicit coverage rather than invented zeros.

    At most 100 runs get job reads, each paginated. This is a one-shot read, not a
    monitor. All attempts of those runs count, including jobs outside the creation
    window; the scope is stated in the result. Recent receipt events are a separate
    window, so old runs retried now can be visible there without full job coverage.
    """
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("report days must be between 1 and 30")
    now = now or snapshot.now_utc()
    since, before_calls = now - dt.timedelta(days=days), gh.calls
    runs, complete = recent_runs(gh, since)
    records = [record for record in snapshot.fetch_coord_comments(gh, since)
               if train_record(record) and (moment(record.get("createdAt")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= since]
    latest, plans, holds = {}, {}, set()
    split_batches, ejected, merged = set(), set(), set()
    for record in records:
        payload = record["payload"]
        batch, event = payload.get("batch"), payload.get("event")
        receipt = payload.get("receipt")
        if batch and train.valid_plan(receipt, batch):
            latest[batch] = receipt
            plans[batch] = receipt
        if event == "held":
            holds.add((payload.get("pr"), payload.get("head"), payload.get("hold")))
        elif event == "split" and batch:
            split_batches.add(batch)
        elif event == "ejected" and batch:
            ejected.add((batch, payload.get("pr")))
        elif event == "merged" and batch:
            merged.add((batch, payload.get("pr")))
    rows, runner_totals, class_totals = [], defaultdict(float), defaultdict(float)
    unavailable = []
    runs = sorted(runs, key=lambda run: run.get("id", 0), reverse=True)
    timed, timing_reads_available = 0, True
    for index, run in enumerate(runs):
        batch, plan = run["head_branch"], run_plan(run)
        if plan:
            plans.setdefault(batch, plan)
        timing = None
        if index < MAX_TIMED_RUNS and timing_reads_available:
            try:
                pages = gh.rest_paginated(f"repos/{{repo}}/actions/runs/{run['id']}/jobs?filter=all&per_page=100")
                timing = job_minutes([job for page in pages for job in page.get("jobs", [])])
                timed += 1
            except GitHubError as error:
                unavailable.append(f"run {run['id']}: {redact(str(error))[:160]}")
                timing_reads_available = False  # no repeated API errors/rate-limit probes
        kind = plan_class(plan)
        if timing:
            for runner, minutes in timing["byRunner"].items():
                runner_totals[runner] += minutes
            class_totals[kind] += timing["minutes"]
        receipt = latest.get(batch)
        if receipt and receipt.get("tip") != run.get("head_sha"):
            receipt = None
        if receipt and receipt.get("run") and receipt["run"]["id"] != run.get("id"):
            receipt = None
        terminal = bool(receipt and receipt.get("outcome") in TERMINAL_OUTCOMES and receipt.get("run"))
        rows.append({"id": run.get("id"), "batch": batch, "tip": run.get("head_sha"),
                     "attempts": attempts(run), "class": kind, "classKnown": plan is not None,
                     "status": run.get("status"), "conclusion": run.get("conclusion"),
                     "timing": timing, "receipt": receipt, "terminalReceipt": terminal, "plan": plan})
    waits, samples, queue_missing = {}, [], 0
    for plan in plans.values():
        members = plan.get("members") or []
        cap, built = plan.get("batchCap"), moment(plan.get("builtAt"))
        if type(cap) is int and cap > 0 and members:
            samples.append(len(members) / cap)
        for member in members:
            if not isinstance(member, dict):
                continue
            queued = moment(member.get("queuedAt"))
            if built and queued and built >= queued:
                key = (member.get("pr"), member.get("head"))
                value = (built - queued).total_seconds() / 60
                waits[key] = min(waits.get(key, value), value)
            else:
                queue_missing += 1
    notes = ["Run cohort: runs created in the window; timings include all their attempts, not pipeline elapsed time.",
             "Receipt outcomes and hold notices are events created in the window; a hold count is not a hold duration.",
             "Unknown historical plans charge heavy; minutes by batch class include every job, including light jobs.",
             "Runner roles and counterfactual CI savings are unavailable; minutes by runner use Actions runner names."]
    if not complete:
        notes.append("GitHub truncated the workflow-run listing; cohort totals are incomplete.")
    if len(runs) > MAX_TIMED_RUNS:
        notes.append(f"Timings limited to the newest {MAX_TIMED_RUNS} runs to bound API use.")
    if not timing_reads_available:
        notes.append("Stopped further timing reads after an API error; remaining timings are unavailable.")
    missing = (not complete or timed < len(rows) or any(row["plan"] is None or row["receipt"] is None
               or (row["status"] == "completed" and not row["terminalReceipt"])
               or not row["timing"] or not row["timing"]["complete"] for row in rows) or queue_missing)
    return {"schema": 1, "status": "partial" if missing else "ok",
            "generatedAt": snapshot.iso(now), "since": snapshot.iso(since), "days": days,
            "apiCalls": gh.calls - before_calls, "runs": rows,
            "totals": {"runs": len(rows), "attempts": sum(row["attempts"] for row in rows),
                       "retryAttempts": sum(row["attempts"] - 1 for row in rows),
                       "measuredRunnerMinutes": sum(runner_totals.values()), "runnerMinutesByRunner": dict(runner_totals),
                       "runnerMinutesByBatchClass": dict(class_totals), "mergedPRs": len(merged),
                       "splitBatches": len(split_batches), "ejectedPRs": len(ejected),
                       "holdNoticesByClass": dict(Counter(hold[2] or "unknown" for hold in holds)),
                       "meanBatchOccupancy": statistics.mean(samples) if samples else None,
                       "meanQueueWaitMinutes": statistics.mean(waits.values()) if waits else None},
            "coverage": {"runListingComplete": complete, "timedRuns": timed,
                         "completeTimings": sum(bool(row["timing"] and row["timing"]["complete"]) for row in rows),
                         "plans": len(plans), "occupancySamples": len(samples), "queueWaitSamples": len(waits),
                         "membersMissingQueueTiming": queue_missing,
                         "runsMissingPlans": sum(row["plan"] is None for row in rows),
                         "runsMissingReceipts": sum(row["receipt"] is None for row in rows),
                         "completedRunsMissingOutcomes": sum(row["status"] == "completed" and not row["terminalReceipt"] for row in rows),
                         "unavailableTimings": unavailable}, "notes": notes}


def render(result: Dict[str, Any]) -> str:
    totals, coverage = result["totals"], result["coverage"]
    occupancy, wait = totals["meanBatchOccupancy"], totals["meanQueueWaitMinutes"]
    return "\n".join([
        f"Train report: {result['since']} to {result['generatedAt']} ({result['apiCalls']} API calls)",
        f"{totals['runs']} runs / {totals['attempts']} attempts ({totals['retryAttempts']} retries); "
        f"{totals['mergedPRs']} merged PR receipts; {totals['splitBatches']} split batches; {totals['ejectedPRs']} ejections",
        f"Measured runner time: {totals['measuredRunnerMinutes']:.1f} min; "
        f"complete timings for {coverage['completeTimings']}/{totals['runs']} runs",
        "Runner minutes: " + (", ".join(f"{name} {value:.1f}" for name, value in totals["runnerMinutesByRunner"].items()) or "unavailable"),
        "Mean batch occupancy: " + (f"{occupancy:.0%} ({coverage['occupancySamples']} samples)" if occupancy is not None else "unavailable"),
        "Mean observed queue-to-build wait: " + (f"{wait:.1f} min ({coverage['queueWaitSamples']} samples)" if wait is not None else "unavailable"),
        "Hold notices: " + (", ".join(f"{name} {value}" for name, value in totals["holdNoticesByClass"].items()) or "none observed"),
        f"Coverage: {coverage['runsMissingPlans']} runs missing plans; {coverage['runsMissingReceipts']} missing receipts; "
        f"{coverage['completedRunsMissingOutcomes']} completed runs missing terminal outcomes; "
        f"{coverage['membersMissingQueueTiming']} members missing queue timing",
        *result["notes"], *coverage["unavailableTimings"],
    ])

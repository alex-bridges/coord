"""Text rendering of a board snapshot. Plain words, no colour, stable enough to grep."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from . import outage, review, train


def ago(value: Optional[str], now: dt.datetime) -> str:
    moment = review.parse_time(value)
    if moment is None:
        return "?"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m" if hours < 10 else f"{hours}h"
    return f"{hours // 24}d"


def stale_words(snapshot: Dict[str, Any]) -> str:
    """A cached board served because nothing fresher could be read (cli.snapshot_despite_budget):
    `STALE: snapshot from 3h ago; GitHub App token unavailable: …`."""
    stale = snapshot.get("stale") or {}
    return f"STALE: snapshot from {stale.get('ago', '?')} ago; {stale.get('reason', '')}".rstrip("; ")


def _clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def ci_words(ci: Dict[str, Any]) -> str:
    """One phrase for a PR's CI, plus how many checks GitHub never started (review.NOT_RUN_PREFIX):
    those are neither red nor green, so they are counted beside the verdict, not in it."""
    words = _ci_verdict_words(ci)
    not_run = ci.get("not_run") or []
    return f"{words}, {len(not_run)} not run (billing)" if not_run else words


def _ci_verdict_words(ci: Dict[str, Any]) -> str:
    verdict = ci.get("verdict")
    if ci.get("failing"):
        names = ", ".join(ci["failing"][:3]) + (" …" if len(ci["failing"]) > 3 else "")
        return f"CI red: {names}"
    if verdict == "success":
        return "CI green"
    if verdict == "pending" or ci.get("pending"):
        waiting = len(ci.get("pending", [])) - len(ci.get("started", []))
        if ci.get("started"):
            return f"CI running ({_count(len(ci['started']), 'check')} running, {waiting} waiting)"
        return f"CI queued ({_count(waiting, 'check')} waiting for a runner)"
    if verdict == "cancelled":
        return "CI cancelled"
    if verdict == "skipped":
        return "CI skipped"
    return "CI none"


def codex_words(pr: Dict[str, Any]) -> str:
    codex = pr["codex"]
    if codex["reviewing"]:
        words = "Codex reviewing"
    elif codex["approved"]:
        words = "Codex approved"
    elif codex["stale"]:
        words = "Codex approval predates head"
    else:
        words = "Codex no approval"
    if pr["unansweredFindings"]:
        words += f", {pr['unansweredFindings']} unanswered finding" + ("s" if pr["unansweredFindings"] != 1 else "")
    return words


def claim_owner(snapshot: Dict[str, Any], pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for claim in snapshot["claims"]:
        if claim["pr"] == pr["number"] or (claim["branch"] == pr["branch"] and not pr.get("fromFork")):
            return claim
    return None


def _run_line(run: Dict[str, Any], now: dt.datetime) -> str:
    jobs = run.get("jobsRunning") or []
    where = "; ".join(
        f"{job['name']} on {job.get('runner') or '?'}" + (f" ({job['step']})" if job.get("step") else "")
        for job in jobs[:3]
    )
    extra = f", {run['jobsQueued']} jobs waiting" if run.get("jobsQueued") else ""
    return (
        f"  running  {run['workflow']} · {run['branch']} ({run['event']}) · "
        f"{ago(run.get('startedAt') or run.get('createdAt'), now)}"
        + (f" · {where}" if where else "")
        + extra
    )


def active_outages(snapshot: Dict[str, Any], now: dt.datetime) -> List[Dict[str, Any]]:
    """The snapshot's outages in force at `now` (a cached snapshot predates the key: none)."""
    return outage.active([outage.deserialize(w) for w in snapshot.get("outages") or []], now)


def outage_line(window: Dict[str, Any], now: dt.datetime) -> str:
    until = window["end"]
    return (f"OUTAGE  {window['harness']} agents are down until {until.strftime('%a %H:%MZ')} "
            f"(in {ago(snapshot_iso(now), until)})" + (f": {_clip(window['reason'], 80)}" if window.get("reason") else "")
            + ". Messages to them wait; their claims do not go stale; the train keeps running.")


def snapshot_iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def conductor_state(snapshot: Dict[str, Any], now: dt.datetime) -> str:
    """One word for the brief: up, paused, down (an outage), stale (no live beat) or absent."""
    info = snapshot.get("conductor") or {}
    hosts = info.get("hosts") or []
    if any(outage.affects(w["harness"], "conductor@") for w in active_outages(snapshot, now)):
        return "down (outage)"
    if info.get("pause"):
        return "paused"
    if any(h.get("leader") for h in hosts):
        return "up"
    return "stale" if hosts else "absent"


def conductor_lines(snapshot: Dict[str, Any], now: dt.datetime) -> List[str]:
    info = snapshot.get("conductor") or {}
    lines = ["CONDUCTOR (message it with: coord send --to 'conductor@*' \"…\")"]
    hosts = info.get("hosts") or []
    if not hosts:
        lines.append("  no watcher running: messages to conductor@* wait for one")
    for host in hosts:
        role = "leader" if host.get("leader") else ("standby" if host.get("alive") else "STALE")
        wake = host.get("lastWake") or {}
        words = f"  {host['agent']} ({role}) · beat {ago(host.get('beat'), now)} ago"
        if host.get("disk") is not None:
            words += f" · disk {host['disk']} GB free"
        if host.get("pending"):
            words += f" · {host['pending']} pending"
        if wake.get("at"):
            words += f" · last wake {ago(wake['at'], now)} ago" + (f": {_clip(wake.get('summary') or '', 60)}" if wake.get("summary") else "")
        lines.append(words)
    if info.get("pause"):
        pause = info["pause"]
        lines.append(f"  PAUSED until {pause['until']} by {pause['agent']}" + (f": {_clip(pause['note'], 80)}" if pause.get("note") else ""))
    for escalation in info.get("escalations") or []:
        lines.append(f"  needs Alex ({ago(escalation['at'], now)} ago): {_clip(escalation['text'], 90)} {escalation['url']}")
    return lines


def board(snapshot: Dict[str, Any], me: str, unread: Optional[int] = None, budget: Optional[str] = None) -> str:
    """`budget`: the API budget line (budget.header_words), current rather than cached."""
    now = review.parse_time(snapshot["generatedAt"]) or dt.datetime.now(dt.timezone.utc)
    lines: List[str] = []
    cache = f" · cached {snapshot['cacheAgeSeconds']}s" if snapshot.get("cacheAgeSeconds") is not None else ""
    lines.append(
        f"{snapshot['repo']} board · main {snapshot['main'].get('sha', '')[:9]} · "
        f"{snapshot['generatedAt']} · {snapshot.get('apiCalls', '?')} API calls{cache}"
        + (f" · {budget}" if budget else "")
        + (f" · {stale_words(snapshot)}" if snapshot.get("stale") else "")
    )
    lines.append(f"you: {me}")
    for window in active_outages(snapshot, now):
        lines.append(outage_line(window, now))
    lines.append("")

    lines.append("IN FLIGHT")
    if not snapshot["prs"] and not snapshot["claims"]:
        lines.append("  nothing open")
    for pr in snapshot["prs"]:
        owner = claim_owner(snapshot, pr)
        who = f" · {owner['agent']}" if owner else ""
        draft = " · draft" if pr["isDraft"] else ""
        in_train = " · in train" if train.TRAIN_LABEL in pr["labels"] else ""
        lines.append(f"  PR #{pr['number']}  {pr['branch']}{draft}{in_train}{who}")
        lines.append(f"      {_clip(pr['title'], 70)}")
        lines.append(f"      {ci_words(pr['ci'])} · {codex_words(pr)} · updated {ago(pr['updatedAt'], now)} ago")
    for claim in snapshot["claims"]:
        if claim["pr"] is not None:
            continue
        flags = []
        if claim["contested"]:
            flags.append("CONTESTED")
        if claim["stale"]:
            flags.append("stale")
        flag = f" · {', '.join(flags)}" if flags else ""
        pushed = "branch pushed" if claim["branchPushed"] else "branch not pushed"
        lines.append(f"  claim #{claim['issue']}  {claim['branch'] or '(no branch)'} · {claim['agent']}{flag}")
        lines.append(f"      {_clip(claim['title'], 70)}")
        lines.append(f"      no PR · {pushed} · claimed {ago(claim['claimedAt'], now)} ago · active {ago(claim['lastActivity'], now)} ago")
    lines.append("")

    if snapshot["overlaps"]:
        lines.append("OVERLAPS (same files in two pieces of work; coordinate, nothing is locked)")
        for overlap in snapshot["overlaps"]:
            files = overlap["files"]
            shown = ", ".join(files[:3]) + (f" (+{len(files) - 3} more)" if len(files) > 3 else "")
            lines.append(f"  {overlap['a']} and {overlap['b']}: {shown}")
        lines.append("")

    lines.append("CI (Tycho runners)")
    runs = snapshot["runs"]
    if not runs["running"] and not runs["queued"]:
        lines.append("  idle")
    for run in runs["running"]:
        lines.append(_run_line(run, now))
    for run in runs["queued"]:
        status = "" if run.get("status") in (None, "queued") else f" [{run['status']}]"
        lines.append(f"  queued   {run['workflow']} · {run['branch']} ({run['event']}){status} · waiting {ago(run['createdAt'], now)}")
    lines.append("")

    lines.append("TRAIN")
    if not snapshot["train"]:
        lines.append("  empty")
    prs = {pr["number"]: pr for pr in snapshot["prs"]}
    for position, entry in enumerate(snapshot["train"], start=1):
        by = f" by {entry['agent']}" if entry.get("agent") else ""
        pr = prs.get(entry["pr"])
        verdict, reason = train.eligibility(pr, entry.get("headAtAdd")) if pr else (train.DROP, "not open")
        state = "ready" if verdict == train.READY else ("held: " if verdict == train.HOLD else "will be removed: ") + reason
        lines.append(f"  {position}. PR #{entry['pr']} · added {ago(entry['addedAt'], now)} ago{by} · {state}")
    lines.append("")

    lines.extend(conductor_lines(snapshot, now))
    lines.append("")

    if unread is not None:
        lines.append(f"INBOX  {unread} new for you" + (" (coord inbox)" if unread else ""))
    return "\n".join(lines).rstrip() + "\n"


def brief(snapshot: Dict[str, Any], me: str, unread: Optional[int] = None, limit: int = 6,
          budget: Optional[str] = None) -> str:
    """At most ~10 lines, for a session-start hook."""
    now = review.parse_time(snapshot["generatedAt"]) or dt.datetime.now(dt.timezone.utc)
    runs = snapshot["runs"]
    open_claims = [c for c in snapshot["claims"] if c["pr"] is None]
    parts = ([stale_words(snapshot)] if snapshot.get("stale") else []) + [
        f"{len(snapshot['prs'])} open PR" + ("s" if len(snapshot["prs"]) != 1 else ""),
        f"{len(open_claims)} claim" + ("s" if len(open_claims) != 1 else "") + " without a PR",
        f"workflow runs {len(runs['running'])} running / {len(runs['queued'])} queued",
        f"train {len(snapshot['train'])}",
    ]
    if snapshot["overlaps"]:
        parts.append(f"{len(snapshot['overlaps'])} file overlap" + ("s" if len(snapshot["overlaps"]) != 1 else ""))
    if snapshot.get("conductor") is not None:
        parts.append(f"conductor {conductor_state(snapshot, now)}")
    if unread:
        parts.append(f"{unread} new message" + ("s" if unread != 1 else "") + " for you")
    if budget:
        parts.append(budget)
    lines = [f"coord ({me}): " + ", ".join(parts)]
    lines.extend(outage_line(window, now) for window in active_outages(snapshot, now))
    for pr in snapshot["prs"][:limit]:
        owner = claim_owner(snapshot, pr)
        who = f" [{owner['agent']}]" if owner else ""
        lines.append(f"  PR #{pr['number']} {pr['branch']}{who}: {ci_words(pr['ci'])}, {codex_words(pr)}")
    for claim in open_claims[: max(0, limit - len(snapshot["prs"]))]:
        lines.append(f"  claim #{claim['issue']} {claim['branch']} [{claim['agent']}]" + (" stale" if claim["stale"] else ""))
    lines.append("  details: Tools/coord/coord board · messages: Tools/coord/coord inbox")
    return "\n".join(lines) + "\n"

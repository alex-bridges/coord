"""Harness outages: every agent of one kind down at once, until a known time.

Every Claude agent (local Claude Code sessions, Claude cloud sessions and the
conductor) draws on ONE subscription, so a usage limit stops all of them together. The
weekly limit resets Thursday 12:00 America/New_York (Alex, 2026-09-25); a five-hour
limit can also stop them in between. Codex has its own quota and keeps working, and so
does the merge train, which runs in Actions and needs no model.

An outage is a pair of marked comments on the conductor issue (conductor.py):

    {"kind": "outage", "event": "start", "harness": "claude", "until": "…Z", "reason": "…"}
    {"kind": "outage", "event": "end", "harness": "claude"}

The conductor's watcher posts `start` when a model run reports a usage limit, and `end`
after the first run that succeeds once it has passed. Anyone may post either with
`coord outage start|end`. Nothing depends on the `end` record: an outage is over when its
`until` passes, so a watcher that dies mid-outage leaves nothing stuck.

What an outage changes:
- the board and the session-start brief show it, so a Codex agent knows why a Claude
  agent is silent;
- `send` to an agent that is down says when it will read the message;
- a claim's 48-hour stale clock does not run while its agent's harness is down, so a
  Claude claim is not taken over just because the weekly limit fell in the middle of it;
- the conductor queues what it would have woken for, and drains it after the reset.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config, review

# The weekly reset ("Thu 12:00 America/New_York") is per repo: config `claude_weekly_reset`
# or COORD_CLAUDE_WEEKLY_RESET. Without either, the outage feature has no reset to aim for.
# When a limit message names no reset time, look again after this long (a run that is
# refused costs nothing). Capped at the next weekly reset.
PROBE_AFTER = dt.timedelta(hours=1)

# The agent ids each harness covers (identity.agent_id). The conductor runs on Claude.
HARNESS_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "claude": ("claude@", "claude-cloud@", "conductor@"),
    "codex": ("codex@",),
}

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _time(value: Any) -> Optional[dt.datetime]:
    """review.parse_time that treats a malformed value as absent: records are hand-writable."""
    try:
        return review.parse_time(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _text(value: Any) -> str:
    """A payload field as display text: records are hand-writable, the board must not crash."""
    return value if isinstance(value, str) else ""


def prefixes(harness: str) -> Tuple[str, ...]:
    return HARNESS_PREFIXES.get(harness, (f"{harness}@",))


def affects(harness: str, agent: str) -> bool:
    """Is `agent` (an id, or an address such as `conductor@*`) one of `harness`'s agents?"""
    return any(agent.startswith(prefix) for prefix in prefixes(harness))


# MARK: - The weekly reset


def weekly_spec() -> str:
    """The weekly reset spec; raises ValueError (config.require) when none is configured."""
    explicit = os.environ.get("COORD_CLAUDE_WEEKLY_RESET")
    if explicit:
        return explicit
    return config.require("claude_weekly_reset")


def _configured_weekly_spec() -> Optional[str]:
    return os.environ.get("COORD_CLAUDE_WEEKLY_RESET") or config.get("claude_weekly_reset") or None


def _nth_sunday(year: int, month: int, nth: int) -> dt.datetime:
    first = dt.datetime(year, month, 1)
    return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


class _USZone(dt.tzinfo):
    """Stdlib US DST fallback when IANA tzdata is absent (e.g., Windows without tzdata)."""

    def __init__(self, name: str, std_hours: int):
        self._name = name
        self._std = dt.timedelta(hours=std_hours)

    def _dst_bounds_utc(self, year: int) -> tuple[dt.datetime, dt.datetime]:
        # US Energy Policy Act of 2005 (effective 2007+): 2nd Sun Mar 02:00 std -> 1st Sun Nov 02:00 DST.
        start_utc = _nth_sunday(year, 3, 2).replace(hour=2) - self._std
        end_utc = _nth_sunday(year, 11, 1).replace(hour=2) - (self._std + dt.timedelta(hours=1))
        return start_utc, end_utc

    def tzname(self, d: Optional[dt.datetime]) -> str:
        return self._name

    def dst(self, d: Optional[dt.datetime]) -> dt.timedelta:
        if d is None:
            return dt.timedelta(0)
        naive = d.replace(tzinfo=None)
        start_local = _nth_sunday(d.year, 3, 2).replace(hour=2)
        end_local = _nth_sunday(d.year, 11, 1).replace(hour=1 if d.fold else 2)
        return dt.timedelta(hours=1) if start_local <= naive < end_local else dt.timedelta(0)

    def utcoffset(self, d: Optional[dt.datetime]) -> dt.timedelta:
        return self._std + self.dst(d)

    def fromutc(self, d: dt.datetime) -> dt.datetime:
        if d.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        naive_utc = d.replace(tzinfo=None)
        start_utc, end_utc = self._dst_bounds_utc(naive_utc.year)
        if start_utc <= naive_utc < end_utc:
            return (d + self._std + dt.timedelta(hours=1)).replace(fold=0)
        fold = 1 if end_utc <= naive_utc < end_utc + dt.timedelta(hours=1) else 0
        return (d + self._std).replace(fold=fold)


_FALLBACK_ZONES: Dict[str, dt.tzinfo] = {
    "UTC": dt.timezone.utc,
    "Etc/UTC": dt.timezone.utc,
    "GMT": dt.timezone.utc,
    "America/New_York": _USZone("America/New_York", -5),
    "US/Eastern": _USZone("US/Eastern", -5),
    "America/Detroit": _USZone("America/Detroit", -5),
    "America/Toronto": _USZone("America/Toronto", -5),
    "America/Chicago": _USZone("America/Chicago", -6),
    "US/Central": _USZone("US/Central", -6),
    "America/Denver": _USZone("America/Denver", -7),
    "US/Mountain": _USZone("US/Mountain", -7),
    "America/Los_Angeles": _USZone("America/Los_Angeles", -8),
    "US/Pacific": _USZone("US/Pacific", -8),
    "America/Phoenix": dt.timezone(dt.timedelta(hours=-7), "America/Phoenix"),
}


def _zone(name: str) -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tz database (Windows without tzdata, or a bare container)
        return _FALLBACK_ZONES.get(name) or dt.timezone(dt.timedelta(hours=-5), name)


def next_weekly_reset(now: dt.datetime, spec: Optional[str] = None) -> dt.datetime:
    """The first reset strictly after `now`, in UTC. `spec`: "<Day> <HH:MM> <IANA zone>".

    Wall-clock time in the zone, so 12:00 New York is 16:00Z in summer and 17:00Z in winter.
    """
    words = (spec or weekly_spec()).split()
    try:
        weekday = _DAYS[words[0][:3].lower()]
        hour, minute = (int(part) for part in words[1].split(":"))
        zone = _zone(words[2] if len(words) > 2 else "UTC")
    except (IndexError, KeyError, ValueError):
        configured = _configured_weekly_spec()
        if spec is None or configured is None or configured == spec:
            raise ValueError("coord: the weekly reset %r is not '<Day> <HH:MM> <IANA zone>'" % (spec or configured))
        return next_weekly_reset(now, configured)  # a bad explicit spec falls back to the configured one
    local = now.astimezone(zone)
    days = (weekday - local.weekday()) % 7
    candidate = dt.datetime.combine(local.date() + dt.timedelta(days=days), dt.time(hour, minute), tzinfo=zone)
    if candidate <= local:
        candidate = dt.datetime.combine(local.date() + dt.timedelta(days=days + 7), dt.time(hour, minute), tzinfo=zone)
    return candidate.astimezone(dt.timezone.utc)


# MARK: - Reading a usage-limit message

# What the Claude CLI says when the subscription is spent (strings in claude 2.1.282:
# "usage limit reached", "you have reached your weekly usage limit", "of your 5-hour
# usage limit", "this limit resets …", "spend limit reached (daily; resets … UTC)").
# A plain API "rate limit" or "overloaded" is transient and is NOT an outage.
_LIMIT = re.compile(
    r"usage limit|weekly (?:usage )?limit|5-hour (?:usage )?limit|session limit|spend limit"
    r"|hit your (?:\w+ )?limit|usage credit limit|out of (?:extra )?usage",
    re.IGNORECASE,
)
_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?\s*(Z|UTC|[+-]\d{2}:?\d{2})?")
_RELATIVE = re.compile(r"resets? in\s+(?:(\d+)\s*d\w*\s*)?(?:(\d+)\s*h\w*\s*)?(?:(\d+)\s*m\w*)?", re.IGNORECASE)


def is_limit(text: str) -> bool:
    return bool(_LIMIT.search(text or ""))


def parse_limit(text: str, now: dt.datetime) -> Optional[Dict[str, Any]]:
    """{"until", "reason", "exact"} when `text` reports a usage limit; None otherwise.

    `until` is the reset the message names (an epoch after `|`, a timestamp, or "resets
    in 3h 20m"); a weekly limit that names none resets at the weekly time; anything else
    is looked at again after PROBE_AFTER. `exact` says whether the time came from the
    message. Only call this on a run that FAILED: a successful run may mention limits.
    """
    if not is_limit(text):
        return None
    lines = [line.strip() for line in text.splitlines() if _LIMIT.search(line)] or [text.strip()]
    reason = lines[0][:200]
    # Times are read only from the lines that name the limit: a failed run's output can
    # hold any timestamp or table, and a far-future one would silence the watcher.
    near = "\n".join(lines)
    try:
        weekly: Optional[dt.datetime] = next_weekly_reset(now)
    except ValueError:
        weekly = None  # no weekly reset configured: only the message's own time and the probe apply
    until: Optional[dt.datetime] = None
    match = _EPOCH.search(near)
    if match:
        until = dt.datetime.fromtimestamp(int(match.group(1)), dt.timezone.utc)
    if until is None:
        match = _STAMP.search(near)
        if match:
            zone = match.group(3) or "UTC"
            offset = "+00:00" if zone in ("Z", "UTC") else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
            until = _time(f"{match.group(1)}T{match.group(2)}:00{offset}")
    if until is None:
        match = _RELATIVE.search(near)
        if match and any(match.groups()):
            days, hours, minutes = (int(value or 0) for value in match.groups())
            until = now + dt.timedelta(days=days, hours=hours, minutes=minutes)
    if until is not None and now < until and (weekly is None or until <= weekly):
        return {"until": until, "reason": reason, "exact": True}
    if until is not None and weekly is not None and until > weekly:
        # No Claude limit outlives the weekly reset; a later time is a misread.
        return {"until": weekly, "reason": reason, "exact": False}
    if weekly is not None and re.search(r"weekly", near, re.IGNORECASE):
        return {"until": weekly, "reason": reason, "exact": False}
    probe = now + PROBE_AFTER
    return {"until": probe if weekly is None else min(probe, weekly), "reason": reason, "exact": False}


# MARK: - Records


def windows(comments: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every outage as a window, oldest first: {harness, start, end, until, reason, agent, url}.

    `comments` are coord comments ({"id", "createdAt", "url", "payload"}) in any order. A
    `start` while one is open for the harness moves its `until` (the reset moved); an
    `end` closes it at the time it was posted, or at its `until` if that came first, and
    marks the latest window `closed` either way.
    """
    out: List[Dict[str, Any]] = []
    open_by_harness: Dict[str, Dict[str, Any]] = {}
    last_by_harness: Dict[str, Dict[str, Any]] = {}
    for comment in sorted(comments, key=lambda c: c.get("id") or 0):
        payload = comment.get("payload") or {}
        if payload.get("kind") != "outage":
            continue
        harness = str(payload.get("harness") or "")
        posted = _time(comment.get("createdAt"))
        if not harness or posted is None:
            continue
        current = open_by_harness.get(harness)
        if current is not None and current["until"] <= posted:
            open_by_harness.pop(harness)  # it had already run out
            current = None
        if payload.get("event") == "start":
            until = _time(payload.get("until"))
            if until is None or until <= posted:
                continue
            if current is not None:
                current.update(until=until, end=until, reason=_text(payload.get("reason")) or current["reason"])
                continue
            window = {"harness": harness, "start": posted, "until": until, "end": until,
                      "reason": _text(payload.get("reason")), "agent": _text(payload.get("agent")),
                      "url": comment.get("url") or "", "closed": False}
            out.append(window)
            open_by_harness[harness] = window
            last_by_harness[harness] = window
        elif payload.get("event") == "end" and harness in last_by_harness:
            # `closed`: someone confirmed the harness is back, even after `until` had passed.
            last = last_by_harness[harness]
            last["closed"] = True
            if current is not None:
                current["end"] = min(current["until"], posted)
                open_by_harness.pop(harness)
    return out


def active(all_windows: Iterable[Dict[str, Any]], now: dt.datetime) -> List[Dict[str, Any]]:
    return [w for w in all_windows if w["start"] <= now < w["end"]]


def down(all_windows: Iterable[Dict[str, Any]], agent: str, now: dt.datetime) -> Optional[Dict[str, Any]]:
    """The active outage that covers `agent` (an id or an address), if any."""
    return next((w for w in active(all_windows, now) if affects(w["harness"], agent)), None)


def paused_time(all_windows: Iterable[Dict[str, Any]], agent: str, since: dt.datetime, now: dt.datetime) -> dt.timedelta:
    """How much of [since, now] `agent`'s harness spent down (overlapping windows counted once)."""
    spans = sorted((max(w["start"], since), min(w["end"], now)) for w in all_windows
                   if affects(w["harness"], agent) and w["end"] > since and w["start"] < now)
    total = dt.timedelta(0)
    cursor = since
    for start, end in spans:
        start = max(start, cursor)
        if end > start:
            total += end - start
            cursor = end
    return total


def serialize(window: Dict[str, Any]) -> Dict[str, Any]:
    """A window as JSON (the snapshot is cached as JSON)."""
    return {key: (value.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(value, dt.datetime) else value)
            for key, value in window.items()}


def deserialize(window: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(window)
    for key in ("start", "until", "end"):
        parsed = _time(window.get(key))
        if parsed is not None:
            out[key] = parsed
    return out

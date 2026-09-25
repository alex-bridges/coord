"""The conductor's records: one heartbeat comment per host, pauses, and escalations.

All of them live on one issue, labelled `coord:conductor` and created on first use, so a
watcher reads its whole world in one call. The conductor may run on several CI boxes;
exactly one of them acts as the conductor at a time:

- Each host's watcher keeps ONE comment on that issue and edits it every tick (the
  heartbeat: `{"kind": "conductor", "event": "beat", "beat": "<time>", …}`). Editing
  instead of posting keeps the issue short and costs one call.
- The leader is the host with the OLDEST heartbeat comment (lowest id) whose beat is
  younger than LEASE_TTL. A second box installed later is a standby: it keeps beating,
  does its own machine's checks, and takes over the conductor's duties only when the
  leader's beat goes stale (the box died, or its watcher did). When the leader comes back
  it leads again. Like claims, this is an announcement, not a lock: in the minutes around
  a takeover two hosts can both act, and the inbox cursor that travels in the beat
  (`seenThrough`) keeps a new leader from re-reading what the old one already handled.
- `pause` / `resume` records (`coord conductor pause --for 2h`) stop every host's watcher
  from waking a model, for example while Alex drives the conductor himself.
- `escalate` records are the conductor asking Alex for something; the board shows the
  last day's.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional

from . import review

CONDUCTOR_LABEL = "coord:conductor"
ADDRESS = "conductor@*"
LEASE_TTL = dt.timedelta(minutes=15)
ESCALATION_WINDOW = dt.timedelta(hours=24)


def _time(value: Any) -> Optional[dt.datetime]:
    try:
        return review.parse_time(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _records(comments: Iterable[Dict[str, Any]], event: str) -> List[Dict[str, Any]]:
    return sorted((c for c in comments if (c.get("payload") or {}).get("kind") == "conductor"
                   and c["payload"].get("event") == event), key=lambda c: c.get("id") or 0)


def hosts(comments: Iterable[Dict[str, Any]], now: dt.datetime, ttl: dt.timedelta = LEASE_TTL) -> List[Dict[str, Any]]:
    """Every host's heartbeat, oldest comment first, with `alive` and `leader`.

    A host with several heartbeat comments (a lost state file) counts by its oldest.
    """
    comments = list(comments)
    seen: Dict[str, Dict[str, Any]] = {}
    for comment in _records(comments, "beat"):
        agent = str(comment["payload"].get("agent") or "")
        if agent and agent not in seen:
            seen[agent] = comment
    out = []
    leader_found = False
    for agent, comment in seen.items():
        payload = comment["payload"]
        beat = _time(payload.get("beat"))
        alive = beat is not None and now - beat <= ttl
        leader = alive and not leader_found
        leader_found = leader_found or leader
        out.append({
            "agent": agent, "host": payload.get("host") or "", "beat": payload.get("beat") if beat else None,
            "alive": alive, "leader": leader, "commentId": comment.get("id"), "url": comment.get("url") or "",
            "seenThrough": payload.get("seenThrough"), "lastWake": payload.get("lastWake"),
            "pending": payload.get("pending"), "disk": payload.get("disk"),
        })
    return out


def pause(comments: Iterable[Dict[str, Any]], now: dt.datetime) -> Optional[Dict[str, Any]]:
    """The pause in force, if any: the newest pause/resume record decides."""
    latest = None
    for comment in sorted((c for c in comments if (c.get("payload") or {}).get("kind") == "conductor"
                           and c["payload"].get("event") in ("pause", "resume")), key=lambda c: c.get("id") or 0):
        latest = comment
    if latest is None or latest["payload"].get("event") != "pause":
        return None
    until = _time(latest["payload"].get("until"))
    if until is None or until <= now:
        return None
    return {"until": latest["payload"].get("until"), "agent": latest["payload"].get("agent") or "",
            "note": latest["payload"].get("note") or "", "url": latest.get("url") or ""}


def escalations(comments: Iterable[Dict[str, Any]], now: dt.datetime) -> List[Dict[str, Any]]:
    out = []
    for comment in _records(comments, "escalate"):
        posted = _time(comment.get("createdAt"))
        if posted is not None and now - posted <= ESCALATION_WINDOW:
            out.append({"at": comment.get("createdAt"), "agent": comment["payload"].get("agent") or "",
                        "text": comment.get("text") or "", "url": comment.get("url") or ""})
    return out

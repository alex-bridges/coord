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

from . import review, train

CONDUCTOR_LABEL = "coord:conductor"
ADDRESS = "conductor@*"
LEASE_TTL = dt.timedelta(minutes=15)
ESCALATION_WINDOW = dt.timedelta(hours=24)


def _time(value: Any) -> Optional[dt.datetime]:
    try:
        return review.parse_time(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _wake(value: Any) -> Optional[Dict[str, Any]]:
    """A beat's lastWake with typed fields, or None: the board renders it for every agent."""
    if not isinstance(value, dict) or _time(value.get("at")) is None:
        return None
    return {"at": value["at"], "model": _text(value.get("model")), "ok": value.get("ok") is True,
            "summary": _text(value.get("summary"))}


def _number(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _reviewer(value: Any) -> Optional[Dict[str, Any]]:
    """A beat's `reviewer` backlog (conductor.py `review_step`, #1092), or None: a
    malformed or absent value shows nothing rather than crashing the board."""
    if not isinstance(value, dict):
        return None
    waiting = _number(value.get("waiting"))
    if not waiting:
        return None
    return {"waiting": waiting, "appAvailable": value.get("appAvailable") is True}


def _owner_records(comments: Iterable[Dict[str, Any]], repo: str) -> List[Dict[str, Any]]:
    """Conductor controls are owner-authored; a marker's agent field is self-declared."""
    owner = (train.owner_login(repo) or "").lower()
    return [c for c in comments if (c.get("author") or {}).get("type") == "User"
            and str((c.get("author") or {}).get("login") or "").lower() == owner]


def _records(comments: Iterable[Dict[str, Any]], event: str, repo: str) -> List[Dict[str, Any]]:
    return sorted((c for c in _owner_records(comments, repo) if (c.get("payload") or {}).get("kind") == "conductor"
                   and c["payload"].get("event") == event), key=lambda c: c.get("id") or 0)


def hosts(comments: Iterable[Dict[str, Any]], now: dt.datetime, repo: str,
          ttl: dt.timedelta = LEASE_TTL) -> List[Dict[str, Any]]:
    """Every host's heartbeat, oldest comment first, with `alive` and `leader`.

    A host with several trusted heartbeat comments (a lost state file) counts by its oldest.
    """
    comments = list(comments)
    seen: Dict[str, Dict[str, Any]] = {}
    for comment in _records(comments, "beat", repo):
        agent = str(comment["payload"].get("agent") or "")
        if agent and agent not in seen:
            seen[agent] = comment
    out = []
    leader_found = False
    for agent, comment in seen.items():
        payload = comment["payload"]
        beat = _time(payload.get("beat"))
        alive = beat is not None and dt.timedelta(0) <= now - beat <= ttl
        leader = alive and not leader_found
        leader_found = leader_found or leader
        out.append({
            "agent": agent, "host": payload.get("host") or "", "beat": payload.get("beat") if beat else None,
            "alive": alive, "leader": leader, "commentId": comment.get("id"), "url": comment.get("url") or "",
            "seenThrough": payload.get("seenThrough") if _time(payload.get("seenThrough")) else None,
            "lastWake": _wake(payload.get("lastWake")), "pending": _number(payload.get("pending")),
            "disk": _number(payload.get("disk")), "wasLeader": payload.get("leader") is True,
            # Review runs of the last 24 h, so the daily cap follows the lead (reviewer.py).
            "reviewRuns": [r for r in (payload.get("reviewRuns") or []) if isinstance(r, str)
                           and (run := _time(r)) is not None
                           and dt.timedelta(0) <= now - run < dt.timedelta(days=1)][:100],
            "reviewer": _reviewer(payload.get("reviewer")),
        })
    return out


def pause(comments: Iterable[Dict[str, Any]], now: dt.datetime, repo: str) -> Optional[Dict[str, Any]]:
    """The pause in force, if any: the newest pause/resume record decides."""
    latest = None
    for comment in sorted((c for c in _owner_records(comments, repo) if (c.get("payload") or {}).get("kind") == "conductor"
                           and c["payload"].get("event") in ("pause", "resume")), key=lambda c: c.get("id") or 0):
        latest = comment
    if latest is None or latest["payload"].get("event") != "pause":
        return None
    until = _time(latest["payload"].get("until"))
    if until is None or until <= now:
        return None
    return {"until": latest["payload"]["until"], "agent": _text(latest["payload"].get("agent")),
            "note": _text(latest["payload"].get("note")), "url": _text(latest.get("url"))}


def escalations(comments: Iterable[Dict[str, Any]], now: dt.datetime, repo: str) -> List[Dict[str, Any]]:
    out = []
    for comment in _records(comments, "escalate", repo):
        posted = _time(comment.get("createdAt"))
        if posted is not None and now - posted <= ESCALATION_WINDOW:
            out.append({"at": comment.get("createdAt"), "agent": _text(comment["payload"].get("agent")),
                        "text": _text(comment.get("text")), "url": _text(comment.get("url"))})
    return out

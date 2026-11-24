"""Handoffs that wake their recipient.

A handoff is a `send --handoff` message: durable on GitHub first, like every message.
Delivery (the inbox) and activation (waking an idle session) are separate steps:

- Activation, phase 1: a Claude Code session on the same machine can be woken by a
  session-to-session message from its harness. coord cannot send that message; it tells
  the sender the harness name to message and a one-line text that names the handoff and
  carries no work. The name comes from a local registry each session writes for itself
  (`coord session register <name>`), because only the session can learn its harness name
  and only a session on the same machine can message it.
- Acknowledgment: the recipient takes the handoff with `coord handoff take <id>`, which
  posts an ack. The ack is an ordinary `msg` record to the sender with an additive
  `ack` field (the handoff's comment id), so coord versions without this module still
  deliver it as a message instead of dropping an unknown kind.
- Duplicates: a handoff is identified by its comment id. `take` refuses to start work on
  a handoff that already has an ack (by anyone), so a repeated wake, a restart or a
  compaction costs one read and no duplicate work.

Everything here reads the board snapshot (claims, messages and acks of the last
CLAIM_WINDOW_DAYS), so status and the pending lists spend no call beyond the board's.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional

from . import identity, marker, review, snapshot

# A harness session name as ListAgents prints it ("worker-a", "team-lead"). Never a raw
# session id: the registry gives the sender a name and the harness resolves it.
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
# An agent id as the brief shows one (render._brief_identity): no spaces, no newlines.
_AGENT = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)?")


def session_name_error(name: str) -> Optional[str]:
    """Why `name` cannot be registered, or None."""
    if not isinstance(name, str) or not _SESSION_NAME.fullmatch(name):
        return ("a session name is the name ListAgents prints after \"This session is\" "
                "(letters, digits, '.', '_', '-'; at most 64)")
    if _UUID.fullmatch(name):
        return "that is a session id, not a session name: register the name ListAgents prints"
    return None


def is_exact_address(address: str) -> bool:
    """One agent id: not `all`, not a prefix ending in *, not `#N` (whoever works on N)."""
    return bool(address) and address != "all" and not address.endswith("*") and not address.startswith("#")


def trusted(message: Dict[str, Any], repo: Optional[str]) -> bool:
    """Whether a handoff, ack or give-up record counts: posted by the trusted user login
    (snapshot.trusted_attestation) or by the configured app login, the mailbox's rule.
    The marker's `agent` field is self-declared, so the author decides."""
    author = message.get("author") or {}
    if not repo:
        return False
    return snapshot.trusted_attestation(author, repo) or author.get("login") == snapshot.sidechain_login()


def agent_words(agent: Any) -> Optional[str]:
    """An agent id fit to print in a line someone will send on, or None."""
    return agent if isinstance(agent, str) and len(agent) <= 160 and _AGENT.fullmatch(agent) else None


def is_handoff(message: Dict[str, Any]) -> bool:
    payload = message.get("payload") or {}
    return payload.get("kind") == "msg" and payload.get("handoff") is True and isinstance(message.get("id"), int)


def ack_of(message: Dict[str, Any]) -> Optional[int]:
    payload = message.get("payload") or {}
    value = payload.get("ack")
    return value if payload.get("kind") == "msg" and type(value) is int and value > 0 else None


def acks(messages: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """Handoff id -> its acks, oldest first. Any agent's ack counts: the first one took it."""
    out: Dict[int, List[Dict[str, Any]]] = {}
    for message in sorted(messages, key=lambda m: m.get("id") or 0):
        handoff = ack_of(message)
        if handoff is not None:
            out.setdefault(handoff, []).append({
                "id": message.get("id"), "by": (message.get("payload") or {}).get("agent"),
                "at": message.get("createdAt"), "url": message.get("url"),
            })
    return out


def addressed_to(message: Dict[str, Any], me: str) -> bool:
    return any(identity.matches(to, me) for to in marker.recipients(message.get("payload") or {}))


def handoffs(snap: Dict[str, Any], now: dt.datetime) -> List[Dict[str, Any]]:
    """Every handoff on the board with its ack state, oldest first."""
    repo = snap.get("repo")
    messages = [m for m in snap.get("messages") or [] if trusted(m, repo)]
    acked = acks(messages)
    gave_up = {}
    for message in messages:
        payload = message.get("payload") or {}
        value = payload.get("relayGaveUp")
        if payload.get("kind") == "msg" and type(value) is int and value > 0:
            gave_up.setdefault(value, {"by": payload.get("agent"), "at": message.get("createdAt"), "url": message.get("url")})
    out = []
    for message in sorted(messages, key=lambda m: m.get("id") or 0):
        if not is_handoff(message):
            continue
        created = review.parse_time(message.get("createdAt"))
        taken = acked.get(message["id"]) or []
        out.append({
            "id": message["id"],
            "from": message["payload"].get("agent"),
            "to": marker.recipients(message["payload"]),
            "on": message.get("on"),
            "addressedOn": message["payload"].get("on"),
            "at": message.get("createdAt"),
            "ageSeconds": int((now - created).total_seconds()) if created else None,
            "url": message.get("url"),
            "text": message.get("text") or "",
            "acked": bool(taken),
            "acks": taken,
            "relayGaveUp": gave_up.get(message["id"]),
        })
    return out


def find(snap: Dict[str, Any], handoff_id: int, now: dt.datetime) -> Optional[Dict[str, Any]]:
    return next((h for h in handoffs(snap, now) if h["id"] == handoff_id), None)


def open_for(snap: Dict[str, Any], me: str, now: dt.datetime) -> List[Dict[str, Any]]:
    """Handoffs addressed to `me` that nobody has acknowledged, whatever the inbox cursor
    says: a session that read one and then crashed or compacted still sees it."""
    return [h for h in handoffs(snap, now) if not h["acked"] and h["from"] != me
            and any(identity.matches(to, me) for to in h["to"])]


def select(items: List[Dict[str, Any]], sender: Optional[str], unacked: bool,
           older_than: Optional[dt.timedelta]) -> List[Dict[str, Any]]:
    """`handoff list`: by sender (None for every sender), unacked only, older than an age."""
    out = []
    for item in items:
        if sender is not None and item["from"] != sender:
            continue
        if unacked and item["acked"]:
            continue
        if older_than is not None and (item["ageSeconds"] is None or item["ageSeconds"] < older_than.total_seconds()):
            continue
        out.append(item)
    return out


def wake_text(kind: str, comment_id: int, sender: str, where: str, sent_at: str) -> str:
    """The one line a sender gives the harness. It names the coord item and carries no
    work. `sent_at` is when the line is printed (the send, or a later `handoff status`),
    so a re-wake is not dropped as a copy of the sender's previous message."""
    sender = agent_words(sender) or "(sender not shown)"
    if kind == "handoff":
        return (f"coord handoff {comment_id} for you from {sender} ({where}, sent {sent_at}): "
                f"run `coord handoff take {comment_id}`")
    return f"coord message {comment_id} for you from {sender} ({where}, sent {sent_at}): run `coord inbox`"


def wake_lines(recipients: List[str], sessions: Dict[str, Dict[str, Any]], text: str,
               now: dt.datetime, ago) -> List[str]:
    """What `send` prints after the record is posted: per recipient, the session to wake
    and how, or plainly that there is none. `ago` formats an ISO time as an age."""
    lines = []
    for address in recipients:
        if address == "all" or address.startswith("#"):
            continue  # a broadcast, or whoever works on an issue: no single session
        if address.endswith("*"):
            matches = sorted(agent for agent in sessions if identity.matches(address, agent))
        else:
            matches = [address] if address in sessions else []
        if not matches:
            lines.append(f"wake: no session on this machine is registered for {address}; the message waits in "
                         "its coord inbox until it reads it (its next `coord inbox` or session start)")
            continue
        for agent in matches:
            entry = sessions[agent]
            lines.append(f"wake: {agent} registered as session `{entry['name']}` on this machine "
                         f"{ago(entry.get('at'), now)} ago. Only if `{entry['name']}` is in your ListAgents, "
                         "message it this one line (SendMessage):")
            lines.append(f"  {text}")
        lines.append(f"  If it is not listed or the message fails, nothing is lost: the record is in {address}'s inbox.")
    return lines

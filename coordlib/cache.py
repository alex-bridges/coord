"""Per-machine cache: the last board snapshot, each agent's inbox cursor, the last
GitHub API budget readings of each principal, and the mailbox issue numbers seen.

The snapshot is shared by every agent on one machine (same user), so ten
sessions polling the board within a minute cost GitHub one snapshot, not ten.
Nothing here is authoritative; deleting the directory loses only the inbox
cursors (the next inbox shows the last 7 days again) and the mailbox numbers
(the next board, inbox or mailbox send learns them again). The GitHub App's tokens are
cached elsewhere (apptoken.py), never here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

from . import compat, config, review


def state_dir_name() -> str:
    """The per-user state directory name (config 'state_dir_name', default 'coord')."""
    return config.get("state_dir_name") or "coord"


def legacy_state_dir_names() -> List[str]:
    """Older names whose files are still read (never written): config 'legacy_state_dir_names'."""
    return [name for name in (config.get("legacy_state_dir_names") or []) if name != state_dir_name()]


def base_cache_dir(name: Optional[str] = None) -> str:
    """COORD_CACHE_DIR, else $XDG_CACHE_HOME, or %LOCALAPPDATA%/<name>/cache on Windows
    / ~/.cache/<name> on POSIX."""
    explicit = os.environ.get("COORD_CACHE_DIR")
    if explicit:
        return explicit
    dir_name = name or state_dir_name()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(xdg, dir_name)
    if compat.IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], dir_name, "cache")
    return os.path.join(os.path.expanduser("~/.cache"), dir_name)


def cache_dir(repo: str) -> str:
    # Preserve the shared budget.json path used by older clients wherever the
    # original separator identifies just one repo. Discard ambiguous cache keys.
    separator = "+" if "_" in repo else "_"
    owner, _, name = repo.lower().partition("/")
    return os.path.join(base_cache_dir(), quote(owner, safe="") + separator + quote(name, safe=""))


def _read(path: str) -> Optional[Dict[str, Any]]:
    """The file's JSON, or None. Anything a corrupt, truncated or absurdly deep file can
    raise (ValueError, RecursionError, MemoryError, …) is no data: the cache fails open."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - a cache read must never take a command down
        return None


def _write(path: str, data: Dict[str, Any]) -> None:
    """Atomic replace, so a concurrent reader sees the old file or the new one."""
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        compat.atomic_replace(tmp, path)
        tmp = None
    except Exception:  # noqa: BLE001 - a cache that cannot be written is a slower board, not an error
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def snapshot_path(repo: str) -> str:
    return os.path.join(cache_dir(repo), "board.json")


def load_snapshot(repo: str, max_age_seconds: float) -> Optional[Dict[str, Any]]:
    path = snapshot_path(repo)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    if age > max_age_seconds:
        return None
    data = _read(path)
    if data is None or _timestamp(data.get("generatedAt")) is None:
        return None
    data["cacheAgeSeconds"] = max(0, int(age))
    return data


def store_snapshot(repo: str, snapshot: Dict[str, Any]) -> None:
    _write(snapshot_path(repo), snapshot)


def invalidate_snapshot(repo: str) -> None:
    """Age the snapshot out of every `max_age` instead of deleting it. When the API budget
    is low, reads fall back to it at any age (cli.snapshot_despite_budget), and a `send`,
    which the guard always allows, must not take that fallback away."""
    try:
        os.utime(snapshot_path(repo), (0, 0))
    except OSError:
        pass


def mailboxes_path(repo: str) -> str:
    return os.path.join(cache_dir(repo), "mailboxes.json")


def load_mailboxes(repo: str) -> List[int]:
    """Mailbox issue numbers an `inbox` read or a `send` on this machine has seen (#1070)."""
    data = _read(mailboxes_path(repo))
    # Version 1 cached unauthenticated comment markers indefinitely. Discard it
    # so a forged marker seen before the author check cannot poison future sends.
    numbers = data.get("numbers") if isinstance(data, dict) and data.get("version") == 2 else None
    return [n for n in numbers if type(n) is int and n > 0] if isinstance(numbers, list) else []


def remember_mailboxes(repo: str, numbers: List[int]) -> None:
    """Add to the known mailbox numbers. Nothing ever leaves: a retired mailbox still has
    nobody working on it. Written only when something is new, so most reads cost no write."""
    new = {n for n in numbers if type(n) is int and n > 0}
    if new and new.difference(load_mailboxes(repo)):
        with _cache_lock(repo, "mailboxes.lock", 5.0) as acquired:
            if acquired:
                _write(mailboxes_path(repo), {"version": 2, "numbers": sorted(new.union(load_mailboxes(repo)))})


def budget_path(repo: str) -> str:
    """Alex's readings, as #1033 writes them. Every coord on the machine shares it."""
    return os.path.join(cache_dir(repo), "budget.json")


def app_budget_path(repo: str) -> str:
    """The app principals' readings, in a file of their own: a coord from before #1035
    replaces budget.json without a lock, so anything else kept there could be lost or
    rolled back between its read and its write (Codex on #1046)."""
    return os.path.join(cache_dir(repo), "budget-app.json")


USER = "user"
APP_PREFIX = "app:"
# Keys that are not Alex's readings but may be in budget.json: an early draft of #1035
# wrote {"version": 2, "user": {…}}, and earlier heads of #1046 kept each app principal
# there as "app:<installation>". No merged coord wrote either. They are ignored on read
# and dropped by the next write of Alex's readings; the app's readings are not moved to
# budget-app.json (its windows last an hour, so at most one hour's estimate starts afresh).
_DRAFT_KEYS = ("version", "user")


def load_principal(repo: str, principal: str) -> Dict[str, Any]:
    """One principal's last readings per resource, shared by every agent on the machine:
    Alex's from budget.json ({"core": {…}, "graphql": {…}}), an app principal's from
    budget-app.json ({"app:<installation>": {"core": {"windows": […]}, …}})."""
    if principal == USER:
        data = _read(budget_path(repo))
        readings = {key: value for key, value in data.items()
                    if not str(key).startswith(APP_PREFIX) and key not in _DRAFT_KEYS} if isinstance(data, dict) else {}
    else:
        data = _read(app_budget_path(repo))
        readings = data.get(principal) if isinstance(data, dict) else None
    if not isinstance(readings, dict):
        return {}  # a file of the wrong shape is no reading (budget.valid checks each one's fields)
    return {resource: reading for resource, reading in readings.items() if isinstance(reading, dict)}


def store_principal(repo: str, principal: str, readings: Dict[str, Any]) -> None:
    """Replace one principal's readings. Alex's go to budget.json exactly as #1033 writes
    them; an app principal's to budget-app.json, keeping every other app principal's."""
    if principal == USER:
        _write(budget_path(repo), {resource: reading for resource, reading in readings.items()
                                   if not str(resource).startswith(APP_PREFIX) and resource not in _DRAFT_KEYS})
        return
    data = _read(app_budget_path(repo))
    out = {key: value for key, value in data.items()
           if str(key).startswith(APP_PREFIX) and isinstance(value, dict)} if isinstance(data, dict) else {}
    out[principal] = readings
    _write(app_budget_path(repo), out)


def load_budget(repo: str) -> Dict[str, Any]:
    """Alex's (the `user` principal's) last reading per resource:
    {"core": {"remaining", "limit", "used", "reset", "at"}, "graphql": {…}}."""
    return load_principal(repo, USER)


def store_budget(repo: str, readings: Dict[str, Any]) -> None:
    store_principal(repo, USER, readings)


@contextlib.contextmanager
def budget_lock(repo: str, timeout_seconds: float = 5.0, principal: str = USER) -> Iterator[bool]:
    """Serialize read-modify-writes of one principal's file between processes, so one
    agent's reading cannot overwrite another's (the app principal keeps a maximum per
    window): budget.lock for Alex's, budget-app.lock for the app's. Best effort: a lock
    that cannot be had within the timeout is skipped, not waited on."""
    name = "budget.lock" if principal == USER else "budget-app.lock"
    with _cache_lock(repo, name, timeout_seconds) as acquired:
        yield acquired


@contextlib.contextmanager
def _cache_lock(repo: str, name: str, timeout_seconds: float) -> Iterator[bool]:
    fd = None
    acquired = False
    try:
        os.makedirs(cache_dir(repo), exist_ok=True)
        fd = os.open(os.path.join(cache_dir(repo), name), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                compat.try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
    except OSError:
        pass
    try:
        yield acquired
    finally:
        if fd is not None:
            if acquired:
                compat.unlock(fd)
            os.close(fd)


def sessions_path(repo: str) -> str:
    return os.path.join(cache_dir(repo), "sessions.json")


def load_sessions(repo: str) -> Dict[str, Dict[str, Any]]:
    """coord identity -> {"name", "at"}: the harness session name each local session
    registered for itself. Only names: a sender messages the name and its harness
    resolves it, so nothing here is ever used as a session id."""
    data = _read(sessions_path(repo))
    agents = data.get("agents") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(agents, dict):
        return {}
    return {agent: {"name": entry["name"], "at": _timestamp(entry.get("at"))}
            for agent, entry in agents.items()
            if isinstance(agent, str) and isinstance(entry, dict) and isinstance(entry.get("name"), str)}


def store_session(repo: str, agent: str, name: Optional[str], at: str) -> bool:
    """Register (or with name None, forget) `agent`'s session name. False when the lock
    could not be had, so the caller can say the registry was not changed."""
    with _cache_lock(repo, "sessions.lock", 5.0) as acquired:
        if not acquired:
            return False
        agents = load_sessions(repo)
        if name is None:
            agents.pop(agent, None)
        else:
            agents[agent] = {"name": name, "at": at}
        _write(sessions_path(repo), {"version": 1, "agents": agents})
        return load_sessions(repo).get(agent, {}).get("name") == name


def acked_path(repo: str) -> str:
    return os.path.join(cache_dir(repo), "acked.json")


def load_acked(repo: str) -> Dict[str, Dict[str, Any]]:
    """Handoff id (as a string) -> the ack this machine posted or saw. Only positives are
    kept, and an ack is never withdrawn, so a hit is always true; a miss means "ask GitHub"."""
    data = _read(acked_path(repo))
    handoffs = data.get("handoffs") if isinstance(data, dict) and data.get("version") == 1 else None
    return {k: v for k, v in handoffs.items() if isinstance(v, dict)} if isinstance(handoffs, dict) else {}


def remember_acked(repo: str, handoff_id: int, record: Dict[str, Any]) -> None:
    with _cache_lock(repo, "acked.lock", 5.0) as acquired:
        if acquired:
            handoffs = load_acked(repo)
            handoffs.setdefault(str(handoff_id), record)
            _write(acked_path(repo), {"version": 1, "handoffs": handoffs})


def _cursor_path(repo: str, agent: str) -> str:
    """Readable prefix plus a digest of the FULL id: `a+b` and `a_b` both read `a_b`, and
    two agents sharing a cursor would mark each other's messages read (Codex on #989)."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", agent)[:60]
    digest = hashlib.sha256(agent.encode()).hexdigest()[:12]
    return os.path.join(cache_dir(repo), "inbox", f"{readable}-{digest}.json")


def _timestamp(value: Any) -> Optional[str]:
    if isinstance(value, str):
        try:
            if review.parse_time(value) is not None:
                return value
        except ValueError:
            pass
    return None


def load_cursor(repo: str, agent: str) -> Optional[str]:
    data = _read(_cursor_path(repo, agent))
    return _timestamp((data or {}).get("seenThrough"))


def load_seen_ids(repo: str, agent: str) -> List[int]:
    data = _read(_cursor_path(repo, agent))
    ids = (data or {}).get("seenIds")
    return [i for i in ids if type(i) is int and i != 0] if isinstance(ids, list) else []


def store_cursor(repo: str, agent: str, seen_through: str, seen_ids: Optional[List[int]] = None) -> None:
    """`seenIds`: the ids delivered in the overlap window before `seenThrough`. Readers
    look back that far and skip these, so a comment created in the same second as the
    cursor is neither lost nor shown twice (Codex on #989)."""
    _write(_cursor_path(repo, agent), {"seenThrough": seen_through, "seenIds": sorted(set(seen_ids or []))})

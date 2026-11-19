"""Per-machine cache: the last board snapshot, each agent's inbox cursor, and the last
GitHub API budget reading.

The snapshot is shared by every agent on one machine (same user), so ten
sessions polling the board within a minute cost GitHub one snapshot, not ten.
Nothing here is authoritative; deleting the directory loses only the inbox
cursors (the next inbox shows the last 7 days again).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional


def cache_dir(repo: str) -> str:
    base = os.environ.get("COORD_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "example-coord"
    )
    return os.path.join(base, re.sub(r"[^A-Za-z0-9._-]", "_", repo))


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write(path: str, data: Dict[str, Any]) -> None:
    """Atomic replace, so a concurrent reader sees the old file or the new one."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written is a slower board, not an error


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
    if data is not None:
        data["cacheAgeSeconds"] = int(age)
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


def budget_path(repo: str) -> str:
    return os.path.join(cache_dir(repo), "budget.json")


def load_budget(repo: str) -> Dict[str, Any]:
    """The last GitHub API budget reading per resource (budget.py), shared by every agent
    on the machine: {"core": {"remaining", "limit", "used", "reset", "at"}, "graphql": {…}}."""
    data = _read(budget_path(repo))
    if not isinstance(data, dict):
        return {}  # a file of the wrong shape is no reading (budget.valid checks each one's fields)
    return {resource: reading for resource, reading in data.items() if isinstance(reading, dict)}


def store_budget(repo: str, readings: Dict[str, Any]) -> None:
    _write(budget_path(repo), readings)


def _cursor_path(repo: str, agent: str) -> str:
    """Readable prefix plus a digest of the FULL id: `a+b` and `a_b` both read `a_b`, and
    two agents sharing a cursor would mark each other's messages read (Codex on #989)."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", agent)[:60]
    digest = hashlib.sha256(agent.encode()).hexdigest()[:12]
    return os.path.join(cache_dir(repo), "inbox", f"{readable}-{digest}.json")


def load_cursor(repo: str, agent: str) -> Optional[str]:
    data = _read(_cursor_path(repo, agent))
    return (data or {}).get("seenThrough")


def load_seen_ids(repo: str, agent: str) -> List[int]:
    data = _read(_cursor_path(repo, agent))
    return [int(i) for i in (data or {}).get("seenIds", []) if isinstance(i, int)]


def store_cursor(repo: str, agent: str, seen_through: str, seen_ids: Optional[List[int]] = None) -> None:
    """`seenIds`: the ids delivered in the overlap window before `seenThrough`. Readers
    look back that far and skip these, so a comment created in the same second as the
    cursor is neither lost nor shown twice (Codex on #989)."""
    _write(_cursor_path(repo, agent), {"seenThrough": seen_through, "seenIds": sorted(set(seen_ids or []))})

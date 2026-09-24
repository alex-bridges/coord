"""Per-machine cache: the last board snapshot and each agent's inbox cursor.

The snapshot is shared by every agent on one machine (same user), so ten
sessions polling the board within a minute cost GitHub one snapshot, not ten.
Nothing here is authoritative; deleting the directory loses only the inbox
cursors (the next inbox shows the last 7 days again).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Optional


def cache_dir(repo: str) -> str:
    base = os.environ.get("COORD_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "inisa-coord"
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
    try:
        os.remove(snapshot_path(repo))
    except OSError:
        pass


def _cursor_path(repo: str, agent: str) -> str:
    return os.path.join(cache_dir(repo), "inbox", re.sub(r"[^A-Za-z0-9._-]", "_", agent) + ".json")


def load_cursor(repo: str, agent: str) -> Optional[str]:
    data = _read(_cursor_path(repo, agent))
    return (data or {}).get("seenThrough")


def store_cursor(repo: str, agent: str, seen_through: str) -> None:
    _write(_cursor_path(repo, agent), {"seenThrough": seen_through})

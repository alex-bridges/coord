"""The shared GitHub API budget: what is left, and whether a command may spend it.

Every agent and Alex's own `gh` post as one login, so they share one budget of 5,000
REST calls an hour. On 2026-09-25 it ran out twice (5,229 and about 7,700 calls an
hour), both times mostly from cloud workers polling `coord train status` after their
PRs were queued, and the lockout stopped every agent and Alex's `gh` alike.

The reading comes from real responses: every HTTPS response carries
`X-RateLimit-Remaining`, `-Reset` and `-Resource`, and `gh.GitHub._http` records them
here, per resource (core and graphql are separate budgets). The `gh` transport sees no
headers, so before a gated command it probes with one `gh api -i` call when the
reading is older than PROBE_AFTER_SECONDS (`GitHub.probe_budget`).

Never the `rate_limit` endpoint: that night it reported 5000/5000 while real calls
were refused with 403. Only the headers on real responses told the truth.

The guard (`decision`) gates on core, the budget that ran out; graphql is recorded and
shown but not gated. Floors are environment variables so they can move without a
release. A reading whose reset time has passed counts as a full budget.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from . import cache

PROBE_AFTER_SECONDS = 120
READ_FLOOR_DEFAULT = 1500
HARD_FLOOR_DEFAULT = 300

# Command classes. Reads degrade to the cached snapshot below the read floor; `ci`
# is a read whose one write (the dispatch) still needs the hard floor. `send` always
# runs, so an agent can always report. `train step` runs in Actions on the Actions
# token, a separate budget, and `whoami` makes no call.
CACHE_READS = frozenset({"board", "inbox", "train status"})  # below the read floor: served from the cache, no call
READS = CACHE_READS | {"ci"}
UNGATED = frozenset({"send", "whoami", "train step"})

OK, LOW, REFUSE = "ok", "low", "refuse"


def command_name(args: Any) -> str:
    """`train status`, `board`, …: the command and, for `train`, its subcommand."""
    command = getattr(args, "command", "") or ""
    sub = getattr(args, "train_command", None)
    return f"{command} {sub}" if command == "train" and sub else command


def floor(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def read_floor() -> int:
    return floor("COORD_BUDGET_READ_FLOOR", READ_FLOOR_DEFAULT)


def hard_floor() -> int:
    return floor("COORD_BUDGET_HARD_FLOOR", HARD_FLOOR_DEFAULT)


# MARK: - Readings


def parse_header_block(text: str) -> Dict[str, str]:
    """Response headers from `gh api -i` output (status line, headers, blank line, body),
    lowercased. gh prints Go's canonical casing (`X-Ratelimit-Remaining`) and CRLF lines."""
    headers: Dict[str, str] = {}
    for line in (text or "").replace("\r\n", "\n").split("\n")[1:]:
        if not line.strip():
            break
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def reading_from_headers(headers: Mapping[str, str], default_resource: str = "core",
                         now: Optional[float] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(resource, reading) from response headers, or None when they carry no budget (a
    proxy that strips them must not overwrite a good reading)."""
    lower = {str(k).lower(): v for k, v in headers.items()}
    try:
        remaining = int(lower["x-ratelimit-remaining"])
        reset = int(lower["x-ratelimit-reset"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        limit: Optional[int] = int(lower.get("x-ratelimit-limit", ""))
    except ValueError:
        limit = None
    resource = lower.get("x-ratelimit-resource") or default_resource
    return resource, {"remaining": remaining, "limit": limit, "reset": reset,
                      "at": time.time() if now is None else now}


def record(repo: str, headers: Mapping[str, str], default_resource: str = "core") -> Optional[Dict[str, Any]]:
    """Store the reading these headers carry, per resource, in the machine's cache. Not in
    Actions: the Actions token has its own budget and must not stand in for the shared one."""
    found = reading_from_headers(headers, default_resource)
    if found is None or os.environ.get("GITHUB_ACTIONS") == "true":
        return None
    resource, reading = found
    data = cache.load_budget(repo)
    data[resource] = reading
    cache.store_budget(repo, data)
    return reading


def live(reading: Optional[Dict[str, Any]], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The reading while its window lasts; None when there is none or its reset has passed
    (a new window: the budget is full again)."""
    now = time.time() if now is None else now
    if not reading or not isinstance(reading.get("reset"), int) or reading["reset"] <= now:
        return None
    return reading


def current(repo: str, resource: str = "core", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    return live(cache.load_budget(repo).get(resource), now)


def needs_probe(reading: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return reading is None or not isinstance(reading.get("at"), (int, float)) or now - reading["at"] > PROBE_AFTER_SECONDS


def clock(epoch: int) -> str:
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def low_words(reading: Dict[str, Any]) -> str:
    return f"GitHub API budget low: {reading['remaining']} left until {clock(reading['reset'])}"


def header_words(repo: str, now: Optional[float] = None) -> str:
    """The board's budget line, from recorded readings only (it never spends a call)."""
    core = current(repo, "core", now)
    words = f"API: {core['remaining']} left (resets {clock(core['reset'])})" if core else "API: no current reading"
    graphql = current(repo, "graphql", now)
    if graphql:
        words += f", GraphQL {graphql['remaining']} left"
    return words


# MARK: - The guard


def decision(command: str, reading: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(OK | LOW | REFUSE, message) for `command` given the live core reading.

    LOW: a read may not refresh; it serves the cached snapshot (the command decides how).
    REFUSE: the command must not run. No reading, or a passed reset, is a full budget.
    """
    if command in UNGATED or reading is None:
        return OK, ""
    remaining = reading["remaining"]
    if remaining < hard_floor() and command not in CACHE_READS:
        return REFUSE, f"{low_words(reading)}; not running `coord {command}` (below {hard_floor()})"
    if command in READS and remaining < read_floor():
        return LOW, low_words(reading)
    return OK, ""

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

What is left is not `Remaining` alone. The Claude cloud proxy's token authenticates as
the same user but reports `X-RateLimit-Limit: 15000`, while Alex's gh token reports
5000 for the same reset, and `X-RateLimit-Used` is ONE counter shared by both (probed
2026-09-25 06:07Z: local Used 46, cloud 52 -> 106 over 50 calls, local 114). So when
the 5,000 that locks out Alex's gh is spent, a cloud session still reads ~10,000
remaining. The guard therefore uses `left()`: the lower of Remaining and
COORD_BUDGET_USER_LIMIT (5000) minus Used.

The guard (`decision`) gates on core, the budget that ran out; graphql is recorded and
shown but not gated. Floors are environment variables so they can move without a
release. A reading whose reset time has passed counts as a full budget, and so does
one older than an hour (a window never lasts longer, so a far-future or skewed reset
cannot hold commands back). A reading of the wrong shape counts as absent: the guard
fails open, and on gh the next probe replaces it.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from . import cache

PROBE_AFTER_SECONDS = 120
READ_FLOOR_DEFAULT = 1500
HARD_FLOOR_DEFAULT = 300
USER_LIMIT_DEFAULT = 5000
WINDOW_SECONDS = 3600

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


def user_limit() -> int:
    """The per-user budget the shared Used counter runs against (see the module doc)."""
    return floor("COORD_BUDGET_USER_LIMIT", USER_LIMIT_DEFAULT)


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

    def number(name: str) -> Optional[int]:
        try:
            return int(lower[name])
        except (KeyError, TypeError, ValueError):
            return None

    remaining, reset = number("x-ratelimit-remaining"), number("x-ratelimit-reset")
    limit, used = number("x-ratelimit-limit"), number("x-ratelimit-used")
    if used is None and limit is not None and remaining is not None:
        used = limit - remaining
    if remaining is None or reset is None or used is None:
        return None
    resource = lower.get("x-ratelimit-resource") or default_resource
    return resource, {"remaining": remaining, "limit": limit, "used": used, "reset": reset,
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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid(reading: Any) -> bool:
    """A reading of the shape `record` writes. Anything else (a hand edit, a half-written
    file, another version's format) is treated as absent, so the guard fails open."""
    at = reading.get("at") if isinstance(reading, dict) else None
    return (isinstance(reading, dict) and all(_is_int(reading.get(key)) for key in ("remaining", "used", "reset"))
            and isinstance(at, (int, float)) and not isinstance(at, bool))


def until(reading: Dict[str, Any]) -> float:
    """When the reading stops holding anything back: its reset, but never more than a
    window after it was taken (a far-future or skewed reset must not hold for hours)."""
    return min(reading["reset"], reading["at"] + WINDOW_SECONDS)


def live(reading: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The reading while its window lasts; None when there is none, it has the wrong
    shape, or its window is over (a new window: the budget is full again)."""
    now = time.time() if now is None else now
    if not valid(reading) or until(reading) <= now:
        return None
    return reading


def left(reading: Dict[str, Any]) -> int:
    """What the shared budget still allows: Remaining, but never more than the per-user
    limit minus the shared Used counter (a 15,000-limit token must not read 10,000 left
    when the 5,000 everyone shares is gone)."""
    return max(0, min(reading["remaining"], user_limit() - reading["used"]))


def current(repo: str, resource: str = "core", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    return live(cache.load_budget(repo).get(resource), now)


def summary(repo: str, resource: str = "core", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A live reading and what the guard makes of it, for `board --json`."""
    reading = current(repo, resource, now)
    return None if reading is None else dict(reading, left=left(reading), until=int(until(reading)))


def needs_probe(reading: Any, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return not valid(reading) or now - reading["at"] > PROBE_AFTER_SECONDS


def clock(epoch: float) -> str:
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def low_words(reading: Dict[str, Any]) -> str:
    return f"GitHub API budget low: {left(reading)} left until {clock(until(reading))}"


def header_words(repo: str, now: Optional[float] = None) -> str:
    """The board's budget line, from recorded readings only (it never spends a call)."""
    core = current(repo, "core", now)
    words = f"API: {left(core)} left (resets {clock(until(core))})" if core else "API: no current reading"
    graphql = current(repo, "graphql", now)
    if graphql:
        words += f", GraphQL {left(graphql)} left"
    return words


# MARK: - The guard


def decision(command: str, reading: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(OK | LOW | REFUSE, message) for `command` given the live core reading (`current`).

    Floors compare against `left()`, not Remaining. LOW: a read may not refresh; it
    serves the cached snapshot (the command decides how). REFUSE: the command must not
    run. No reading, a passed reset or a bad shape is a full budget.
    """
    if command in UNGATED or reading is None:
        return OK, ""
    available = left(reading)
    if available < hard_floor() and command not in CACHE_READS:
        return REFUSE, f"{low_words(reading)}; not running `coord {command}` (below {hard_floor()})"
    if command in READS and available < read_floor():
        return LOW, low_words(reading)
    return OK, ""

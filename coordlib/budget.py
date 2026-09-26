"""The GitHub API budgets: what is left, and whether a command may spend it.

Budgets are kept per PRINCIPAL, the account whose counter a call spends, not per token
(#1035):
- `user`: Alex's counter. His `gh`, every agent's `gh`, coord outside app mode, and the
  Claude cloud proxy's token all spend it.
- `app:<installation id>`: the inisa-sidechain GitHub App's installation, which coord
  uses in app mode (apptoken.py). Its hourly tokens are one budget, not several.
- Actions (the train's GITHUB_TOKEN) is a third, and is not recorded here.

Outside app mode every agent and Alex's own `gh` post as one login, so they share one
user budget of 5,000 REST calls an hour. On 2026-09-25 it ran out twice (5,229 and about
7,700 calls an hour), both times mostly from cloud workers polling `coord train status`
after their PRs were queued, and the lockout stopped every agent and Alex's `gh` alike.

The reading comes from real responses: every HTTPS response carries
`X-RateLimit-Remaining`, `-Used`, `-Limit`, `-Reset` and `-Resource`, and
`gh.GitHub._http` records them here, per resource (core and graphql are separate
budgets); `-Limit` stands in for a missing `-Used` (Used = Limit - Remaining). The `gh` transport sees no
headers, so before a gated command it probes with one `gh api -i` call when the
reading is older than PROBE_AFTER_SECONDS (`GitHub.probe_budget`).

Never the `rate_limit` endpoint: that night it reported 5000/5000 while real calls
were refused with 403. Only the headers on real responses told the truth.

For the user principal, what is left is not `Remaining` alone. The Claude cloud proxy's token authenticates as
the same user but reports `X-RateLimit-Limit: 15000`, while Alex's gh token reports
5000 for the same reset, and `X-RateLimit-Used` is ONE counter shared by both (probed
2026-09-25 06:07Z: local Used 46, cloud 52 -> 106 over 50 calls, local 114). So when
the 5,000 that locks out Alex's gh is spent, a cloud session still reads ~10,000
remaining. The guard therefore uses `left()`: the lower of Remaining and
COORD_BUDGET_USER_LIMIT (5000) minus Used.

The app installation's counter is NOT one counter. Probed 2026-09-25 (#1035): 25
sequential calls alternated between two windows whose resets were 3 s apart (used
32..40 on one, 23..38 on the other), across two different tokens. A single reading can
therefore go DOWN between two calls, and the latest reading is not an estimate. For an
app principal the file keeps, per resource, the highest `used` seen for each distinct
reset still in the future; the estimate of what is used is the SUM of those maxima, and
left is COORD_BUDGET_APP_LIMIT (5000) minus that sum. A lower reading never replaces a
higher one for the same reset, and a window whose reset has passed drops out. Alex's
counter showed one window, strictly increasing, in the same probe, so the user
principal keeps #1033's latest-reading rule unchanged.

The guard (`decision`) gates on core, the budget that ran out; graphql is recorded and
shown but not gated. A command is gated on the principal it spends: app-mode commands on
the app's, user-identity commands on Alex's. An exhausted app never spills onto Alex's
budget. Floors are environment variables so they can move without a release. A reading
whose reset time has passed counts as a full budget, and so does one older than an hour
(a window never lasts longer, so a far-future or skewed reset cannot hold commands
back). A reading of the wrong shape counts as absent: the guard fails open, and on gh
the next probe replaces it.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import cache

PROBE_AFTER_SECONDS = 120
READ_FLOOR_DEFAULT = 1500
HARD_FLOOR_DEFAULT = 300
USER_LIMIT_DEFAULT = 5000
APP_LIMIT_DEFAULT = 5000
WINDOW_SECONDS = 3600
AT_SKEW_SECONDS = 300  # how far in the future a reading's `at` may be (clock skew between writers)
MAX_APP_WINDOWS = 8  # two were measured; more than a few live at once is a malformed file

USER = cache.USER
APP_PREFIX = cache.APP_PREFIX  # budget-app.json: each app principal under "app:<id>" (Alex's are in budget.json)

# Command classes. Reads degrade to the cached snapshot below the read floor; `ci`
# is a read whose one write (the dispatch) still needs the hard floor, and so is
# `token exec` (the command it runs reads on the app's budget). `send` always runs, so
# an agent can always report. `train step` runs in Actions on the Actions token, a
# separate budget, and `whoami` and `token status` make no call.
CACHE_READS = frozenset({"board", "inbox", "train status", "conductor status"})  # below the read floor: served from the cache, no call
READS = CACHE_READS | {"ci", "token exec"}
# `conductor tick` keeps its own floors (conductor.Watcher): it must beat and check the
# machine even when reads are refused.
UNGATED = frozenset({"send", "whoami", "train step", "conductor tick", "token status"})

OK, LOW, REFUSE = "ok", "low", "refuse"


def command_name(args: Any) -> str:
    """`train status`, `token exec`, `board`, …: the command and its subcommand, if it has one."""
    command = getattr(args, "command", "") or ""
    sub = getattr(args, f"{command}_command", None)
    return f"{command} {sub}" if sub else command


def app_principal(installation_id: Any) -> str:
    return f"{APP_PREFIX}{installation_id}"


def is_app(principal: Optional[str]) -> bool:
    return bool(principal) and str(principal).startswith(APP_PREFIX)


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


def app_limit() -> int:
    """The app installation's hourly budget: 5,000 for one repository (GitHub scales it
    only above 20 repositories or 20 organization users)."""
    return floor("COORD_BUDGET_APP_LIMIT", APP_LIMIT_DEFAULT)


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


def record(repo: str, headers: Mapping[str, str], default_resource: str = "core",
           principal: str = USER) -> Optional[Dict[str, Any]]:
    """Store the reading these headers carry, per resource, under the principal whose token
    made the call. Not in Actions: the Actions token has its own budget and must not stand
    in for either of these. Returns the reading as the headers gave it."""
    found = reading_from_headers(headers, default_resource)
    if found is None or os.environ.get("GITHUB_ACTIONS") == "true":
        return None
    resource, reading = found
    with cache.budget_lock(repo, principal=principal):
        data = cache.load_principal(repo, principal)
        if is_app(principal):
            data[resource] = merge_window(data.get(resource), reading, reading["at"])
        else:
            data[resource] = reading
        cache.store_principal(repo, principal, data)
    return reading


# MARK: - The app principal's windows


def _window_valid(window: Any, now: float) -> bool:
    if not isinstance(window, dict) or not all(_is_int(window.get(key)) for key in ("used", "reset")):
        return False
    at = window.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    return 0 <= at <= now + AT_SKEW_SECONDS


def _window_until(window: Dict[str, Any]) -> float:
    return min(window["reset"], window["at"] + WINDOW_SECONDS)


def live_windows(stored: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The app principal's windows whose reset (capped at an hour after the reading) is
    still ahead. Anything of the wrong shape is dropped, so a bad file fails open."""
    now = time.time() if now is None else now
    windows = stored.get("windows") if isinstance(stored, dict) else None
    if not isinstance(windows, list):
        return []
    return [w for w in windows if _window_valid(w, now) and _window_until(w) > now]


def merge_window(stored: Any, reading: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Fold one reading into the app principal's windows: the maximum `used` per reset.
    A lower reading for a known reset changes nothing but the time it was seen; a new
    reset is a new window; windows whose reset has passed are dropped."""
    windows = [dict(w) for w in live_windows(stored, now)]
    for window in windows:
        if window["reset"] == reading["reset"]:
            window["used"] = max(window["used"], reading["used"])
            window["at"] = max(window["at"], reading["at"])
            if _is_int(reading.get("limit")):
                window["limit"] = reading["limit"]
            break
    else:
        windows.append({"reset": reading["reset"], "used": reading["used"], "limit": reading.get("limit"),
                        "at": reading["at"]})
    windows.sort(key=lambda w: w["reset"])
    return {"windows": windows[-MAX_APP_WINDOWS:]}


def app_estimate(stored: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The app principal's reading as the guard reads it: `used` is the SUM of the live
    windows' maxima, `remaining` the app limit minus that, and `reset` the first moment a
    window ends (when something is freed). None when no window is live."""
    now = time.time() if now is None else now
    windows = live_windows(stored, now)
    if not windows:
        return None
    used = sum(w["used"] for w in windows)
    return {"remaining": max(0, app_limit() - used), "limit": app_limit(), "used": used,
            "reset": int(min(_window_until(w) for w in windows)), "at": max(w["at"] for w in windows),
            "windows": windows}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid(reading: Any, now: Optional[float] = None) -> bool:
    """A reading of the shape `record` writes. Anything else (a hand edit, a half-written
    file, another version's format) is treated as absent, so the guard fails open.

    `at` must lie between the epoch and AT_SKEW_SECONDS from now: `until()` caps a hold
    at `at` plus an hour, and a NaN, infinite or far-future `at` would defeat that cap
    (min(reset, nan) is reset) and keep `needs_probe` from ever firing. The bounds are
    plain comparisons, which reject NaN and ±inf and compare an int of any size exactly;
    a float conversion (math.isfinite) raises OverflowError on a 309-digit JSON integer.
    """
    if not isinstance(reading, dict) or not all(_is_int(reading.get(key)) for key in ("remaining", "used", "reset")):
        return False
    at = reading.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    return 0 <= at <= (time.time() if now is None else now) + AT_SKEW_SECONDS


def until(reading: Dict[str, Any]) -> float:
    """When the reading stops holding anything back: its reset, but never more than a
    window after it was taken (a far-future or skewed reset must not hold for hours)."""
    return min(reading["reset"], reading["at"] + WINDOW_SECONDS)


def live(reading: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The reading while its window lasts; None when there is none, it has the wrong
    shape, or its window is over (a new window: the budget is full again)."""
    now = time.time() if now is None else now
    if not valid(reading, now) or until(reading) <= now:
        return None
    return reading


def left(reading: Dict[str, Any]) -> int:
    """What the budget still allows.

    User principal: Remaining, but never more than the per-user limit minus the shared
    Used counter (a 15,000-limit token must not read 10,000 left when the 5,000 everyone
    shares is gone). App principal (an `app_estimate`): the app limit minus the sum of
    its windows' maxima.
    """
    if "windows" in reading:
        return max(0, app_limit() - reading["used"])
    return max(0, min(reading["remaining"], user_limit() - reading["used"]))


def current(repo: str, resource: str = "core", now: Optional[float] = None,
            principal: str = USER) -> Optional[Dict[str, Any]]:
    """The live reading of one principal's resource, as the guard reads it."""
    if is_app(principal):
        return app_estimate(cache.load_principal(repo, principal).get(resource), now)
    return live(cache.load_budget(repo).get(resource), now)


def summary(repo: str, resource: str = "core", now: Optional[float] = None,
            principal: str = USER) -> Optional[Dict[str, Any]]:
    """A live reading and what the guard makes of it, for `board --json`."""
    reading = current(repo, resource, now, principal)
    return None if reading is None else dict(reading, left=left(reading), until=int(until(reading)))


def needs_probe(reading: Any, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return not valid(reading, now) or now - reading["at"] > PROBE_AFTER_SECONDS


def clock(epoch: float) -> str:
    return time.strftime("%H:%MZ", time.gmtime(epoch))


def low_words(reading: Dict[str, Any]) -> str:
    where = " on the GitHub App" if "windows" in reading else ""
    return f"GitHub API budget low: {left(reading)} left{where} until {clock(until(reading))}"


def principal_words(repo: str, principal: Optional[str], now: Optional[float] = None) -> str:
    core = current(repo, "core", now, principal) if principal else None
    words = f"{left(core)} left (resets {clock(until(core))})" if core else "no current reading"
    graphql = current(repo, "graphql", now, principal) if principal else None
    if graphql:
        words += f", GraphQL {left(graphql)} left"
    return words


def header_words(repo: str, now: Optional[float] = None, principal: Optional[str] = USER) -> str:
    """The board's budget line, from recorded readings only (it never spends a call). In
    app mode (`principal` not USER; None before the installation is known) it shows the
    app's budget, which coord spends, then Alex's."""
    if principal == USER:
        return "API: " + principal_words(repo, USER, now)
    return f"API (app): {principal_words(repo, principal, now)} · Alex: {principal_words(repo, USER, now)}"


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

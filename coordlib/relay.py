"""The handoff relay: a watcher that wakes the recipient of a handoff nobody has taken.

A handoff (`send --handoff`) is durable on GitHub until it is taken (`handoff take` posts an
ack). Phase one of waking is the sender's: only a session that can send a harness session
message can wake another. Senders that cannot (CI bots, the merge train, a conductor, other
harnesses) leave the handoff waiting. This watcher closes that gap on one machine. Run it
from a scheduler (`coord handoff watch` every few minutes). Each pass:

1. reads the board (the cached snapshot when it is fresh enough), which holds every
   handoff, ack and give-up record of the last CLAIM_WINDOW_DAYS;
2. for each handoff nobody has taken, addressed to a session registered on this machine
   (`coord session register`), starts the relay command: a headless agent run that may
   use only ListAgents and SendMessage, finds the session BY NAME, and sends it one fixed
   line;
3. records the attempt in its own state file, outside any repository, before moving on.

Rules this module enforces:
- **Durable.** GitHub is the record. The state file only remembers what this machine did
  (attempts, times, outcomes). After a restart, or with the file lost, the board still
  says which handoffs were taken (acks) or given up (give-up records), so neither is
  woken again.
- **Bounded.** At most `max_attempts` relays per handoff, spaced by an exponential
  backoff. At the cap the watcher posts one give-up record (a `msg` to the sender with
  `relayGaveUp`) and stops.
- **Identifiers only.** The relay prompt is a fixed template. Its only variable parts are
  the handoff id, the issue or PR number and the session name, each checked against a
  strict pattern first. No handoff text, note or comment text is ever put in it. The
  session name is never resolved to a session id here: the relay agent finds it through
  ListAgents.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from . import cache, compat, config, handoff, identity, review, snapshot

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_MINUTES = 10.0  # first retry after this; doubles each attempt
DEFAULT_TIMEOUT_SECONDS = 180
STATE_VERSION = 1

_HANDOFF_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,9}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# The whole relay instruction. Only {session}, {handoff} and {number} vary, and each is
# validated by the pattern above before it is substituted.
PROMPT = (
    "You relay one notice and nothing else. Call ListAgents. If a session named exactly "
    "{session} is listed, call SendMessage once, addressed to {session}, with exactly this "
    "message text: coord handoff {handoff} on #{number}: run `coord handoff take {handoff}`. "
    "Call no other tool and send nothing else. Then reply with one word: RELAYED if you sent "
    "it, NOT_LISTED if no session with exactly that name is listed."
)
RELAY_TOOLS = "ListAgents,SendMessage"
DEFAULT_COMMAND = ["claude", "-p", "{prompt}", "--tools", RELAY_TOOLS, "--allowedTools", RELAY_TOOLS,
                   "--permission-prompts", "none", "--strict-mcp-config", "--no-session-persistence"]

Runner = Callable[[List[str], str, Dict[str, str], int], "subprocess.CompletedProcess[str]"]


class IdentifierError(ValueError):
    """An identifier failed its pattern; nothing is relayed for that handoff."""


def checked(value: Any, pattern: "re.Pattern[str]", what: str) -> str:
    text = str(value) if type(value) is int else value
    if not isinstance(text, str) or not pattern.fullmatch(text):
        raise IdentifierError(f"{what} does not match its pattern")
    return text


def prompt(handoff_id: Any, number: Any, session: Any) -> str:
    """The fixed relay text, or IdentifierError. Nothing but the three identifiers varies."""
    return PROMPT.format(handoff=checked(handoff_id, _HANDOFF_ID, "handoff id"),
                         number=checked(number, _NUMBER, "issue or PR number"),
                         session=checked(session, _SESSION, "session name"))


def command(text: str) -> List[str]:
    """The relay argv: config `relay_command` (an argv list with a "{prompt}" item), or the
    default headless Claude Code run limited to ListAgents and SendMessage. The prompt is
    one argv item; no shell is involved."""
    configured = config.get("relay_command") or []
    template = list(configured) if configured else list(DEFAULT_COMMAND)
    if "{prompt}" not in template:
        raise ValueError('relay_command must contain a "{prompt}" item')
    return [text if part == "{prompt}" else part for part in template]


def default_runner(argv: List[str], cwd: str, env: Dict[str, str], timeout: int) -> "subprocess.CompletedProcess[str]":
    try:
        return compat.run_isolated_process(argv, cwd, env, timeout)
    except OSError as error:
        return subprocess.CompletedProcess(argv, 127, "", str(error))


def outcome(result: "subprocess.CompletedProcess[str]") -> str:
    """relayed, not-listed or failed, from the relay run's exit code and last word."""
    if result.returncode != 0:
        return "failed"
    words = (result.stdout or "").split()
    last = words[-1].strip(".") if words else ""
    return {"RELAYED": "relayed", "NOT_LISTED": "not-listed"}.get(last, "failed")


# MARK: - Settings and state


def _env_number(env: Dict[str, str], name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(env.get(name, ""))
    except ValueError:
        return default
    return value if low <= value <= high else default


def settings(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = dict(os.environ if env is None else env)
    return {
        "maxAttempts": int(_env_number(env, "COORD_RELAY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 20)),
        "backoffMinutes": _env_number(env, "COORD_RELAY_BACKOFF_MINUTES", DEFAULT_BACKOFF_MINUTES, 0, 24 * 60),
        "timeout": int(_env_number(env, "COORD_RELAY_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, 10, 3600)),
    }


def state_dir(repo: str, env: Optional[Dict[str, str]] = None) -> str:
    """COORD_RELAY_STATE_DIR, else $XDG_STATE_HOME (%LOCALAPPDATA% on Windows, else
    ~/.local/state) / <state_dir_name>-relay / repo-<owner%2Fname>. Never inside a checkout."""
    env = dict(os.environ if env is None else env)
    if env.get("COORD_RELAY_STATE_DIR"):
        return env["COORD_RELAY_STATE_DIR"]
    if env.get("XDG_STATE_HOME"):
        root = env["XDG_STATE_HOME"]
    elif compat.IS_WINDOWS and env.get("LOCALAPPDATA"):
        root = env["LOCALAPPDATA"]
    else:
        root = os.path.expanduser("~/.local/state")
    namespace = (config.get("state_dir_name") or "coord") + "-relay"
    return os.path.join(root, namespace, "repo-" + urllib.parse.quote(repo.lower(), safe=""))


def state_path(directory: str) -> str:
    return os.path.join(directory, "relay.json")


def load_state(directory: str) -> Dict[str, Any]:
    """{"version": 1, "handoffs": {id: entry}}. A missing file is an empty state; an
    unreadable one is set aside (relay.json.bad) and the board decides what is handled."""
    path = state_path(directory)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("version") == STATE_VERSION and isinstance(data.get("handoffs"), dict):
            return data
        raise ValueError("unexpected shape")
    except FileNotFoundError:
        return {"version": STATE_VERSION, "handoffs": {}}
    except (OSError, ValueError, RecursionError):
        try:
            compat.atomic_replace(path, path + ".bad")
        except OSError:
            pass
        return {"version": STATE_VERSION, "handoffs": {}, "recovered": True}


def save_state(directory: str, state: Dict[str, Any]) -> None:
    """Atomic: a reader (or a crash) sees the old file or the new one, never half of one."""
    os.makedirs(directory, exist_ok=True)
    out = {"version": STATE_VERSION, "handoffs": state.get("handoffs") or {}}
    tmp = state_path(directory) + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    compat.atomic_replace(tmp, state_path(directory))


# MARK: - One pass


def targets(item: Dict[str, Any], sessions: Dict[str, Dict[str, Any]]) -> List[str]:
    """Registered agents on this machine the handoff is addressed to (never its sender)."""
    return sorted(agent for agent in sessions if agent != item["from"]
                  and any(identity.matches(to, agent) for to in item["to"]))


class Watcher:
    def __init__(self, gh: Any, post: Callable[[int, str, Dict[str, Any]], Dict[str, Any]],
                 runner: Runner = default_runner, env: Optional[Dict[str, str]] = None,
                 now: Optional[dt.datetime] = None, me: Optional[str] = None):
        self.gh = gh
        self.post = post
        self.runner = runner
        self.env = dict(os.environ if env is None else env)
        self.now = now or snapshot.now_utc()
        self.settings = settings(self.env)
        self.dir = state_dir(gh.repo, self.env)
        self.lines: List[str] = []
        self.me = me or identity.agent_id()

    def say(self, text: str) -> None:
        self.lines.append(text)

    def backoff(self, attempts: int) -> dt.timedelta:
        return dt.timedelta(minutes=self.settings["backoffMinutes"] * (2 ** max(0, attempts - 1)))

    def run(self, snap: Dict[str, Any], dry_run: bool = False) -> List[str]:
        """One pass under the state lock. Another watcher holding it: say so and stop."""
        os.makedirs(self.dir, exist_ok=True)
        fd = os.open(os.path.join(self.dir, "relay.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                compat.try_lock(fd)
            except OSError:
                self.say("another relay watcher is running for this repository; nothing done")
                return self.lines
            try:
                self._pass(snap, dry_run)
            finally:
                compat.unlock(fd)
        finally:
            os.close(fd)
        return self.lines

    def _pass(self, snap: Dict[str, Any], dry_run: bool) -> None:
        state = load_state(self.dir)
        if state.get("recovered"):
            self.say(f"the state file was unreadable; set aside as {state_path(self.dir)}.bad. "
                     "The board's acks and give-up records still decide what is handled")
        entries: Dict[str, Dict[str, Any]] = state["handoffs"]
        items = handoff.handoffs(snap, self.now)
        sessions = cache.load_sessions(self.gh.repo)
        cap = self.settings["maxAttempts"]
        waiting = 0
        for item in items:
            key = str(item["id"])
            entry = entries.get(key)
            if item["acked"]:
                if entry is not None and not entry.get("acknowledged"):
                    entry["acknowledged"] = item["acks"][0]["at"]
                    self.say(f"handoff {key}: taken; done")
                continue
            if item["relayGaveUp"]:
                if entry is not None and not entry.get("gaveUp"):
                    entry["gaveUp"] = item["relayGaveUp"]["at"]
                continue
            if entry is not None and (entry.get("gaveUp") or entry.get("acknowledged")):
                continue
            names = targets(item, sessions)
            if not names:
                continue  # nobody on this machine to wake; the handoff waits in the inbox
            entry = entries.setdefault(key, {"attempts": 0})
            due = review.parse_time(entry.get("nextAttempt"))
            if due is not None and self.now < due:
                waiting += 1  # the last attempt also gets its full wait before the give-up
                continue
            if entry["attempts"] >= cap:
                self.give_up(item, entry, f"no ack after {entry['attempts']} relay attempt(s)", dry_run)
                self.save(state, dry_run)
                continue
            try:
                texts = [(sessions[agent]["name"], prompt(item["id"], item.get("on"), sessions[agent]["name"]))
                         for agent in names]
            except IdentifierError as error:
                self.give_up(item, entry, f"not relayed: {error}", dry_run)
                self.save(state, dry_run)
                continue
            entry["attempts"] += 1
            entry["lastAttempt"] = snapshot.iso(self.now)
            entry["nextAttempt"] = snapshot.iso(self.now + self.backoff(entry["attempts"]))
            self.save(state, dry_run)  # the attempt is on disk before the relay runs
            results = []
            for name, text in texts:
                if dry_run:
                    results.append(f"{name}: dry run")
                    continue
                result = self.runner(command(text), self.dir, self.env, self.settings["timeout"])
                results.append(f"{name}: {outcome(result)}")
            entry["lastResult"] = ", ".join(results)
            self.say(f"handoff {key}: relay attempt {entry['attempts']}/{cap} ({entry['lastResult']})")
            self.save(state, dry_run)
        self.prune(entries, {str(h["id"]) for h in items})
        self.save(state, dry_run)
        if waiting:
            self.say(f"{waiting} handoff(s) waiting for their next attempt")
        if not self.lines:
            self.say("nothing to relay")

    def give_up(self, item: Dict[str, Any], entry: Dict[str, Any], why: str, dry_run: bool) -> None:
        """Record the give-up here and once on GitHub, where the board and the sender see it."""
        entry["gaveUp"] = snapshot.iso(self.now)
        entry["gaveUpReason"] = why
        self.say(f"handoff {item['id']}: gave up ({why}); it stays in the recipient's inbox")
        if dry_run or not isinstance(item.get("on"), int):
            return
        payload = {"kind": "msg", "v": 1, "agent": self.me, "to": [item["from"]], "relayGaveUp": item["id"]}
        if item.get("addressedOn") is not None:
            payload["on"] = item["on"]
        text = (f"**Relay gave up** on handoff {item['url']} after {entry.get('attempts', 0)} attempt(s). "
                "It is still in the recipient's inbox.")
        posted = self.post(item["on"], text, payload)
        entry["gaveUpRecord"] = (posted or {}).get("html_url")

    def prune(self, entries: Dict[str, Dict[str, Any]], on_board: set) -> None:
        """Forget finished handoffs that have left the board's window."""
        for key in [k for k, e in entries.items() if k not in on_board and (e.get("gaveUp") or e.get("acknowledged"))]:
            del entries[key]

    def save(self, state: Dict[str, Any], dry_run: bool) -> None:
        if not dry_run:
            save_state(self.dir, state)

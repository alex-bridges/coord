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

import contextlib
import datetime as dt
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from . import cache, compat, config, handoff, identity, review, snapshot

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_MINUTES = 10.0  # first retry after this; doubles each attempt
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MODEL = "claude-haiku-5-5"  # small and pinned; COORD_RELAY_MODEL overrides
GIVE_UP_POST_LIMIT = 5  # tries at posting one give-up record before recording it here only
STATE_VERSION = 1

_HANDOFF_ID = re.compile(r"[1-9][0-9]{0,19}")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_ATTEMPT = re.compile(r"[1-9][0-9]?")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# The whole relay instruction. Only {session}, {handoff}, {number} and {attempt} vary, and
# each is checked (session names by handoff.session_name_error) before it is substituted.
PROMPT = (
    "You relay one notice and nothing else. Call ListAgents. If a session named exactly "
    "{session} is listed, call SendMessage once, addressed to {session}, with exactly this "
    "message text: coord handoff {handoff} on #{number} (relay attempt {attempt}): run "
    "`coord handoff take {handoff}`. "
    "Call no other tool and send nothing else. Then reply with one word: RELAYED if you sent "
    "it, NOT_LISTED if no session with exactly that name is listed."
)
RELAY_TOOLS = "ListAgents,SendMessage"
# The conductor's diagnosis recipe (runtime.claude_command, role "diagnose"): safe mode (no
# user hooks, plugins, skills or CLAUDE.md), dontAsk, no MCP servers, no prompts, nothing
# saved; only the two tools the relay needs; a small pinned model.
DEFAULT_COMMAND = ["claude", "-p", "{prompt}", "--tools", RELAY_TOOLS, "--allowedTools", RELAY_TOOLS,
                   "--safe-mode", "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--permission-prompts", "none", "--no-session-persistence", "--model", "{model}"]
# Never passed to the relay: credentials, and every variable that ties a process to the
# coord identity or to the Claude Code session that started the watcher.
SCRUBBED = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "COORD_AGENT",
            "COORD_SESSION_ID", "CODEX_THREAD_ID", "CONDUCTOR_WAKE", "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE")
SCRUBBED_PREFIXES = ("CLAUDE_CODE_MESSAGING_", "COORD_")

Runner = Callable[[List[str], str, Dict[str, str], int], "subprocess.CompletedProcess[str]"]


class IdentifierError(ValueError):
    """An identifier failed its pattern; nothing is relayed for that handoff."""


def checked(value: Any, pattern: "re.Pattern[str]", what: str) -> str:
    text = str(value) if type(value) is int else value
    if not isinstance(text, str) or not pattern.fullmatch(text):
        raise IdentifierError(f"{what} does not match its pattern")
    return text


def session_name(value: Any) -> str:
    """The registration rule (handoff.session_name_error, which also refuses a session id)."""
    if handoff.session_name_error(value) is not None:
        raise IdentifierError("session name does not match its pattern")
    return value


def prompt(handoff_id: Any, number: Any, session: Any, attempt: Any = 1) -> str:
    """The fixed relay text, or IdentifierError. Nothing but the four identifiers varies."""
    return PROMPT.format(handoff=checked(handoff_id, _HANDOFF_ID, "handoff id"),
                         number=checked(number, _NUMBER, "issue or PR number"),
                         session=session_name(session),
                         attempt=checked(attempt, _ATTEMPT, "attempt number"))


def command(text: str, model: str = DEFAULT_MODEL) -> List[str]:
    """The relay argv: config `relay_command` (an argv list with a "{prompt}" item and,
    optionally, a "{model}" item), or the default isolated Claude Code run. The prompt is
    one argv item; no shell is involved."""
    configured = config.get("relay_command") or []
    template = list(configured) if configured else list(DEFAULT_COMMAND)
    if "{prompt}" not in template:
        raise ValueError('relay_command must contain a "{prompt}" item')
    model = checked(model, _MODEL, "model")
    return [text if part == "{prompt}" else model if part == "{model}" else part for part in template]


def relay_environment(env: Dict[str, str]) -> Dict[str, str]:
    """The watcher's environment less credentials and session ties (SCRUBBED)."""
    return {key: value for key, value in env.items()
            if key not in SCRUBBED and not key.startswith(SCRUBBED_PREFIXES)}


class ChildRunner:
    """Runs one relay at a time in its own process group (a Job Object on Windows, through
    compat), and kills that group when the watcher is told to stop (SIGTERM, SIGINT), so a
    relay never outlives the watcher and its timeout."""

    def __init__(self) -> None:
        self.child: Optional[subprocess.Popen] = None

    def __call__(self, argv: List[str], cwd: str, env: Dict[str, str], timeout: int) -> "subprocess.CompletedProcess[str]":
        if compat.IS_WINDOWS:
            try:
                return compat.run_isolated_process(argv, cwd, env, timeout)
            except OSError as error:
                return subprocess.CompletedProcess(argv, 127, "", str(error))
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out, \
                tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
            try:
                self.child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                              stderr=err, text=True, start_new_session=True)
            except OSError as error:
                return subprocess.CompletedProcess(argv, 127, "", str(error))
            timed_out = False
            try:
                self.child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                self.kill()
                self.child.wait()
            code = 124 if timed_out else self.child.returncode
            self.child = None
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(argv, code, out.read(), err.read())

    def kill(self) -> None:
        child = self.child
        if child is not None and not compat.IS_WINDOWS:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, signal.SIGKILL)


@contextlib.contextmanager
def stop_kills(runner: Any):
    """While a pass runs: SIGTERM, SIGINT and (POSIX) SIGHUP kill the running relay's group first, then
    stop the watcher as they would have. Only in the main thread (signal's rule)."""
    if not hasattr(runner, "kill") or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}

    def handler(signum, frame):
        runner.kill()
        old = previous.get(signum)
        if callable(old):
            old(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    stops = [signal.SIGTERM, signal.SIGINT] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
    for signum in stops:
        if signal.getsignal(signum) == signal.SIG_IGN:
            continue  # inherited "ignore" (nohup, a background job): leave it, and the relay, alone
        previous[signum] = signal.signal(signum, handler)
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


def default_runner(argv: List[str], cwd: str, env: Dict[str, str], timeout: int) -> "subprocess.CompletedProcess[str]":
    """A one-off ChildRunner (the Watcher keeps its own, so a stop signal can reach it)."""
    return ChildRunner()(argv, cwd, env, timeout)


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
        "model": env.get("COORD_RELAY_MODEL") or DEFAULT_MODEL,
    }


def inside_work_tree(path: str) -> Optional[str]:
    """The git work tree (a parent holding .git) that contains `path`, or None. No git
    call: a directory or file named .git is what makes a work tree."""
    current = os.path.abspath(path)
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def state_dir(repo: str, env: Optional[Dict[str, str]] = None) -> str:
    """COORD_RELAY_STATE_DIR, else $XDG_STATE_HOME (%LOCALAPPDATA% on Windows, else
    ~/.local/state) / <state_dir_name>-relay / repo-<owner%2Fname>. ValueError inside a
    git work tree: the state, and the lock that keeps one watcher per repository, belong
    to the user, not to a checkout."""
    env = dict(os.environ if env is None else env)
    if env.get("COORD_RELAY_STATE_DIR"):
        directory = env["COORD_RELAY_STATE_DIR"]
    else:
        if env.get("XDG_STATE_HOME"):
            root = env["XDG_STATE_HOME"]
        elif compat.IS_WINDOWS and env.get("LOCALAPPDATA"):
            root = env["LOCALAPPDATA"]
        else:
            root = os.path.expanduser("~/.local/state")
        namespace = (config.get("state_dir_name") or "coord") + "-relay"
        directory = os.path.join(root, namespace, "repo-" + urllib.parse.quote(repo.lower(), safe=""))
    tree = inside_work_tree(directory)
    if tree is not None:
        home = os.path.abspath(os.path.expanduser("~"))
        where = ("your home folder is itself a git work tree, so every folder under it is inside one; "
                 "set COORD_RELAY_STATE_DIR to a folder outside your home folder"
                 if os.path.normcase(tree) == os.path.normcase(home) else
                 "set COORD_RELAY_STATE_DIR (or XDG_STATE_HOME) to a folder outside it")
        raise ValueError(f"the relay state folder {directory} is inside the git work tree {tree}, whose commits and "
                         f"branch would reach the relay run as context; {where}")
    return directory


def state_path(directory: str) -> str:
    return os.path.join(directory, "relay.json")


_TIMES = ("lastAttempt", "nextAttempt", "acknowledged", "givingUp", "gaveUp")
_TEXTS = ("lastResult", "gaveUpReason", "gaveUpRecord", "lastError")


def entry_valid(entry: Any) -> bool:
    """One handoff's record: a dict, a non-negative int `attempts`, times that parse."""
    if not isinstance(entry, dict) or type(entry.get("attempts")) is not int or entry["attempts"] < 0:
        return False
    if type(entry.get("givingUpPosts", 0)) is not int:
        return False
    for key in _TIMES:
        value = entry.get(key)
        if value is not None:
            try:
                if not isinstance(value, str) or review.parse_time(value) is None:
                    return False
            except (ValueError, OverflowError):
                return False
    return all(entry.get(key) is None or isinstance(entry[key], str) for key in _TEXTS)


def load_state(directory: str, set_aside: bool = True) -> Dict[str, Any]:
    """{"version": 1, "handoffs": {id: entry}}. A missing file is an empty state. An
    unreadable file, or one with bad entries, is copied aside (relay.json.bad, not in a
    dry run) and the bad entries dropped: the board's acks and give-up records still
    decide what is handled."""
    path = state_path(directory)
    empty: Dict[str, Any] = {"version": STATE_VERSION, "handoffs": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not (isinstance(data, dict) and data.get("version") == STATE_VERSION and isinstance(data.get("handoffs"), dict)):
            raise ValueError("unexpected shape")
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, RecursionError):
        if set_aside:
            with contextlib.suppress(OSError):
                compat.atomic_replace(path, path + ".bad")
        return dict(empty, recovered=True)
    good = {key: entry for key, entry in data["handoffs"].items()
            if isinstance(key, str) and _HANDOFF_ID.fullmatch(key) and entry_valid(entry)}
    if len(good) != len(data["handoffs"]):
        if set_aside:
            with contextlib.suppress(OSError), open(path + ".bad", "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, sort_keys=True)
        return {"version": STATE_VERSION, "handoffs": good, "dropped": len(data["handoffs"]) - len(good)}
    return data


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
    """Registered agents on this machine the handoff names exactly (never its sender). A
    handoff to `all` or to a prefix wakes nobody: `send --handoff` refuses those."""
    return sorted(agent for agent in sessions if agent != item["from"] and agent in item["to"])


Reader = Callable[[int, str], List[Dict[str, Any]]]


class Watcher:
    def __init__(self, gh: Any, post: Callable[[int, str, Dict[str, Any]], Dict[str, Any]],
                 runner: Optional[Runner] = None, env: Optional[Dict[str, str]] = None,
                 now: Optional[dt.datetime] = None, me: Optional[str] = None,
                 read: Optional[Reader] = None, state_directory: Optional[str] = None):
        self.gh = gh
        self.post = post
        self.read = read
        self.runner = runner if runner is not None else ChildRunner()
        self.env = dict(os.environ if env is None else env)
        self.now = now or snapshot.now_utc()
        self.settings = settings(self.env)
        self.dir = state_directory or state_dir(gh.repo, self.env)
        self.lines: List[str] = []
        self.me = me or identity.agent_id()

    def say(self, text: str) -> None:
        self.lines.append(text)

    def backoff(self, attempts: int) -> dt.timedelta:
        return dt.timedelta(minutes=self.settings["backoffMinutes"] * (2 ** max(0, attempts - 1)))

    def run(self, snap: Dict[str, Any], dry_run: bool = False) -> List[str]:
        """One pass under the state lock. Another watcher holding it: say so and stop. A
        dry run takes no lock and writes nothing."""
        if dry_run:
            self._pass(snap, True)
            return self.lines
        os.makedirs(self.dir, exist_ok=True)
        fd = os.open(os.path.join(self.dir, "relay.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                compat.try_lock(fd)
            except OSError:
                self.say("another relay watcher is running for this repository; nothing done")
                return self.lines
            try:
                with stop_kills(self.runner):
                    self._pass(snap, False)
            finally:
                compat.unlock(fd)
        finally:
            os.close(fd)
        return self.lines

    def _pass(self, snap: Dict[str, Any], dry_run: bool) -> None:
        state = load_state(self.dir, set_aside=not dry_run)
        bad = state_path(self.dir) + ".bad"
        if state.get("recovered"):
            self.say(f"the state file is unreadable; a real pass sets it aside as {bad}" if dry_run else
                     f"the state file was unreadable; set aside as {bad}. "
                     "The board's acks and give-up records still decide what is handled")
        if state.get("dropped"):
            noun = f"malformed state entr{'y' if state['dropped'] == 1 else 'ies'}"
            self.say(f"{state['dropped']} {noun}; a real pass drops them and keeps a copy in {bad}" if dry_run else
                     f"dropped {state['dropped']} {noun} (a copy is in {bad})")
        try:
            command(prompt(1, 1, "check", 1), self.settings["model"])
        except (ValueError, IdentifierError) as error:
            # A configuration error is no attempt: nothing is counted, nothing is given up.
            self.say(f"relay configuration error ({error}); nothing relayed or counted this pass. "
                     "Check COORD_RELAY_MODEL and relay_command")
            return
        entries: Dict[str, Dict[str, Any]] = state["handoffs"]
        items = handoff.handoffs(snap, self.now)
        sessions = cache.load_sessions(self.gh.repo)
        waiting = 0
        for item in items:
            key = str(item["id"])
            try:
                waiting += self.step(item, entries, sessions, state, dry_run)
            except Exception as error:  # noqa: BLE001 - one handoff's failure must not stop the others
                entry = entries.get(key)
                if entry is not None:
                    entry["lastError"] = f"{type(error).__name__}: {str(error)[:200]}"
                self.say(f"handoff {key}: {type(error).__name__}: {str(error)[:200]}; trying again next pass")
                self.save(state, dry_run)
        self.prune(entries, {str(h["id"]) for h in items})
        self.save(state, dry_run)
        if waiting:
            self.say(f"{waiting} handoff(s) waiting for their next attempt")
        if not self.lines:
            self.say("nothing to relay")

    def step(self, item: Dict[str, Any], entries: Dict[str, Dict[str, Any]], sessions: Dict[str, Dict[str, Any]],
             state: Dict[str, Any], dry_run: bool) -> int:
        """One handoff; 1 when it waits for its next attempt, else 0."""
        key = str(item["id"])
        entry = entries.get(key)
        if item["acked"]:
            if entry is not None and not entry.get("acknowledged"):
                entry["acknowledged"] = item["acks"][0]["at"]
                self.say(f"handoff {key}: taken; done")
            return 0
        if item["relayGaveUp"]:
            if entry is not None and not entry.get("gaveUp"):
                entry["gaveUp"] = item["relayGaveUp"]["at"]
            return 0
        if entry is not None and (entry.get("gaveUp") or entry.get("acknowledged")):
            return 0
        local = cache.load_acked(self.gh.repo).get(key)  # a take on this machine since the board was read
        if local:
            if entry is not None:
                entry["acknowledged"] = local.get("at") or snapshot.iso(self.now)
            return 0
        names = targets(item, sessions)
        if not names:
            return 0  # nobody on this machine to wake; the handoff waits in the inbox
        entry = entries.setdefault(key, {"attempts": 0})
        if entry.get("givingUp"):
            self.give_up(item, entry, entry.get("gaveUpReason") or "no ack", state, dry_run)
            return 0
        due = review.parse_time(entry.get("nextAttempt"))
        if due is not None and self.now < due:
            return 1  # the last attempt also gets its full wait before the give-up
        if entry["attempts"] >= self.settings["maxAttempts"]:
            self.give_up(item, entry, f"no ack after {entry['attempts']} relay attempt(s)", state, dry_run)
            return 0
        attempt = entry["attempts"] + 1
        texts = []
        for agent in names:
            name = sessions[agent].get("name")
            try:
                texts.append((name, command(prompt(item["id"], item.get("on"), name, attempt), self.settings["model"])))
            except IdentifierError as error:
                # This machine's registry entry is bad; skip it here, give nothing up for anyone.
                self.say(f"handoff {key}: skipped {agent} ({error}); fix it with `coord session register`")
        if not texts:
            if not entries[key].get("lastAttempt"):
                del entries[key]
            return 0
        entry["attempts"] = attempt
        entry["lastAttempt"] = snapshot.iso(self.now)
        entry["nextAttempt"] = snapshot.iso(self.now + self.backoff(attempt))
        self.save(state, dry_run)  # the attempt is on disk before the relay runs
        results = []
        for name, text in texts:
            if dry_run:
                results.append(f"{name}: dry run")
                continue
            os.makedirs(self.dir, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="run-", dir=self.dir) as scratch:  # under the checked state folder
                result = self.runner(text, scratch,
                                     relay_environment(self.env), self.settings["timeout"])
            results.append(f"{name}: {outcome(result)}")
        entry["lastResult"] = ", ".join(results)
        self.say(f"handoff {key}: relay attempt {attempt}/{self.settings['maxAttempts']} ({entry['lastResult']})")
        self.save(state, dry_run)
        return 0

    def give_up(self, item: Dict[str, Any], entry: Dict[str, Any], why: str, state: Dict[str, Any],
                dry_run: bool) -> None:
        """Record the give-up here, and exactly once on GitHub. The intent is on disk before
        the post; while it is unconfirmed, a later pass first reads the handoff's issue for
        a give-up record (or an ack) that an earlier, interrupted post may have left."""
        if dry_run:
            self.say(f"handoff {item['id']}: would give up ({why})")
            return
        resuming = bool(entry.get("givingUp"))
        entry.setdefault("givingUp", snapshot.iso(self.now))
        entry.setdefault("gaveUpReason", why)
        self.save(state, False)
        if not isinstance(item.get("on"), int):
            self.confirm(entry, None, "no issue to post on")
            return
        if resuming:
            if self.read is None:
                raise RuntimeError("an earlier give-up post may have landed and nothing can read the issue to check")
            records = self.read(item["on"], item.get("at") or entry["givingUp"])
            if handoff.acks(records).get(item["id"]):
                entry.pop("givingUp", None)
                entry["acknowledged"] = snapshot.iso(self.now)
                self.say(f"handoff {item['id']}: taken; done")
                self.save(state, False)
                return
            earlier = next((r for r in records if (r.get("payload") or {}).get("relayGaveUp") == item["id"]), None)
            if earlier is not None:
                self.confirm(entry, earlier.get("url"), "found the earlier give-up record")
                self.save(state, False)
                return
        if entry.get("givingUpPosts", 0) >= GIVE_UP_POST_LIMIT:
            self.confirm(entry, None, f"the give-up record could not be posted in {GIVE_UP_POST_LIMIT} tries; "
                                      "recorded on this machine only")
            self.save(state, False)
            return
        entry["givingUpPosts"] = entry.get("givingUpPosts", 0) + 1
        self.save(state, False)
        payload = {"kind": "msg", "v": 1, "agent": self.me, "to": [item["from"]], "relayGaveUp": item["id"]}
        if item.get("addressedOn") is not None:
            payload["on"] = item["on"]
        text = (f"**Relay gave up** on handoff {item['url']} after {entry.get('attempts', 0)} attempt(s). "
                "It is still in the recipient's inbox.")
        posted = self.post(item["on"], text, payload)
        self.confirm(entry, (posted or {}).get("html_url"), "posted the give-up record")
        self.save(state, False)

    def confirm(self, entry: Dict[str, Any], url: Optional[str], how: str) -> None:
        entry.pop("givingUp", None)
        entry["gaveUp"] = snapshot.iso(self.now)
        entry["gaveUpRecord"] = url
        self.say(f"gave up ({entry.get('gaveUpReason')}); {how}; the handoff stays in the recipient's inbox")

    def prune(self, entries: Dict[str, Dict[str, Any]], on_board: set) -> None:
        """Forget finished handoffs that have left the board's window."""
        for key in [k for k, e in entries.items() if k not in on_board and (e.get("gaveUp") or e.get("acknowledged"))]:
            del entries[key]

    def save(self, state: Dict[str, Any], dry_run: bool) -> None:
        if not dry_run:
            save_state(self.dir, state)

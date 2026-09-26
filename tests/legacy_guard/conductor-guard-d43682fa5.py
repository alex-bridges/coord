#!/usr/bin/env python3
"""PreToolUse(Bash|Edit|Write|MultiEdit|NotebookEdit): what an unattended conductor run may never do.

The conductor (Tools/coord/coordlib/conductor.py) runs `claude -p --agent conductor` with
no one watching, on Alex's own GitHub token (its `coord` commands may use the GitHub App,
#1035; this hook blocks the sanctioned routes to the app's token or key from a shell), and
its prompt quotes GitHub comments that anyone could have written. Its standing orders forbid moving main by hand, approving for
Alex and changing its own controls, and each run also gets a permissions.deny list. A
substitute review of #1043 showed that deny rules are prefix patterns: a merge through
`gh api -X PUT …/pulls/N/merge`, `git push origin --force …` and
`./Tools/coord/coord train approve` all got past them. This hook looks at the whole command.

Rules apply to what the shell will run: the command with its quoted strings removed,
plus the quoted scripts of `bash -c` / `sh -c` / `eval`. So a commit message or a comment
that talks about `train approve` is fine, and `bash -c 'gh pr merge 5'` is not. The
conductor's own controls (train.ALEX_ONLY_PATHS in its checkout, and the user's Claude
settings and hooks) may be read but not written.

The GitHub App's rules read the raw command instead, quotes, pipes and heredocs included:
the token cache path, the Keychain service name, and python together with apptoken
anywhere in one command (after NFKC normalisation and case folding). So prose that names
python and apptoken is refused too, in a commit message, a PR body or a `coord send`, and
so is every shell way to write it to a file. Write such text with the Write tool (for
example under $CONDUCTOR_STATE_DIR), which this hook checks only for its path, and pass the
file (`--body-file` / `-F`, or `"$(cat FILE)"` for `coord send`).

It is active only for the conductor: when COORD_AGENT starts with `conductor@` (the
watcher sets it; an interactive conductor session should too) or CONDUCTOR_WAKE=1. Every
other session exits at once. When active it fails CLOSED: anything it cannot read or
decide is refused (exit 2), and so is anything it cannot decide within DEADLINE_SECONDS
(the hook's own timeout would let the call through).

Exit 2 blocks the call and shows the reason to the model. It is a tripwire, not a
sandbox. A run determined to get round it can still write a script that does the same.
The guarantee that main moves only through the train is a ruleset on main; see
Docs/agent-coordination.md, "The conductor".
"""

import json
import os
import re
import select
import signal
import sys
import time
import unicodedata

# Kept equal to train.ALEX_ONLY_PATHS (a test checks). Not imported: the guard must not
# run code from the tree it guards.
CONTROL_PATHS = (".claude/agents/", ".claude/settings.json", ".claude/hooks/", "Tools/coord/", "CLAUDE.md")
HOME_CONTROLS = ("settings.json", "settings.local.json", "hooks/", "agents/")  # under ~/.claude/

_API_WRITE = r"(?=[^\n;&|]*(-X\s*(POST|PATCH|PUT|DELETE)\b|--method\s+(POST|PATCH|PUT|DELETE)\b|\s-[fF]\s|--(raw-)?field\b|--input\b))"
_START = r"(^|[;&|(`]\s*|\b(exec|xargs|env|nohup|time)\s+)"
_REPO_FLAG = r"(--repo(=|\s+)\S+\s+)?"  # coord's one top-level option, which may come before the command
# A program named at a command's start or by a path: `coord`, `./Tools/coord/coord`. The same
# matches as `(^|[\s;&|(])(\S*/)?coord` (a path always starts after a space or the start),
# without rescanning the rest of the word from every `(`, which was quadratic.
_AT_START_OR_PATH = r"(^|(?<=[\s;&|(/]))"

# Checked against the whole command, quoted strings included: a path or a Python script is
# usually quoted. So prose that names them is blocked too; say it another way.
RAW_RULES = [
    (re.compile(r"Library/Caches/inisa-coord"), "the GitHub App's token cache (Library/Caches/inisa-coord)"),
    # The Keychain item's service name: catches a quoted `security` call or one made from Python.
    (re.compile(r"example-coord-github-app"), "the GitHub App's private key (Keychain item example-coord-github-app)"),
]
# The app's token code run from Python: a python token and "apptoken" anywhere in the raw
# command, however they are quoted, piped or fed. Nothing is parsed: two rounds of review
# found that a parser for pipelines and heredocs failed open. Both are matched on the NFKC
# form of the command, case-folded: `Python3` runs python3 on macOS's case-insensitive
# filesystem, Python reads identifiers in NFKC (`import coordlib.\uff41pptoken`), and
# PYTHONCASEOK=1 imports `coordlib.APPTOKEN`. The one exemption keeps the test runner
# usable: the test module's name, `test_apptoken`, when no word character comes before it.
_PYTHON = re.compile(r"\bpython[0-9.]*\b|/python")
_TEST_MODULE = re.compile(r"(?<!\w)test_apptoken")
APPTOKEN_WHY = ("python and `apptoken` in one command (the GitHub App's token code, coordlib.apptoken). The whole "
                "command is read, prose included, so every shell way to write such text is refused too: write it "
                "with the Write tool (for example under $CONDUCTOR_STATE_DIR) and pass the file (--body-file / -F, "
                "or \"$(cat FILE)\" for coord send)")
# A command this long is refused unread: _QUOTED rescans to the end for each unclosed quote,
# and a slow check is worse than a refusal (the hook's timeout lets the call through).
MAX_COMMAND = 20000

# A slow check fails open (the hook's timeout lets the call through), so main() refuses when
# the verdict is not ready in time. The hook's timeout is 10 s.
DEADLINE_SECONDS = 7
SLOW = (f"the guard could not decide within {DEADLINE_SECONDS} s, and a guard that cannot decide refuses; split the "
        "command, or write long text with the Write tool and pass the file")


class _InOrder:
    """Patterns that must match in order within one segment (the text between newlines, `;`,
    `&` and `|`), each after the one before. Checked step by step this is linear; as one
    regex the push rule was cubic in the number of `git push` repeats."""

    def __init__(self, *patterns):
        self.steps = [re.compile(pattern) for pattern in patterns]
        self.pattern = " ".join(pattern for pattern in patterns)

    def search(self, text):
        for segment in re.split(r"[\n;&|]", text):
            position = 0
            for step in self.steps:
                match = step.search(segment, position)
                if not match:
                    break
                position = match.end()
            else:
                return True
        return False


RULES = [
    # Moving main or rewriting history by hand. The train moves main. A word ends at a space,
    # a `)` or a backtick, or at the end of its segment: `git push origin main;echo ok`.
    (_InOrder(r"\bgit\b", r"\bpush\b",
              r"--force|--mirror|\s-[A-Za-z]*f\b|\s\+\S|:main\b|:refs/heads/main\b|\s(refs/heads/)?main(?=[\s)`]|$)"),
     "force-pushing, or pushing to main by hand (main moves only through the merge train)"),
    (r"\bgh\s+pr\b[^\n;&|]*\bmerge\b", "merging a PR by hand (queue it with `coord train add`)"),
    (r"\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*(pulls/\d+/merge\b|/merges\b|/git/refs?\b|/protection\b|/rulesets\b)",
     "merging or moving refs, protection or rulesets through the API"),
    (_AT_START_OR_PATH + r"coord\s+" + _REPO_FLAG + r"train\s+approve\b",
     "`coord train approve` (only Alex approves, in person)"),
    # The GitHub App (#1035). A conductor run never needs its token or key in a shell: its
    # tick runs on Alex's identity and its coord commands mint their own. These block the
    # sanctioned routes (`coord token exec`, the Keychain item, the token cache, apptoken
    # itself); like the rest of this hook they are a tripwire for a prompt-injected run,
    # not a sandbox, and a quoting trick can get round them.
    (_AT_START_OR_PATH + r"coord\s+" + _REPO_FLAG + r"token\s+exec\b",
     "`coord token exec` (it hands out a GitHub App token)"),
    (r"(^|(?<=[\s;&|(`/]))security\s+[^\n;&|]*\b(find-generic-password|find-internet-password|dump-keychain"
     r"|export)\b", "reading secrets from the Keychain (the GitHub App's private key is there)"),
    (r"\bgh\s+(variable|secret)\b|\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*actions/(variables|secrets)\b",
     "repository variables and secrets"),
    (r"\bgh\s+(repo\s+(delete|edit|rename|archive)|workflow\s+disable|auth\s+token)\b",
     "changing the repository, disabling workflows, or reading the GitHub token"),
    (_START + r"(sudo|launchctl)\b", "launchd and sudo"),
]
RULES = [(re.compile(rule) if isinstance(rule, str) else rule, why) for rule, why in RULES]

# Commands that write files. A protected path in a command WITHOUT one of these is a read.
# Writer programs count only where a command starts (`install.sh` is a file, not `install`).
_WRITES = re.compile(r">|" + _START + r"((sed|perl)\s+-[A-Za-z]*i|(tee|rm|mv|cp|ln|chmod|chown|truncate|install|dd|patch"
                     r"|rsync)\b)|\bgit\s+(-C\s+\S+\s+)?(checkout|restore|apply|am|stash|reset|clean|mv|rm)\b")
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|\d?>\s*/dev/null")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SCRIPT = re.compile(r"\b(?:(?:ba|z|da)?sh\s+-[A-Za-z]*c|eval)\s+('[^']*'|\"(?:\\.|[^\"\\])*\")")
_CD = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(&&|;)")
SELF = "the conductor's own controls (its hooks, settings, orders, Tools/coord or CLAUDE.md in its checkout; change them by PR in a worktree)"


def active(env) -> bool:
    return env.get("COORD_AGENT", "").startswith("conductor@") or env.get("CONDUCTOR_WAKE") == "1"


def executable_text(command: str) -> str:
    """The command without quoted strings, plus the quoted scripts it runs."""
    scripts = [m.group(1)[1:-1] for m in _SCRIPT.finditer(command)]
    return "\n".join([_QUOTED.sub("''", command)] + [executable_text(s) for s in scripts])


def protected(path: str, project: str) -> bool:
    home_claude = os.path.join(os.path.realpath(os.path.expanduser("~")), ".claude") + "/"
    if path.startswith(home_claude):
        return any(path[len(home_claude):].startswith(p) for p in HOME_CONTROLS)
    if project:
        return any(path == os.path.join(project, p.rstrip("/")) or path.startswith(os.path.join(project, p))
                   for p in CONTROL_PATHS)
    return False


def _python_apptoken(command: str) -> bool:
    """A python token and `apptoken` anywhere in the command's NFKC case-folded form, the test
    module's name aside."""
    folded = unicodedata.normalize("NFKC", command).casefold()
    return bool(_PYTHON.search(folded)) and "apptoken" in _TEST_MODULE.sub("", folded)


def bash_verdict(command: str, cwd: str, project: str) -> str:
    if len(command) > MAX_COMMAND:
        return (f"a command longer than {MAX_COMMAND} characters (too long to check; write it to a file and pass "
                "the file, e.g. --body-file / -F, or split the command)")
    text = executable_text(command)
    for rule, why in RULES:
        if rule.search(text):
            return why
    for pattern, why in RAW_RULES:
        if pattern.search(command):
            return why
    if _python_apptoken(command):
        return APPTOKEN_WHY
    if not _WRITES.search(_HARMLESS_REDIRECT.sub(" ", text)):
        return ""
    match = _CD.match(command)
    if match:
        cwd = os.path.realpath(os.path.join(cwd, os.path.expanduser(match.group(1).strip("'\""))))
    home = os.path.realpath(os.path.expanduser("~"))
    for segment in re.split(r"&&|\|\||[;|\n]", command.replace("$HOME", home)):
        # The program a segment runs is not a write target: `Tools/coord/coord inbox > out` is fine.
        for token in re.findall(r"[^\s'\"=<>()]+", segment)[1:]:
            if protected(os.path.realpath(os.path.join(cwd, os.path.expanduser(token))), project):
                return SELF
    return ""


def verdict(payload: dict, env) -> str:
    """The reason to block, or "" to allow."""
    tool = payload.get("tool_name") or ""
    data = payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())
    project = os.path.realpath(env.get("CLAUDE_PROJECT_DIR") or cwd)
    if tool == "Bash":
        return bash_verdict(str(data.get("command") or ""), cwd, project)
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = str(data.get("file_path") or data.get("notebook_path") or "")
        if path and protected(os.path.realpath(os.path.join(cwd, os.path.expanduser(path))), project):
            return SELF
    return ""


class _Undecided(Exception):
    """verdict() raised in the child; the argument is the exception's class name."""


def decide(payload: dict, env) -> str:
    """verdict(), computed in a forked child that the parent waits DEADLINE_SECONDS for. A slow
    regex holds the GIL and checks no signals, so neither a thread nor an alarm can stop it,
    and the hook's own timeout would let the call through: a late verdict is SLOW, a refusal.
    Without fork (or a pipe), it decides inline, as before."""
    try:
        read_end, write_end = os.pipe()
    except OSError:
        return verdict(payload, env)
    try:
        child = os.fork()
    except OSError:
        os.close(read_end)
        os.close(write_end)
        return verdict(payload, env)
    if child == 0:
        status = 1
        try:
            os.close(read_end)
            try:
                result = {"why": verdict(payload, env)}
            except Exception as error:
                result = {"error": error.__class__.__name__}
            data = json.dumps(result).encode()
            while data:
                data = data[os.write(write_end, data):]
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    chunks, deadline = [], time.monotonic() + DEADLINE_SECONDS
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([read_end], [], [], left)[0]:
                os.kill(child, signal.SIGKILL)
                return SLOW
            chunk = os.read(read_end, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_end)
        os.waitpid(child, 0)
    result = json.loads(b"".join(chunks) or b"{}")
    if "why" not in result:
        raise _Undecided(result.get("error", "the check exited without a verdict"))
    return result["why"]


def main() -> int:
    env = os.environ
    if not active(env):
        return 0
    try:
        payload = json.load(sys.stdin)
        why = decide(payload if isinstance(payload, dict) else {}, env)
    except Exception as error:  # fail closed: a guard that cannot decide refuses
        name = error.args[0] if isinstance(error, _Undecided) else error.__class__.__name__
        print(f"conductor-guard: could not check this call ({name}); refusing", file=sys.stderr)
        return 2
    if why:
        print(f"conductor-guard: blocked: {why}. This is enforced for conductor runs; if it is really needed, "
              "ask Alex with `Tools/coord/coord conductor escalate \"…\"`.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

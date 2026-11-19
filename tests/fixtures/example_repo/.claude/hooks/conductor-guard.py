#!/usr/bin/env python3
"""PreToolUse(Bash|Edit|Write|MultiEdit|NotebookEdit): what an unattended conductor run may never do.

The conductor (Tools/coord/coordlib/conductor.py) runs `claude -p --agent conductor` with
no one watching, on Alex's own GitHub token, and its prompt quotes GitHub comments that
anyone could have written. Its standing orders forbid moving main by hand, approving for
Alex and changing its own controls, and each run also gets a permissions.deny list. A
substitute review of #1043 showed that deny rules are prefix patterns: a merge through
`gh api -X PUT …/pulls/N/merge`, `git push origin --force …` and
`./Tools/coord/coord train approve` all got past them. This hook looks at the whole command.

Rules apply to what the shell will run: the command with its quoted strings removed,
plus the quoted scripts of `bash -c` / `sh -c` / `eval`. So a commit message or a comment
that talks about `train approve` is fine, and `bash -c 'gh pr merge 5'` is not. The
conductor's own controls (train.ALEX_ONLY_PATHS in its checkout, and the user's Claude
settings and hooks) may be read but not written.

The GitHub App (#1035) adds rules beside these, matched on the raw command, quoted strings
included: `coord token exec` (and `coord train approve` with coord's options in it), reading
the Keychain with `security`, the app's token cache and Keychain item, and python together with `apptoken`
anywhere in one command. So prose that names them is refused too, and so is every shell way
to write it to a file: write such text with the Write tool (for example under
$CONDUCTOR_STATE_DIR), which this hook checks only for its path, and pass the file
(`--body-file` / `-F`, or `"$(cat FILE)"` for `coord send`). Main's rules above are unchanged.

It is active only for the conductor: when COORD_AGENT starts with `conductor@` (the
watcher sets it; an interactive conductor session should too) or CONDUCTOR_WAKE=1. Every
other session exits at once. When active it fails CLOSED: anything it cannot read or
decide is refused (exit 2).
The verdict is computed in a forked child, and a verdict not ready within DEADLINE_SECONDS
is a refusal too: a slow rule (main's push rule is cubic in `git push` repeats) must not run
past the hook's timeout, which lets the call through.

Exit 2 blocks the call and shows the reason to the model. It is a blocking tripwire with
known limitations, not a sandbox: it matches a denylist, so spellings the shell accepts can
still get past it (logged on #1051), and a run determined to get round it can still write a
script that does the same. It does not make main move only through the train; what does and
does not stop main moving by hand is in Docs/agent-coordination.md, "The conductor".
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
CONTROL_PATHS += ("Tools/ci-runner/",)  # the laptop runner's on/off tool, which runs with Alex's token
HOME_CONTROLS = ("settings.json", "settings.local.json", "hooks/", "agents/")  # under ~/.claude/

_API_WRITE = r"(?=[^\n;&|]*(-X\s*(POST|PATCH|PUT|DELETE)\b|--method\s+(POST|PATCH|PUT|DELETE)\b|\s-[fF]\s|--(raw-)?field\b|--input\b))"
_START = r"(^|[;&|(`]\s*|\b(exec|xargs|env|nohup|time)\s+)"

RULES = [
    # Moving main or rewriting history by hand. The train moves main.
    (r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(--force|--mirror|(^|\s)-[A-Za-z]*f\b|\s\+\S|:main\b|:refs/heads/main\b"
     r"|\smain(\s|$)|\srefs/heads/main(\s|$))",
     "force-pushing, or pushing to main by hand (main moves only through the merge train)"),
    (r"\bgh\s+pr\b[^\n;&|]*\bmerge\b", "merging a PR by hand (queue it with `coord train add`)"),
    (r"\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*(pulls/\d+/merge\b|/merges\b|/git/refs?\b|/protection\b|/rulesets\b)",
     "merging or moving refs, protection or rulesets through the API"),
    (r"(^|[\s;&|(])(\S*/)?coord\s+train\s+approve\b", "`coord train approve` (only Alex approves, in person)"),
    (r"\bgh\s+(variable|secret)\b|\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*actions/(variables|secrets)\b",
     "repository variables and secrets"),
    (r"\bgh\s+(repo\s+(delete|edit|rename|archive)|workflow\s+disable|auth\s+token)\b",
     "changing the repository, disabling workflows, or reading the GitHub token"),
    (_START + r"(sudo|launchctl)\b", "launchd and sudo"),
]

# The GitHub App's rules (#1035), added beside main's and matched on the RAW command, quoted
# strings included, so a quoted script (`sh -c '…'`) is caught, and so is prose that names
# them. Like the rest of this hook they are a tripwire for a prompt-injected run, not a
# sandbox. A program counts where a word starts, after a quote, or at the end of a path; the
# lookbehind reads each position once (`(\S*/)?` rescanned the word from every `(`).
_PROGRAM_START = r"(^|(?<=[\s;&|(`/'\"]))"
# `coord token exec` and `coord train approve` (the second beside main's rule, which takes the
# form without options): coord's words, with any run of option words before each subcommand
# word, each option perhaps taking the next word as its value. argparse takes `--repo X`,
# `--repo=X`, an abbreviation (`--rep X`, `--re=X`, `--r X`), a repeated or empty `--repo`, and,
# from Python 3.12, a bare `--` (`coord -- token exec`, `coord token -- exec`). Checked in code,
# in one pass: the same pattern as a regex backtracks without bound on `coord -x train -x …`.
_COORD_WORD = re.compile(r"[^\s;&|]+|[;&|]")
TOKEN_EXEC_WHY = "`coord token exec` (it hands out a GitHub App token)"
APPROVE_WHY = "`coord train approve` (only Alex approves, in person)"
# The shell's quoting is not decoded: argparse runs `coord 'token' exec`, `coord t''oken exec`
# and `coord tok\en exec` as `token exec`, and no real command quotes its subcommand, so a
# subcommand word spelled with any of these is refused instead (it fails closed).
_SHELL_QUOTING = frozenset("'\"\\$")
_COORD_LINE_WORD = re.compile(r"[^\s;&|]+|[;&|\n]")
QUOTED_SUBCOMMAND_WHY = "a coord subcommand spelled with quotes or escapes"
RAW_RULES = [
    # The Keychain item's service name: catches a quoted `security` call or one made from Python.
    (re.compile(r"example-coord-github-app", re.IGNORECASE),
     "the GitHub App's private key (Keychain item example-coord-github-app)"),
]
# `security` runs the FIRST command in its table that its command word is a prefix of: `f`,
# `find` and `find-` are find-generic-password, `find-i` is find-internet-password (it comes
# before find-identity), `du` and `dump` are dump-keychain, `e` and `expor` are export. So the
# command word is refused when it is a non-empty prefix of a command that reads secrets (or
# starts with `export`), and so is interactive mode (-i, or -p PROMPT), which reads commands
# from stdin. The command word is the first word after `security` that is not an option, with
# redirections skipped and quotes removed. The shell's escapes are not decoded: a word before
# or at the command word with `$` or a backslash is refused (`$'\x64ump-keychain'` is
# dump-keychain), and so is a command word the shell builds (a glob, a brace, a backtick).
_SECURITY = re.compile(_PROGRAM_START + r"security(?![^\s;&|()<>`])")
_SECURITY_SECRETS = ("find-generic-password", "find-internet-password", "find-key", "dump-keychain", "export")
_SECURITY_TOKEN = re.compile(r"(?:[ \t]|\\\n)*(?:(\d*(?:&>>?|>>|>[|&]|<>|<<<|<<-?|<&|[<>])(?:[ \t]|\\\n)*[^\s;&|()<>`]*)"
                             r"|([^\s;&|()<>`]+)|(`)|)")
SECURITY_WHY = "reading secrets from the Keychain (the GitHub App's private key is there)"
# A `security` the shell has to assemble ("security", secu\rity, s"ec"urity, sec$'u'rity): with
# every quote, backslash and `$` deleted, the command names `security` more often than it does
# as written. Refused whatever follows; the shell's quoting is not decoded.
_SHELL_QUOTING_CHARACTERS = str.maketrans("", "", "'\"\\$")
ASSEMBLED_SECURITY_WHY = ("`security` spelled with quotes or escapes (the Keychain holds the GitHub App's private key). "
                          "The whole command is read, prose included: write such text with the Write tool (for example "
                          "under $CONDUCTOR_STATE_DIR) and pass the file (--body-file / -F, or \"$(cat FILE)\" for "
                          "coord send)")
# The app's token code run from Python: a python token and "apptoken" anywhere in the raw
# command, however they are quoted, piped or fed. Nothing is parsed. Both are matched on the
# NFKC form of the command, case-folded: `Python3` runs python3 on macOS, Python reads
# identifiers in NFKC (`import coordlib.ａpptoken`), and PYTHONCASEOK=1 imports
# `coordlib.APPTOKEN`. The one exemption keeps the test runner usable: the test module's name,
# `test_apptoken`, when no word character comes before it.
_PYTHON = re.compile(r"\bpython[0-9.]*\b|/python")
_TEST_MODULE = re.compile(r"(?<!\w)test_apptoken")
# The app's token cache (~/Library/Caches/example-coord): `example-coord` and `cache` anywhere in
# the case-folded command. A path has too many spellings to match (`Library//Caches`, `./`,
# `cd` then a relative path, other letter case), so nothing is parsed, as for apptoken. The
# repo's own name is not enough (`/Users/dev/Projects/example-repo`, `--repo example-org/example-repo`
# beside `--cached` or `gh cache`): a glob such as `example*` is the accepted cost (#1051).
TOKEN_CACHE_WHY = ("`example-coord` and `cache` in one command (the GitHub App's token cache is "
                   "~/Library/Caches/example-coord). "
                   "The whole command is read, prose included: write such text with the Write tool (for example under "
                   "$CONDUCTOR_STATE_DIR) and pass the file (--body-file / -F, or \"$(cat FILE)\" for coord send)")
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
NO_CHECKER = "the guard could not start its checker ({}), and a guard that cannot decide refuses"

# Commands that write files. A protected path in a command WITHOUT one of these is a read.
# Writer programs count only where a command starts (`install.sh` is a file, not `install`).
_WRITES = re.compile(r">|" + _START + r"((sed|perl)\s+-[A-Za-z]*i|(tee|rm|mv|cp|ln|chmod|chown|truncate|install|dd|patch"
                     r"|rsync)\b)|\bgit\s+(-C\s+\S+\s+)?(checkout|restore|apply|am|stash|reset|clean|mv|rm)\b")
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|\d?>\s*/dev/null")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SCRIPT = re.compile(r"\b(?:(?:ba|z|da)?sh\s+-[A-Za-z]*c|eval)\s+('[^']*'|\"(?:\\.|[^\"\\])*\")")
_CD = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(&&|;)")
SELF = "the conductor's own controls (its hooks, settings, orders, Tools/coord or CLAUDE.md in its checkout; change them by PR in a worktree)"


# A review run (Tools/coord/coordlib/reviewer.py, COORD_AGENT=reviewer@…) reads, builds and
# tests; it never writes to GitHub or pushes. Its gh and git carry no credential anyway
# (reviewer.isolated_env); these name the attempt instead of letting it fail obscurely.
REVIEWER_RULES = [
    # `push` as git's subcommand only: `git log --grep=push` and `git grep push` are reads.
    (r"\bgit(\s+-[Cc]\s+\S+|\s+--?[\w-]+(=\S+)?)*\s+push\b", "pushing (a review run is read-only)"),
    (r"\bgh\s+(pr|issue)\s+(comment|review|edit|close|reopen|merge|create|ready|lock|develop)\b",
     "commenting on, reviewing or changing a PR or issue (the watcher posts the review)"),
    (r"\bgh\s+api\b[^\n;&|]*(--input\b|--(raw-)?field\b|\s-[fF]\s|-X\s*(POST|PATCH|PUT|DELETE)\b"
     r"|--method[=\s]+(POST|PATCH|PUT|DELETE)\b)",
     "a writing GitHub API call (a review run is read-only)"),
    (r"\bgh\s+(label|release|workflow\s+run|run\s+(rerun|cancel)|auth\s+(login|setup-git|refresh))\b",
     "changing GitHub state or logging in (a review run is read-only)"),
    (r"(^|[\s;&|(])(\S*/)?coord\s+(send|claim|release|train\s+(?!status\b)\S+|outage|conductor|ci|token)\b",
     "coord commands that write (a review run is read-only)"),
]
# Checked on the raw command, quotes included: coord's own code driven from a script.
REVIEWER_RAW = [
    (re.compile(r"coordlib\.cli|from\s+coordlib\s+import\s+cli|coordlib\.(conductor|reviewer|train_step)"),
     "running coord's code from a script (a review run is read-only)"),
]


def reviewer(env) -> bool:
    return env.get("COORD_AGENT", "").startswith("reviewer@")


def reviewer_verdict(command: str, env) -> str:
    """Why a review run may not run `command`, or "" (and always "" outside a review run)."""
    if not reviewer(env):
        return ""
    text = executable_text(command)
    for pattern, why in REVIEWER_RULES:
        if re.search(pattern, text):
            return why
    for pattern, why in REVIEWER_RAW:
        if pattern.search(command):
            return why
    return ""


READ_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")


def reviewer_read_verdict(tool: str, data: dict, cwd: str, project: str, env) -> str:
    """A review run's own reads (Read, Grep, Glob, LS; the sandbox covers only its Bash)
    reach nothing under the home folder but the reviews directory and the conductor's
    checkout: no ~/.aws, ~/.npmrc or ~/.netrc to copy into a posted review (Codex on #1049).
    Paths outside the home folder (the toolchains) stay readable."""
    if not reviewer(env) or tool not in READ_TOOLS:
        return ""
    home = os.path.realpath(os.path.expanduser("~"))
    state = env.get("CONDUCTOR_STATE_DIR") or ""
    allowed = [project] + ([os.path.realpath(os.path.join(os.path.expanduser(state), "reviews"))] if state else [])
    paths = [str(data.get(k)) for k in ("file_path", "path", "notebook_path") if data.get(k)] or [cwd]
    pattern = str(data.get("pattern") or "")
    if tool == "Glob" and (pattern.startswith("/") or pattern.startswith("~")):
        paths.append(pattern.split("*", 1)[0] or "/")
    for path in paths:
        real = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        inside = lambda root: real == root or real.startswith(root + os.sep)
        if inside(home) and not any(inside(root) for root in allowed):
            return "reading under the home folder outside the PR's tree, scratch and the conductor's checkout"
        if any(inside(home) and not inside(root) and root.startswith(real + os.sep) for root in allowed) \
                and tool in ("Grep", "Glob", "LS"):
            # A search from a parent of the allowed roots (~ itself, ~/.local) would list or
            # read beside them.
            return "searching the home folder outside the PR's tree, scratch and the conductor's checkout"
    return ""


def reviewer_write_verdict(path: str, cwd: str, env) -> str:
    """A review run edits files only in its throwaway tree and scratch directory, both under
    the conductor state's `reviews` directory; never the conductor's checkout (Codex on #1049)."""
    if not reviewer(env):
        return ""
    state = env.get("CONDUCTOR_STATE_DIR") or ""
    if not path or not state:
        return "writing outside the review's scratch directory and the PR's throwaway tree"
    root = os.path.realpath(os.path.join(os.path.expanduser(state), "reviews")) + os.sep
    if not os.path.realpath(os.path.join(cwd, os.path.expanduser(path))).startswith(root):
        return "writing outside the review's scratch directory and the PR's throwaway tree"
    return ""


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


def _names_the_token_cache(command: str) -> bool:
    """`example-coord` and `cache` anywhere in the case-folded command (see TOKEN_CACHE_WHY)."""
    folded = command.casefold()
    return "example-coord" in folded and "cache" in folded


def _coord_runs(command: str, first: str, second: str, with_option: bool) -> bool:
    """Whether a `coord` call runs `coord … first … second` (with at least one option word, if
    with_option). One pass over the words that follows every reading at once: a reading is
    (subcommand words seen, the last word was an option, an option word was seen)."""
    readings = set()
    for match in _COORD_WORD.finditer(command):
        word = match.group(0)
        if word in (";", "&", "|"):
            readings = set()
            continue
        option = word.startswith("-")
        after = set()
        for seen, after_option, any_option in readings:
            if option:
                after.add((seen, True, True))
                continue
            if seen == 0 and word == first:
                after.add((1, False, any_option))
            elif seen == 1 and re.match(second + r"\b", word) and (any_option or not with_option):
                return True
            if after_option:
                after.add((seen, False, any_option))  # the word is the option's value
        if _is_coord(word):
            after.add((0, False, False))
        readings = after
    return False


def _is_coord(word: str) -> bool:
    """`coord` as a program: the whole word, or after a path, a quote, `(` or a backtick."""
    return word == "coord" or (len(word) > 5 and word.endswith("coord") and word[-6] in "/(`'\"")


def _coord_subcommand_quoted(command: str) -> bool:
    """A `coord` call whose subcommand word, or (after `token` or `train`) its second one, is
    spelled with quotes, a backslash or `$`. Option words and their values are passed over as in
    _coord_runs; a word that may be an option's value is checked too (it may be the subcommand).
    The quotes in a later argument (`coord send 'a message'`) are not a subcommand's."""
    readings = set()  # (subcommand words seen, the last word was an option)
    for match in _COORD_LINE_WORD.finditer(command):
        word = match.group(0)
        if word in (";", "&", "|", "\n"):  # a newline ends the command (a continuation's `\` is refused)
            readings = set()
            continue
        option = word.startswith("-")
        after = set()
        for seen, after_option in readings:
            if option:
                after.add((seen, True))
                continue
            if _SHELL_QUOTING & set(word):
                return True
            if seen == 0 and word in ("token", "train"):
                after.add((1, False))
            if after_option:
                after.add((seen, False))  # the word is the option's value
        if _is_coord(word):
            after.add((0, False))
        readings = after
    return False


def _security_reads_secrets(command: str) -> bool:
    """A `security` call whose command word reads secrets, or that runs interactively. A later
    `security` inside words already read (an option, a redirection's target) is not a program."""
    read_to = 0
    for match in _SECURITY.finditer(command):
        if match.start() < read_to:
            continue
        position, options_done = match.end(), False
        while True:
            token = _SECURITY_TOKEN.match(command, position)
            redirection, word, backtick = token.groups()
            if backtick:
                return True  # the command word comes from a command substitution
            if not word:
                if redirection is not None:
                    position = token.end()
                    continue
                break  # the end of the command: no command word, `security` prints its usage
            position = token.end()
            if set(word) & {"$", "\\"}:
                return True  # `$'\x66ind-…'`, `$X`, `\f`: decoded or built by the shell, never here
            word = re.sub(r"['\"]", "", word)
            if not options_done and word.startswith("-"):
                if word == "--":
                    options_done = True
                elif set(word[1:]) & {"i", "p"}:
                    return True  # -i, or -p PROMPT: commands from stdin
                continue
            if set(word) & set("*?[{"):
                return True  # built at run time (a glob or a brace)
            if word and (word.startswith("export") or any(name.startswith(word) for name in _SECURITY_SECRETS)):
                return True
            break
        read_to = position
    return False


def _security_assembled(command: str) -> bool:
    """`security` found more often once every quote, backslash and `$` is deleted."""
    def count(text: str) -> int:
        return sum(1 for _ in _SECURITY.finditer(text))
    return count(command.translate(_SHELL_QUOTING_CHARACTERS)) > count(command)


def bash_verdict(command: str, cwd: str, project: str) -> str:
    if len(command) > MAX_COMMAND:
        return (f"a command longer than {MAX_COMMAND} characters (too long to check; write long text with the Write "
                "tool and pass the file, e.g. --body-file / -F, or split the command)")
    text = executable_text(command)
    for pattern, why in RULES:
        if re.search(pattern, text):
            return why
    for pattern, why in RAW_RULES:
        if pattern.search(command):
            return why
    if _coord_runs(command, "token", "exec", False):
        return TOKEN_EXEC_WHY
    if _coord_runs(command, "train", "approve", True):
        return APPROVE_WHY
    if _coord_subcommand_quoted(command):
        return QUOTED_SUBCOMMAND_WHY
    if _security_reads_secrets(command):
        return SECURITY_WHY
    if _security_assembled(command):
        return ASSEMBLED_SECURITY_WHY
    if _python_apptoken(command):
        return APPTOKEN_WHY
    if _names_the_token_cache(command):
        return TOKEN_CACHE_WHY
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
    refusal = reviewer_read_verdict(tool, data, cwd, project, env)
    if refusal:
        return refusal
    if tool == "Bash":
        refusal = reviewer_verdict(str(data.get("command") or ""), env)
        if refusal:
            return refusal
        return bash_verdict(str(data.get("command") or ""), cwd, project)
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = str(data.get("file_path") or data.get("notebook_path") or "")
        refusal = reviewer_write_verdict(path, cwd, env)
        if refusal:
            return refusal
        if path and protected(os.path.realpath(os.path.join(cwd, os.path.expanduser(path))), project):
            return SELF
    return ""


def main() -> int:
    env = os.environ
    if not active(env):
        return 0
    try:
        payload = json.load(sys.stdin)
        why = verdict(payload if isinstance(payload, dict) else {}, env)
    except Exception as error:  # fail closed: a guard that cannot decide refuses
        print(f"conductor-guard: could not check this call ({error.__class__.__name__}); refusing", file=sys.stderr)
        return 2
    if why:
        print(f"conductor-guard: blocked: {why}. This is enforced for conductor runs; if it is really needed, "
              "ask Alex with `Tools/coord/coord conductor escalate \"…\"`.", file=sys.stderr)
        return 2
    return 0


class _Undecided(Exception):
    """verdict() raised in the child; the argument is the exception's class name."""


def decide(payload: dict, env) -> str:
    """verdict(), computed in a forked child that the parent waits DEADLINE_SECONDS for. A slow
    regex holds the GIL and checks no signals, so neither a thread nor an alarm can stop it,
    and the hook's own timeout would let the call through: a late verdict is SLOW, a refusal.
    Without a pipe or fork (an OSError, or AttributeError where os.fork does not exist, as on
    Windows) there is no deadline to keep, so it refuses too. The parent never waits for the
    child: _reap kills it if it is still running."""
    try:
        read_end, write_end = os.pipe()
    except (OSError, AttributeError) as error:
        return NO_CHECKER.format(error.__class__.__name__)
    try:
        child = os.fork()
    except (OSError, AttributeError) as error:
        os.close(read_end)
        os.close(write_end)
        return NO_CHECKER.format(error.__class__.__name__)
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
        _reap(child)
    result = json.loads(b"".join(chunks) or b"{}")
    if "why" not in result:
        raise _Undecided(result.get("error", "the check exited without a verdict"))
    return result["why"]


def _reap(child: int) -> None:
    """Collect the child without waiting: kill it first if it is still running. A blocking
    wait (a child stuck in uninterruptible I/O, or a parent whose read failed) could outlast
    the hook's timeout, which lets the call through."""
    try:
        if os.waitpid(child, os.WNOHANG) == (0, 0):
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, os.WNOHANG)
    except OSError:
        pass


# This main() replaces the one above, which is main's, kept line for line (a test checks that
# every line of main's guard is here, in order). The one difference: decide(), the watchdog,
# where main's calls verdict() inline.
def main() -> int:  # noqa: F811
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

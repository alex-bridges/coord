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

It is active only for the conductor: when COORD_AGENT starts with `conductor@` (the
watcher sets it; an interactive conductor session should too) or CONDUCTOR_WAKE=1. Every
other session exits at once. When active it fails CLOSED: anything it cannot read or
decide is refused (exit 2).

Exit 2 blocks the call and shows the reason to the model. It is a tripwire, not a
sandbox. A run determined to get round it can still write a script that does the same.
The guarantee that main moves only through the train is a ruleset on main; see
Docs/agent-coordination.md, "The conductor".
"""

import json
import os
import re
import sys

# Kept equal to train.ALEX_ONLY_PATHS (a test checks). Not imported: the guard must not
# run code from the tree it guards.
CONTROL_PATHS = (".claude/agents/", ".claude/settings.json", ".claude/hooks/", "Tools/coord/", "CLAUDE.md")
HOME_CONTROLS = ("settings.json", "settings.local.json", "hooks/", "agents/")  # under ~/.claude/

_API_WRITE = r"(?=[^\n;&|]*(-X\s*(POST|PATCH|PUT|DELETE)\b|--method\s+(POST|PATCH|PUT|DELETE)\b|\s-[fF]\s|--(raw-)?field\b|--input\b))"
_START = r"(^|[;&|(`]\s*|\b(exec|xargs|env|nohup|time)\s+)"
_REPO_FLAG = r"(--repo(=|\s+)\S+\s+)?"  # coord's one top-level option, which may come before the command

# Checked against the whole command, quoted strings included: a path or a Python script is
# usually quoted. So prose that names them is blocked too; say it another way.
RAW_RULES = [
    (re.compile(r"Library/Caches/example-coord"), "the GitHub App's token cache (Library/Caches/example-coord)"),
    # The Keychain item's service name: catches a quoted `security` call or one made from Python.
    (re.compile(r"example-coord-github-app"), "the GitHub App's private key (Keychain item example-coord-github-app)"),
]
# A Python script given on the command line or on stdin that uses the app's token code,
# both in the same command segment (a later `git log -- …/apptoken.py` is not one).
_PYTHON_INLINE = re.compile(r"\bpython[0-9.]*(\s+-[A-Za-z]+)*\s+-(c\b|\s|$)", re.M)
_APPTOKEN = re.compile(r"\bapptoken\b|\bAppTokens\b")
_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")

RULES = [
    # Moving main or rewriting history by hand. The train moves main.
    (r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(--force|--mirror|(^|\s)-[A-Za-z]*f\b|\s\+\S|:main\b|:refs/heads/main\b"
     r"|\smain(\s|$)|\srefs/heads/main(\s|$))",
     "force-pushing, or pushing to main by hand (main moves only through the merge train)"),
    (r"\bgh\s+pr\b[^\n;&|]*\bmerge\b", "merging a PR by hand (queue it with `coord train add`)"),
    (r"\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*(pulls/\d+/merge\b|/merges\b|/git/refs?\b|/protection\b|/rulesets\b)",
     "merging or moving refs, protection or rulesets through the API"),
    (r"(^|[\s;&|(])(\S*/)?coord\s+" + _REPO_FLAG + r"train\s+approve\b",
     "`coord train approve` (only Alex approves, in person)"),
    # The GitHub App (#1035). A conductor run never needs its token or key in a shell: its
    # tick runs on Alex's identity and its coord commands mint their own. These block the
    # sanctioned routes (`coord token exec`, the Keychain item, the token cache, apptoken
    # itself); like the rest of this hook they are a tripwire for a prompt-injected run,
    # not a sandbox, and a quoting trick can get round them.
    (r"(^|[\s;&|(])(\S*/)?coord\s+" + _REPO_FLAG + r"token\s+exec\b",
     "`coord token exec` (it hands out a GitHub App token)"),
    (r"(^|[\s;&|(`])(\S*/)?security\s+[^\n;&|]*\b(find-generic-password|find-internet-password|dump-keychain"
     r"|export)\b", "reading secrets from the Keychain (the GitHub App's private key is there)"),
    (r"\bgh\s+(variable|secret)\b|\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*actions/(variables|secrets)\b",
     "repository variables and secrets"),
    (r"\bgh\s+(repo\s+(delete|edit|rename|archive)|workflow\s+disable|auth\s+token)\b",
     "changing the repository, disabling workflows, or reading the GitHub token"),
    (_START + r"(sudo|launchctl)\b", "launchd and sudo"),
]

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
    """A Python one-liner that uses coordlib.apptoken, both in one command segment. A heredoc
    (`python3 - <<EOF` and its body on the lines after) is one segment with its script."""
    heredoc = re.search(r"\bpython[0-9.]*(\s+-[A-Za-z]+)*\s+-\s*<<", command)
    if heredoc and _APPTOKEN.search(command[heredoc.start():]):
        return True
    # Segment boundaries are found with quoted strings masked, so a `;` inside a quoted
    # script (`python3 -c 'import sys; import coordlib.apptoken'`) does not split it.
    masked = _QUOTED.sub(lambda m: m.group(0)[0] + "x" * (len(m.group(0)) - 2) + m.group(0)[-1], command)
    parts, start = [], 0
    for separator in _SEGMENTS.finditer(masked):
        parts.append(command[start:separator.start()])
        start = separator.end()
    parts.append(command[start:])
    return any(_PYTHON_INLINE.search(part) and _APPTOKEN.search(part) for part in parts)


def bash_verdict(command: str, cwd: str, project: str) -> str:
    text = executable_text(command)
    for pattern, why in RULES:
        if re.search(pattern, text):
            return why
    for pattern, why in RAW_RULES:
        if pattern.search(command):
            return why
    if _python_apptoken(command):
        return "running the GitHub App's token code (coordlib.apptoken) from a one-liner"
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


if __name__ == "__main__":
    sys.exit(main())

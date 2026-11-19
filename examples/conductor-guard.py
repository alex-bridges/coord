#!/usr/bin/env python3
"""Starter PreToolUse hook for unattended coord conductor and reviewer runs.

Copy to `.claude/hooks/conductor-guard.py`, `chmod +x`, and configure `conductor.guard_hook`
and `reviewer.guard_hook` in `.coord/config.json`.

Active ONLY when `COORD_AGENT` starts with `conductor@` or `reviewer@`, or `CONDUCTOR_WAKE=1`.
Every other Claude Code session exits 0 immediately. When active, it fails CLOSED (exit 2).

Note: this hook is a blocking tripwire, not a security sandbox. Enforce trunk protection on
GitHub itself with a branch ruleset allowing only the merge-train token to push.
"""

import json
import os
import re
import sys

# Paths the unattended conductor may read but never modify in its own checkout.
# Keep aligned with `owner_only_paths` in `.coord/config.json`.
CONTROL_PATHS = (".coord/", ".claude/agents/", ".claude/settings.json", ".claude/hooks/", "CLAUDE.md")
HOME_CONTROLS = ("settings.json", "settings.local.json", "hooks/", "agents/")

# Match your `.coord/config.json` values (defaults shown).
KEYCHAIN_SERVICE = os.environ.get("COORD_KEYCHAIN_SERVICE", "coord-github-app")
STATE_DIR_NAME = os.environ.get("COORD_STATE_DIR_NAME", "coord")

_API_WRITE = r"(?=[^\n;&|]*(-X\s*(POST|PATCH|PUT|DELETE)\b|--method\s+(POST|PATCH|PUT|DELETE)\b|\s-[fF]\s|--(raw-)?field\b|--input\b))"
_START = r"(^|[;&|(`]\s*|\b(exec|xargs|env|nohup|time)\s+)"
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SCRIPT = re.compile(r"\b(?:(?:ba|z|da)?sh\s+-[A-Za-z]*c|eval)\s+('[^']*'|\"(?:\\.|[^\"\\])*\")")
_WRITES = re.compile(
    r">|" + _START + r"((sed|perl)\s+-[A-Za-z]*i|(tee|rm|mv|cp|ln|chmod|chown|truncate|install|dd|patch|rsync)\b)"
    r"|\bgit\s+(-C\s+\S+\s+)?(checkout|restore|apply|am|stash|reset|clean|mv|rm)\b"
)
_HARMLESS_REDIRECT = re.compile(r"\d?>&\d|\d?>\s*/dev/null")

CONDUCTOR_RULES = [
    (r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(--force|--mirror|(^|\s)-[A-Za-z]*f\b|\s\+\S|:main\b|:refs/heads/main\b|\smain(\s|$)|\srefs/heads/main(\s|$))",
     "force-pushing or pushing to main directly (main moves through the merge train)"),
    (r"\bgh\s+pr\b[^\n;&|]*\bmerge\b", "merging a PR by hand (queue it with `coord train add`)"),
    (r"\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*(pulls/\d+/merge\b|/merges\b|/git/refs?\b|/protection\b|/rulesets\b)",
     "merging or modifying refs/branch protection via the API"),
    (r"(^|[\s;&|(])(\S*/)?coord\b[^\n;&|]*\btrain\s+approve\b", "`coord train approve` (owner approval only)"),
    (r"(^|[\s;&|(])(\S*/)?coord\b[^\n;&|]*\btoken\s+exec\b", "`coord token exec` (hands out a GitHub App token)"),
    (r"\bgh\s+(variable|secret)\b|\bgh\s+api\b" + _API_WRITE + r"[^\n;&|]*actions/(variables|secrets)\b",
     "modifying repository variables or secrets"),
    (r"\bgh\s+(repo\s+(delete|edit|rename|archive)|workflow\s+disable|auth\s+token)\b",
     "changing repository settings, disabling workflows, or reading GitHub tokens"),
    (_START + r"(sudo|launchctl)\b", "sudo or launchctl"),
    (_START + r"security\b[^\n;&|]*(find-|dump-keychain|export|-i\b|-p\b)", "reading secrets from macOS Keychain"),
]

REVIEWER_RULES = [
    (r"\bgit(\s+-[Cc]\s+\S+|\s+--?[\w-]+(=\S+)?)*\s+push\b", "git push (review runs are read-only)"),
    (r"\bgh\s+(pr|issue)\s+(comment|review|edit|close|reopen|merge|create|ready|lock|develop)\b",
     "mutating GitHub PRs or issues (the watcher posts the review)"),
    (r"\bgh\s+api\b" + _API_WRITE, "writing GitHub API call (review runs are read-only)"),
    (r"\bgh\s+(label|release|workflow\s+run|run\s+(rerun|cancel)|auth\s+(login|setup-git|refresh))\b",
     "mutating GitHub state (review runs are read-only)"),
    (r"(^|[\s;&|(])(\S*/)?coord\s+(send|claim|release|train\s+(?!status\b)\S+|outage|conductor|ci|token)\b",
     "writing coord command (review runs are read-only)"),
]


def is_reviewer(env) -> bool:
    return env.get("COORD_AGENT", "").startswith("reviewer@")


def is_active(env) -> bool:
    return (env.get("COORD_AGENT", "").startswith(("conductor@", "reviewer@"))
            or env.get("CONDUCTOR_WAKE") == "1")


def executable_text(command: str) -> str:
    scripts = [m.group(1)[1:-1] for m in _SCRIPT.finditer(command)]
    return "\n".join([_QUOTED.sub("''", command)] + [executable_text(s) for s in scripts])


def is_protected(path: str, project: str) -> bool:
    home_claude = os.path.join(os.path.realpath(os.path.expanduser("~")), ".claude") + os.sep
    if path.startswith(home_claude):
        return any(path[len(home_claude):].startswith(p) for p in HOME_CONTROLS)
    if project:
        return any(path == os.path.join(project, p.rstrip("/")) or path.startswith(os.path.join(project, p))
                   for p in CONTROL_PATHS)
    return False


def verdict(payload: dict, env) -> str:
    tool = payload.get("tool_name") or ""
    data = payload.get("tool_input")
    data = data if isinstance(data, dict) else {}
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())
    project = os.path.realpath(env.get("CLAUDE_PROJECT_DIR") or cwd)

    if tool == "Bash":
        command = str(data.get("command") or "")
        folded = command.casefold()
        if KEYCHAIN_SERVICE.casefold() in folded:
            return f"accessing Keychain service `{KEYCHAIN_SERVICE}`"
        if f"library/caches/{STATE_DIR_NAME.casefold()}" in folded:
            return f"accessing token cache `Library/Caches/{STATE_DIR_NAME}`"
        text = executable_text(command)
        rules = REVIEWER_RULES if is_reviewer(env) else CONDUCTOR_RULES
        for pattern, why in rules:
            if re.search(pattern, text):
                return why
        if not is_reviewer(env) and _WRITES.search(_HARMLESS_REDIRECT.sub(" ", text)):
            home = os.path.realpath(os.path.expanduser("~"))
            for segment in re.split(r"&&|\|\||[;|\n]", command.replace("$HOME", home)):
                for token in re.findall(r"[^\s'\"=<>()]+", segment)[1:]:
                    if is_protected(os.path.realpath(os.path.join(cwd, os.path.expanduser(token))), project):
                        return "writing to conductor control files (change them via a PR in a worktree)"
        return ""

    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = str(data.get("file_path") or data.get("notebook_path") or "")
        if not path:
            return ""
        real = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        if is_reviewer(env):
            state = env.get("CONDUCTOR_STATE_DIR") or ""
            root = os.path.realpath(os.path.join(os.path.expanduser(state), "reviews")) + os.sep if state else ""
            if not root or not real.startswith(root):
                return "writing outside the review worktree and scratch directory"
        elif is_protected(real, project):
            return "editing conductor control files (change them via a PR in a worktree)"
    return ""


def main() -> int:
    if not is_active(os.environ):
        return 0
    try:
        payload = json.load(sys.stdin)
        why = verdict(payload if isinstance(payload, dict) else {}, os.environ)
    except Exception as error:
        print(f"conductor-guard: could not evaluate call ({error.__class__.__name__}); refusing", file=sys.stderr)
        return 2
    if why:
        print(f"conductor-guard: blocked: {why}. Escalate with `coord conductor escalate \"…\"` if needed.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

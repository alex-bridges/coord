"""Who is running this command: `<harness>@<host>:<worktree>`.

Examples: claude@Eidolon:agent-coord, codex@Eidolon:leveltrim, claude-cloud@a1b2c3:INISA.
Set COORD_AGENT to override (a Codex environment the heuristics miss, a
teammate who wants a fixed name).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from typing import Mapping, Optional


def detect_harness(env: Mapping[str, str], toplevel: str) -> str:
    if env.get("CLAUDE_CODE_REMOTE") == "true":
        return "claude-cloud"
    if env.get("CLAUDECODE") == "1":
        return "claude"
    # Codex worktrees live under ~/.codex/worktrees. CODEX_COMPANION_* belongs
    # to the Codex plugin running INSIDE Claude Code, so it does not count.
    if "/.codex/" in toplevel or any(
        key.startswith("CODEX_") and not key.startswith("CODEX_COMPANION_") for key in env
    ):
        return "codex"
    if env.get("GITHUB_ACTIONS") == "true":
        return "actions"
    return "human"


def detect_host() -> str:
    """The Mac's LocalHostName (Eidolon, Tycho) when available; hostname otherwise."""
    try:
        name = subprocess.run(
            ["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    if not name:
        name = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", name) or "unknown"


def git_toplevel(cwd: Optional[str] = None) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def git_branch(cwd: Optional[str] = None) -> str:
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def agent_id(env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> str:
    env = os.environ if env is None else env
    explicit = env.get("COORD_AGENT")
    if explicit:
        return explicit
    toplevel = git_toplevel(cwd)
    worktree = os.path.basename(toplevel) if toplevel else "nogit"
    return f"{detect_harness(env, toplevel)}@{detect_host()}:{worktree}"


def matches(address: str, agent: str) -> bool:
    """Does a message address reach this agent?

    "all" reaches everyone; an exact id reaches that agent; a trailing "*" is a
    prefix match ("codex@*" reaches every Codex agent, "claude@Eidolon:*" every
    Claude session on Eidolon).
    """
    if address == "all" or address == agent:
        return True
    if address.endswith("*"):
        return agent.startswith(address[:-1])
    return False

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
import uuid
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


def detached_description(cwd: Optional[str] = None) -> str:
    """For a checkout with no branch (a cloud session checks out FETCH_HEAD)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=cwd, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        sha = ""
    return f"(detached at {sha}; pass --branch to claim)" if sha else "(no git checkout)"


def agent_id(env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None) -> str:
    env = os.environ if env is None else env
    explicit = env.get("COORD_AGENT")
    if explicit:
        return explicit
    toplevel = git_toplevel(cwd)
    worktree = os.path.basename(toplevel) if toplevel else "nogit"
    harness = detect_harness(env, toplevel)
    if harness == "claude-cloud":
        # Every cloud container reports host "vm" and checkout "INISA" (probed
        # 2026-09-24), so host:worktree would give all cloud sessions ONE identity and
        # mix their claims and messages. A container is one session: name it by that.
        session = env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID") or container_id()
        return f"claude-cloud@{re.sub(r'[^A-Za-z0-9]', '', session)[:8] or 'unknown'}:{worktree}"
    return f"{harness}@{detect_host()}:{worktree}"


def container_id() -> str:
    """A random id created once per machine and kept in the coord cache."""
    base = os.environ.get("COORD_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "inisa-coord")
    path = os.path.join(base, "container-id")
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
            if existing:
                return existing
    except OSError:
        pass
    fresh = uuid.uuid4().hex
    try:
        os.makedirs(base, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(fresh)
    except OSError:
        pass
    return fresh


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

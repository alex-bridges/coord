"""Who is running this command: `<harness>@<host>:<worktree>-<identity digest>`.

The digest distinguishes tasks in one checkout and same-named worktrees. Set
COORD_AGENT for a fixed address, or COORD_SESSION_ID for a harness-independent
task identifier. An automatically chosen address stays stable while its task,
host and checkout stay the same.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import tempfile
import uuid
from typing import Mapping, Optional

from . import cache


class IdentityError(RuntimeError):
    """A stable agent address could not be established."""


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
    """The Mac's LocalHostName when available; hostname otherwise."""
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
    # Codex worktrees often share one basename; that basename is a label, not an id.
    # Normalize aliases before both naming and hashing the checkout.
    checkout = os.path.normcase(os.path.realpath(toplevel or cwd or os.getcwd()))
    worktree = os.path.basename(checkout) if toplevel else "nogit"
    worktree = re.sub(r"[^A-Za-z0-9_-]", "-", worktree) or "worktree"
    harness = detect_harness(env, checkout)
    session = env.get("COORD_SESSION_ID")
    if not session and harness == "codex":
        session = env.get("CODEX_THREAD_ID")
    elif not session and harness in ("claude", "claude-cloud"):
        session = env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID")
    if not session and harness == "claude-cloud":
        # Cloud containers can share BOTH hostname and checkout path. Keep a
        # container id only for the known cloud environment without a session id.
        session = container_id()
    source = f"session:{session}" if session else f"checkout:{checkout}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"{harness}@{detect_host()}:{worktree}-{digest}"


def container_id() -> str:
    """A persistent cloud fallback id, published once even by concurrent creators.

    Only a completely written file becomes visible at container-id. A contender
    reads the winner; an unavailable cache must never yield a one-command identity.
    """
    base = cache.base_cache_dir()
    path = os.path.join(base, "container-id")
    # Read-only fallback: an id published under a legacy state dir name stays the identity.
    legacy_paths = [] if os.environ.get("COORD_CACHE_DIR") else [
        os.path.join(cache.base_cache_dir(name), "container-id") for name in cache.legacy_state_dir_names()]
    message = "cannot read or persist a stable cloud identity; set COORD_AGENT or COORD_SESSION_ID"

    def read() -> str:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if not existing:
            raise IdentityError(message)
        return existing

    try:
        return read()
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        raise IdentityError(message) from None
    for legacy in legacy_paths:
        try:
            with open(legacy, encoding="utf-8") as handle:
                existing = handle.read().strip()
            if existing:
                return existing
        except (OSError, UnicodeError):
            continue
    temporary = None
    try:
        os.makedirs(base, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=base, prefix=".container-id-")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(uuid.uuid4().hex)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # another creator published first; return the same identity
        return read()
    except (OSError, UnicodeError):
        raise IdentityError(message) from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


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

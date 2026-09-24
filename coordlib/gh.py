"""Thin wrapper over the `gh` CLI.

Every agent environment that can push to the repository already has `gh`
authenticated, so this is the one transport. All agents share ONE GitHub login
(alex-bridges) and therefore one quota of 5,000 REST and 5,000 GraphQL points an
hour; `calls` counts what a command spent so the board can print it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_REPO = "example-owner/example-repo"
CALL_TIMEOUT_SECONDS = 30


class GitHubError(RuntimeError):
    """gh failed: not installed, not authenticated, network, or an API error."""


class NotFound(GitHubError):
    pass


Runner = Callable[[Sequence[str], Optional[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(args: Sequence[str], stdin: Optional[str]) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ)
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        return subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=CALL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise GitHubError("the gh CLI is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise GitHubError(f"gh timed out after {CALL_TIMEOUT_SECONDS}s: {' '.join(args[:3])}") from error


def repo_from_remote_url(url: str) -> Optional[str]:
    """owner/name from an https or ssh GitHub remote URL, or None."""
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def detect_repo(cwd: Optional[str] = None) -> str:
    """COORD_REPO, else the origin remote of the current checkout, else the default.

    Parsing the remote costs no API call; `gh repo view` would.
    """
    explicit = os.environ.get("COORD_REPO")
    if explicit:
        return explicit
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        url = ""
    return repo_from_remote_url(url) or DEFAULT_REPO


class GitHub:
    def __init__(self, repo: Optional[str] = None, runner: Optional[Runner] = None):
        self.repo = repo or detect_repo()
        self.owner, self.name = self.repo.split("/", 1)
        self._runner = runner or _default_runner
        self.calls = 0

    # MARK: - Transport

    def _run(self, args: List[str], stdin: Optional[str] = None) -> str:
        self.calls += 1
        result = self._runner(["gh"] + args, stdin)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if "HTTP 404" in message or "Not Found" in message:
                raise NotFound(message)
            raise GitHubError(message or f"gh {' '.join(args[:2])} exited {result.returncode}")
        return result.stdout

    def rest(self, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Any:
        """One REST call. `path` may contain {repo}."""
        args = ["api", path.replace("{repo}", self.repo), "--method", method]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        out = self._run(args, stdin)
        return json.loads(out) if out.strip() else None

    def rest_paginated(self, path: str) -> List[Any]:
        """Every page of a list endpoint, flattened.

        `--paginate` alone prints one JSON document PER PAGE, which a single
        json.loads rejects (Codex P1 on #989); `--slurp` wraps the pages in one
        array. Endpoints that wrap their list in an object (actions/runs) come
        back as a list of those objects; the caller unwraps them.
        """
        out = self._run(["api", path.replace("{repo}", self.repo), "--paginate", "--slurp"])
        pages = json.loads(out) if out.strip() else []
        flat: List[Any] = []
        for page in pages:
            if isinstance(page, list):
                flat.extend(page)
            else:
                flat.append(page)
        return flat

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        out = self._run(["api", "graphql", "--input", "-"], json.dumps(payload))
        data = json.loads(out)
        if data.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise GitHubError(f"GraphQL: {messages}")
        return data["data"]

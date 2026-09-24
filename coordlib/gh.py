"""GitHub access: the `gh` CLI when it is installed, plain HTTPS with a token when not.

Every agent environment that can push already has `gh` authenticated or a token
(GH_TOKEN/GITHUB_TOKEN: Actions jobs, containers). Tycho's runners do not all
have `gh`, so the HTTPS path is what the merge-train workflow uses there.

All agents share ONE GitHub login (alex-bridges) and therefore one quota of
5,000 REST and 5,000 GraphQL points an hour; `calls` counts what a command spent
so the board can print it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


def _next_link(header: Optional[str]) -> Optional[str]:
    for part in (header or "").split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


def repo_path_link(url: str, repo: str) -> str:
    """A next-page link rewritten from `repositories/<numeric id>/…` to `repos/<owner>/<name>/…`.

    GitHub writes pagination links with the repository's numeric id, and the Claude
    cloud proxy refuses that form ("Numeric-ID repository paths … are not supported
    through this proxy", probe 3, 2026-09-24), so every read that needed a second page
    failed there. The query (page number or cursor) is kept as GitHub wrote it.
    """
    return re.sub(r"/repositories/\d+/", f"/repos/{repo}/", url, count=1)


class GitHub:
    """`runner` injects a fake `gh` (tests); `transport` forces "gh" or "http"."""

    def __init__(
        self,
        repo: Optional[str] = None,
        runner: Optional[Runner] = None,
        transport: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.repo = repo or detect_repo()
        self.owner, self.name = self.repo.split("/", 1)
        self._runner = runner
        self._transport = transport or os.environ.get("COORD_TRANSPORT") or ("gh" if runner else None)
        self._token = token
        self.api = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.calls = 0

    # MARK: - Transport selection

    def transport(self) -> str:
        if self._transport is None:
            if shutil.which("gh"):
                self._transport = "gh"
            elif self.token():
                self._transport = "http"
            else:
                raise GitHubError("neither the gh CLI nor a GH_TOKEN/GITHUB_TOKEN is available")
        return self._transport

    def token(self) -> str:
        return self._token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

    # MARK: - gh CLI

    def _run(self, args: List[str], stdin: Optional[str] = None) -> str:
        self.calls += 1
        result = (self._runner or _default_runner)(["gh"] + args, stdin)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if "HTTP 404" in message:
                raise NotFound(message)
            raise GitHubError(message or f"gh {' '.join(args[:2])} exited {result.returncode}")
        return result.stdout

    # MARK: - HTTPS

    def _http(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, str]]:
        self.calls += 1
        if not url.startswith("http"):
            url = f"{self.api}/{url.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token()}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "inisa-coord")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
                headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            # Whole, or nearly: a proxy's 403 can carry the list of routes to use instead.
            detail = error.read().decode(errors="replace")[:4000]
            if error.code == 404:
                raise NotFound(f"HTTP 404 {method} {url}: {detail}") from error
            raise GitHubError(f"HTTP {error.code} {method} {url}: {detail}") from error
        except (urllib.error.URLError, OSError) as error:
            raise GitHubError(f"{method} {url}: {error}") from error
        return (json.loads(raw) if raw.strip() else None), headers

    # MARK: - API

    def rest(self, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Any:
        """One REST call. `path` may contain {repo}."""
        path = path.replace("{repo}", self.repo)
        if self.transport() == "http":
            return self._http(method, path, body)[0]
        args = ["api", path, "--method", method]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        out = self._run(args, stdin)
        return json.loads(out) if out.strip() else None

    def rest_paginated(self, path: str) -> List[Any]:
        """Every page of a list endpoint, flattened.

        `gh api --paginate` alone prints one JSON document PER PAGE, which a single
        json.loads rejects (Codex P1 on #989); `--slurp` wraps the pages in one
        array. Endpoints that wrap their list in an object (actions/runs) come
        back as a list of those objects; the caller unwraps them.
        """
        path = path.replace("{repo}", self.repo)
        pages: List[Any] = []
        if self.transport() == "http":
            url: Optional[str] = path
            while url:
                page, headers = self._http("GET", url)
                pages.append(page)
                link = _next_link(headers.get("link"))
                url = repo_path_link(link, self.repo) if link else None
        else:
            out = self._run(["api", path, "--paginate", "--slurp"])
            pages = json.loads(out) if out.strip() else []
        flat: List[Any] = []
        for page in pages:
            if isinstance(page, list):
                flat.extend(page)
            elif page is not None:
                flat.append(page)
        return flat

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        if self.transport() == "http":
            data = self._http("POST", "graphql", payload)[0]
        else:
            data = json.loads(self._run(["api", "graphql", "--input", "-"], json.dumps(payload)))
        if data.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in data["errors"])
            raise GitHubError(f"GraphQL: {messages}")
        return data["data"]

    def dispatch_workflow(self, workflow: str, ref: str, inputs: Optional[Dict[str, str]] = None) -> None:
        self.rest(f"repos/{{repo}}/actions/workflows/{workflow}/dispatches", method="POST",
                  body={"ref": ref, "inputs": inputs or {}})

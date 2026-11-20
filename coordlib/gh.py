"""GitHub access: the `gh` CLI when it is installed, plain HTTPS with a token when not.

Every agent environment that can push already has `gh` authenticated or a token
(GH_TOKEN/GITHUB_TOKEN: Actions jobs, containers). CI runners do not all
have `gh`, so the HTTPS path is what the merge-train workflow uses there.

Two identities (#1035):
- **The app.** On a Mac with the app config, outside Actions and unless COORD_APP=off
  (apptoken.detect), coord's own calls use the repo's GitHub App: reads with a
  read-only installation token, operational writes (claims, messages, train records,
  CI dispatches) with a narrow write token. Each call goes over HTTPS with the token set
  on that one request. GH_TOKEN is never exported, so `git push` and every other
  process stay on Alex's login. A token that cannot be had raises AppAuthError, and
  nothing falls back to Alex's token.
- **User login** for everything else: all of coord when app mode is off
  (cloud sessions, other machines, COORD_APP=off), and, in app mode, the attestations
  (`train approve`, `train review` and its old name `train substitute-review`) and a
  hand-run `train step` (`GitHub.as_user`).

Each identity has its own hourly budget; budget.py records every reading under the
principal whose token made the call. `calls` counts what a command spent so the board
can print it.
"""

from __future__ import annotations

import contextlib
import copy
import http.client
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import budget, cache

# The one place the app's JWT and tokens may go (apptoken.detect turns app mode off when
# GITHUB_API_URL names anything else).
GITHUB_API = "https://api.github.com"
CALL_TIMEOUT_SECONDS = 30


class GitHubError(RuntimeError):
    """gh failed: not installed, not authenticated, network, or an API error. `status` is
    the HTTP status of an HTTPS call GitHub answered with an error, and `resource` the
    budget it spent (core or graphql); else both are None."""

    status: Optional[int] = None
    resource: Optional[str] = None


class NotFound(GitHubError):
    pass


class AppAuthError(GitHubError):
    """No GitHub App token (config, Keychain, openssl, network, or GitHub refused the mint).

    Never answered with Alex's token: the caller serves a cache or refuses, naming the
    cause and COORD_APP=off as the deliberate override.
    """


class NullPageNode(GitHubError):
    """GraphQL returned a null entry inside a top-level connection page (no `errors`
    entry) — seen on `pullRequests` (#1218). The entry carries no id to complete over
    REST, and dropping it would read as the PR no longer being open, so the caller
    re-fetches the whole page over REST instead of guessing."""


# What must never reach a terminal or a log: GitHub tokens (an installation token is
# `ghs_…`, and the stateless kind embeds a JWT), JWTs, PEM blocks, and long base64 runs
# (the Keychain holds the key as base64).
_SECRETS = (
    re.compile(r"\b(?:gh[opsur]_|github_pat_)[A-Za-z0-9_.\-]+"),
    re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN[^-]*-----.*?(?:-----END[^-]*-----|$)", re.S),
    re.compile(r"[A-Za-z0-9+/=]{100,}"),
)


def redact(text: str) -> str:
    """`text` with anything shaped like a token, JWT or key replaced by [redacted]."""
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    return text


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


def _valid_repo(repo: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9._-]+", repo)
                and repo.split("/")[1] not in (".", ".."))


def repo_from_remote_url(url: str) -> Optional[str]:
    """owner/name from a GitHub remote URL, or None."""
    url = url.strip()
    if "://" in url:
        try:
            remote = urllib.parse.urlsplit(url)
            if remote.scheme not in ("https", "http", "ssh", "git") or remote.query or remote.fragment:
                return None
            if remote.hostname != "github.com" and (remote.scheme, remote.hostname) != ("ssh", "ssh.github.com"):
                return None
        except ValueError:
            return None
        path = remote.path.removeprefix("/")
    else:
        match = re.fullmatch(r"(?:[^/@:]+@)?github\.com:(.+)", url, re.IGNORECASE)
        if not match:
            return None
        path = match.group(1)
    repo = path.removesuffix("/").removesuffix(".git")
    return repo if _valid_repo(repo) else None


def detect_repo(cwd: Optional[str] = None) -> str:
    """COORD_REPO, else the origin remote of the current checkout; GitHubError when neither names a repo.

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
    repo = repo_from_remote_url(url)
    if not repo:
        raise GitHubError(
            "cannot tell which GitHub repository this is: no origin remote of a github.com repo "
            "here and COORD_REPO is not set (run from a checkout, or pass --repo owner/name)")
    return repo


def _next_link(header: Optional[str]) -> Optional[str]:
    for part in (header or "").split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


# MARK: - Authenticated requests


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects for requests carrying a GitHub credential.

    urllib's own handler copies headers added with `add_header` onto 301, 302, 303 and 307
    redirects, to any host and to plain http, so a redirect from GitHub (or from anything
    between) would hand the credential to whoever it names. Here the credential travels as
    an unredirected header, a redirect to another scheme, host or port is refused, and a
    redirect within GitHub's own origin gets the credential back.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's signature
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc.lower()) != (new.scheme, new.netloc.lower()):
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"refused a redirect to another origin: credentials stay with {old.netloc}",
                headers, fp)
        follow = super().redirect_request(req, fp, code, msg, headers, newurl)
        credential = req.unredirected_hdrs.get("Authorization")
        if follow is not None and credential:
            follow.add_unredirected_header("Authorization", credential)
        return follow


_API_OPENER: Optional[urllib.request.OpenerDirector] = None


def api_open(request: urllib.request.Request, timeout: float = CALL_TIMEOUT_SECONDS) -> Any:
    """Open an authenticated API request using same-origin redirects."""
    global _API_OPENER
    if _API_OPENER is None:
        _API_OPENER = urllib.request.build_opener(SameOriginRedirects)
    return _API_OPENER.open(request, timeout=timeout)


def api_request(method: str, url: str, credential: str, data: Optional[bytes] = None) -> urllib.request.Request:
    """A request with its credential as an UNREDIRECTED Authorization header, so no
    redirect handler copies it anywhere. Send it with `api_open`."""
    request = urllib.request.Request(url, data=data, method=method)
    request.add_unredirected_header("Authorization", f"Bearer {credential}")
    _api_headers(request, data)
    return request


def _api_headers(request: urllib.request.Request, data: Optional[bytes]) -> None:
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", cache.state_dir_name())
    if data is not None:
        request.add_header("Content-Type", "application/json")


def token_kind(method: str, url: str) -> str:
    """Which app token a call uses: `read` for a GET and for GraphQL (coord's GraphQL is
    queries only; it sends no mutation), `ops` for every other method."""
    path = url.split("?", 1)[0].rstrip("/")
    if method == "GET" or path == "graphql" or path.endswith("/graphql"):
        return "read"
    return "ops"


def repo_path_link(url: str, repo: str) -> str:
    """A next-page link rewritten from `repositories/<numeric id>/…` to `repos/<owner>/<name>/…`.

    GitHub writes pagination links with the repository's numeric id, and the Claude
    cloud proxy refuses that form ("Numeric-ID repository paths … are not supported
    through this proxy", probe 3, 2026-09-24), so every read that needed a second page
    failed there. The query (page number or cursor) is kept as GitHub wrote it.
    """
    return re.sub(r"/repositories/\d+/", f"/repos/{repo}/", url, count=1)


def _response_json(raw: str) -> Any:
    try:
        return json.loads(raw) if raw.strip() else None
    except (ValueError, RecursionError):
        raise GitHubError("GitHub returned invalid JSON") from None


def http_error_detail(error: urllib.error.HTTPError) -> str:
    """Read a bounded error body without losing the status if its stream fails."""
    if error.fp is None:
        return "(no readable body)"  # Python 3.9 leaves the response wrapper uninitialized
    try:
        return error.read(4000).decode(errors="replace")
    except Exception:
        return "(no readable body)"
    finally:
        with contextlib.suppress(Exception):
            error.close()


class GitHub:
    """`runner` injects a fake `gh` (tests); `transport` forces "gh" or "http".

    `app` is the app's token source (apptoken.AppTokens) when app mode is on for this
    command. cli.main attaches it, so a GitHub built anywhere else is Alex's, as before.
    """

    def __init__(
        self,
        repo: Optional[str] = None,
        runner: Optional[Runner] = None,
        transport: Optional[str] = None,
        token: Optional[str] = None,
        app: Optional[Any] = None,
    ):
        self.repo = repo or detect_repo()
        if not _valid_repo(self.repo):
            raise GitHubError("expected a GitHub repository in owner/name form")
        self.owner, self.name = self.repo.split("/", 1)
        self._runner = runner
        self._transport = transport or os.environ.get("COORD_TRANSPORT") or ("gh" if runner else None)
        self._token = token
        self.api = (os.environ.get("GITHUB_API_URL") or GITHUB_API).rstrip("/")
        self.calls = 0
        self.app = app
        self.app_mode: Optional[Any] = None  # apptoken.Mode, for status lines (cli.main sets it)

    # MARK: - Identity

    def as_user(self) -> "GitHub":
        """This GitHub on Alex's identity (gh/keyring, or GH_TOKEN), for the commands that
        stay his in app mode. Chosen per command, never as a fallback."""
        user = copy.copy(self)
        user.app = None
        return user

    def principal(self) -> Optional[str]:
        """Whose budget this GitHub's calls spend: budget.USER, `app:<installation>`, or
        None in app mode before any token has named the installation (no reading yet)."""
        if self.app is None:
            return budget.USER
        installation = self.app.installation_id()
        return budget.app_principal(installation) if installation else None

    # MARK: - Transport selection

    def transport(self) -> str:
        if self.app is not None:
            return "http"  # the app's token is set per request, never through gh
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

    def _http(self, method: str, url: str, body: Optional[Dict[str, Any]] = None,
              kind: Optional[str] = None) -> Tuple[Any, Dict[str, str]]:
        """One HTTPS call. In app mode the token is the app's `kind` (`token_kind`: `read`
        for a GET or a GraphQL query, `ops` for every other write), obtained BEFORE anything
        else: a failure raises AppAuthError and no request is sent, with any token."""
        kind = kind or token_kind(method, url)
        base = GITHUB_API if self.app is not None else self.api
        if not url.startswith(("http:", "https:", "//")):
            url = f"{base}/{url.lstrip('/')}"
        if not url.startswith(base + "/"):
            error_type = AppAuthError if self.app is not None else GitHubError
            raise error_type(f"refused to send a credential outside {base}")
        if self.app is not None:
            bearer = self.app.token(kind)
            principal = self.principal() or budget.USER  # the token just named the installation
        else:
            bearer, principal = self.token(), budget.USER
        self.calls += 1
        data = json.dumps(body).encode() if body is not None else None
        request = api_request(method, url, bearer, data)
        resource = "graphql" if url.rstrip("/").endswith("/graphql") else "core"
        try:
            with api_open(request, timeout=CALL_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
                headers = {k.lower(): v for k, v in response.headers.items()}
            budget.record(self.repo, headers, resource, principal)
        except urllib.error.HTTPError as error:
            # The 403 of an exhausted budget carries `X-RateLimit-Remaining: 0`: the
            # reading that matters most.
            error_headers = {k.lower(): v for k, v in error.headers.items()} if error.headers else {}
            budget.record(self.repo, error_headers, resource, principal)
            # Whole, or nearly: a proxy's 403 can carry the list of routes to use instead.
            detail = http_error_detail(error)
            if error.code == 401 and self.app is not None:
                # The app's token was refused: revoked, or the installation suspended. Drop
                # it from the cache and refuse (reads serve their cache; nothing goes to Alex).
                reason = f"GitHub refused the app's {kind} token (HTTP 401: {redact(detail)[:200]})"
                self.app.reject(kind, bearer, reason)
                raise AppAuthError(reason) from None
            if error.code == 404:
                raise NotFound(f"HTTP 404 {method} {url}: {detail}") from error
            # A token without a permission the endpoint needs: GitHub names the ones it accepts.
            accepted = error_headers.get("x-accepted-github-permissions")
            if accepted:
                detail += f" (accepted permissions: {accepted})"
            failure = GitHubError(f"HTTP {error.code} {method} {url}: {detail}")
            failure.status, failure.resource = error.code, resource
            raise failure from error
        except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeDecodeError) as error:
            raise GitHubError(f"{method} {url}: {type(error).__name__}: {error}") from error
        return _response_json(raw), headers

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
        return _response_json(out)

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
            visited = set()
            while url:
                if url in visited:
                    raise GitHubError("GitHub returned a repeated pagination link")
                visited.add(url)
                page, headers = self._http("GET", url)
                pages.append(page)
                link = _next_link(headers.get("link"))
                if self.app is None and link and link.startswith(GITHUB_API + "/"):
                    # A proxy may return GitHub's canonical link. Continue through the
                    # selected API endpoint on every page.
                    link = self.api + link[len(GITHUB_API):]
                url = repo_path_link(link, self.repo) if link else None
        else:
            out = self._run(["api", path, "--paginate", "--slurp"])
            pages = _response_json(out) if out.strip() else []
            if not isinstance(pages, list):
                raise GitHubError("gh returned an invalid paginated response")
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
            data = _response_json(self._run(["api", "graphql", "--input", "-"], json.dumps(payload)))
        if not isinstance(data, dict):
            raise GitHubError("GitHub returned an invalid GraphQL response")
        if data.get("errors"):
            errors = data["errors"]
            messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e)
                                 for e in errors) if isinstance(errors, list) else str(errors)
            raise GitHubError(f"GraphQL: {messages}")
        if not isinstance(data.get("data"), dict):
            raise GitHubError("GitHub returned no GraphQL data")
        return data["data"]

    def probe_budget(self) -> Optional[Dict[str, Any]]:
        """One `gh api -i repos/<repo>` call, to read the core budget from its headers.

        The gh transport otherwise sees no headers. Never the `rate_limit` endpoint: on
        2026-09-25 it reported 5000/5000 while real calls got 403; only the headers of a
        real response told the truth. Runs the runner directly, not `_run`: a refused
        call exits non-zero yet still prints the headers, and that is the reading that
        matters. None when gh printed no budget headers.
        """
        self.calls += 1
        try:
            result = (self._runner or _default_runner)(["gh", "api", "-i", f"repos/{self.repo}"], None)
        except GitHubError:
            return None
        return budget.record(self.repo, budget.parse_header_block(result.stdout or ""), "core", budget.USER)

    def dispatch_workflow(self, workflow: str, ref: str, inputs: Optional[Dict[str, str]] = None) -> None:
        self.rest(f"repos/{{repo}}/actions/workflows/{workflow}/dispatches", method="POST",
                  body={"ref": ref, "inputs": inputs or {}})

"""Fixtures for the GitHub App tests (test_apptoken.py, test_budget_principals.py).

Nothing here reaches GitHub, the real Keychain item or the real token cache:
- a throwaway RSA key, generated at import with /usr/bin/openssl and kept in memory;
- a fake `security` (a shell script) that prints the throwaway key's base64 only when a
  test asks for it, and otherwise trips: exit 97, logged;
- a fake GitHub behind `urllib.request.urlopen`, which mints tokens shaped like the real
  ones (`ghs_…`, 390 characters) and answers the endpoints coord calls;
- a fake `gh` runner that records every call and answers from the fake GitHub.

`AppTestCase` points COORD_APP_CONFIG, COORD_TOKEN_CACHE_DIR and COORD_CACHE_DIR at a
temporary directory and installs the fakes for every test, so a test that forgets one
fails instead of reaching anything real. App mode sends credentials to
https://api.github.com only (apptoken.detect), so the tests leave GITHUB_API_URL unset
and the fake `urlopen` answers for that host: it is patched on `urllib.request` itself,
which both gh.py and apptoken.py call, and it fails any request to another host.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import email.message
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import apptoken, cli  # noqa: E402
from coordlib import gh as ghmodule  # noqa: E402

REPO = "o/r"
CLIENT_ID = "Iv23test0client"
INSTALLATION = 1000001
API = "https://api.github.com"  # the fake urlopen answers for it; nothing leaves the process
TOKEN_LENGTH = 390  # representative GitHub token length; nothing may rely on it

OPENSSLS = [path for path in ("/usr/bin/openssl", "/opt/homebrew/bin/openssl") if os.path.exists(path)]


def _generate_key() -> bytes:
    """A throwaway 2048-bit PKCS#1 key (LibreSSL's genrsa writes the form GitHub issues)."""
    return subprocess.run(["/usr/bin/openssl", "genrsa", "2048"], capture_output=True, check=True).stdout


if os.name != "nt" and os.path.exists("/usr/bin/openssl"):
    PEM = _generate_key()
    PEM_B64 = base64.b64encode(PEM).decode()
    PUBLIC_PEM = subprocess.run(["/usr/bin/openssl", "rsa", "-pubout"], input=PEM, capture_output=True,
                                check=True).stdout
    KEY_LINES = [line for line in PEM.decode().splitlines() if line and not line.startswith("-----")]
else:
    PEM, PEM_B64, PUBLIC_PEM, KEY_LINES = b"", "", b"", []

JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

FAKE_SECURITY = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SECURITY_LOG"
case "$FAKE_SECURITY_MODE" in
  key) printf '%s\\n' "$FAKE_KEYCHAIN_B64" ;;
  missing) echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2; exit 44 ;;
  garbage) echo "not base64 at all!" ;;
  *) echo "tripwire: security was run in a test that did not expect it" >&2; exit 97 ;;
esac
"""


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jwt_parts(jwt: str) -> List[Dict[str, Any]]:
    def decode(part: str) -> Dict[str, Any]:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    header, payload, _ = jwt.split(".")
    return [decode(header), decode(payload)]


class FakeResponse:
    def __init__(self, body: Any, headers: Dict[str, str], status: int = 200):
        self._body = b"" if body is None else json.dumps(body).encode()
        self.status = status
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def rate_headers(used: int, reset: int, resource: str = "core", limit: int = 5000) -> Dict[str, str]:
    return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(limit - used),
            "X-RateLimit-Used": str(used), "X-RateLimit-Reset": str(reset), "X-RateLimit-Resource": resource}


def pr_node(number: int = 101) -> Dict[str, Any]:
    """A minimal open PR as the board's GraphQL query returns it."""
    return {
        "number": number, "title": "Example", "url": f"https://example/{number}", "isDraft": False,
        "isCrossRepository": False, "headRefName": "feature/example", "headRefOid": "a" * 40,
        "baseRefName": "main", "createdAt": "2026-09-25T10:00:00Z", "updatedAt": "2026-09-25T11:00:00Z",
        "mergeable": "MERGEABLE", "labels": {"nodes": []},
        "files": {"totalCount": 1, "nodes": [{"path": "Docs/X.md"}]}, "reactions": {"nodes": []},
        "comments": {"totalCount": 0, "nodes": []}, "closingIssuesReferences": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": "a" * 40, "committedDate": "2026-09-25T10:00:00Z",
                                          "checkSuites": {"nodes": []},
                                          "statusCheckRollup": {"contexts": {"nodes": []}}}}]},
        "reviewThreads": {"nodes": []},
    }


class FakeGitHub:
    """urlopen for the app's mint endpoints and every endpoint coord calls. It records each
    request with the bearer token it carried, and knows which kind each minted token is."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.tokens: Dict[str, str] = {}  # token -> kind
        self.jwts: List[str] = []
        self.mints = 0
        self.discoveries = 0
        self.mint_status: Optional[int] = None  # answer the mint with this HTTP error
        self.discover_status: Optional[int] = None
        self.mint_delay = 0.0
        self.grant: Optional[Dict[str, str]] = None  # grant these instead of what was asked
        self.expires_in = 3600
        self.token_length = TOKEN_LENGTH
        self.installation = INSTALLATION
        self.reset = int(time.time()) + 1800
        self.used = 10
        self.prs: List[Dict[str, Any]] = [pr_node()]
        self.forbid_writes = ""  # answer every write 403 with this X-Accepted-GitHub-Permissions
        self.echo_secrets = 0  # answer every API call with this 4xx, its body echoing the token and a JWT
        self.refuse_kinds: set = set()  # refuse to mint these kinds (403)
        self.lock = threading.Lock()

    # MARK: urlopen

    def __call__(self, request: urllib.request.Request, timeout: Optional[float] = None) -> FakeResponse:
        url = request.full_url
        assert url.startswith(API + "/"), f"a request left the fake API: {url}"
        path = url[len(API) + 1:]
        method = request.get_method()
        auth = request.get_header("Authorization") or ""  # unredirected (app) or ordinary (Alex)
        bearer = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
        if (JWT_SHAPE.fullmatch(bearer) or bearer.startswith("ghs_")) and "Authorization" in request.headers:
            raise AssertionError("an app credential travelled as a header a redirect would copy (add_header)")
        body = json.loads(request.data) if request.data else None
        with self.lock:
            self.requests.append({"method": method, "path": path, "bearer": bearer,
                                  "kind": self.tokens.get(bearer), "body": body})
        if path.startswith("app/") or path.endswith("/installation"):
            return self._app_call(method, path, bearer, body)
        if bearer not in self.tokens:
            raise self._error(url, 401, {"message": "Bad credentials"})
        if self.forbid_writes and method != "GET" and path != "graphql":
            # What GitHub answers a token that lacks the endpoint's permission.
            raise self._error(url, 403, {"message": "Resource not accessible by integration"},
                              {"X-Accepted-GitHub-Permissions": self.forbid_writes})
        if self.echo_secrets:
            # A hostile or buggy 4xx whose body repeats the credentials it was sent.
            jwt = self.jwts[-1] if self.jwts else "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjIzIn0.c2lnbmF0dXJlLWJ5dGVz"
            raise self._error(url, self.echo_secrets, {"message": f"Validation failed for token {bearer}",
                                                        "jwt": jwt, "authorization": auth})
        return self._api_call(method, path, body)

    def _error(self, url: str, code: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None):
        message = email.message.Message()
        for name, value in (headers or {}).items():
            message[name] = value
        return urllib.error.HTTPError(url, code, "fake", message, io.BytesIO(json.dumps(body).encode()))

    def _app_call(self, method: str, path: str, jwt: str, body: Any) -> FakeResponse:
        assert JWT_SHAPE.fullmatch(jwt), "the app's calls carry a JWT"
        self.jwts.append(jwt)
        if path.endswith("/installation"):
            self.discoveries += 1
            if self.discover_status:
                raise self._error(API + "/" + path, self.discover_status, {"message": "Not Found"})
            return FakeResponse({"id": self.installation, "app_id": 100001}, {})
        if self.mint_delay:
            time.sleep(self.mint_delay)
        match = re.fullmatch(r"app/installations/(\d+)/access_tokens", path)
        if method != "POST" or not match:
            raise self._error(API + "/" + path, 404, {"message": "Not Found"})
        if int(match.group(1)) != self.installation:
            raise self._error(API + "/" + path, 404, {"message": "Not Found"})
        if self.mint_status:
            raise self._error(API + "/" + path, self.mint_status,
                              {"message": "A JSON web token could not be decoded"})
        kind = "ops" if body["permissions"] == apptoken.OPS_PERMISSIONS else "read"
        if kind in self.refuse_kinds:
            raise self._error(API + "/" + path, 403, {"message": f"fake: no {kind} token today"})
        token = "ghs_" + secrets.token_urlsafe(self.token_length)[: self.token_length - 4]
        with self.lock:
            self.tokens[token] = kind
            self.mints += 1
        granted = self.grant if self.grant is not None else body["permissions"]
        return FakeResponse({"token": token, "expires_at": iso(time.time() + self.expires_in),
                             "permissions": granted, "repository_selection": "selected"}, {}, 201)

    def _api_call(self, method: str, path: str, body: Any) -> FakeResponse:
        resource = "graphql" if path == "graphql" else "core"
        with self.lock:
            self.used += 1
            headers = rate_headers(self.used, self.reset, resource)
        route = path.split("?", 1)[0]
        if route == "graphql":
            query = body["query"]
            if "pullRequests(" in query:
                data = {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": "f" * 40}},
                                       "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                        "nodes": json.loads(json.dumps(self.prs))}}}
            else:
                repo: Dict[str, Any] = {}
                for number in re.findall(r"\bi(\d+): issueOrPullRequest", query):
                    repo[f"i{number}"] = {"number": int(number), "title": "t", "state": "OPEN", "url": "u"}
                for index in re.findall(r"\bb(\d+): ref", query):
                    repo[f"b{index}"] = None
                data = {"repository": repo}
            return FakeResponse({"data": data}, headers)
        if method == "GET":
            if re.search(r"actions/(workflows/[^/]+/)?runs$", route):
                return FakeResponse({"workflow_runs": []}, headers)
            if re.search(r"issues/comments$|pulls/comments$|issues/\d+/comments$|git/matching-refs/", route):
                return FakeResponse([], headers)
            if route.endswith("/issues"):
                return FakeResponse([], headers)  # no mailbox yet
            if re.search(r"/pulls/\d+$", route):
                return FakeResponse({"head": {"sha": "a" * 40}}, headers)
        if method == "POST":
            if route.endswith("/labels") and "/issues/" not in route:
                return FakeResponse({"name": (body or {}).get("name")}, headers, 201)
            if route.endswith("/issues"):
                return FakeResponse({"number": 900, "html_url": "https://example/900"}, headers, 201)
            if re.search(r"issues/\d+/comments$", route):
                return FakeResponse({"id": 1, "html_url": "https://example/c/1"}, headers, 201)
            if re.search(r"issues/\d+/labels$", route):
                return FakeResponse([], headers)
            if route.endswith("/dispatches"):
                return FakeResponse(None, headers, 204)
        if method == "GET" and route.endswith("/actions/runners"):
            return FakeResponse({"total_count": 0, "runners": []}, headers)
        if method == "PATCH" and re.search(r"issues/comments/\d+$", route):
            return FakeResponse({"id": int(route.rsplit("/", 1)[1])}, headers)
        if method == "DELETE" and re.search(r"issues/\d+/labels/", route):
            return FakeResponse([], headers)
        raise self._error(API + "/" + path, 404, {"message": f"fake: no route for {method} {route}"}, headers)

    # MARK: queries

    def api_requests(self) -> List[Dict[str, Any]]:
        """Requests to the API proper (not the app's JWT calls)."""
        return [r for r in self.requests if not (r["path"].startswith("app/") or r["path"].endswith("/installation"))]

    def seen_secrets(self) -> List[str]:
        return list(self.tokens) + list(self.jwts)


class FakeRunner:
    """A fake `gh` for Alex's identity: records every call, answers the budget probe, and
    answers everything else from the fake GitHub's routes (as Alex: no app token, and not
    recorded among the app's requests)."""

    def __init__(self, server: "FakeGitHub", reset: Optional[int] = None):
        self.calls: List[List[str]] = []
        self.server = server
        self.reset = reset or int(time.time()) + 1800

    def __call__(self, args, stdin):
        self.calls.append(list(args))
        if list(args[:3]) == ["gh", "api", "-i"]:
            out = (f"HTTP/2.0 200 OK\r\nX-Ratelimit-Limit: 5000\r\nX-Ratelimit-Remaining: 4000\r\n"
                   f"X-Ratelimit-Reset: {self.reset}\r\nX-Ratelimit-Resource: core\r\nX-Ratelimit-Used: 1000\r\n\r\n{{}}")
            return subprocess.CompletedProcess(args, 0, out, "")
        path = args[2] if len(args) > 2 else ""
        method = args[args.index("--method") + 1] if "--method" in args else ("POST" if path == "graphql" else "GET")
        body = json.loads(stdin) if stdin else None
        try:
            answer = json.loads(self.server._api_call(method, path, body).read() or b"null")  # noqa: SLF001
        except urllib.error.HTTPError as error:
            return subprocess.CompletedProcess(args, 1, "", f"gh: fake (HTTP {error.code})")
        if "--slurp" in args:
            answer = [answer]
        return subprocess.CompletedProcess(args, 0, "" if answer is None else json.dumps(answer), "")


class AppTestCase(unittest.TestCase):
    """Every app test runs isolated: its own config, token cache and board cache, a closed
    API port, a tripwire `security`, the fake GitHub, and a recording `gh` runner."""

    ENV = ("COORD_APP", "COORD_APP_CONFIG", "COORD_TOKEN_CACHE_DIR", "COORD_CACHE_DIR", "COORD_REPO",
           "COORD_AGENT", "GITHUB_ACTIONS", "GITHUB_API_URL", "GH_TOKEN", "GITHUB_TOKEN", "COORD_TRANSPORT",
           "COORD_BUDGET_READ_FLOOR", "COORD_BUDGET_HARD_FLOOR", "COORD_BUDGET_USER_LIMIT",
           "COORD_BUDGET_APP_LIMIT", "CLAUDE_CODE_REMOTE", "COORD_REST_ONLY",
           "FAKE_SECURITY_LOG", "FAKE_SECURITY_MODE", "FAKE_KEYCHAIN_B64", "TMPDIR", "COORD_SECURITY_BIN")

    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("AppTestCase uses POSIX /bin/sh fake security; Windows DPAPI flow is in test_windows_compat.py")
        # Registered first, so it runs last (cleanups run in reverse): no test may leave HOME
        # changed for the test modules that run after it.
        self.addCleanup(self._assert_home_unchanged, os.environ.get("HOME"))
        self.saved_env = {name: os.environ.get(name) for name in self.ENV}
        for name in self.ENV:
            os.environ.pop(name, None)
        self.root = tempfile.mkdtemp(prefix="coord-app-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config_path = os.path.join(self.root, "config", "github-app.json")
        self.token_dir = os.path.join(self.root, "tokens")
        self.write_config({"app_id": 100001, "client_id": CLIENT_ID, "repo": REPO,
                           "app_login": "example-reviewer[bot]"})
        os.environ.update({
            "COORD_APP_CONFIG": self.config_path, "COORD_TOKEN_CACHE_DIR": self.token_dir,
            "COORD_CACHE_DIR": os.path.join(self.root, "cache"), "COORD_REPO": REPO,
            "COORD_AGENT": "claude@test:app",
            "FAKE_SECURITY_LOG": os.path.join(self.root, "security.log"), "FAKE_SECURITY_MODE": "tripwire",
            "FAKE_KEYCHAIN_B64": PEM_B64,
        })
        security = os.path.join(self.root, "bin", "security")
        os.makedirs(os.path.dirname(security))
        with open(security, "w", encoding="utf-8") as handle:
            handle.write(FAKE_SECURITY)
        os.chmod(security, 0o700)
        self.patch(apptoken, "SECURITY", security)
        self.server = FakeGitHub()
        self.patch(urllib.request, "urlopen", self.server)  # Alex's HTTPS calls
        self.patch(ghmodule, "app_open", self.server)  # every request carrying an app credential
        self.runner = FakeRunner(self.server)
        self.patch(ghmodule, "_default_runner", self.runner)

    def tearDown(self) -> None:
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _assert_home_unchanged(self, home: Optional[str]) -> None:
        self.assertEqual(os.environ.get("HOME"), home, "a test left HOME changed")

    def patch(self, owner: Any, name: str, value: Any) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    # MARK: helpers

    def write_config(self, data: Any) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def keychain(self, mode: str = "key") -> None:
        os.environ["FAKE_SECURITY_MODE"] = mode

    def forget_failures(self) -> None:
        """Remove the remembered mint failures (as FAILURE_HOLD_SECONDS passing would)."""
        for kind in apptoken.KINDS:
            try:
                os.remove(os.path.join(self.token_dir, f"{kind}.failed.json"))
            except OSError:
                pass

    def security_calls(self) -> List[str]:
        try:
            with open(os.environ["FAKE_SECURITY_LOG"], encoding="utf-8") as handle:
                return [line.rstrip("\n") for line in handle]
        except OSError:
            return []

    def tokens(self, clock=None) -> apptoken.AppTokens:
        config = apptoken.load_config(self.config_path)
        return apptoken.AppTokens(config, self.token_dir, clock=clock or time.time)

    def main(self, argv: List[str]):
        """cli.main in-process, exactly as the `coord` script runs it: (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def seed_token(self, kind: str, permissions: Optional[Dict[str, str]] = None, expires_in: float = 3600,
                   mode: int = 0o600, token: Optional[str] = None) -> str:
        """A token cache entry as a mint writes it."""
        token = token or "ghs_" + secrets.token_urlsafe(300)
        os.makedirs(self.token_dir, exist_ok=True)
        path = os.path.join(self.token_dir, f"{kind}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"kind": kind, "token": token, "expires_at": iso(time.time() + expires_in),
                       "installation_id": INSTALLATION,
                       "permissions": permissions if permissions is not None else apptoken.PERMISSIONS[kind],
                       "client_id": CLIENT_ID, "repo": REPO, "minted_at": int(time.time())}, handle)
        os.chmod(path, mode)
        self.server.tokens[token] = kind
        return token

    def seed_snapshot(self, age_seconds: float = 3600) -> None:
        from coordlib import cache
        then = time.time() - age_seconds
        cache.store_snapshot(REPO, {
            "schema": 1, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(then)), "repo": REPO,
            "main": {"sha": "f" * 40}, "prs": [], "claims": [], "overlaps": [],
            "train": [], "runs": {"running": [], "queued": []}, "messages": [], "apiCalls": 9,
        })
        os.utime(cache.snapshot_path(REPO), (then, then))

    def assert_no_secret(self, *texts: str) -> None:
        """No token, JWT or key material in any of `texts`."""
        joined = "\n".join(texts)
        self.assertNotIn("ghs_", joined)
        self.assertIsNone(JWT_SHAPE.search(joined), "a JWT-shaped string was printed")
        self.assertNotIn("PRIVATE KEY", joined)
        self.assertNotIn(PEM_B64[:40], joined)
        for line in KEY_LINES[:3]:
            self.assertNotIn(line, joined)
        for secret in self.server.seen_secrets():
            self.assertNotIn(secret, joined)


def file_mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)

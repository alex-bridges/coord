"""GitHub App installation tokens for coord's own calls on the Mac (#1035).

The app `inisa-sidechain` is installed on example-owner/example-repo only. Its installation has
its own hourly budget, separate from Alex's user counter, so coord's reads and its
operational writes (claims, messages, train records, CI dispatches) stop spending the
budget Alex's `gh` and every agent share.

**When app mode is on** (`detect`): the non-secret config exists
(`~/.config/inisa-coord/github-app.json`, or COORD_APP_CONFIG), the command does not run
in GitHub Actions (the train keeps GITHUB_TOKEN), COORD_APP is not `off`, GITHUB_API_URL
is unset or https://api.github.com (app credentials go to GitHub only), and the
repository is the one the app is installed on. Cloud sessions have no config, so they
run exactly as before. COORD_APP=off is the deliberate override: Alex's gh/keyring
identity for everything.

**Two kinds of token**, each minted with narrowed permissions and cached on its own:
- `read`: every permission `read`. coord's reads, and later the agents' read wrapper.
- `ops`: what coord's operational writes need and nothing more (OPS_PERMISSIONS).

**Minting** (`AppTokens._mint`): the private key is read from the login Keychain
(service KEYCHAIN_SERVICE, account = the client id; the value is the base64 of the PEM)
with /usr/bin/security, and reaches /usr/bin/openssl through an inherited pipe
(`/dev/fd/N`), never a file. openssl signs an RS256 JWT (iat = now - 60, exp = now +
540, iss = the client id). The installation is found with GET /repos/{repo}/installation
and the token minted with POST /app/installations/{id}/access_tokens, narrowed to the
one repository and the kind's permissions. GitHub must grant exactly what was asked.

**Cache**: one file per kind (`read.json`, `ops.json`) in ~/Library/Caches/inisa-coord
(or COORD_TOKEN_CACHE_DIR), mode 0600, holding the token, its expiry, the installation
id and the permissions. A token is reused while more than REUSE_MARGIN_SECONDS remain,
and never when its permissions differ from its kind's. The cached path never runs
`security`. An `fcntl.flock` per kind makes concurrent agents mint once.

**No fallback**: any failure raises AppAuthError. Nothing here, and nothing that calls
it, answers a failure with Alex's token; the caller serves a cache or refuses. A failed
mint is remembered for FAILURE_HOLD_SECONDS across processes (`<kind>.failed.json`,
0600, the time and the redacted cause), and a token GitHub answers 401 to is dropped
from the cache (`reject`).

**Nothing here prints or returns a secret in a message.** Every error text passes
through gh.redact; the token itself leaves only through `token()`, to a request header
or a child process's environment.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import fcntl
import http.client
import json
import os
import pwd
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

from . import gh, review
from .gh import GITHUB_API, AppAuthError, redact

SWITCH_ENV = "COORD_APP"
CONFIG_ENV = "COORD_APP_CONFIG"
TOKEN_CACHE_ENV = "COORD_TOKEN_CACHE_DIR"
SECURITY_ENV = "COORD_SECURITY_BIN"
KEYCHAIN_SERVICE = "example-coord-github-app"

# Absolute paths, never looked up on PATH. The PEM goes INTO openssl, so whoever could
# choose that binary could read the key: OPENSSL is never taken from the environment.
# `security` only hands a key OUT, so a test that runs the script as a subprocess may
# replace it (security_binary): only with an app config that is not a default one by any
# spelling (under $HOME or the real home), and only for a client id that is not the real
# app's. A refused replacement raises; it never falls back to /usr/bin/security. Tests in
# this process replace the module attributes.
SECURITY = "/usr/bin/security"
OPENSSL = "/usr/bin/openssl"

KINDS = ("read", "ops")
READ_PERMISSIONS = {
    "actions": "read", "checks": "read", "contents": "read", "issues": "read",
    "metadata": "read", "pull_requests": "read", "statuses": "read",
}
# The writes coord makes as the app, and the permission each needs:
# - issues AND pull_requests write: POST issues/{n}/comments (claim, release, send, train
#   add/remove, outage), POST and DELETE issues/{n}/labels (train add/remove), POST labels
#   (the mailbox, train and conductor labels), POST issues (the mailbox and conductor
#   issues). GitHub's docs say "at least one of" the two, but it enforces the one that
#   matches the NUMBER: measured 2026-09-25 ~23:15Z with non-mutating probes, issues
#   write alone was refused (403 "Resource not accessible by integration") for a
#   comment, a label add and a label remove on PR #1046 and worked on issue #991;
#   pull_requests write alone was the mirror image; both together worked on both.
# - actions write: POST actions/workflows/{id}/dispatches (`coord ci`, the train wake).
# - metadata read: mandatory for any repository permission.
# Every read goes through the `read` token, so ops carries no read permission of its own.
OPS_PERMISSIONS = {"actions": "write", "issues": "write", "metadata": "read", "pull_requests": "write"}
PERMISSIONS = {"read": READ_PERMISSIONS, "ops": OPS_PERMISSIONS}

REUSE_MARGIN_SECONDS = 300
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
JWT_REUSE_SECONDS = 240  # one JWT signs both kinds' mints within one command
LOCK_TIMEOUT_SECONDS = 60
SUBPROCESS_TIMEOUT_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 30
# A failed mint is remembered this long across processes (`<kind>.failed.json`), so a
# locked Keychain or a refused mint costs one prompt or one stall, not one per command.
FAILURE_HOLD_SECONDS = 300

OVERRIDE = "COORD_APP=off runs coord on Alex's gh identity instead, deliberately"


# MARK: - Mode


class Mode:
    """Whether app mode is on for this command, and why (for `coord token status`)."""

    def __init__(self, on: bool, reason: str, path: str = "", config: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None):
        self.on = on
        self.reason = reason
        self.path = path
        self.config = config
        self.error = error


def default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "inisa-coord", "github-app.json")


def config_path(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return env.get(CONFIG_ENV) or default_config_path()


def real_home() -> str:
    """The invoking user's home from the password database, not $HOME: a test or a probe
    that moves HOME must not stop the real config from being recognised as the real one."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


def real_default_config_path() -> str:
    return os.path.join(real_home(), ".config", "inisa-coord", "github-app.json")


def _default_configs() -> Sequence[str]:
    """The default config under $HOME and under the real home (the same path unless HOME moved)."""
    return tuple(dict.fromkeys((default_config_path(), real_default_config_path())))


def _is_default_config(path: str) -> bool:
    """Whether `path` names a default config, however it is spelled: a symlink, a hard link,
    `..`, or other letter case (macOS volumes are case-insensitive by default, so the
    comparison is too; being wrong that way only refuses an override)."""
    for default in _default_configs():
        try:
            if os.path.samefile(path, default):
                return True
        except OSError:
            pass
        if os.path.realpath(path).lower() == os.path.realpath(default).lower():
            return True
    return False


def _default_client_ids() -> Sequence[str]:
    """The client ids of the default configs that exist (the real app's, on the Mac)."""
    ids = []
    for path in _default_configs():
        try:
            ids.append(load_config(path)["client_id"])
        except AppAuthError:
            pass
    return ids


def security_binary(env: Optional[Dict[str, str]] = None, client_id: Optional[str] = None) -> str:
    """SECURITY, or COORD_SECURITY_BIN for tests that run the `coord` script as a
    subprocess (which module patches cannot reach).

    With COORD_SECURITY_BIN unset, SECURITY. With it set, it is used only when
    COORD_APP_CONFIG names a config that is not a default config by any spelling
    (`_is_default_config`) and the client id about to be looked up is not a default
    config's (the real app's). Otherwise it raises AppAuthError: a refused override never
    falls back to /usr/bin/security, so a test or probe that set a fake binary can never
    read the real key and mint live instead.
    """
    env = os.environ if env is None else env
    override, configured = env.get(SECURITY_ENV), env.get(CONFIG_ENV)
    if not override:
        return SECURITY
    if not configured:
        refused = "COORD_APP_CONFIG is not set, so the default (real) app config is in use"
    elif _is_default_config(configured):
        refused = f"{configured} is the default app config"
    elif client_id is None:
        refused = "no client id was named"
    elif client_id in _default_client_ids():
        refused = f"client id {client_id} is the real app's"
    else:
        return override
    raise AppAuthError(f"COORD_SECURITY_BIN is set but refused for this config: {refused}; "
                       "it never falls back to /usr/bin/security (unset it to use the Keychain)")


def token_cache_dir(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return env.get(TOKEN_CACHE_ENV) or os.path.join(os.path.expanduser("~"), "Library", "Caches", "inisa-coord")


def load_config(path: str) -> Dict[str, Any]:
    """The non-secret app config, checked. Raises AppAuthError naming what is wrong."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise AppAuthError(f"the app config {path} cannot be read ({type(error).__name__})") from None
    if not isinstance(data, dict):
        raise AppAuthError(f"the app config {path} is not a JSON object")
    client_id, repo = data.get("client_id"), data.get("repo")
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", client_id):
        raise AppAuthError(f"the app config {path} has no usable client_id")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo):
        raise AppAuthError(f"the app config {path} has no usable repo (owner/name)")
    login = data.get("app_login")
    return {"client_id": client_id, "repo": repo, "app_login": login if isinstance(login, str) else "",
            "app_id": data.get("app_id") if isinstance(data.get("app_id"), int) else None}


def detect(repo: str, env: Optional[Dict[str, str]] = None) -> Mode:
    """App mode for `repo`: on only when every condition in the module doc holds.

    A config that exists but cannot be used is app mode ON with an error: every call
    then refuses. Absent is not failed; broken is.
    """
    env = os.environ if env is None else env
    path = config_path(env)
    if env.get(SWITCH_ENV) == "off":
        return Mode(False, "COORD_APP=off: the deliberate override, Alex's gh/keyring identity", path)
    if env.get("GITHUB_ACTIONS") == "true":
        return Mode(False, "in GitHub Actions, where the train keeps GITHUB_TOKEN", path)
    api = (env.get("GITHUB_API_URL") or GITHUB_API).rstrip("/")
    if api != GITHUB_API:
        # The app's JWT and tokens go to GitHub and nowhere else (a local test port, a
        # proxy, a typo), so any other API URL means Alex's identity, as before the app.
        return Mode(False, f"GITHUB_API_URL is {api}, not {GITHUB_API}; app credentials go to GitHub only", path)
    if not os.path.lexists(path):
        return Mode(False, f"no app config at {path} (cloud sessions and machines without the app)", path)
    try:
        config = load_config(path)
    except AppAuthError as error:
        return Mode(True, "the app config exists but cannot be used", path, error=str(error))
    if config["repo"].lower() != repo.lower():
        return Mode(False, f"the app is installed on {config['repo']} only, not {repo}", path, config)
    return Mode(True, f"app config {path}", path, config)


def for_mode(mode: Mode) -> "AppTokens":
    """The token source for an app-mode command (a failing one when the config is broken).
    It talks to GITHUB_API only; `detect` turned app mode off for any other API URL."""
    if mode.error or mode.config is None:
        return Unavailable(mode.error or "no app config")
    return AppTokens(mode.config, token_cache_dir())


# MARK: - JWT


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _write_all(fd: int, data: bytes) -> None:
    """Feed the key into openssl's end of the pipe, then close ours (EOF). A reader that
    died early is openssl's error to report, not a crash here."""
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except OSError:
        pass
    finally:
        os.close(fd)


def sign(message: bytes, pem: bytes, openssl: Optional[str] = None) -> bytes:
    """RS256 signature of `message` by `openssl dgst -sha256 -sign /dev/fd/N`.

    The key goes through a pipe the child inherits (`pass_fds`), so it is never written to
    a file and never appears in an argument list.
    """
    binary = openssl or OPENSSL
    read_fd, write_fd = os.pipe()
    try:
        try:
            process = subprocess.Popen(
                [binary, "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(read_fd,),
            )
        except OSError as error:
            os.close(write_fd)
            raise AppAuthError(f"{binary} could not be started ({error.strerror})") from None
    finally:
        os.close(read_fd)
    writer = threading.Thread(target=_write_all, args=(write_fd, pem), daemon=True)
    writer.start()
    try:
        signature, errors = process.communicate(message, timeout=SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AppAuthError(f"{binary} did not sign within {SUBPROCESS_TIMEOUT_SECONDS}s") from None
    finally:
        writer.join(SUBPROCESS_TIMEOUT_SECONDS)
    if process.returncode != 0 or not signature:
        detail = redact(errors.decode(errors="replace").strip())[:200]
        raise AppAuthError(f"{binary} could not sign the app JWT (exit {process.returncode}: {detail})")
    return signature


def make_jwt(client_id: str, pem: bytes, now: float, openssl: Optional[str] = None) -> str:
    """The app's JWT: RS256, iat 60 s in the past, exp 540 s ahead (under GitHub's 10
    minutes), iss the client id (GitHub recommends it over the app id)."""
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"iat": int(now) - JWT_BACKDATE_SECONDS, "exp": int(now) + JWT_LIFETIME_SECONDS,
                                 "iss": client_id}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}"
    return f"{signing_input}.{b64url(sign(signing_input.encode('ascii'), pem, openssl))}"


# MARK: - Tokens


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def expiry_epoch(entry: Dict[str, Any]) -> Optional[float]:
    try:
        moment = review.parse_time(entry.get("expires_at"))
    except (TypeError, ValueError):
        return None
    return None if moment is None else moment.timestamp()


class AppTokens:
    """Mints, caches and hands out the app's installation tokens for one config."""

    def __init__(self, config: Dict[str, Any], cache_dir: str,
                 urlopen: Optional[Callable[..., Any]] = None, clock: Callable[[], float] = time.time):
        self.config = config
        self.cache_dir = cache_dir
        self.api = GITHUB_API  # never anything else: app credentials go to GitHub only
        self.owner, self.name = config["repo"].split("/", 1)
        self._urlopen = urlopen
        self._clock = clock
        self._installation: Optional[int] = None
        self._jwt: Optional[str] = None
        self._jwt_at = 0.0
        self._failures: Dict[str, str] = {}  # kind -> the failure repeated for the rest of the process
        self.mints = 0  # tokens this object minted (tests and `token status`)

    # MARK: public

    def token(self, kind: str) -> str:
        """The `kind` token: cached while it has more than five minutes, else minted.

        Raises AppAuthError on any failure, and repeats it for the rest of the process
        instead of asking the Keychain again: for both kinds when getting the key failed,
        for this kind alone when its own mint was refused (a refused read mint must not
        stop an ops token that works, and the reverse). A failed mint is also remembered
        for FAILURE_HOLD_SECONDS across processes (`<kind>.failed.json`), so the next
        commands refuse at once instead of prompting or stalling again.
        """
        if kind not in PERMISSIONS:
            raise ValueError(f"unknown token kind {kind!r}")
        if kind in self._failures:
            raise AppAuthError(self._failures[kind])
        try:
            entry = self.cached(kind)
            if entry is None:
                # A refused COORD_SECURITY_BIN is this process's setting, not a failed mint:
                # refuse now, before the lock, and never write it down for other processes.
                security_binary(client_id=self.config["client_id"])
                self._check_failed(kind)
                with self._lock(kind):
                    entry = self.cached(kind)  # another process may have minted meanwhile
                    if entry is None:
                        self._check_failed(kind)  # or failed meanwhile
                        entry = self._mint_remembering_failure(kind)
        except AppAuthError as error:
            message = redact(str(error))
            for failed in getattr(error, "kinds", (kind,)):
                self._failures[failed] = message
            raise AppAuthError(message) from None
        self._installation = entry["installation_id"]
        return entry["token"]

    def reject(self, kind: str, token: str, reason: str) -> None:
        """GitHub answered 401 to this kind's token (revoked, or the installation suspended):
        drop the cached token if it is still the one that was refused, and refuse this kind
        for the rest of this process. The next command mints afresh."""
        path = self.path(kind)
        entry = _read_json(path)
        if entry is not None and entry.get("token") == token:
            with contextlib.suppress(OSError):
                os.unlink(path)
        self._failures[kind] = redact(reason)

    def installation_id(self) -> Optional[int]:
        """The installation, from this process or any token cache entry of this config
        (expired ones too). Never a call: the budget principal is named without minting."""
        if self._installation is not None:
            return self._installation
        for kind in KINDS:
            entry = _read_json(self.path(kind))
            if entry and self._same_config(entry) and _is_int(entry.get("installation_id")):
                return entry["installation_id"]
        return None

    def describe(self, kind: str) -> Dict[str, Any]:
        """What `coord token status` shows for a kind. Never the token."""
        entry = _read_json(self.path(kind))
        if entry is None:
            return {"state": "none cached"}
        expires = expiry_epoch(entry)
        out = {"permissions": entry.get("permissions") if isinstance(entry.get("permissions"), dict) else None,
               "expires_at": entry.get("expires_at") if isinstance(entry.get("expires_at"), str) else None}
        if self.usable(entry, kind):
            out["state"] = "reusable"
            out["minutes_left"] = int((expires - self._clock()) // 60) if expires else None
        elif not self._same_config(entry):
            out["state"] = "for another app config (not used)"
        elif entry.get("permissions") != PERMISSIONS[kind]:
            out["state"] = "permissions differ from the kind (not used)"
        elif not self._private_file(self.path(kind)):
            out["state"] = "file not private to this user (not used)"
        else:
            out["state"] = "expired or within five minutes of expiry (the next call mints)"
        return out

    def failure(self, kind: str) -> Optional[Dict[str, Any]]:
        """A mint failure of this kind, for THIS config, still being held: {"at", "cause",
        "until"}, or None. Another config's record in a shared cache directory is ignored."""
        record = _read_json(self.failed_path(kind))
        if not record or not isinstance(record.get("cause"), str) or not self._same_config(record):
            return None
        at = record.get("at")
        now = self._clock()
        if not isinstance(at, (int, float)) or isinstance(at, bool) or not 0 <= at <= now + 60:
            return None
        if now - at >= FAILURE_HOLD_SECONDS or not self._private_file(self.failed_path(kind)):
            return None
        return {"at": at, "cause": record["cause"], "until": at + FAILURE_HOLD_SECONDS}

    def path(self, kind: str) -> str:
        return os.path.join(self.cache_dir, f"{kind}.json")

    def failed_path(self, kind: str) -> str:
        return os.path.join(self.cache_dir, f"{kind}.failed.json")

    # MARK: failures across processes

    def _check_failed(self, kind: str) -> None:
        held = self.failure(kind)
        if held is not None:
            clock = time.strftime("%H:%M:%SZ", time.gmtime(held["until"]))
            raise AppAuthError(f"{held['cause']} (a mint failed {int(self._clock() - held['at'])}s ago; the next "
                               f"try is after {clock}, or delete {self.failed_path(kind)})")

    def _mint_remembering_failure(self, kind: str) -> Dict[str, Any]:
        try:
            entry = self._mint(kind)
        except AppAuthError as error:
            for failed in getattr(error, "kinds", (kind,)):
                self._write_private(self.failed_path(failed), {
                    "at": self._clock(), "cause": redact(str(error))[:300], "stage": getattr(error, "stage", "mint"),
                    "client_id": self.config["client_id"], "repo": self.config["repo"]})
            raise
        for other in KINDS:
            record = _read_json(self.failed_path(other))
            if not record or not self._same_config(record):
                continue  # another config's hold is not ours to clear
            if other == kind or record.get("stage") == "key":
                with contextlib.suppress(OSError):
                    os.unlink(self.failed_path(other))  # the key works again
        return entry

    # MARK: cache

    def _same_config(self, entry: Dict[str, Any]) -> bool:
        return entry.get("client_id") == self.config["client_id"] and \
            str(entry.get("repo", "")).lower() == self.config["repo"].lower()

    def _private_file(self, path: str) -> bool:
        """Owned by this user and readable by nobody else: anything looser is not ours."""
        try:
            info = os.stat(path)
        except OSError:
            return False
        return info.st_uid == os.getuid() and (info.st_mode & 0o077) == 0

    def usable(self, entry: Optional[Dict[str, Any]], kind: str) -> bool:
        if not isinstance(entry, dict) or not self._same_config(entry):
            return False
        token = entry.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]+", token):
            return False
        if entry.get("permissions") != PERMISSIONS[kind] or not _is_int(entry.get("installation_id")):
            return False
        expires = expiry_epoch(entry)
        return expires is not None and expires - self._clock() > REUSE_MARGIN_SECONDS

    def cached(self, kind: str) -> Optional[Dict[str, Any]]:
        """The kind's cached entry when it may be used. Reads one file; never `security`."""
        path = self.path(kind)
        entry = _read_json(path)
        if entry is None or not self.usable(entry, kind) or not self._private_file(path):
            return None
        return entry

    def _store(self, kind: str, entry: Dict[str, Any]) -> None:
        self._write_private(self.path(kind), entry)

    def _write_private(self, path: str, data: Dict[str, Any]) -> None:
        """Atomic replace; mkstemp creates the file 0600. A cache that cannot be written
        costs a mint next time, never the command."""
        tmp = None
        try:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
            tmp = None
        except (OSError, ValueError, TypeError):
            pass
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    @contextlib.contextmanager
    def _lock(self, kind: str) -> Iterator[None]:
        try:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            fd = os.open(os.path.join(self.cache_dir, f"{kind}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise AppAuthError(f"the token cache {self.cache_dir} cannot be used ({error.strerror})") from None
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                        raise AppAuthError(f"the token cache lock failed ({error.strerror})") from None
                if time.monotonic() > deadline:
                    raise AppAuthError(f"another coord process has held the {kind} token lock for "
                                       f"{LOCK_TIMEOUT_SECONDS}s (a Keychain prompt waiting?)")
                time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    # MARK: minting

    def _private_key(self) -> bytes:
        """The PEM from the login Keychain. Only minting calls this."""
        binary = security_binary(client_id=self.config["client_id"])
        command = [binary, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", self.config["client_id"], "-w"]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=SUBPROCESS_TIMEOUT_SECONDS)
        except FileNotFoundError:
            raise AppAuthError(f"{binary} is not available (the app key lives in the macOS login Keychain)") from None
        except subprocess.TimeoutExpired:
            raise AppAuthError(f"the Keychain did not answer within {SUBPROCESS_TIMEOUT_SECONDS}s "
                               "(locked, or waiting on an access prompt)") from None
        except OSError as error:
            raise AppAuthError(f"{binary} could not be started ({error.strerror})") from None
        if result.returncode != 0:
            detail = redact(result.stderr.decode(errors="replace").strip())[:200]
            raise AppAuthError(f"the Keychain did not return the app key (security exited {result.returncode}: {detail})")
        try:
            pem = base64.b64decode(result.stdout.strip(), validate=True)
        except (binascii.Error, ValueError):
            raise AppAuthError("the Keychain item is not the base64 of a PEM key") from None
        if b"-----BEGIN" not in pem or b"PRIVATE KEY-----" not in pem:
            raise AppAuthError("the Keychain item does not decode to a PEM private key")
        return pem

    def _app_jwt(self) -> str:
        now = self._clock()
        if self._jwt is None or now - self._jwt_at > JWT_REUSE_SECONDS:
            self._jwt = make_jwt(self.config["client_id"], self._private_key(), now)
            self._jwt_at = now
        return self._jwt

    def _call(self, method: str, path: str, jwt: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """One JWT-authenticated call. These are the app's own calls, on its own limit, so
        they are not recorded against the installation's budget."""
        data = json.dumps(body).encode() if body is not None else None
        # The JWT as an unredirected header, sent through gh.app_open: a redirect to another
        # origin is refused, so the JWT never reaches anyone but GitHub.
        request = gh.app_request(method, f"{self.api}/{path}", jwt, data)
        opener = self._urlopen or gh.app_open
        try:
            with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            try:
                detail = redact(error.read().decode(errors="replace").strip())[:300]
            except Exception:  # noqa: BLE001 - an unreadable error body is still this HTTP error
                detail = "(no readable body)"
            failure = AppAuthError(f"{method} {path} answered HTTP {error.code}: {detail}")
            failure.status = error.code  # type: ignore[attr-defined]
            raise failure from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeDecodeError) as error:
            # A refused connection, a timeout, a truncated or malformed answer, bytes that
            # are not UTF-8: all the same to the caller, no token.
            raise AppAuthError(f"{method} {path} failed ({type(error).__name__}: {redact(str(error))[:200]})") from None
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            raise AppAuthError(f"{method} {path} did not answer JSON") from None

    def _discover(self, jwt: str) -> int:
        found = self._call("GET", f"repos/{self.config['repo']}/installation", jwt)
        installation = (found or {}).get("id") if isinstance(found, dict) else None
        if not _is_int(installation):
            raise AppAuthError(f"GET repos/{self.config['repo']}/installation named no installation id")
        return installation

    def _mint(self, kind: str) -> Dict[str, Any]:
        """Mint one token. Every failure is an AppAuthError, never a traceback, and says
        which kinds it holds back: a failure getting the key, the JWT or the installation
        (`stage` "key") holds both, a refused mint only this kind."""
        try:
            jwt = self._app_jwt()
            installation = self.installation_id() or self._discover(jwt)
        except Exception as error:  # noqa: BLE001 - see _staged
            raise _staged(error, "key", KINDS) from None
        try:
            return self._mint_with(kind, jwt, installation)
        except Exception as error:  # noqa: BLE001 - see _staged
            raise _staged(error, "mint", (kind,)) from None

    def _mint_with(self, kind: str, jwt: str, installation: int) -> Dict[str, Any]:
        body = {"repositories": [self.name], "permissions": PERMISSIONS[kind]}
        path = f"app/installations/{installation}/access_tokens"
        try:
            minted = self._call("POST", path, jwt, body)
        except AppAuthError as error:
            if getattr(error, "status", None) != 404:
                raise
            installation = self._discover(jwt)  # the cached installation is gone: find it again, once
            path = f"app/installations/{installation}/access_tokens"
            minted = self._call("POST", path, jwt, body)
        if not isinstance(minted, dict):
            raise AppAuthError(f"POST {path} answered no token")
        token = minted.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise AppAuthError(f"POST {path} answered no usable token")
        granted = minted.get("permissions")
        if granted != PERMISSIONS[kind]:
            asked = ", ".join(f"{k}:{v}" for k, v in sorted(PERMISSIONS[kind].items()))
            got = ", ".join(f"{k}:{v}" for k, v in sorted(granted.items())) if isinstance(granted, dict) else repr(granted)
            raise AppAuthError(f"GitHub granted the {kind} token {{{got}}}, not the {{{asked}}} asked for; not using it")
        entry = {"kind": kind, "token": token, "expires_at": minted.get("expires_at"),
                 "installation_id": installation, "permissions": granted,
                 "client_id": self.config["client_id"], "repo": self.config["repo"], "minted_at": int(self._clock())}
        if expiry_epoch(entry) is None:
            raise AppAuthError(f"POST {path} answered no usable expires_at")
        self._store(kind, entry)
        self.mints += 1
        return entry


def _staged(error: Exception, stage: str, kinds: Sequence[str]) -> AppAuthError:
    """`error` as an AppAuthError that knows its stage and the kinds it holds back. Anything
    that is not already one (http.client's IncompleteRead or BadStatusLine, a bad byte, a
    bug) becomes one with its type and a redacted message, never a traceback."""
    if isinstance(error, AppAuthError):
        failure = error
    else:
        failure = AppAuthError(f"minting failed ({type(error).__name__}: {redact(str(error))[:200]})")
    failure.stage = stage  # type: ignore[attr-defined]
    failure.kinds = tuple(kinds)  # type: ignore[attr-defined]
    return failure


class Unavailable:
    """App mode is on but cannot work (a broken config): every token request refuses."""

    def __init__(self, reason: str):
        self.reason = redact(reason)
        self.config: Optional[Dict[str, Any]] = None
        self.mints = 0

    def token(self, kind: str) -> str:
        raise AppAuthError(self.reason)

    def reject(self, kind: str, token: str, reason: str) -> None:
        pass

    def failure(self, kind: str) -> Optional[Dict[str, Any]]:
        return None

    def installation_id(self) -> Optional[int]:
        return None

    def describe(self, kind: str) -> Dict[str, Any]:
        return {"state": f"unavailable: {self.reason}"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

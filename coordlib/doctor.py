"""Read-only coordination preflight. Configuration is not proof of authorization."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Dict

from . import budget, cache, identity, review, snapshot
from .gh import GitHub, GitHubError, redact

_PLATFORMS = {"darwin": "Darwin", "linux": "Linux", "win32": "Windows", "cygwin": "CYGWIN"}


def _platform_name() -> str:
    """Platform name without spawning subprocesses (platform.system() can shell out on Windows)."""
    return _PLATFORMS.get(sys.platform, sys.platform)


def _python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def inspect(gh: GitHub, online: bool = False) -> Dict[str, Any]:
    """No model starts or GitHub writes; token minting/network require online.

    The online probe reads repository metadata using the same identity as coord.
    Successful reads do not establish write permissions or cloud compatibility.
    """
    mode = getattr(gh, "app_mode", None)
    try:
        transport = gh.transport()
        problem = None
    except GitHubError as error:
        transport, problem = None, redact(str(error))
    cached = cache.load_snapshot(gh.repo, float("inf"))
    generated = cached.get("generatedAt") if isinstance(cached, dict) else None
    try:
        parsed = review.parse_time(generated) if isinstance(generated, str) else None
    except (ValueError, OverflowError):
        parsed = None
    if mode and mode.error:
        problem = redact(mode.error)
    age = max(0, int((snapshot.now_utc() - parsed).total_seconds())) if parsed else None
    result: Dict[str, Any] = {
        "schema": 1,
        "status": "unverified",
        "agent": identity.agent_id(),
        "repo": gh.repo,
        "environment": {"platform": _platform_name(), "python": _python_version(),
                        "supported": os.name in ("posix", "nt") and sys.version_info >= (3, 9)},
        "tools": {name: shutil.which(name) is not None for name in ("git", "gh", "claude", "codex")},
        "transport": transport,
        "authentication": {"principal": gh.principal(),
                           "mode": "app" if gh.app is not None else "user",
                           "configured": transport is not None and problem is None,
                           "readVerified": False, "writeVerified": False,
                           "problem": problem},
        "graphql": "REST-only" if snapshot.rest_only(gh) else "configured-unverified",
        "cache": {"present": cached is not None, "generatedAt": generated, "ageSeconds": age},
        "apiBudget": {"core": budget.summary(gh.repo, principal=gh.principal()) if gh.principal() else None},
        "capabilities": {"coordination": "requires GitHub reads and operational writes",
                         "train": "submission requires operational writes; merging stays in Actions",
                         "claudeRuntime": "conductor",
                         "codexRuntime": "read-only diagnosis; explicit repair handoff to conductor",
                         "codexCloud": "not probed",
                         "nativeWindows": "supported (DPAPI key store, Task Scheduler installer, Job Object isolation)"},
    }
    local_app = ((getattr(mode, "config", None) or {}).get("app_login") or "") if mode else ""
    app_mode = bool(local_app) or bool(getattr(mode, "on", False)) or getattr(gh, "app", None) is not None
    result["warnings"] = []
    trusted_apps: Any = None
    if app_mode:  # only app mode reads the repository's trust settings
        try:
            trusted_apps = sorted(snapshot.trusted_app_logins())
            for login in snapshot.configured_app_logins():
                if not snapshot.is_bot_login(login):
                    result["warnings"].append(
                        f"the repository names the app login {login!r} without the [bot] suffix; it trusts nobody. "
                        f"Write the app's login as GitHub shows it, e.g. \"{login}[bot]\"")
        except Exception as error:  # noqa: BLE001 - a preflight reports, it does not fail
            result["warnings"].append(f"could not read the repository's trusted app logins: {redact(str(error))}")
        if not local_app:
            result["warnings"].append(
                "app mode is on, but the app config has no app_login: add it (the app's login, e.g. "
                "\"my-coord-app[bot]\") so doctor can check that the repository trusts what this machine posts")
    result["appLogin"] = {"local": local_app or None, "trusted": trusted_apps}
    if local_app and trusted_apps is not None and local_app.lower() not in trusted_apps:
        result["warnings"].append(
            f"this machine posts as {local_app} in app mode, but the repository trusts "
            + (", ".join(trusted_apps) if trusted_apps else "no app login")
            + f": set \"coord_app_login\": \"{local_app}\" in .coord/config.json, or its handoffs and acks will not count")
    if online:
        gh.rest("repos/{repo}")
        principal = gh.principal()
        result["authentication"]["principal"] = principal
        result["apiBudget"]["core"] = budget.summary(gh.repo, principal=principal) if principal else None
        result["authentication"]["readVerified"] = True
        result["status"] = "ok"
    if problem or not result["environment"]["supported"]:
        result["status"] = "unavailable"
    return result


def render(report: Dict[str, Any]) -> str:
    auth = report["authentication"]
    cached = report["cache"]
    return "\n".join([
        f"coord doctor: {report['status']}",
        f"agent: {report['agent']}",
        f"repo: {report['repo']}",
        f"environment: {report['environment']['platform']}, Python {report['environment']['python']}",
        "tools: " + ", ".join(f"{name} {'available' if yes else 'missing'}" for name, yes in report["tools"].items()),
        f"transport: {report['transport'] or 'unavailable'}; principal: {auth['principal'] or 'not yet known'}",
        "authentication: " + ("repository read verified" if auth["readVerified"] else "not probed (use --online)"),
        "writes: not probed",
        "cache: " + (f"generated {cached['generatedAt']}, age {cached['ageSeconds']}s" if cached["present"] else "none"),
        *([f"problem: {auth['problem']}"] if auth["problem"] else []),
        *(f"warning: {warning}" for warning in report.get("warnings") or []),
        "Codex cloud: not probed; native Windows: supported",
    ]) + "\n"

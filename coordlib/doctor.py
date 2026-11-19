"""Read-only coordination preflight. Configuration is not proof of authorization."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from typing import Any, Dict

from . import budget, cache, identity, review, snapshot
from .gh import GitHub, GitHubError, redact


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
        "environment": {"platform": platform.system(), "python": platform.python_version(),
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
        "Codex cloud: not probed; native Windows: supported",
    ]) + "\n"

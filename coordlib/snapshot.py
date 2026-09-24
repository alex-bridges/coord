"""Build the board: one JSON-serializable picture of everything in flight.

Sources, and why each is trustworthy without anyone maintaining it:
- open PRs (GraphQL): branch, head, files, CI, Codex state, train label — GitHub's own state
- Actions runs (REST): what Tycho is running and what waits — GitHub's own state
- coord comments (REST, repo-wide since a window): claims, releases, messages —
  the only hand-written records, and each is an ordinary comment
- claimed issues and branches (GraphQL, one aliased query): whether a claim's
  issue is still open and whether its branch exists

A board costs about 5 + (running workflow runs) calls; `calls` records it.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import marker, review
from .gh import GitHub

CLAIM_WINDOW_DAYS = 14
STALE_HOURS = 48
TRAIN_LABEL = "train"
MAILBOX_LABEL = "coord:mailbox"

PR_FIELDS = """
number title url isDraft headRefName headRefOid baseRefName createdAt updatedAt mergeable
labels(first: 20) { nodes { name } }
files(first: 100) { totalCount nodes { path } }
reactions(first: 50) { nodes { content createdAt user { login } } }
closingIssuesReferences(first: 10) { nodes { number } }
commits(last: 1) { nodes { commit {
  oid committedDate
  checkSuites(first: 10) { nodes { createdAt } }
  statusCheckRollup { state contexts(first: 80) { nodes {
    __typename
    ... on CheckRun { name status conclusion }
    ... on StatusContext { context state }
  } } }
} } }
reviewThreads(first: 50) { totalCount nodes { isResolved comments(first: 10) { nodes { author { login } } } } }
"""

PRS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name target { oid } }
    pullRequests(states: OPEN, first: 25, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}
""" % PR_FIELDS


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# MARK: - Fetch


def fetch_prs(gh: GitHub) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    nodes: List[Dict[str, Any]] = []
    cursor = None
    main: Dict[str, Any] = {}
    while True:
        data = gh.graphql(PRS_QUERY, {"owner": gh.owner, "name": gh.name, "cursor": cursor})
        repo = data["repository"]
        branch = repo.get("defaultBranchRef") or {}
        main = {"name": branch.get("name", "main"), "sha": (branch.get("target") or {}).get("oid", "")}
        page = repo["pullRequests"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return main, nodes


def fetch_runs(gh: GitHub) -> Dict[str, List[Dict[str, Any]]]:
    running: List[Dict[str, Any]] = []
    for page in gh.rest_paginated("repos/{repo}/actions/runs?status=in_progress&per_page=50"):
        running.extend(page.get("workflow_runs", []))
    queued: List[Dict[str, Any]] = []
    for page in gh.rest_paginated("repos/{repo}/actions/runs?status=queued&per_page=50"):
        queued.extend(page.get("workflow_runs", []))

    def summary(run: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": run.get("id"),
            "workflow": run.get("name"),
            "branch": run.get("head_branch"),
            "sha": run.get("head_sha"),
            "event": run.get("event"),
            "createdAt": run.get("created_at"),
            "startedAt": run.get("run_started_at"),
            "attempt": run.get("run_attempt"),
            "url": run.get("html_url"),
        }

    out_running = []
    for run in running:
        item = summary(run)
        jobs: List[Dict[str, Any]] = []
        for page in gh.rest_paginated(f"repos/{{repo}}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100"):
            jobs.extend(page.get("jobs", []))
        item["jobsRunning"] = [
            {
                "name": job.get("name"),
                "runner": job.get("runner_name"),
                "startedAt": job.get("started_at"),
                "step": next((s.get("name") for s in job.get("steps") or [] if s.get("status") == "in_progress"), None),
            }
            for job in jobs if job.get("status") == "in_progress"
        ]
        item["jobsQueued"] = sum(1 for job in jobs if job.get("status") in ("queued", "waiting", "pending"))
        item["jobsDone"] = sum(1 for job in jobs if job.get("status") == "completed")
        out_running.append(item)
    return {"running": out_running, "queued": [summary(run) for run in queued]}


_ISSUE_URL = re.compile(r"/issues/(\d+)$")


def fetch_coord_comments(gh: GitHub, since: dt.datetime) -> List[Dict[str, Any]]:
    """Every issue/PR conversation comment updated since `since` that carries a marker.

    One repo-wide endpoint, so a claim on any issue is found without knowing
    the issue in advance.
    """
    raw = gh.rest_paginated(f"repos/{{repo}}/issues/comments?since={iso(since)}&per_page=100")
    out = []
    for comment in raw:
        payload = marker.decode(comment.get("body"))
        if payload is None:
            continue
        match = _ISSUE_URL.search(comment.get("issue_url") or "")
        out.append({
            "id": comment.get("id"),
            "on": int(match.group(1)) if match else None,
            "createdAt": comment.get("created_at"),
            "url": comment.get("html_url"),
            "text": marker.strip(comment.get("body") or ""),
            "payload": payload,
        })
    out.sort(key=lambda c: c["id"] or 0)
    return out


def fetch_targets(gh: GitHub, numbers: Iterable[int], branches: Iterable[str]) -> Dict[str, Any]:
    """State of claimed issues and existence of claimed branches, in one query."""
    numbers = sorted(set(numbers))
    branches = sorted(set(b for b in branches if b))
    if not numbers and not branches:
        return {"issues": {}, "branches": {}}
    parts = []
    for n in numbers:
        parts.append(
            f"i{n}: issueOrPullRequest(number: {n}) {{ "
            f"... on Issue {{ number title state url }} "
            f"... on PullRequest {{ number title state url }} }}"
        )
    for index, branch in enumerate(branches):
        qualified = ("refs/heads/" + branch).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'b{index}: ref(qualifiedName: "{qualified}") {{ target {{ oid ... on Commit {{ committedDate }} }} }}')
    query = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { %s } }" % " ".join(parts)
    repo = gh.graphql(query, {"owner": gh.owner, "name": gh.name})["repository"]
    issues = {n: repo.get(f"i{n}") for n in numbers}
    found = {}
    for index, branch in enumerate(branches):
        ref = repo.get(f"b{index}")
        found[branch] = None if ref is None else {
            "sha": (ref.get("target") or {}).get("oid"),
            "committedAt": (ref.get("target") or {}).get("committedDate"),
        }
    return {"issues": issues, "branches": found}


# MARK: - Compute


def summarize_pr(pr: Dict[str, Any]) -> Dict[str, Any]:
    files = ((pr.get("files") or {}).get("nodes")) or []
    total = (pr.get("files") or {}).get("totalCount", len(files))
    return {
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("url"),
        "branch": pr.get("headRefName"),
        "head": pr.get("headRefOid"),
        "base": pr.get("baseRefName"),
        "isDraft": bool(pr.get("isDraft")),
        "mergeable": pr.get("mergeable"),
        "updatedAt": pr.get("updatedAt"),
        "labels": [l["name"] for l in ((pr.get("labels") or {}).get("nodes")) or []],
        "closes": [n["number"] for n in ((pr.get("closingIssuesReferences") or {}).get("nodes")) or []],
        "files": [f["path"] for f in files],
        "filesTruncated": total > len(files),
        "ci": review.ci_state(pr),
        "codex": review.codex_state(pr),
        "unansweredFindings": review.unanswered_findings(pr),
    }


def resolve_claims(
    comments: List[Dict[str, Any]],
    targets: Dict[str, Any],
    prs: List[Dict[str, Any]],
    now: dt.datetime,
) -> List[Dict[str, Any]]:
    """Active claims, oldest first, with their PR, branch and staleness.

    Per (issue, agent) the newest claim/release wins. Several agents with active
    claims on one issue are all shown; the earliest (lowest comment id) holds it
    and the rest are marked contested. GitHub comments have no compare-and-swap,
    so two simultaneous claims can both post — the board makes that visible
    instead of pretending it cannot happen.
    """
    latest: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for comment in comments:
        payload = comment["payload"]
        kind = payload.get("kind")
        if kind not in ("claim", "release"):
            continue
        issue = payload.get("issue")
        agent = payload.get("agent")
        if not isinstance(issue, int) or not agent:
            continue
        if kind == "release" and payload.get("everyone"):
            for key in [k for k in latest if k[0] == issue]:
                latest.pop(key)
            continue
        latest[(issue, agent)] = comment

    pr_by_branch = {pr["branch"]: pr for pr in prs}
    claims: List[Dict[str, Any]] = []
    for (issue, agent), comment in latest.items():
        payload = comment["payload"]
        if payload.get("kind") != "claim":
            continue
        target = (targets.get("issues") or {}).get(issue) or {}
        if target.get("state") in ("CLOSED", "MERGED"):
            continue
        branch = payload.get("branch") or ""
        pr = pr_by_branch.get(branch)
        branch_state = (targets.get("branches") or {}).get(branch)
        claimed_at = review.parse_time(comment["createdAt"]) or now
        activity = [claimed_at]
        if branch_state and branch_state.get("committedAt"):
            activity.append(review.parse_time(branch_state["committedAt"]) or claimed_at)
        if pr and pr.get("updatedAt"):
            activity.append(review.parse_time(pr["updatedAt"]) or claimed_at)
        last_activity = max(activity)
        claims.append({
            "issue": issue,
            "title": target.get("title", ""),
            "agent": agent,
            "branch": branch,
            "files": [str(f) for f in payload.get("files") or []],
            "note": payload.get("note", ""),
            "claimedAt": comment["createdAt"],
            "commentId": comment["id"],
            "url": comment["url"],
            "pr": pr["number"] if pr else None,
            "branchPushed": branch_state is not None,
            "lastActivity": iso(last_activity),
            "stale": pr is None and (now - last_activity) > dt.timedelta(hours=STALE_HOURS),
            "contested": False,
        })
    claims.sort(key=lambda c: c["commentId"] or 0)
    holders: Dict[int, str] = {}
    for claim in claims:
        if claim["issue"] in holders and holders[claim["issue"]] != claim["agent"]:
            claim["contested"] = True
        holders.setdefault(claim["issue"], claim["agent"])
    return claims


def _paths_overlap(a: str, b: str) -> bool:
    a = a.rstrip("/")
    b = b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlaps(prs: List[Dict[str, Any]], claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pairs of in-flight work touching the same files. A warning, never a lock."""
    items: List[Tuple[str, List[str]]] = []
    for pr in prs:
        items.append((f"PR #{pr['number']}", pr["files"]))
    for claim in claims:
        if claim["pr"] is None and claim["files"]:
            items.append((f"claim #{claim['issue']} ({claim['agent']})", claim["files"]))
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = sorted({a for a in items[i][1] for b in items[j][1] if _paths_overlap(a, b)})
            if shared:
                out.append({"a": items[i][0], "b": items[j][0], "files": shared})
    return out


def train_queue(prs: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PRs carrying the train label, in the order they were added."""
    added: Dict[int, Dict[str, Any]] = {}
    for comment in comments:
        payload = comment["payload"]
        if payload.get("kind") == "train-add" and isinstance(payload.get("pr"), int):
            added[payload["pr"]] = {"at": comment["createdAt"], "agent": payload.get("agent"), "head": payload.get("head")}
    queue = []
    for pr in prs:
        if TRAIN_LABEL not in pr["labels"]:
            continue
        entry = added.get(pr["number"], {})
        queue.append({
            "pr": pr["number"],
            "addedAt": entry.get("at"),
            "agent": entry.get("agent"),
            "headAtAdd": entry.get("head"),
            "headMoved": bool(entry.get("head")) and entry.get("head") != pr["head"],
        })
    queue.sort(key=lambda q: (q["addedAt"] or "9999", q["pr"]))
    return queue


def build(gh: GitHub, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    now = now or now_utc()
    main, pr_nodes = fetch_prs(gh)
    prs = [summarize_pr(node) for node in pr_nodes]
    runs = fetch_runs(gh)
    comments = fetch_coord_comments(gh, now - dt.timedelta(days=CLAIM_WINDOW_DAYS))
    claim_payloads = [c["payload"] for c in comments if c["payload"].get("kind") == "claim"]
    targets = fetch_targets(
        gh,
        [p["issue"] for p in claim_payloads if isinstance(p.get("issue"), int)],
        [p.get("branch") or "" for p in claim_payloads],
    )
    claims = resolve_claims(comments, targets, prs, now)
    messages = [c for c in comments if c["payload"].get("kind") == "msg"]
    return {
        "schema": 1,
        "generatedAt": iso(now),
        "repo": gh.repo,
        "main": main,
        "prs": prs,
        "claims": claims,
        "overlaps": overlaps(prs, claims),
        "train": train_queue(prs, comments),
        "runs": runs,
        "messages": messages,
        "claimComments": [c for c in comments if c["payload"].get("kind") in ("claim", "release")],
        "apiCalls": gh.calls,
    }

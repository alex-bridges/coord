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
import os
import posixpath
import re
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import marker, review
from .gh import GitHub, GitHubError, NotFound

CLAIM_WINDOW_DAYS = 14
STALE_HOURS = 48
TRAIN_LABEL = "train"
MAILBOX_LABEL = "coord:mailbox"
WAITING_RUN_STATUSES = ("queued", "waiting", "requested", "pending")

PR_FIELDS = """
number title url isDraft isCrossRepository headRefName headRefOid baseRefName createdAt updatedAt mergeable
labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
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
reviewThreads(first: 50) { totalCount pageInfo { hasNextPage endCursor } nodes { isResolved comments(first: 10) { nodes { author { login } } } } }
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


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved comments(first: 10) { nodes { author { login } } } }
      }
    }
  }
}
"""


# MARK: - Fetch


def complete_pr_node(gh: GitHub, node: Dict[str, Any]) -> None:
    """Fill in what the batched query truncates, for the few PRs that need it.

    The PR query takes the first 100 files and 50 review threads; a PR past either
    limit would otherwise show fewer overlaps or fewer unanswered findings than it
    has (Codex on #989). The extra calls are spent only on those PRs.
    """
    files = node.get("files") or {}
    if files.get("totalCount", 0) > len(files.get("nodes") or []):
        listed = gh.rest_paginated(f"repos/{{repo}}/pulls/{node['number']}/files?per_page=100")
        node["files"] = {"totalCount": len(listed), "nodes": [{"path": f["filename"]} for f in listed]}
    labels = node.get("labels") or {}
    if (labels.get("pageInfo") or {}).get("hasNextPage"):
        # Labels carry policy (needs-alex, alex-approved, train:solo): never decide on a
        # truncated list (Codex on #989).
        listed = gh.rest_paginated(f"repos/{{repo}}/issues/{node['number']}/labels?per_page=100")
        node["labels"] = {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": l["name"]} for l in listed]}
    threads = node.get("reviewThreads") or {}
    page_info = threads.get("pageInfo") or {}
    cursor = page_info.get("endCursor")
    while page_info.get("hasNextPage"):
        data = gh.graphql(THREADS_QUERY, {"owner": gh.owner, "name": gh.name, "number": node["number"], "cursor": cursor})
        more = data["repository"]["pullRequest"]["reviewThreads"]
        threads.setdefault("nodes", []).extend(more["nodes"])
        page_info = more["pageInfo"]
        cursor = page_info.get("endCursor")
    threads["pageInfo"] = {"hasNextPage": False, "endCursor": cursor}


def fetch_prs(gh: GitHub) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    return with_rest_fallback(gh, lambda: fetch_prs_graphql(gh), lambda: fetch_prs_rest(gh))


def fetch_prs_graphql(gh: GitHub) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
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
    for node in nodes:
        complete_pr_node(gh, node)
    return main, nodes


def fetch_runs(gh: GitHub) -> Dict[str, List[Dict[str, Any]]]:
    running: List[Dict[str, Any]] = []
    for page in gh.rest_paginated("repos/{repo}/actions/runs?status=in_progress&per_page=50"):
        running.extend(page.get("workflow_runs", []))
    # Every status a run can wait in, not only `queued`: a run held in `waiting`,
    # `requested` or `pending` is work in line too (Codex on #989).
    queued: List[Dict[str, Any]] = []
    for status in WAITING_RUN_STATUSES:
        for page in gh.rest_paginated(f"repos/{{repo}}/actions/runs?status={status}&per_page=50"):
            queued.extend(page.get("workflow_runs", []))
    queued.sort(key=lambda run: run.get("created_at") or "")

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
            "status": run.get("status"),
        }

    def with_jobs(run: Dict[str, Any]) -> Dict[str, Any]:
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
        item["jobsQueued"] = sum(1 for job in jobs if job.get("status") in ("queued", "waiting", "pending", "requested"))
        item["jobsDone"] = sum(1 for job in jobs if job.get("status") == "completed")
        return item

    # A run's own status says "queued" while any of its jobs still wait, even when
    # others are running on Tycho (observed 2026-09-24: main's run, 2 h old, one job
    # in progress, listed as queued). So read the jobs of every active run and call a
    # run running when one of its jobs is.
    out_running, out_queued = [], []
    for run in running + queued:
        item = with_jobs(run)
        (out_running if item["jobsRunning"] else out_queued).append(item)
    out_queued.sort(key=lambda item: item.get("createdAt") or "")
    return {"running": out_running, "queued": out_queued}


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
    """State of claimed issues and existence of claimed branches."""
    numbers = sorted(set(numbers))
    branches = sorted(set(b for b in branches if b))
    if not numbers and not branches:
        return {"issues": {}, "branches": {}}
    return with_rest_fallback(gh, lambda: fetch_targets_graphql(gh, numbers, branches),
                              lambda: fetch_targets_rest(gh, numbers, branches))


def fetch_targets_graphql(gh: GitHub, numbers: List[int], branches: List[str]) -> Dict[str, Any]:
    """The GraphQL form: every claimed issue and branch in one aliased query."""
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


# MARK: - REST fallback
#
# Claude cloud sessions reach GitHub through a proxy that refuses GraphQL (HTTP 403,
# "GitHub GraphQL is not available from Claude Code sessions; use the REST API"),
# while REST works, writes included (probed 2026-09-24). The REST path builds the SAME
# node shape the GraphQL query returns, so review.py's rules stay the one
# implementation. It costs about seven calls per open PR instead of one query for
# all of them, so it is used only where GraphQL is unavailable.


def rest_only(gh: GitHub) -> bool:
    return (os.environ.get("COORD_REST_ONLY") == "1"
            or os.environ.get("CLAUDE_CODE_REMOTE") == "true"
            or bool(getattr(gh, "graphql_blocked", False)))


def with_rest_fallback(gh: GitHub, graphql_call: Callable[[], Any], rest_call: Callable[[], Any]) -> Any:
    if rest_only(gh):
        return rest_call()
    try:
        return graphql_call()
    except GitHubError as error:
        message = str(error)
        if "403" in message and "graphql" in message.lower():
            gh.graphql_blocked = True  # type: ignore[attr-defined]
            return rest_call()
        raise


_CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")


def _upper(value: Optional[str]) -> Optional[str]:
    return value.upper() if isinstance(value, str) else value


def rest_review_threads(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Review threads rebuilt from review comments: a reply's in_reply_to_id names the
    thread's first comment. REST cannot say whether a thread was resolved, so every
    thread counts as unresolved; the house rule replies to every finding, so a
    replied thread still reads as answered."""
    threads: Dict[int, List[Dict[str, Any]]] = {}
    for comment in sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0)):
        root = comment.get("in_reply_to_id") or comment.get("id")
        threads.setdefault(root, []).append(comment)
    return [{"isResolved": False,
             "comments": {"nodes": [{"author": {"login": (c.get("user") or {}).get("login")}} for c in thread]}}
            for thread in threads.values()]


def rest_pr_node(gh: GitHub, pull: Dict[str, Any]) -> Dict[str, Any]:
    number = pull["number"]
    sha = pull["head"]["sha"]
    detail = gh.rest(f"repos/{{repo}}/pulls/{number}")  # `mergeable` is only on the single-PR form
    files = gh.rest_paginated(f"repos/{{repo}}/pulls/{number}/files?per_page=100")
    reactions = gh.rest_paginated(f"repos/{{repo}}/issues/{number}/reactions?per_page=100")
    check_runs = [r for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-runs?per_page=100")
                  for r in page.get("check_runs", [])]
    statuses = (gh.rest(f"repos/{{repo}}/commits/{sha}/status") or {}).get("statuses", [])
    suites = [c for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-suites?per_page=100")
              for c in page.get("check_suites", [])]
    committed = None
    if not suites:
        committed = ((gh.rest(f"repos/{{repo}}/commits/{sha}") or {}).get("commit") or {}).get("committer", {}).get("date")
    review_comments = gh.rest_paginated(f"repos/{{repo}}/pulls/{number}/comments?per_page=100")
    head_repo = (pull["head"].get("repo") or {}).get("full_name")
    mergeable = detail.get("mergeable")
    reaction_names = {"+1": "THUMBS_UP", "eyes": "EYES", "-1": "THUMBS_DOWN"}
    return {
        "number": number,
        "title": pull.get("title", ""),
        "url": pull.get("html_url"),
        "isDraft": bool(pull.get("draft")),
        "isCrossRepository": head_repo != gh.repo,
        "headRefName": pull["head"].get("ref"),
        "headRefOid": sha,
        "baseRefName": (pull.get("base") or {}).get("ref"),
        "createdAt": pull.get("created_at"),
        "updatedAt": pull.get("updated_at"),
        "mergeable": "MERGEABLE" if mergeable is True else "CONFLICTING" if mergeable is False else "UNKNOWN",
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": l["name"]} for l in pull.get("labels") or []]},
        "files": {"totalCount": len(files), "nodes": [{"path": f["filename"]} for f in files]},
        "reactions": {"nodes": [{"content": reaction_names.get(r.get("content"), _upper(r.get("content"))),
                                 "createdAt": r.get("created_at"), "user": {"login": (r.get("user") or {}).get("login")}}
                                for r in reactions]},
        "closingIssuesReferences": {"nodes": [{"number": int(n)} for n in _CLOSES.findall(pull.get("body") or "")]},
        "commits": {"nodes": [{"commit": {
            "oid": sha,
            "committedDate": committed,
            "checkSuites": {"nodes": [{"createdAt": c.get("created_at")} for c in suites]},
            "statusCheckRollup": {"contexts": {"nodes": (
                [{"__typename": "CheckRun", "name": r.get("name"), "status": _upper(r.get("status")),
                  "conclusion": _upper(r.get("conclusion"))} for r in check_runs]
                + [{"__typename": "StatusContext", "context": c.get("context"), "state": _upper(c.get("state"))}
                   for c in statuses]
            )}},
        }}]},
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": rest_review_threads(review_comments)},
    }


def fetch_prs_rest(gh: GitHub) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    ref = gh.rest("repos/{repo}/git/ref/heads/main") or {}
    main = {"name": "main", "sha": (ref.get("object") or {}).get("sha", "")}
    pulls = gh.rest_paginated("repos/{repo}/pulls?state=open&per_page=100")
    return main, [rest_pr_node(gh, pull) for pull in pulls]


def fetch_targets_rest(gh: GitHub, numbers: List[int], branches: List[str]) -> Dict[str, Any]:
    issues: Dict[int, Any] = {}
    for number in numbers:
        try:
            issue = gh.rest(f"repos/{{repo}}/issues/{number}")
        except NotFound:
            issues[number] = None
            continue
        state = (issue.get("state") or "").upper()
        if (issue.get("pull_request") or {}).get("merged_at"):
            state = "MERGED"
        issues[number] = {"number": number, "title": issue.get("title", ""), "state": state, "url": issue.get("html_url")}
    found: Dict[str, Any] = {}
    for branch in branches:
        try:
            data = gh.rest(f"repos/{{repo}}/branches/{urllib.parse.quote(branch, safe='/')}")
        except NotFound:
            found[branch] = None
            continue
        commit = (data or {}).get("commit") or {}
        found[branch] = {"sha": commit.get("sha"),
                         "committedAt": ((commit.get("commit") or {}).get("committer") or {}).get("date")}
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
        "fromFork": bool(pr.get("isCrossRepository")),
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
        if kind == "claim":
            # A forced claim (`claim --force`) retires the holders it names.
            for previous in payload.get("takesOverFrom") or []:
                latest.pop((issue, previous), None)
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
            "files": [n for n in (normalize_path(str(f)) for f in payload.get("files") or []) if n],
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


def normalize_path(path: str) -> str:
    """A claimed path in the repository-relative form GitHub uses for PR files.

    `./a/b`, `a//b`, `a/c/../b` and `a/b/` all become `a/b`, so a claim spelled either
    way overlaps the same PR file (Codex on #989). Returns "" for a path that leaves the
    repository or names its root; callers reject those.
    """
    text = (path or "").strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return ""
    normal = posixpath.normpath(text)
    if normal in (".", "..") or normal.startswith("../"):
        return ""
    return normal


def _paths_overlap(a: str, b: str) -> bool:
    a = a.rstrip("/")
    b = b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def work_items(prs: List[Dict[str, Any]], claims: List[Dict[str, Any]]) -> List[Tuple[str, List[str], Dict[str, Any]]]:
    """Each piece of in-flight work with every file it touches or expects to.

    A PR's entry includes the declared files of the claims linked to it: files the
    claim expects but the PR has not touched yet still count. Shared by the board's
    overlap list and by `coord claim`'s warning, so the two cannot disagree.
    """
    items: List[Tuple[str, List[str], Dict[str, Any]]] = []
    for pr in prs:
        declared = [f for c in claims if c["pr"] == pr["number"] for f in c["files"]]
        items.append((f"PR #{pr['number']}", sorted(set(pr["files"]) | set(declared)),
                      {"pr": pr["number"], "branch": pr["branch"]}))
    for claim in claims:
        if claim["pr"] is None and claim["files"]:
            items.append((f"claim #{claim['issue']} ({claim['agent']})", claim["files"],
                          {"issue": claim["issue"], "branch": claim.get("branch", "")}))
    return items


def overlapping(files: List[str], other: List[str]) -> List[str]:
    return sorted({f for f in files for g in other if _paths_overlap(f, g)})


def overlaps(prs: List[Dict[str, Any]], claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pairs of in-flight work touching the same files. A warning, never a lock."""
    items = [(label, files) for label, files, _ in work_items(prs, claims)]
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = overlapping(items[i][1], items[j][1])
            if shared:
                out.append({"a": items[i][0], "b": items[j][0], "files": shared})
    return out


def attach_substitute_reviews(prs: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> None:
    """Set each PR's `substituteReviewHead`: the head its newest review-substitute comment names."""
    latest: Dict[int, str] = {}
    for comment in comments:  # oldest first
        payload = comment["payload"]
        if payload.get("kind") == "review-substitute" and isinstance(payload.get("pr"), int):
            latest[payload["pr"]] = payload.get("head")
    for pr in prs:
        pr["substituteReviewHead"] = latest.get(pr["number"])


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
    attach_substitute_reviews(prs, comments)
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

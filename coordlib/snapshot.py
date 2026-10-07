"""Build the board: one JSON-serializable picture of everything in flight.

Sources, and why each is trustworthy without anyone maintaining it:
- open PRs (GraphQL): branch, head, files, CI, Codex state, train label — GitHub's own state
- Actions runs (REST): what Tycho is running and what waits — GitHub's own state
- coord comments (REST, repo-wide since a window): claims, releases, messages —
  the only hand-written records, and each is an ordinary comment
- claimed issues and branches (GraphQL, one aliased query): whether a claim's
  issue is still open and whether its branch exists

A board costs about 5 + (running workflow runs) + (failed check runs at PR heads, and
two more for each refused one, at most MAX_CHECK_RUN_READS a head) calls; `calls`
records it.
"""

from __future__ import annotations

import datetime as dt
import os
import posixpath
import re
import urllib.parse
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from . import lease, marker, outage, review, train
from .gh import AppAuthError, GitHub, GitHubError, NotFound, NullPageNode

CLAIM_WINDOW_DAYS = 14
STALE_HOURS = 48
TRAIN_LABEL = "train"
MAILBOX_LABEL = "coord:mailbox"
WAITING_RUN_STATUSES = ("queued", "waiting", "requested", "pending")

PR_FIELDS = """
number title url isDraft isCrossRepository headRefName headRefOid baseRefName createdAt updatedAt mergeable
labels(first: 100) { totalCount nodes { name } }
files(first: 100) { totalCount nodes { path changeType } }
reactions(first: 100) { totalCount nodes { content createdAt user { login } } }
comments(first: 10) { totalCount nodes { author { login } body } }
closingIssuesReferences(first: 10) { totalCount nodes { number repository { nameWithOwner } } }
commits(last: 1) { nodes { commit {
  oid committedDate
  checkSuites(first: 20) { totalCount nodes { createdAt } }
  statusCheckRollup { state contexts(first: 100) { totalCount nodes {
    __typename
    ... on CheckRun { id databaseId name status conclusion }
    ... on StatusContext { context state }
  } } }
} } }
reviewThreads(first: 50) { totalCount pageInfo { hasNextPage endCursor } nodes { isResolved comments(first: 50) { totalCount nodes { author { login } } } } }
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


CLAIM_PRS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url isCrossRepository headRefName updatedAt files(first: 100) { totalCount nodes { path changeType } } }
    }
  }
}
"""


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
        nodes { isResolved comments(first: 50) { totalCount nodes { author { login } } } }
      }
    }
  }
}
"""


# MARK: - Fetch


def truncated(connection: Optional[Dict[str, Any]]) -> bool:
    connection = connection or {}
    return (connection.get("totalCount", 0) > len(connection.get("nodes") or [])
            or bool((connection.get("pageInfo") or {}).get("hasNextPage")))


REACTION_NAMES = {"+1": "THUMBS_UP", "eyes": "EYES", "-1": "THUMBS_DOWN"}


# The most REST reads attach_first_annotations may spend on one commit: annotation
# reads, job records, and attempt listings together. Past it a failed run stays
# red: the cap can hold a PR, never let one through.
MAX_CHECK_RUN_READS = 10


def rest_run_node(run: Dict[str, Any]) -> Dict[str, Any]:
    """A REST check run in the GraphQL rollup's node shape, plus `annotationsCount`
    (which GraphQL lacks) so attach_first_annotations can skip a run that has none."""
    return {"__typename": "CheckRun", "databaseId": run.get("id"), "name": run.get("name"),
            "status": _upper(run.get("status")), "conclusion": _upper(run.get("conclusion")),
            "annotationsCount": (run.get("output") or {}).get("annotations_count")}


def attach_first_annotations(gh: GitHub, sha: str, contexts: List[Dict[str, Any]]) -> None:
    """Give each failed check run in a rollup at `sha` what review.never_started reads.

    The one place both transports read annotations, over REST. The GraphQL query does
    not ask for them: the nested connection is priced per PR per context, and it took
    the board query from 15 to 40 points a page for a case the rollup rarely shows.

    1. The first annotation of each run whose answer can change
       (review.needs_first_annotation), unless REST says it has none. A green head costs
       no call, and a red head one call per failed run.
    2. Only for a run whose first annotation is the refusal: ONE listing of every attempt
       of that check name at the commit (`filter=all`; the rollup and `filter=latest`
       show only the newest), and the first annotation of each other failed attempt. A
       refused re-run must not hide an earlier real failure (review of #1032). The run
       is marked review.NO_OTHER_ATTEMPT_FAILED only if every such attempt was read and
       was itself a refusal. Each refused attempt must also have an Actions job record
       proving it never received a runner or steps; annotation text alone is forgeable.

    At most MAX_CHECK_RUN_READS reads per commit. GraphQL `databaseId` is a
    32-bit Int and can be null for current 64-bit Actions IDs; resolve those
    through the REST run's matching node ID before reading annotations.
    Whatever the cap or an unresolved ID leaves unread stays red.
    """
    budget = [MAX_CHECK_RUN_READS]

    def spend() -> bool:
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return True

    missing = [c for c in contexts if c.get("__typename") == "CheckRun"
               and c.get("databaseId") is None and c.get("id")]
    if missing and spend():
        ids = {c["id"]: c for c in missing}
        for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-runs?filter=latest&per_page=100"):
            for run in page.get("check_runs", []):
                context = ids.get(run.get("node_id"))
                if context is not None:
                    context["databaseId"] = run.get("id")

    def first_annotation(run_id: Any) -> Optional[Dict[str, Any]]:
        """The run's `annotations` connection, or None when the cap is spent."""
        if not spend():
            return None
        listed = gh.rest(f"repos/{{repo}}/check-runs/{run_id}/annotations?per_page=1") or []
        return {"nodes": [{"message": a.get("message")} for a in listed[:1]]}

    def job_never_started(run_id: Any, name: str) -> bool:
        """Only an Actions job without a runner or steps can be a billing refusal."""
        if not spend():
            return False
        try:
            job = gh.rest(f"repos/{{repo}}/actions/jobs/{run_id}")
        except GitHubError:
            return False  # third-party checks and unreadable jobs remain red
        return (isinstance(job, dict) and job.get("id") == run_id
                and job.get("head_sha") == sha and job.get("name") == name
                and job.get("status") == "completed" and job.get("conclusion") == "failure"
                and job.get("runner_id") == 0 and job.get("steps") == [])

    def other_attempt_failed(context: Dict[str, Any]) -> bool:
        """False only when every other failed attempt of this check name was read and was a
        refusal too. Anything unread counts as a real failure."""
        if not spend():
            return True
        name = urllib.parse.quote(context.get("name") or "", safe="")
        attempts = [rest_run_node(r) for page in gh.rest_paginated(
                        f"repos/{{repo}}/commits/{sha}/check-runs?check_name={name}&filter=all&per_page=100")
                    for r in page.get("check_runs", [])]
        # This run's own annotation was just read, so a listing without it (empty, another
        # name, a body `gh` or the proxy mangled) is broken, not evidence (#1032 review).
        if not any(a["databaseId"] == context.get("databaseId") for a in attempts):
            return True
        for attempt in attempts:
            if attempt["databaseId"] == context.get("databaseId") or not review.needs_first_annotation(attempt):
                continue  # this run itself, or an attempt that passed, was cancelled or is running
            if attempt["databaseId"] is None or attempt.get("annotationsCount") == 0:
                return True  # failed with no refusal to read: red
            attempt["annotations"] = first_annotation(attempt["databaseId"])
            if attempt["annotations"] is None or not review.first_annotation_is_refusal(attempt):
                return True
            if not job_never_started(attempt["databaseId"], attempt["name"]):
                return True
        return False

    for context in contexts:
        run_id = context.get("databaseId")
        if run_id is None or not review.needs_first_annotation(context) or context.get("annotationsCount") == 0:
            continue
        annotations = first_annotation(run_id)
        if annotations is None:
            break  # the cap is spent: this run and every later one stay red
        context["annotations"] = annotations
        if review.first_annotation_is_refusal(context) and job_never_started(run_id, context.get("name") or ""):
            context[review.NO_OTHER_ATTEMPT_FAILED] = not other_attempt_failed(context)


def rest_contexts(gh: GitHub, sha: str) -> List[Dict[str, Any]]:
    """Every check run (newest attempt of each) and commit status at `sha`, in the GraphQL
    rollup's node shape."""
    runs = [r for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-runs?per_page=100")
            for r in page.get("check_runs", [])]
    # The list endpoint is paginated and newest first. Keep the newest state for
    # each context; an older red attempt must not override its green replacement.
    latest_statuses: Dict[str, Dict[str, Any]] = {}
    for status in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/statuses?per_page=100"):
        context = status.get("context")
        if context is not None:
            latest_statuses.setdefault(context, status)
    return ([rest_run_node(r) for r in runs]
            + [{"__typename": "StatusContext", "context": c["context"], "state": _upper(c.get("state"))}
               for c in latest_statuses.values()])


def rest_codex_summary(gh: GitHub, number: int) -> List[Dict[str, Any]]:
    """Codex's pinned summary comment on a PR, in the GraphQL comment node shape."""
    out = []
    for comment in gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?per_page=100"):
        login = (comment.get("user") or {}).get("login") or ""
        if login.startswith(review.CODEX_LOGIN_PREFIX) and review.SUMMARY_MARKER in (comment.get("body") or ""):
            out.append({"author": {"login": login}, "body": comment.get("body")})
    return out


def rest_files(gh: GitHub, number: int) -> List[str]:
    """Every path a PR touches, BOTH sides of a rename: a PR renaming A to B conflicts
    with work on A as surely as with work on B (Codex on #989)."""
    paths: List[str] = []
    for f in gh.rest_paginated(f"repos/{{repo}}/pulls/{number}/files?per_page=100"):
        paths.append(f["filename"])
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])
    return paths


def _drop_nulls(connection: Optional[Dict[str, Any]]) -> None:
    """Remove null entries from a connection's nodes in place (totalCount is kept)."""
    if connection and isinstance(connection.get("nodes"), list):
        connection["nodes"] = [n for n in connection["nodes"] if isinstance(n, dict)]


def complete_pr_node(gh: GitHub, node: Dict[str, Any]) -> None:
    """Make every list in a PR node whole before any rule reads it.

    The batched query caps each connection (files, labels, reactions, checks, review
    threads and their comments). Codex found these one at a time on #989 — files, then
    threads, then labels, then reactions and thread comments — so this is the class
    fix: every connection reports totalCount, each one that feeds a rule is completed
    over REST when cut short, and the one that cannot be completed cheaply (the
    comments inside a very long review thread) marks the node `incomplete`, which
    holds the PR in the train instead of letting a guess decide.
    """
    number = node["number"]
    incomplete: List[str] = []
    # GitHub can return a null node inside a connection with no `errors` entry (a null
    # reaction crashed the train step on 2026-09-30, run 10000098). Dropping it leaves
    # the connection shorter than its totalCount, so the completions below re-read that
    # list over REST. Where a null cannot be re-read or its position matters (the head
    # commit, a review thread or its starter), the PR is held on partial data instead.
    for key in ("files", "labels", "comments", "reactions", "closingIssuesReferences"):
        _drop_nulls(node.get(key))
    if truncated(node.get("closingIssuesReferences")):
        # Unlike files/labels/comments/reactions, this one can't be re-read over REST
        # without losing fidelity: rest_pr_node can only recover a body "Closes #N"
        # keyword, not an issue linked through the sidebar (Codex on #1218). Guessing
        # the shortened list is authoritative would let `close_issues` skip an issue
        # whose closing reference landed on the dropped node, leaving its claim open.
        incomplete.append("closing issues (GitHub returned a null entry)")
    commit_nodes = ((node.get("commits") or {}).get("nodes")) or []
    if commit_nodes and not isinstance(commit_nodes[-1], dict):
        incomplete.append("head commit (GitHub returned it empty)")
    else:
        head = (commit_nodes[-1].get("commit") or {}) if commit_nodes else {}
        _drop_nulls((head.get("statusCheckRollup") or {}).get("contexts"))
        _drop_nulls(head.get("checkSuites"))
    files = node.get("files") or {}
    if truncated(files) or any(f.get("changeType") == "RENAMED" for f in files.get("nodes") or []):
        paths = rest_files(gh, number)
        node["files"] = {"totalCount": len(paths), "nodes": [{"path": p} for p in paths]}
    if truncated(node.get("labels")):
        listed = gh.rest_paginated(f"repos/{{repo}}/issues/{number}/labels?per_page=100")
        node["labels"] = {"totalCount": len(listed), "nodes": [{"name": l["name"]} for l in listed]}
    if review.summary_reviewed_commit(node) is None and truncated(node.get("comments")):
        # Codex's summary is usually among the first comments; if not, find it.
        summaries = rest_codex_summary(gh, number)
        if summaries:
            node["comments"] = {"totalCount": len(summaries), "nodes": summaries}
    if truncated(node.get("reactions")):
        listed = gh.rest_paginated(f"repos/{{repo}}/issues/{number}/reactions?per_page=100")
        node["reactions"] = {"totalCount": len(listed), "nodes": [
            {"content": REACTION_NAMES.get(r.get("content"), _upper(r.get("content"))), "createdAt": r.get("created_at"),
             "user": {"login": (r.get("user") or {}).get("login")}} for r in listed]}
    commits = ((node.get("commits") or {}).get("nodes")) or []
    if commits and isinstance(commits[-1], dict):
        commit = commits[-1].get("commit") or {}
        sha = commit.get("oid") or node.get("headRefOid")
        rollup = commit.get("statusCheckRollup") or {}
        if truncated(rollup.get("contexts")):
            contexts = rest_contexts(gh, sha)
            rollup["contexts"] = {"totalCount": len(contexts), "nodes": contexts}
            commit["statusCheckRollup"] = rollup
        attach_first_annotations(gh, sha, ((rollup.get("contexts") or {}).get("nodes")) or [])
        if truncated(commit.get("checkSuites")):
            suites = [c for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-suites?per_page=100")
                      for c in page.get("check_suites", [])]
            commit["checkSuites"] = {"totalCount": len(suites), "nodes": [{"createdAt": c.get("created_at")} for c in suites]}
    threads = node.get("reviewThreads") or {}
    page_info = threads.get("pageInfo") or {}
    cursor = page_info.get("endCursor")
    while page_info.get("hasNextPage"):
        data = gh.graphql(THREADS_QUERY, {"owner": gh.owner, "name": gh.name, "number": number, "cursor": cursor})
        more = data["repository"]["pullRequest"]["reviewThreads"]
        threads.setdefault("nodes", []).extend(more["nodes"])
        page_info = more["pageInfo"]
        cursor = page_info.get("endCursor")
    threads["pageInfo"] = {"hasNextPage": False, "endCursor": cursor}
    thread_nodes = threads.get("nodes") or []
    if any(not isinstance(t, dict) for t in thread_nodes) or any(
            not isinstance(c, dict) for t in thread_nodes if isinstance(t, dict)
            for c in ((t.get("comments") or {}).get("nodes") or [])):
        # A null thread could hide an unanswered finding, and a null comment shifts which
        # one started its thread: neither can be guessed around.
        incomplete.append("review threads (GitHub returned an empty thread or comment)")
    elif any(truncated(t.get("comments")) for t in thread_nodes):
        incomplete.append("review-thread comments (a thread longer than 50)")
    node["incomplete"] = incomplete


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
        if any(n is None for n in page["nodes"]):
            # No `errors` entry accompanies this (the same class of gap as the null
            # reaction on #1218); the node carries no number to complete over REST, and
            # dropping it would read as that PR no longer being open. Give up on GraphQL
            # for this fetch and let the REST fallback read the page directly.
            raise NullPageNode("pullRequests page held a null node with no GraphQL error")
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
    out = [c for c in map(coord_comment, raw) if c is not None]
    out.sort(key=lambda c: c["id"] or 0)
    return out


def coord_comment(comment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A REST comment in the snapshot's shape, or None if it carries no marker."""
    payload = marker.decode(comment.get("body"))
    if payload is None:
        return None
    match = _ISSUE_URL.search(comment.get("issue_url") or "")
    return {
        "id": comment.get("id"),
        "on": int(match.group(1)) if match else None,
        "createdAt": comment.get("created_at"),
        "url": comment.get("html_url"),
        "text": marker.strip(comment.get("body") or ""),
        "payload": payload,
        "author": comment_author(comment),  # attestations count only from a trusted user
    }


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
# implementation. It costs about seven calls per open PR (plus attach_first_annotations'
# reads for failed check runs, at most MAX_CHECK_RUN_READS) instead of one query for all of them, so it is
# used only where GraphQL is unavailable.
#
# The two need not see the same checks: on #1026's head (probed 2026-09-25) GraphQL's
# statusCheckRollup listed 18 contexts and left out the CoreKit Plugins suite, a
# `workflow_dispatch` run, whose check runs REST's commits/{sha}/check-runs does list.


def rest_only(gh: GitHub) -> bool:
    return (os.environ.get("COORD_REST_ONLY") == "1"
            or os.environ.get("CLAUDE_CODE_REMOTE") == "true"
            or bool(getattr(gh, "graphql_blocked", False)))


def with_rest_fallback(gh: GitHub, graphql_call: Callable[[], Any], rest_call: Callable[[], Any]) -> Any:
    if rest_only(gh):
        return rest_call()
    try:
        return graphql_call()
    except AppAuthError:
        raise  # no app token: not a proxy's GraphQL refusal
    except NullPageNode:
        # A one-off gap (gh.py), not a lasting block like the 403 case below: retry the
        # same GraphQL fetch once before paying the REST fallback's cost. Unlike a null
        # reaction or label, a null `pullRequests` page entry forces the fallback to
        # rebuild EVERY PR in the batch via REST, and REST's closingIssuesReferences can
        # only recover a body "Closes #N" keyword, not a sidebar-linked issue (#1218) —
        # so a transient glitch here would silently cost every open PR's closing-issue
        # fidelity, not just the affected one, unless it actually recurs.
        try:
            return graphql_call()
        except NullPageNode:
            return rest_call()
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
    files = rest_files(gh, number)
    reactions = gh.rest_paginated(f"repos/{{repo}}/issues/{number}/reactions?per_page=100")
    contexts = rest_contexts(gh, sha)
    attach_first_annotations(gh, sha, contexts)
    suites =[c for page in gh.rest_paginated(f"repos/{{repo}}/commits/{sha}/check-suites?per_page=100")
              for c in page.get("check_suites", [])]
    committed = None
    if not suites:
        committed = ((gh.rest(f"repos/{{repo}}/commits/{sha}") or {}).get("commit") or {}).get("committer", {}).get("date")
    review_comments = gh.rest_paginated(f"repos/{{repo}}/pulls/{number}/comments?per_page=100")
    summaries = rest_codex_summary(gh, number)
    head_repo = (pull["head"].get("repo") or {}).get("full_name")
    mergeable = detail.get("mergeable")
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
        "labels": {"nodes": [{"name": l["name"]} for l in pull.get("labels") or []]},
        "files": {"totalCount": len(files), "nodes": [{"path": f} for f in files]},
        "reactions": {"nodes": [{"content": REACTION_NAMES.get(r.get("content"), _upper(r.get("content"))),
                                 "createdAt": r.get("created_at"), "user": {"login": (r.get("user") or {}).get("login")}}
                                for r in reactions]},
        "closingIssuesReferences": {"nodes": [{"number": int(n)} for n in _CLOSES.findall(pull.get("body") or "")]},
        "commits": {"nodes": [{"commit": {
            "oid": sha,
            "committedDate": committed,
            "checkSuites": {"nodes": [{"createdAt": c.get("created_at")} for c in suites]},
            "statusCheckRollup": {"contexts": {"nodes": contexts}},
        }}]},
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": rest_review_threads(review_comments)},
        "comments": {"totalCount": len(summaries), "nodes": summaries},
        "incomplete": [],
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
        # With the repository, so the train never closes an issue of another repository by number.
        "closesRefs": [{"number": n.get("number"), "repo": ((n.get("repository") or {}).get("nameWithOwner")) or ""}
                       for n in ((pr.get("closingIssuesReferences") or {}).get("nodes")) or []],
        "files": [f["path"] for f in files],
        "filesTruncated": total > len(files),
        "ci": review.ci_state(pr),
        "codex": review.codex_state(pr),
        "unansweredFindings": review.unanswered_findings(pr),
        "incomplete": list(pr.get("incomplete") or []),
    }


def local_prs_by_branch(prs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Open PRs from THIS repository by branch name. A fork's PR can use the same
    branch name as local work; it must never be taken for it (Codex on #989)."""
    return {pr["branch"]: pr for pr in prs if not pr.get("fromFork")}


def resolve_claims(
    comments: List[Dict[str, Any]],
    targets: Dict[str, Any],
    prs: List[Dict[str, Any]],
    now: dt.datetime,
    outage_windows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Active claims, oldest first, with their PR, branch and staleness.

    The stale clock stops while the claim's agent is down in an outage (outage.py): a
    Claude claim does not go stale because the weekly usage limit fell in the middle of it.

    Per (issue, agent) the newest claim/release wins. Several agents with active
    claims on one issue are all shown; the earliest (lowest comment id) holds it
    and the rest are marked contested. GitHub comments have no compare-and-swap,
    so two simultaneous claims can both post — the board makes that visible
    instead of pretending it cannot happen.
    """
    pr_by_branch = local_prs_by_branch(prs)
    merged_by_branch = targets.get("merged") or {}
    claims: List[Dict[str, Any]] = []
    for comment in latest_claims(comments):
        payload = comment["payload"]
        issue, agent = payload["issue"], payload["agent"]
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
        merged = merged_by_branch.get(branch) if pr is None else None
        merged_pr = merged["number"] if merged and (review.parse_time(merged["mergedAt"]) or now) >= claimed_at else None
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
            # The claim's PR merged (`Refs #N`, so the issue stayed open): its work is done (#1110).
            "mergedPr": merged_pr,
            "stale": merged_pr is not None or pr is None and (
                now - last_activity - outage.paused_time(outage_windows or [], agent, last_activity, now)
                > dt.timedelta(hours=STALE_HOURS)),
            "contested": False,
        })
    claims.sort(key=lambda c: c["commentId"] or 0)
    holders: Dict[int, str] = {}
    for claim in claims:
        if claim["issue"] in holders and holders[claim["issue"]] != claim["agent"]:
            claim["contested"] = True
        holders.setdefault(claim["issue"], claim["agent"])
    return claims


def latest_claims(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The claim comments still standing: per (issue, agent) the newest claim/release wins,
    `release --everyone` clears an issue, and a claim's `takesOverFrom` retires the
    holders it names. Issue state, PRs and staleness are resolve_claims'."""
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
    return [c for c in latest.values() if c["payload"].get("kind") == "claim"]


MAX_CLOSED_PR_PAGES = 10


def fetch_merged_branches(gh: GitHub, branches: Iterable[str], since: dt.datetime) -> Dict[str, Dict[str, Any]]:
    """The newest PR from this repository merged since `since` for each of `branches`:
    branch -> {number, mergedAt} (#1110). A claim whose `Refs #N` PR merged is matched by
    branch, as the board matches an open PR.

    Closed PRs newest-updated first, a page at a time, stopping at the first PR last
    updated before `since`: a PR merged after a claim was updated after it too. Usually
    one call. Explicit pages, not rest_paginated, which would read every closed PR."""
    wanted = {b for b in branches if b}
    found: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return found
    for page in range(1, MAX_CLOSED_PR_PAGES + 1):
        pulls = gh.rest(f"repos/{{repo}}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}") or []
        for pull in pulls:
            head = pull.get("head") or {}
            branch, merged_at = head.get("ref"), pull.get("merged_at")
            if (branch in wanted and merged_at and (head.get("repo") or {}).get("full_name") == gh.repo
                    and merged_at > (found.get(branch) or {}).get("mergedAt", "")):
                found[branch] = {"number": pull["number"], "mergedAt": merged_at}
        oldest = review.parse_time((pulls[-1] if pulls else {}).get("updated_at"))
        if len(pulls) < 100 or oldest is None or oldest < since:
            break
    return found


def merged_candidates(comments: List[Dict[str, Any]], prs: List[Dict[str, Any]],
                      closed: Iterable[int] = ()) -> Tuple[List[str], Optional[dt.datetime]]:
    """The branches of standing claims on issues not in `closed` that have no open PR, and
    the earliest of those claims: what fetch_merged_branches must look for."""
    open_branches = set(local_prs_by_branch(prs))
    closed = set(closed)
    pending = [c for c in latest_claims(comments)
               if c["payload"]["issue"] not in closed and (c["payload"].get("branch") or "") not in open_branches
               and c["payload"].get("branch")]
    times = [t for t in (review.parse_time(c["createdAt"]) for c in pending) if t is not None]
    return sorted({c["payload"]["branch"] for c in pending}), (min(times) if times else None)


def claim_prs_rest(gh: GitHub) -> List[Dict[str, Any]]:
    """Open PRs with their files, for claim_view: the list, then one call per PR."""
    return [{
        "number": p["number"],
        "title": p.get("title", ""),
        "url": p.get("html_url"),
        "branch": (p.get("head") or {}).get("ref"),
        "fromFork": ((p.get("head") or {}).get("repo") or {}).get("full_name") != gh.repo,
        "updatedAt": p.get("updated_at"),
        "files": rest_files(gh, p["number"]),
    } for p in gh.rest_paginated("repos/{repo}/pulls?state=open&per_page=100")]


def claim_prs_graphql(gh: GitHub) -> List[Dict[str, Any]]:
    """claim_prs_rest's result in one query per 50 PRs; a PR with more than 100 files or
    a rename is completed over REST, as complete_pr_node does."""
    out: List[Dict[str, Any]] = []
    cursor = None
    while True:
        page = gh.graphql(CLAIM_PRS_QUERY, {"owner": gh.owner, "name": gh.name, "cursor": cursor})
        page = page["repository"]["pullRequests"]
        for node in page["nodes"]:
            files = node.get("files") or {}
            paths = ([f["path"] for f in files.get("nodes") or []]
                     if not truncated(files) and not any(f.get("changeType") == "RENAMED" for f in files.get("nodes") or [])
                     else rest_files(gh, node["number"]))
            out.append({"number": node["number"], "title": node.get("title", ""), "url": node.get("url"),
                        "branch": node.get("headRefName"), "fromFork": bool(node.get("isCrossRepository")),
                        "updatedAt": node.get("updatedAt"), "files": paths})
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def claim_view(gh: GitHub, issue: int, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """What `coord claim` reads (#1109): open PRs with their files and the standing claims,
    without the board's reviews, CI, runs, train and conductor state.

    In REST-only mode the board cost about 250 calls and 100 s (2026-09-28), mostly an
    issue and a branch read for every claim record in the window. This is one call for the
    open PRs and one per open PR for its files, the record window, one listing of the
    issues closed since the oldest standing claim, the merged-PR pages, and a branch read
    only for the claims on `issue` (their staleness is shown when they block). Where
    GraphQL works, the open PRs and their files are one query."""
    now = now or now_utc()
    prs = with_rest_fallback(gh, lambda: claim_prs_graphql(gh), lambda: claim_prs_rest(gh))
    comments = fetch_coord_comments(gh, now - dt.timedelta(days=CLAIM_WINDOW_DAYS))
    standing = latest_claims(comments)
    closed: Set[int] = set()
    times = [t for t in (review.parse_time(c["createdAt"]) for c in standing) if t is not None]
    if times:
        # A claim comment bumps its issue's updated_at, so this lists every closed issue a
        # standing claim is on (PRs too: the issues endpoint lists both).
        closed = {i["number"] for i in gh.rest_paginated(
            f"repos/{{repo}}/issues?state=closed&since={iso(min(times))}&per_page=100")}
    branches, since = merged_candidates(comments, prs, closed)
    mine = [c["payload"].get("branch") or "" for c in standing if c["payload"]["issue"] == issue]
    targets = fetch_targets(gh, [], mine)
    targets["issues"] = {n: {"state": "CLOSED"} for n in closed}
    targets["merged"] = fetch_merged_branches(gh, branches, since) if since else {}
    resolved = resolve_claims(comments, targets, prs, now, outage.windows(comments))
    return {"prs": prs, "claims": [c for c in resolved if c["mergedPr"] is None],
            "staleClaims": [c for c in resolved if c["mergedPr"] is not None]}


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
                      {"pr": pr["number"], "branch": pr["branch"], "fork": bool(pr.get("fromFork"))}))
    for claim in claims:
        if claim["pr"] is None and claim["files"]:
            items.append((f"claim #{claim['issue']} ({claim['agent']})", claim["files"],
                          {"issue": claim["issue"], "branch": claim.get("branch", "")}))
    return items


def overlapping(files: List[str], other: List[str]) -> List[str]:
    return sorted({f for f in files for g in other if _paths_overlap(f, g)})


def describe_overlap(files: List[str], other: List[str], files_label: str = "", other_label: str = "") -> List[str]:
    """The overlap of two pieces of work in words that name what each declared (#1069).

    A path both name is shown once. A directory one side declared is shown as that
    directory with the other side's paths inside it, e.g. `Tests/X/ (directory, claim #7),
    which contains: A.swift`, so a new file is not read as one the other work touches.
    Without `files_label` (`coord claim`, where `files` are the claimant's own), a
    directory of the claimant's is shown as the other side's paths `(inside Tests/X/)`.
    Bound the detail to what the board/claim command can display: nested directory
    claims must not multiply the snapshot's size by repeating every child path.
    """
    same, dirs = set(), {}  # dirs: (declaring side, directory) -> [count, up to three paths]
    other_paths = {p.rstrip("/") for p in other}
    for f in {p.rstrip("/") for p in files}:
        for g in other_paths:
            if f == g:
                same.add(f)
                continue
            elif f.startswith(g + "/"):
                key, child = ("other", g), f
            elif g.startswith(f + "/"):
                key, child = ("files", f), g
            else:
                continue
            count, sample = dirs.setdefault(key, [0, set()])
            dirs[key][0] = count + 1
            if len(sample) < 3:
                sample.add(child)
            elif child < max(sample):
                sample.remove(max(sample))
                sample.add(child)
    limit = 5  # coord claim displays five; the board displays only three
    out = sorted(same)[:limit]
    omitted = max(0, len(same) - limit)
    for (side, directory), (count, inside) in sorted(dirs.items(), key=lambda item: (item[0][1], item[0][0])):
        if len(out) == limit:
            omitted += 1
            continue
        label = (other_label if side == "other" else files_label).split(" (")[0]  # "claim #7 (agent)" -> "claim #7"
        if side == "files" and not files_label:
            names = ", ".join(sorted(inside))
            more = f", +{count - len(inside)} more" if count > len(inside) else ""
            out.append(f"{names}{more} (inside {directory}/)")
            continue
        names = ", ".join(sorted(p[len(directory) + 1:] for p in inside))
        more = f", +{count - len(inside)} more" if count > len(inside) else ""
        out.append(f"{directory}/ (directory{', ' + label if label else ''}), which contains: {names}{more}")
    if omitted:
        out.append(f"+{omitted} more overlap locations")
    return out


def overlaps(prs: List[Dict[str, Any]], claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pairs of in-flight work touching the same files. A warning, never a lock."""
    items = [(label, files) for label, files, _ in work_items(prs, claims)]
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = overlapping(items[i][1], items[j][1])
            if shared:
                entry = {"a": items[i][0], "b": items[j][0], "files": shared}
                described = describe_overlap(items[i][1], items[j][1], items[i][0], items[j][0])
                if described != shared:  # a directory on one side: say whose (#1069)
                    entry["described"] = described
                out.append(entry)
    return out


# Attestations: records that name one head, and the PR field each sets. `review-clean`
# is what `coord train review` writes; `review-substitute` is what `train
# substitute-review` wrote before 2026-09-26, and still counts for the head it names.
#
# Anyone who can comment can write a marker, bots included: the Codex connector, whose
# text a PR can steer, and coord's own app, which has issues:write. So an attestation
# counts only when GitHub names a trusted user as its comment's author (Codex P1 on
# #1054). The attestation commands always post as Alex (cli.ATTESTATIONS never run on
# the app), so every real record passes.
HEAD_RECORDS = {"review-clean": "reviewedHead", "review-substitute": "reviewedHead", "alex-approve": "alexApprovedHead"}
SIDECHAIN_LOGIN = "inisa-sidechain[bot]"  # the app that posts `@inisa-sidechain review` results (reviewer.py)


def sidechain_blocks(comment: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
    """The head a comment blocks: a `review` marker with verdict `changes`, posted by the
    app itself (anyone can write a marker; Codex on #1049). Such a head is held whatever
    else is said of it (Codex's 👍 included); only a new head answers it."""
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else ((comment.get("user") or {}).get("login"))
    if login != SIDECHAIN_LOGIN:
        return None
    if payload.get("kind") == "review" and payload.get("verdict") == "changes" and isinstance(payload.get("head"), str):
        return payload["head"]
    return None


def trusted_attesters(repo: str) -> FrozenSet[str]:
    """The logins, lower-cased, whose attestation records count: the repository's owner
    (alex-bridges for example-owner/example-repo). The one place this set is decided."""
    return frozenset({repo.split("/", 1)[0].lower()})


def comment_author(comment: Dict[str, Any]) -> Dict[str, str]:
    """A REST comment's author: its login and account type ("User", "Bot"; "" if absent)."""
    user = comment.get("user") or {}
    return {"login": str(user.get("login") or ""), "type": str(user.get("type") or "")}


def trusted_attestation(author: Optional[Dict[str, Any]], repo: str) -> bool:
    """Whether an attestation posted by `author` counts: a user, never a bot, whose
    login is in trusted_attesters(repo)."""
    login = str((author or {}).get("login") or "")
    account = str((author or {}).get("type") or "")
    if not login or login.endswith("[bot]") or account not in ("", "User"):
        return False
    return login.lower() in trusted_attesters(repo)


def attach_head_records(prs: List[Dict[str, Any]], comments: List[Dict[str, Any]], repo: str) -> None:
    """Set each PR's `reviewedHead` and `alexApprovedHead`: the head its newest
    review-clean or review-substitute / alex-approve comment names, counting only
    comments a trusted user posted (trusted_attestation) on the PR the record names,
    as train_step.train_added does."""
    latest: Dict[Tuple[int, str], str] = {}
    for comment in comments:  # oldest first
        payload = comment["payload"]
        field = HEAD_RECORDS.get(payload.get("kind"))
        on_its_pr = isinstance(payload.get("pr"), int) and comment.get("on") == payload["pr"]
        if field and on_its_pr and trusted_attestation(comment.get("author"), repo):
            latest[(payload["pr"], field)] = payload.get("head")
    # For the board; the train re-reads each queued PR's whole history (train_step.train_added),
    # so a block older than this snapshot's comment window still holds there.
    blocked: Dict[int, set] = {}
    for comment in comments:
        head = sidechain_blocks(comment, comment["payload"])
        if head and isinstance(comment["payload"].get("pr"), int):
            blocked.setdefault(comment["payload"]["pr"], set()).add(head)
    for pr in prs:
        for field in set(HEAD_RECORDS.values()):
            pr[field] = latest.get((pr["number"], field))
        pr["sidechainBlockedHeads"] = sorted(blocked.get(pr["number"], ()))


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


# The label that says a PR has a record, and the record's kind (any of them).
LABELLED_RECORDS = {
    train.REVIEWED_LABEL: ("review-clean", "review-substitute"),
    train.ALEX_APPROVED_LABEL: ("alex-approve",),
    TRAIN_LABEL: ("train-add",),
}


def with_aged_head_records(gh: GitHub, prs: List[Dict[str, Any]], comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`comments` plus the whole history of each PR whose label promises a record the
    window does not hold (#1055): `reviewed`, `alex-approved` or `train` on a PR whose
    review, approval or train-add was last touched more than CLAIM_WINDOW_DAYS ago.

    API cost: one paginated call per such PR per snapshot, and none for a PR whose
    records are all in the window, so it fires only when a head has sat that long.
    For head records and the train queue only: old claims, messages and leases stay
    out of the rest of the snapshot."""
    seen = {(c["payload"].get("pr"), c["payload"].get("kind")) for c in comments
            if c.get("on") == c["payload"].get("pr")
            and (c["payload"].get("kind") not in HEAD_RECORDS or trusted_attestation(c.get("author"), gh.repo))}
    extra: List[Dict[str, Any]] = []
    for pr in prs:
        number = pr["number"]
        if not any(label in pr["labels"] and not any((number, kind) in seen for kind in kinds)
                   for label, kinds in LABELLED_RECORDS.items()):
            continue
        for raw in gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?per_page=100"):
            comment = coord_comment(raw)
            if comment is not None and comment["on"] == number and comment["payload"].get("pr") == number:
                extra.append(comment)
    if not extra:
        return comments
    merged = {c["id"]: c for c in comments}
    merged.update({c["id"]: c for c in extra})
    return sorted(merged.values(), key=lambda c: c["id"] or 0)


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
    records = with_aged_head_records(gh, prs, comments)
    attach_head_records(prs, records, gh.repo)
    closed = [n for n, t in targets["issues"].items() if (t or {}).get("state") in ("CLOSED", "MERGED")]
    branches, since = merged_candidates(comments, prs, closed)
    targets["merged"] = fetch_merged_branches(gh, branches, since) if since else {}
    outage_windows = outage.windows(comments)
    resolved = resolve_claims(comments, targets, prs, now, outage_windows)
    # A claim whose PR merged is done: out of flight and overlaps, kept here (#1110).
    claims = [c for c in resolved if c["mergedPr"] is None]
    # A `send` message, or any other record addressed to someone (the train's held notice).
    messages = [c for c in comments if c["payload"].get("kind") == "msg" or marker.recipients(c["payload"])]
    return {
        "schema": 1,
        "generatedAt": iso(now),
        "repo": gh.repo,
        "main": main,
        "prs": prs,
        "claims": claims,
        "overlaps": overlaps(prs, claims),
        "train": train_queue(prs, records),
        "runs": runs,
        "messages": messages,
        # Outage windows of the last CLAIM_WINDOW_DAYS; `active` ones are what the board shows.
        "outages": [outage.serialize(w) for w in outage_windows],
        "conductor": {"hosts": lease.hosts(comments, now, gh.repo), "pause": lease.pause(comments, now, gh.repo),
                      "escalations": lease.escalations(comments, now, gh.repo)},
        "staleClaims": [c for c in resolved if c["mergedPr"] is not None],
        "claimComments": [c for c in comments if c["payload"].get("kind") in ("claim", "release")],
        "apiCalls": gh.calls,
    }

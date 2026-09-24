"""Command line: coord board | whoami | claim | release | send | inbox | ci."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
from typing import Any, Dict, List, NoReturn, Optional

from . import EXIT_BACKEND, EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from . import cache, identity, marker, render, review, snapshot, train, train_step
from .gh import GitHub, GitHubError, NotFound

INBOX_DEFAULT_DAYS = 7
DEFAULT_MAX_AGE = 60


class UsageError(Exception):
    pass


class ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a bad invocation; here 2 means a claim conflict, so a usage
    error must exit EXIT_USAGE (Codex on #989). Subparsers inherit this class."""

    def error(self, message: str) -> "NoReturn":  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


# MARK: - Shared helpers


def get_snapshot(gh: GitHub, max_age: float) -> Dict[str, Any]:
    if max_age > 0:
        cached = cache.load_snapshot(gh.repo, max_age)
        if cached is not None and cached.get("schema") == 1:
            return cached
    fresh = snapshot.build(gh)
    cache.store_snapshot(gh.repo, fresh)
    return fresh


def unread_direct(snap: Dict[str, Any], me: str, cursor: Optional[str], current_branch: str = "") -> int:
    """Messages addressed to `me` newer than the inbox cursor, from the snapshot alone.

    "Mine" is what `inbox_items` treats as mine: claimed issues, their PRs, and the PR
    of the current branch (an agent on a PR whose claim aged out still gets its mail).
    """
    since = review.parse_time(cursor) if cursor else None
    mine = {f"#{n}" for c in snap["claims"] if c["agent"] == me for n in (c["issue"], c["pr"]) if n}
    mine |= {f"#{pr['number']}" for pr in snap["prs"] if current_branch and pr["branch"] == current_branch}
    count = 0
    for message in snap["messages"]:
        payload = message["payload"]
        if payload.get("agent") == me:
            continue
        # `send --on N` addresses "#N": whoever works on N.
        if not any(identity.matches(str(to), me) or str(to) in mine for to in payload.get("to") or []):
            continue
        created = review.parse_time(message["createdAt"])
        if since is None or (created is not None and created > since):
            count += 1
    return count


def post_comment(gh: GitHub, number: int, text: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return gh.rest(f"repos/{{repo}}/issues/{number}/comments", method="POST", body={"body": marker.compose(text, payload)})


def find_or_create_mailbox(gh: GitHub) -> int:
    found = gh.rest(f"repos/{{repo}}/issues?labels={snapshot.MAILBOX_LABEL}&state=open&per_page=5")
    if found:
        return int(sorted(found, key=lambda issue: issue["number"])[0]["number"])
    try:
        gh.rest("repos/{repo}/labels", method="POST", body={
            "name": snapshot.MAILBOX_LABEL, "color": "5319e7",
            "description": "coord: the shared mailbox issue for agent broadcasts",
        })
    except GitHubError:
        pass  # already exists
    issue = gh.rest("repos/{repo}/issues", method="POST", body={
        "title": "Agent mailbox (coord)",
        "body": (
            "Broadcast and direct messages between agents, posted by `Tools/coord/coord send`.\n\n"
            "Each comment ends with a `<!-- coord:v1 ... -->` marker naming the sending agent and "
            "the recipients. Read yours with `Tools/coord/coord inbox`. Keep this issue open; "
            "`coord` finds it by the `coord:mailbox` label."
        ),
        "labels": [snapshot.MAILBOX_LABEL],
    })
    return int(issue["number"])


# MARK: - Commands


def cmd_whoami(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    info = {"agent": me, "repo": gh.repo, "branch": identity.git_branch() or identity.detached_description(),
            "graphql": "no (REST only)" if snapshot.rest_only(gh) else "yes", "cacheDir": cache.cache_dir(gh.repo)}
    print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
    return EXIT_OK


def cmd_board(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    snap = get_snapshot(gh, 0 if args.fresh else args.max_age)
    unread = unread_direct(snap, me, cache.load_cursor(gh.repo, me), identity.git_branch())
    if args.json:
        out = {k: v for k, v in snap.items() if k != "claimComments"}
        out["me"] = me
        out["unreadDirectMessages"] = unread
        print(json.dumps(out, indent=2))
    elif args.brief:
        sys.stdout.write(render.brief(snap, me, unread))
    else:
        sys.stdout.write(render.board(snap, me, unread))
    return EXIT_OK


def _issue_claims(gh: GitHub, number: int, prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    since = snapshot.now_utc() - dt.timedelta(days=snapshot.CLAIM_WINDOW_DAYS)
    raw = gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?since={snapshot.iso(since)}&per_page=100")
    comments = []
    for comment in raw:
        payload = marker.decode(comment.get("body"))
        if payload is not None:
            comments.append({
                "id": comment.get("id"), "on": number, "createdAt": comment.get("created_at"),
                "url": comment.get("html_url"), "text": "", "payload": payload,
            })
    comments.sort(key=lambda c: c["id"] or 0)
    targets = {"issues": {number: {"state": "OPEN"}}, "branches": {}}
    return snapshot.resolve_claims(comments, targets, prs, snapshot.now_utc())


def cmd_claim(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    branch = args.branch or identity.git_branch()
    if not branch or branch in ("main", "master"):
        raise UsageError("name the branch you will work on with --branch (you are not on a feature branch)")
    targets = snapshot.fetch_targets(gh, [args.issue], [])
    target = targets["issues"].get(args.issue)
    if target is None:
        print(f"coord: #{args.issue} does not exist in {gh.repo}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if target.get("state") != "OPEN":
        print(f"coord: #{args.issue} is {target.get('state', '').lower()}; nothing to claim", file=sys.stderr)
        return EXIT_USAGE
    snap = get_snapshot(gh, 0)
    others = [c for c in snap["claims"] if c["issue"] == args.issue and c["agent"] != me]
    if others and not args.force:
        for claim in others:
            print(
                f"coord: #{args.issue} is claimed by {claim['agent']} on {claim['branch']} "
                f"(claimed {claim['claimedAt']}, last active {claim['lastActivity']}"
                + (", STALE" if claim["stale"] else "") + f"): {claim['url']}",
                file=sys.stderr,
            )
        print("coord: message them with `coord send --on %d ...`, pick other work, or take it over with --force "
              "(say why in --note)." % args.issue, file=sys.stderr)
        return EXIT_CONFLICT

    files = []
    for raw in args.files or []:
        normal = snapshot.normalize_path(raw)
        if not normal:
            raise UsageError(f"{raw!r}: name files by their path from the repository root (no leading /, no ..)")
        files.append(normal)
    payload = {"kind": "claim", "v": 1, "agent": me, "issue": args.issue, "branch": branch, "files": files}
    if args.note:
        payload["note"] = args.note
    if others:
        payload["takesOverFrom"] = sorted({c["agent"] for c in others})
    lines = [f"**Claimed** by `{me}` on branch `{branch}`."]
    if files:
        lines.append("Expected files: " + ", ".join(f"`{f}`" for f in files))
    if args.note:
        lines.append(args.note)
    if others:
        lines.append("Takes over from: " + ", ".join(f"`{a}`" for a in payload["takesOverFrom"]))
    if args.dry_run:
        print(marker.compose("\n".join(lines), payload))
        return EXIT_OK
    posted = post_comment(gh, args.issue, "\n\n".join(lines), payload)
    cache.invalidate_snapshot(gh.repo)

    # Read back: two agents can post at the same moment. The earliest comment
    # holds the issue; a later claimant withdraws unless it forced.
    claims = _issue_claims(gh, args.issue, snap["prs"])
    holder = next((c for c in claims if c["issue"] == args.issue), None)
    if holder is not None and holder["agent"] != me and not args.force:
        post_comment(gh, args.issue, f"Withdrawn: `{holder['agent']}` claimed this first.",
                     {"kind": "release", "v": 1, "agent": me, "issue": args.issue})
        print(f"coord: {holder['agent']} claimed #{args.issue} first ({holder['url']}); withdrew yours", file=sys.stderr)
        return EXIT_CONFLICT

    print(f"claimed #{args.issue} as {me} on {branch}: {posted.get('html_url')}")
    warnings = []
    for label, other, owner in snapshot.work_items(snap["prs"], snap["claims"]):
        if owner.get("branch") == branch or owner.get("issue") == args.issue:
            continue  # this work itself
        shared = snapshot.overlapping(files, other)
        if shared:
            warnings.append(f"  {label} ({owner.get('branch')}) touches or expects: {', '.join(shared[:5])}")
    if warnings:
        print("overlaps (a warning, nothing is locked):")
        print("\n".join(warnings))
    return EXIT_OK


def cmd_release(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    payload = {"kind": "release", "v": 1, "agent": me, "issue": args.issue}
    text = f"**Released** by `{me}`."
    if args.everyone:
        payload["everyone"] = True
        text = f"**Released for everyone** by `{me}` (clears every claim on this issue)."
    if args.note:
        payload["note"] = args.note
        text += "\n\n" + args.note
    posted = post_comment(gh, args.issue, text, payload)
    cache.invalidate_snapshot(gh.repo)
    print(f"released #{args.issue}: {posted.get('html_url')}")
    return EXIT_OK


def cmd_send(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    recipients = args.to or (["all"] if args.on is None else [f"#{args.on}"])
    target = args.on if args.on is not None else find_or_create_mailbox(gh)
    payload = {"kind": "msg", "v": 1, "agent": me, "to": recipients}
    if args.on is not None:
        payload["on"] = args.on
    heading = f"**From** `{me}` **to** " + ", ".join(f"`{r}`" for r in recipients)
    posted = post_comment(gh, target, f"{heading}\n\n{args.text}", payload)
    cache.invalidate_snapshot(gh.repo)
    print(f"sent to {', '.join(recipients)} on #{target}: {posted.get('html_url')}")
    return EXIT_OK


def _number_from_url(url: str) -> Optional[int]:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def inbox_items(
    issue_comments: List[Dict[str, Any]],
    review_comments: List[Dict[str, Any]],
    snap: Dict[str, Any],
    me: str,
    since: dt.datetime,
    current_branch: str,
) -> List[Dict[str, Any]]:
    """Messages to `me`, plus comments by others on the issues and PRs `me` works on."""
    mine = set()
    for claim in snap["claims"]:
        if claim["agent"] == me:
            mine.add(claim["issue"])
            if claim["pr"]:
                mine.add(claim["pr"])
    for pr in snap["prs"]:
        if current_branch and pr["branch"] == current_branch:
            mine.add(pr["number"])

    items = []
    for comment, number, kind in (
        [(c, _number_from_url(c.get("issue_url", "")), "comment") for c in issue_comments]
        + [(c, _number_from_url(c.get("pull_request_url", "")), "review comment") for c in review_comments]
    ):
        created = review.parse_time(comment.get("created_at"))
        if created is None or created <= since:
            continue  # `since` filters on updated_at; an old comment edited later is not new
        payload = marker.decode(comment.get("body"))
        sender = (payload or {}).get("agent") or (comment.get("user") or {}).get("login") or "?"
        if payload is not None and payload.get("agent") == me:
            continue
        reason = None
        if payload is not None and payload.get("kind") == "msg":
            hits = [str(to) for to in payload.get("to") or [] if identity.matches(str(to), me)]
            if hits:
                reason = "to you" if any(h == me for h in hits) else f"to {hits[0]}"
        if reason is None and number in mine:
            reason = f"on #{number}, which you work on"
        if reason is None:
            continue
        items.append({
            "at": comment.get("created_at"),
            "from": sender,
            "on": number,
            "kind": kind,
            "reason": reason,
            "path": comment.get("path"),
            "url": comment.get("html_url"),
            "text": marker.strip(comment.get("body") or ""),
        })
    items.sort(key=lambda item: item["at"])
    return items


def cmd_inbox(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    cursor = None if args.all else (args.since or cache.load_cursor(gh.repo, me))
    since = review.parse_time(cursor) if cursor else snapshot.now_utc() - dt.timedelta(days=INBOX_DEFAULT_DAYS)
    started = snapshot.now_utc()
    snap = get_snapshot(gh, args.max_age)
    issue_comments = gh.rest_paginated(f"repos/{{repo}}/issues/comments?since={snapshot.iso(since)}&per_page=100")
    review_comments = gh.rest_paginated(f"repos/{{repo}}/pulls/comments?since={snapshot.iso(since)}&per_page=100")
    items = inbox_items(issue_comments, review_comments, snap, me, since, identity.git_branch())
    if args.json:
        print(json.dumps({"me": me, "since": snapshot.iso(since), "items": items}, indent=2))
    else:
        print(f"inbox for {me} since {snapshot.iso(since)}: {len(items)} item" + ("s" if len(items) != 1 else ""))
        for item in items:
            where = f"#{item['on']}" + (f" {item['path']}" if item.get("path") else "")
            print(f"\n[{item['at']}] {item['from']} · {item['kind']} {where} · {item['reason']}")
            text = item["text"]
            if text.startswith("**From** `"):  # the heading `send` writes for humans on GitHub
                text = text.split("\n", 1)[1].strip() if "\n" in text else ""
            text = text if args.full else render._clip(text, 400)
            print("  " + text.replace("\n", "\n  "))
            print(f"  {item['url']}")
    if not args.peek and not args.all and not args.since:
        cache.store_cursor(gh.repo, me, snapshot.iso(started))
    return EXIT_OK


def cmd_ci(args: argparse.Namespace, gh: GitHub) -> int:
    ref = args.ref or identity.git_branch()
    if not ref:
        raise UsageError("no branch: pass --ref")
    if ref.startswith(train.BATCH_PREFIX):
        raise UsageError(f"{ref} is a merge-train batch; only the train dispatches its gate")
    gh.dispatch_workflow("ci.yml", ref, {"lanes": args.lanes})
    cache.invalidate_snapshot(gh.repo)
    runs = snapshot.fetch_runs(gh)
    print(f"requested ci.yml lanes={args.lanes} on {ref}. Tycho now: "
          f"{len(runs['running'])} running, {len(runs['queued'])} queued (yours is among the queued once GitHub registers it).")
    return EXIT_OK


def cmd_train_add(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    snap = get_snapshot(gh, 0)
    pr = next((p for p in snap["prs"] if p["number"] == args.pr), None)
    if pr is None:
        print(f"coord: #{args.pr} is not an open pull request", file=sys.stderr)
        return EXIT_NOT_FOUND
    verdict, reason = train.eligibility(pr)
    if verdict == train.DROP:
        print(f"coord: #{args.pr} cannot join the train: {reason}", file=sys.stderr)
        return EXIT_CONFLICT
    text = (
        f"**Added to the merge train** by `{me}` at head `{pr['head'][:9]}`"
        + (f" ({args.note})" if args.note else "") + ".\n\n"
        "Adding asserts this PR is not a carve-out (a sound-character change, a persistence change "
        "without a migration, a public API break); those carry `needs-alex` until Alex adds "
        "`alex-approved`. A new push after this point takes the PR out of the train."
    )
    # The head marker first, the label second: the label is what queues the PR, so it
    # must never exist without the record of the head it was added at (Codex on #989).
    post_comment(gh, args.pr, text, {"kind": "train-add", "v": 1, "agent": me, "pr": args.pr, "head": pr["head"]})
    train_step.ensure_labels(gh)
    gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels", method="POST", body={"labels": [train.TRAIN_LABEL]})
    cache.invalidate_snapshot(gh.repo)
    try:
        gh.dispatch_workflow("merge-train.yml", "main")
    except GitHubError:
        pass  # the workflow runs on its own schedule too; before it reaches main it cannot be dispatched
    state = "ready now" if verdict == train.READY else f"held until: {reason}"
    print(f"added #{args.pr} at {pr['head'][:9]} to the merge train; {state}")
    return EXIT_OK


def cmd_train_remove(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    try:
        gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels/{train.TRAIN_LABEL}", method="DELETE")
    except NotFound:
        pass
    post_comment(gh, args.pr, f"**Taken out of the merge train** by `{me}`." + (f"\n\n{args.note}" if args.note else ""),
                 {"kind": "train-remove", "v": 1, "agent": me, "pr": args.pr})
    cache.invalidate_snapshot(gh.repo)
    print(f"removed #{args.pr} from the merge train")
    return EXIT_OK


def cmd_train_substitute_review(args: argparse.Namespace, gh: GitHub) -> int:
    """Codex out of quota (Alex's rule): record that a substitute review covered THIS head."""
    me = identity.agent_id()
    pr = gh.rest(f"repos/{{repo}}/pulls/{args.pr}")
    head = (pr.get("head") or {}).get("sha", "")
    text = (f"**Substitute review** recorded by `{me}` for head `{head[:9]}` (Codex unavailable).\n\n{args.note}\n\n"
            "This stands in for Codex's 👍 on this head only; a new push needs a new review.")
    post_comment(gh, args.pr, text, {"kind": "review-substitute", "v": 1, "agent": me, "pr": args.pr, "head": head})
    train_step.ensure_labels(gh)
    gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels", method="POST", body={"labels": [train.CODEX_UNAVAILABLE_LABEL]})
    cache.invalidate_snapshot(gh.repo)
    print(f"recorded a substitute review of #{args.pr} at {head[:9]}")
    return EXIT_OK


def cmd_train_status(args: argparse.Namespace, gh: GitHub) -> int:
    snap = get_snapshot(gh, args.max_age)
    prs = {p["number"]: p for p in snap["prs"]}
    # No trailing slash: the cloud proxy rejects `…/heads/train/` ("Request path could not
    # be canonicalized", probe 4). Without it GitHub also matches `training…`, so keep
    # only refs under refs/heads/train/.
    refs = gh.rest("repos/{repo}/git/matching-refs/heads/" + train.BATCH_PREFIX.rstrip("/")) or []
    refs = [ref for ref in refs if ref.get("ref", "").startswith("refs/heads/" + train.BATCH_PREFIX)]
    batches = []
    for ref in refs:
        name = ref["ref"][len("refs/heads/"):]
        tip = (ref.get("object") or {}).get("sha", "")
        pages = gh.rest_paginated(f"repos/{{repo}}/actions/workflows/{train.CI_WORKFLOW}/runs"
                                  f"?branch={urllib.parse.quote(name, safe='')}&per_page=10")
        runs = [r for page in pages for r in page.get("workflow_runs", []) if train.is_gate_run(r, tip)]
        run = max(runs, key=lambda r: r["id"]) if runs else None
        batches.append({"branch": name, "tip": tip, "run": None if run is None else {
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "attempt": run.get("run_attempt"), "url": run.get("html_url")}})
    queue = []
    for entry in snap["train"]:
        pr = prs.get(entry["pr"])
        verdict, reason = train.eligibility(pr, entry.get("headAtAdd")) if pr else (train.DROP, "not open")
        queue.append(dict(entry, verdict=verdict, reason=reason))
    if args.json:
        print(json.dumps({"batches": batches, "queue": queue}, indent=2))
        return EXIT_OK
    print("batch: " + ("none" if not batches else ""))
    for batch in batches:
        run = batch["run"]
        state = "CI not started" if run is None else f"CI {run['status']}" + (
            f" / {run['conclusion']}" if run.get("conclusion") else "") + f" (attempt {run['attempt']}) {run['url']}"
        print(f"  {batch['branch']} at {batch['tip'][:9]}: {state}")
    print("queue: " + ("empty" if not queue else ""))
    for position, entry in enumerate(queue, start=1):
        print(f"  {position}. #{entry['pr']} {entry['verdict']}" + ("" if entry["verdict"] == train.READY else f": {entry['reason']}"))
    return EXIT_OK


def cmd_train_step(args: argparse.Namespace, gh: GitHub) -> int:
    step = train_step.Step(gh, train_step.Git(args.repo_dir), args.mode, agent=identity.agent_id())
    try:
        lines = step.run()
    except train_step.GitError as error:
        print(f"coord: git: {error}", file=sys.stderr)
        return EXIT_BACKEND
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Merge train step ({args.mode})\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")
    return EXIT_OK


# MARK: - Entry


def parser() -> argparse.ArgumentParser:
    top = ArgumentParser(
        prog="coord",
        description="Shared awareness for agents in example-owner/example-repo: what is in flight, who works on what, "
                    "CI and train state, and messages. Reads GitHub; writes only marked comments.",
    )
    top.add_argument("--repo", help="owner/name (default: origin remote, else example-owner/example-repo)")
    sub = top.add_subparsers(dest="command", required=True)

    board = sub.add_parser("board", help="everything in flight: PRs, claims, overlaps, CI queue, train, inbox count")
    board.add_argument("--json", action="store_true")
    board.add_argument("--brief", action="store_true", help="a ten-line summary (session-start hook)")
    board.add_argument("--fresh", action="store_true", help="ignore the local cache")
    board.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE, help="reuse a cached board this many seconds old")
    board.set_defaults(func=cmd_board)

    whoami = sub.add_parser("whoami", help="this agent's id, repo and branch")
    whoami.add_argument("--json", action="store_true")
    whoami.set_defaults(func=cmd_whoami)

    claim = sub.add_parser("claim", help="announce that you are working on an issue")
    claim.add_argument("issue", type=int)
    claim.add_argument("--branch", help="default: the current branch")
    claim.add_argument("--files", nargs="+", metavar="PATH",
                       help="files or directories you expect to touch, as paths from the repository root")
    claim.add_argument("--note", help="one line of context")
    claim.add_argument("--force", action="store_true", help="claim even though another agent holds it (say why in --note)")
    claim.add_argument("--dry-run", action="store_true", help="print the comment instead of posting it")
    claim.set_defaults(func=cmd_claim)

    release = sub.add_parser("release", help="end your claim on an issue")
    release.add_argument("issue", type=int)
    release.add_argument("--note")
    release.add_argument("--everyone", action="store_true", help="clear every agent's claim (stale takeovers)")
    release.set_defaults(func=cmd_release)

    send = sub.add_parser("send", help="message other agents")
    send.add_argument("text")
    send.add_argument("--to", action="append", metavar="AGENT",
                      help="an agent id, a prefix ending in * (codex@*), or all; repeatable")
    send.add_argument("--on", type=int, metavar="NUMBER",
                      help="post on this issue or PR (default: the mailbox issue)")
    send.set_defaults(func=cmd_send)

    inbox = sub.add_parser("inbox", help="messages to you and comments on the work you claim")
    inbox.add_argument("--json", action="store_true")
    inbox.add_argument("--full", action="store_true", help="do not shorten long messages")
    inbox.add_argument("--peek", action="store_true", help="do not mark as read")
    inbox.add_argument("--all", action="store_true", help=f"the last {INBOX_DEFAULT_DAYS} days, ignoring what was read")
    inbox.add_argument("--since", help="ISO time; ignores and keeps the read cursor")
    inbox.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    inbox.set_defaults(func=cmd_inbox)

    ci = sub.add_parser("ci", help="queue CI lanes on a branch (heavy lanes do not run on PR pushes)")
    ci.add_argument("lanes", help="comma-separated ci.yml lanes: all, mk4, mk4-debug, mk4-release, apps, ...")
    ci.add_argument("--ref", help="default: the current branch")
    ci.set_defaults(func=cmd_ci)

    train_parser = sub.add_parser("train", help="the merge train: add, remove, status (step runs in Actions)")
    train_sub = train_parser.add_subparsers(dest="train_command", required=True)
    add = train_sub.add_parser("add", help="queue a PR; asserts it is not a carve-out")
    add.add_argument("pr", type=int)
    add.add_argument("--note")
    add.set_defaults(func=cmd_train_add)
    remove = train_sub.add_parser("remove", help="take a PR out of the train")
    remove.add_argument("pr", type=int)
    remove.add_argument("--note")
    remove.set_defaults(func=cmd_train_remove)
    substitute = train_sub.add_parser("substitute-review",
                                      help="Codex out of quota: record a substitute review of the PR's current head")
    substitute.add_argument("pr", type=int)
    substitute.add_argument("--note", required=True, help="who reviewed and where the review is")
    substitute.set_defaults(func=cmd_train_substitute_review)
    status = train_sub.add_parser("status", help="the current batch and every queued PR's readiness")
    status.add_argument("--json", action="store_true")
    status.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    status.set_defaults(func=cmd_train_status)
    step = train_sub.add_parser("step", help="advance the train by one step (merge-train.yml runs this)")
    step.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    step.add_argument("--repo-dir", default=".", help="a clone with push access (default: current directory)")
    step.set_defaults(func=cmd_train_step)
    return top


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        gh = GitHub(args.repo)
        return args.func(args, gh)
    except UsageError as error:
        print(f"coord: {error}", file=sys.stderr)
        return EXIT_USAGE
    except NotFound as error:
        print(f"coord: not found: {error}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except GitHubError as error:
        print(f"coord: GitHub error: {error}", file=sys.stderr)
        return EXIT_BACKEND

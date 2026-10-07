"""Command line: coord board | whoami | claim | release | send | inbox | ci | train | outage | conductor | token."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, NoReturn, Optional

from . import EXIT_BACKEND, EXIT_BUDGET, EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from . import apptoken, budget, cache, doctor, identity, lease, marker, outage, render, review, snapshot, train, train_metrics, train_step
from .gh import AppAuthError, GitHub, GitHubError, NotFound, redact

# Which identity each command uses in app mode. Every command not in USER_IDENTITY runs on
# the app: its reads on the read token, its writes on the ops token.
#
# Attestations, not operations: they record that Alex approved, or that a named reviewer
# reviewed, a head. `train substitute-review` is the old name of `train review`. The train
# counts an attestation only when Alex's login posted it (snapshot.trusted_attestation),
# so these must never move to the app.
ATTESTATIONS = frozenset({"train approve", "train review", "train substitute-review"})
# Commands that need what the app does not have:
# - a hand-run `train step` moves and deletes branches (Contents write; in Actions app mode
#   is off and the step uses GITHUB_TOKEN);
# - `conductor tick` lists the Actions runners (Administration read), edits the lease and
#   outage comments it posted as Alex, and gates its spending on Alex's budget
#   (conductor.Watcher.budget_left). The rest of `conductor` (status, pause, resume,
#   escalate) is a board read or a record post, like any other operation.
NEEDS_ALEX = frozenset({"train step", "conductor tick"})
# The records agents post in conversation: claims, releases, messages, outages. They run
# on the app (Alex's decision, 2026-09-25): an app-authored comment notifies Alex on
# threads he takes part in, and he handles that with notification settings. To post them
# as Alex again, add `| CONVERSATION` to USER_IDENTITY below; nothing else changes.
CONVERSATION = frozenset({"claim", "release", "send", "outage start", "outage end"})
# coord has no command that creates a PR or replies in a review thread; if one is added it
# belongs in USER_IDENTITY, because review.is_bot counts an app reply as no answer to a
# Codex finding.
USER_IDENTITY = ATTESTATIONS | NEEDS_ALEX

INBOX_DEFAULT_DAYS = 7
DEFAULT_MAX_AGE = 60
# GitHub timestamps have one-second precision, so a read that stops "now" can miss a
# comment created later in that same second. Every read looks back this far and skips
# the ids it already delivered.
CURSOR_OVERLAP = dt.timedelta(seconds=120)


class UsageError(Exception):
    pass


class BudgetRefused(Exception):
    """The API budget guard refused; the message is the one line to print (exit EXIT_BUDGET)."""


class AppRefused(Exception):
    """App mode could not get a token and there is no cache to serve; the message is the
    one line to print (exit EXIT_BACKEND). Nothing was sent as Alex."""


def app_words(error: Exception) -> str:
    """The cause of an app failure, and the deliberate override, in one line."""
    return f"GitHub App token unavailable ({redact(str(error))}); nothing was sent as Alex ({apptoken.OVERRIDE})"


def app_stale_words(error: Exception) -> str:
    """The same, short enough for the board's first line."""
    return f"GitHub App token unavailable: {render._clip(redact(str(error)), 120)}; COORD_APP=off uses Alex's gh"


class ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a bad invocation; here 2 means a claim conflict, so a usage
    error must exit EXIT_USAGE (Codex on #989). Subparsers inherit this class."""

    def error(self, message: str) -> "NoReturn":  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


# MARK: - Shared helpers


def get_snapshot(gh: GitHub, max_age: float, claim_issue: Optional[int] = None) -> Dict[str, Any]:
    """The board, from the cache when younger than `max_age`. With `claim_issue`, a fresh
    snapshot.claim_view for `coord claim` instead (#1109): open PRs' files and the claims,
    not the whole board, and not cached, since it is not one."""
    low = getattr(gh, "budget_low", None)
    if low:
        return snapshot_despite_budget(gh, low)
    unavailable = getattr(gh, "app_unavailable", None)
    if unavailable:
        return snapshot_despite_budget(gh, unavailable, AppRefused, getattr(gh, "app_stale", None))
    if claim_issue is not None:
        return snapshot.claim_view(gh, claim_issue)
    if max_age > 0:
        cached = cache.load_snapshot(gh.repo, max_age)
        if cached is not None and cached.get("schema") == 1:
            return cached
    try:
        fresh = snapshot.build(gh)
    except AppAuthError as error:
        # A cache read (board, inbox, train status) serves the cached board; anything
        # else (a claim's conflict check) must not act on a stale one, and refuses.
        if not getattr(gh, "cache_read", False):
            raise
        mark_app_unavailable(gh, error)
        return snapshot_despite_budget(gh, gh.app_unavailable, AppRefused, gh.app_stale)
    except GitHubError as error:
        if not mark_exhausted(gh, error):
            raise
        return snapshot_despite_budget(gh, gh.budget_low)
    cache.store_snapshot(gh.repo, fresh)
    return fresh


def mark_exhausted(gh: GitHub, error: GitHubError) -> bool:
    """The read that discovers an exhausted quota (#1052): in a cache read, a 403 or 429
    whose recorded headers (`_http` records them before raising) put the budget below the
    read floor marks the command budget-low, as the next call's gate would, and the caller
    serves its cache. False for any other error, or outside a cache read."""
    low = exhausted_words(gh, error) if getattr(gh, "cache_read", False) else None
    if low is None:
        return False
    gh.budget_low = low  # type: ignore[attr-defined]
    return True


def exhausted_words(gh: GitHub, error: GitHubError) -> Optional[str]:
    """The budget-low line when `error` is a 403 or 429 and the recorded reading of the
    budget it spent, this command's principal's, is now below the read floor; else None."""
    principal = gh.principal()
    if getattr(error, "status", None) not in (403, 429) or not getattr(error, "resource", None) or not principal:
        return None
    reading = budget.current(gh.repo, error.resource, principal=principal)
    if reading is None or budget.left(reading) >= budget.read_floor():
        return None
    return budget.low_words(reading)


def mark_app_unavailable(gh: GitHub, error: Exception) -> None:
    """From here on this command reads only the cache: the app has no token."""
    gh.app_unavailable = app_words(error)  # type: ignore[attr-defined]
    gh.app_stale = app_stale_words(error)  # type: ignore[attr-defined]


def snapshot_despite_budget(gh: GitHub, low: str, refused: type = BudgetRefused,
                            stale: Optional[str] = None) -> Dict[str, Any]:
    """The budget is below the read floor, or the app has no token: the machine's cached
    snapshot at any age, `--max-age` and `--fresh` notwithstanding, with a one-line
    warning; no API call. The snapshot carries `stale` (its age and why), which the
    board's first line and the brief show first, so a stale board never looks current."""
    cached = cache.load_snapshot(gh.repo, float("inf"))
    if cached is None or cached.get("schema") != 1:
        raise refused(f"{low}; no cached snapshot on this machine to show")
    now = snapshot.now_utc()
    generated = review.parse_time(cached.get("generatedAt"))
    if generated is not None:
        # An invalidated snapshot's file time is the epoch; its own timestamp is the truth.
        cached["cacheAgeSeconds"] = max(0, int((now - generated).total_seconds()))
    age = render.ago(cached.get("generatedAt"), now)
    cached["stale"] = {"ago": age, "reason": stale or low}
    print(f"coord: {low}; showing the snapshot from {age} ago", file=sys.stderr)
    return cached


def gate(args: argparse.Namespace, gh: GitHub) -> Optional[int]:
    """The API budget guard, before a command spends anything (budget.py).

    The reading is the principal's this command spends: the app's in app mode (none yet
    before the first token names the installation), else Alex's. On the gh transport,
    which sees no headers, a reading older than budget.PROBE_AFTER_SECONDS is refreshed
    with one probe call first; the app's calls are HTTPS and record their own. A LOW read
    runs from the cache (`gh.budget_low`); a refusal returns EXIT_BUDGET. Ungated commands
    (`send`, `whoami`, `train step`, `token status`) are never checked and never probe.
    """
    command = budget.command_name(args)
    if command == "doctor" and not args.online:
        return None
    if command == "conductor diagnose" and args.input and not args.handoff:
        return None
    if command in budget.UNGATED or (command.startswith("token ") and gh.app is None):
        return None  # a `token` command with app mode off refuses by itself, without a probe
    try:
        transport = gh.transport()
    except GitHubError:
        transport = None
    principal = gh.principal()
    if transport == "gh" and principal == budget.USER and budget.needs_probe(cache.load_budget(gh.repo).get("core")):
        gh.probe_budget()
    reading = budget.current(gh.repo, principal=principal) if principal else None
    verdict, message = budget.decision(command, reading)
    if command in ("doctor", "train report", "conductor diagnose") and verdict == budget.LOW:
        verdict = budget.REFUSE  # explicit probes/reports cannot be reconstructed from the board cache
    if verdict == budget.REFUSE:
        return report_error(args, EXIT_BUDGET, "budget", message)
    if verdict == budget.LOW:
        gh.budget_low = message  # type: ignore[attr-defined]
    return None


def unread_direct(snap: Dict[str, Any], me: str, cursor: Optional[str], current_branch: str = "",
                  seen_ids: Optional[List[int]] = None) -> int:
    """Messages addressed to `me` newer than the inbox cursor, from the snapshot alone.

    "Mine" is what `inbox_items` treats as mine: claimed issues, their PRs, and the PR
    of the current branch (an agent on a PR whose claim aged out still gets its mail).
    """
    since = (review.parse_time(cursor) - CURSOR_OVERLAP) if cursor else None
    seen = set(seen_ids or [])
    mine = {f"#{n}" for c in snap["claims"] if c["agent"] == me for n in (c["issue"], c["pr"]) if n}
    local = snapshot.local_prs_by_branch(snap["prs"])
    if current_branch and current_branch in local:
        mine.add(f"#{local[current_branch]['number']}")
    count = 0
    for message in snap["messages"]:
        payload = message["payload"]
        if payload.get("agent") == me:
            continue
        # `send --on N` addresses "#N": whoever works on N.
        if not any(identity.matches(to, me) or to in mine for to in marker.recipients(payload)):
            continue
        if message.get("id") in seen:
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
    except AppAuthError:
        raise  # no app token is not "the label exists"
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


def cmd_doctor(args: argparse.Namespace, gh: GitHub) -> int:
    report = doctor.inspect(gh, online=args.online)
    print(json.dumps(report, indent=2) if args.json else doctor.render(report), end="\n" if args.json else "")
    return EXIT_BACKEND if report["status"] == "unavailable" else EXIT_OK


def identity_words(gh: GitHub) -> str:
    """Whose identity coord's calls use, and why. Reads the config and token cache only."""
    mode = getattr(gh, "app_mode", None)
    app = getattr(gh, "app", None)
    if app is None:
        why = f" ({mode.reason})" if mode is not None else ""
        return f"Alex's gh/keyring login, or GH_TOKEN{why}"
    login = ((getattr(app, "config", None) or {}).get("app_login")) or "the GitHub App"
    installation = app.installation_id()
    where = f"installation {installation}" if installation else "installation not yet known"
    return (f"{login} ({where}) for reads and operational writes; Alex for "
            + ", ".join(f"`{c}`" for c in sorted(USER_IDENTITY)))


def budget_words(gh: GitHub) -> str:
    """The budget line: the principal coord spends, and in app mode Alex's as well."""
    return budget.header_words(gh.repo, principal=gh.principal() if getattr(gh, "app", None) is not None else budget.USER)


def cmd_whoami(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    info = {"agent": me, "repo": gh.repo, "branch": identity.git_branch() or identity.detached_description(),
            "graphql": "no (REST only)" if snapshot.rest_only(gh) else "yes", "cacheDir": cache.cache_dir(gh.repo),
            "githubIdentity": identity_words(gh), "apiBudget": budget_words(gh)}
    print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
    return EXIT_OK


def cmd_board(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    snap = get_snapshot(gh, 0 if args.fresh else args.max_age)
    unread = unread_direct(snap, me, cache.load_cursor(gh.repo, me), identity.git_branch(),
                           cache.load_seen_ids(gh.repo, me))
    # The budget as last read, not as stamped on a cached snapshot; it costs no call.
    words = budget_words(gh)
    if args.json:
        out = {k: v for k, v in snap.items() if k != "claimComments"}
        out["me"] = me
        out["unreadDirectMessages"] = unread
        principal = gh.principal() if getattr(gh, "app", None) is not None else budget.USER
        out["apiBudget"] = {resource: budget.summary(gh.repo, resource, principal=principal) if principal else None
                            for resource in ("core", "graphql")}
        out["apiBudget"]["principal"] = principal
        if principal != budget.USER:
            out["apiBudget"]["user"] = {resource: budget.summary(gh.repo, resource) for resource in ("core", "graphql")}
        print(json.dumps(out, indent=2))
    elif args.brief:
        sys.stdout.write(render.brief(snap, me, unread, budget=words))
    else:
        sys.stdout.write(render.board(snap, me, unread, budget=words))
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
    snap = get_snapshot(gh, 0, claim_issue=args.issue)
    others = [c for c in snap["claims"] + (snap.get("staleClaims") or [])
              if c["issue"] == args.issue and c["agent"] != me]
    live = [c for c in others if not c.get("mergedPr")]
    if live and not args.force:
        for claim in live:
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
    for raw in (part.strip() for arg in args.files or [] for part in arg.split(",")):
        if not raw:
            continue  # `--files a,b` and `--files a, b` both name two paths (#1083)
        normal = snapshot.normalize_path(raw)
        if not normal:
            raise UsageError(f"{raw!r}: name files by their path from the repository root (no leading /, no ..)")
        files.append(normal)
    if args.branch is None and _commits_ahead_of_main() == 0 and not any(
            pr.get("branch") == branch and not pr.get("fromFork") for pr in snap["prs"]):
        # A cloud session starts on a harness branch it never works on (#1084).
        print(f"coord: claiming on {branch}, which has no work yet; pass --branch <name> if you'll work elsewhere",
              file=sys.stderr)
    payload = {"kind": "claim", "v": 1, "agent": me, "issue": args.issue, "branch": branch, "files": files}
    if args.note:
        payload["note"] = args.note
    if others:
        # A claim whose PR merged (#1110) is retired without --force: its work is done.
        payload["takesOverFrom"] = sorted({c["agent"] for c in others})
        for claim in others:
            if claim.get("mergedPr"):
                print(f"coord: {claim['agent']}'s claim on #{args.issue} is stale (PR #{claim['mergedPr']} merged); "
                      "this claim retires it", file=sys.stderr)
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
        if (owner.get("branch") == branch and not owner.get("fork")) or owner.get("issue") == args.issue:
            continue  # this work itself (a fork's PR with the same branch name is not)
        shared = snapshot.describe_overlap(files, other)
        if shared:
            warnings.append(f"  {label} ({owner.get('branch')}) touches or expects: {'; '.join(shared[:5])}")
    if warnings:
        print("overlaps (a warning, nothing is locked):")
        print("\n".join(warnings))
    return EXIT_OK


def _commits_ahead_of_main() -> Optional[int]:
    """Commits HEAD has that origin/main lacks; None when git cannot say."""
    try:
        done = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None


def cmd_release(args: argparse.Namespace, gh: GitHub) -> int:
    me = identity.agent_id()
    if not args.everyone:
        mine = [c for c in _issue_claims(gh, args.issue, []) if c["agent"] == me]
        if not mine:
            print(f"coord: {me} holds no claim on #{args.issue}; nothing to release", file=sys.stderr)
            return EXIT_NOT_FOUND
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
    if args.on is not None and not args.to and args.on in known_mailboxes(gh.repo):
        # --on N alone reaches whoever works on N; nobody works on the mailbox (#1070).
        raise UsageError(f"#{args.on} is the mailbox issue: nobody works on it, so --on alone reaches no one. "
                         "Name the recipient with --to <agent> (the sender of the message you answer).")
    recipients = args.to or (["all"] if args.on is None else [f"#{args.on}"])
    target = args.on if args.on is not None else find_or_create_mailbox(gh)
    payload = {"kind": "msg", "v": 1, "agent": me, "to": recipients}
    if args.on is not None:
        payload["on"] = args.on
    else:
        cache.remember_mailboxes(gh.repo, [target])
    heading = f"**From** `{me}` **to** " + ", ".join(f"`{r}`" for r in recipients)
    posted = post_comment(gh, target, f"{heading}\n\n{args.text}", payload)
    cache.invalidate_snapshot(gh.repo)
    print(f"sent to {', '.join(recipients)} on #{target}: {posted.get('html_url')}")
    for line in down_warnings(gh.repo, recipients):
        print(line)
    return EXIT_OK


def known_mailboxes(repo: str) -> set:
    """Mailbox issue numbers this machine has seen: on its cached board, or remembered by an
    `inbox` read or a `send` (a message sent without --on lands on the mailbox). The board
    alone misses a mailbox whose first message in its 14-day window arrived after it was
    cached, which `inbox` then read directly (Codex on #1098). No call (`send` must stay
    cheap and always run), so a cold cache knows none."""
    cached = cache.load_snapshot(repo, float("inf"))
    return mailbox_numbers((cached or {}).get("messages") or [], repo) | set(cache.load_mailboxes(repo))


def mailbox_numbers(messages: List[Dict[str, Any]], repo: str) -> set:
    """Trusted mailbox posts only; a marker's shape cannot authenticate its author."""
    return {m.get("on") for m in messages if isinstance(m.get("on"), int)
            and (m.get("payload") or {}).get("kind") == "msg" and "on" not in (m.get("payload") or {})
            and (snapshot.trusted_attestation(m.get("author"), repo)
                 or (m.get("author") or {}).get("login") == snapshot.SIDECHAIN_LOGIN)}


def down_warnings(repo: str, recipients: List[str]) -> List[str]:
    """Recipients whose harness is in an outage, from this machine's cached board (no call:
    `send` must stay cheap and always run)."""
    cached = cache.load_snapshot(repo, float("inf"))
    if not cached:
        return []
    now = snapshot.now_utc()
    windows = [outage.deserialize(w) for w in cached.get("outages") or []]
    out = []
    for address in recipients:
        window = outage.down(windows, address, now)
        if window is not None:
            out.append(f"note: {window['harness']} agents are down until {window['end'].strftime('%a %H:%MZ')}; "
                       f"{address} will read this after that")
    return out



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
    seen_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Messages to `me`, plus comments by others on the issues and PRs `me` works on."""
    mine = set()
    for claim in snap["claims"]:
        if claim["agent"] == me:
            mine.add(claim["issue"])
            if claim["pr"]:
                mine.add(claim["pr"])
    local = snapshot.local_prs_by_branch(snap["prs"])
    if current_branch and current_branch in local:
        mine.add(local[current_branch]["number"])
    seen = set(seen_ids or [])

    items = []
    for comment, number, kind in (
        [(c, _number_from_url(c.get("issue_url", "")), "comment") for c in issue_comments]
        + [(c, _number_from_url(c.get("pull_request_url", "")), "review comment") for c in review_comments]
    ):
        if comment.get("id") in seen:
            continue  # delivered by an earlier read inside the overlap window
        created = review.parse_time(comment.get("created_at"))
        if created is None or created <= since:
            continue  # `since` filters on updated_at; an old comment edited later is not new
        payload = marker.decode(comment.get("body"))
        sender = (payload or {}).get("agent") or (comment.get("user") or {}).get("login") or "?"
        if payload is not None and payload.get("agent") == me:
            continue
        reason = None
        if payload is not None:
            hits = [to for to in marker.recipients(payload) if identity.matches(to, me)]
            if hits:
                reason = "to you" if any(h == me for h in hits) else f"to {hits[0]}"
        if reason is None and number in mine:
            reason = f"on #{number}, which you work on"
        if reason is None:
            continue
        items.append({
            "id": comment.get("id"),
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
    if getattr(gh, "budget_low", None) or getattr(gh, "app_unavailable", None):
        return inbox_from_snapshot(args, gh, me)
    return inbox_read(args, gh, me)


def inbox_from_snapshot(args: argparse.Namespace, gh: GitHub, me: str,
                        snap: Optional[Dict[str, Any]] = None) -> int:
    """Budget low, or no app token: count what the cached snapshot holds for `me`; reading
    comments would cost calls. Nothing is marked read."""
    snap = snap if snap is not None else get_snapshot(gh, args.max_age)
    unread = unread_direct(snap, me, cache.load_cursor(gh.repo, me), identity.git_branch(),
                           cache.load_seen_ids(gh.repo, me))
    after = "after the reset" if getattr(gh, "budget_low", None) else "once the app token is back"
    if args.json:
        print(json.dumps({"schema": 1, "status": "stale", "me": me, "items": None,
                          "unreadDirectMessages": unread, "markedRead": False,
                          "generatedAt": snap.get("generatedAt"), "stale": snap.get("stale"),
                          "reason": getattr(gh, "budget_low", None) or getattr(gh, "app_unavailable", None)}, indent=2))
        return EXIT_OK
    print(f"inbox for {me}: {unread} new in that snapshot; run `coord inbox` {after} to read them "
          "(nothing was marked read)")
    return EXIT_OK


def inbox_read(args: argparse.Namespace, gh: GitHub, me: str) -> int:
    stored = None if (args.all or args.since) else cache.load_cursor(gh.repo, me)
    seen_ids = cache.load_seen_ids(gh.repo, me) if stored else []
    if args.since:
        since = review.parse_time(args.since)
    elif stored:
        since = review.parse_time(stored) - CURSOR_OVERLAP
    else:
        since = snapshot.now_utc() - dt.timedelta(days=INBOX_DEFAULT_DAYS)
    started = snapshot.now_utc()
    snap = get_snapshot(gh, args.max_age)
    if getattr(gh, "budget_low", None) or getattr(gh, "app_unavailable", None):
        # The snapshot build was the refused read (#1052): reading comments would cost more.
        return inbox_from_snapshot(args, gh, me, snap)
    try:
        issue_comments = gh.rest_paginated(f"repos/{{repo}}/issues/comments?since={snapshot.iso(since)}&per_page=100")
        review_comments = gh.rest_paginated(f"repos/{{repo}}/pulls/comments?since={snapshot.iso(since)}&per_page=100")
    except AppAuthError as error:
        if not getattr(gh, "app_unavailable", None):  # get_snapshot served a fresh cache and said nothing
            mark_app_unavailable(gh, error)
            print(f"coord: {gh.app_unavailable}; counting from the cached snapshot", file=sys.stderr)
        return inbox_from_snapshot(args, gh, me, snap)
    except GitHubError as error:
        if not mark_exhausted(gh, error):
            raise
        print(f"coord: {gh.budget_low}; counting from the cached snapshot", file=sys.stderr)
        return inbox_from_snapshot(args, gh, me, snap)
    items = inbox_items(issue_comments, review_comments, snap, me, since, identity.git_branch(), seen_ids)
    if args.json:
        print(json.dumps({"schema": 1, "status": "ok", "me": me,
                          "since": snapshot.iso(since), "items": items}, indent=2))
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
    cache.remember_mailboxes(gh.repo, sorted(mailbox_numbers(
        [{"on": _number_from_url(c.get("issue_url", "")), "payload": marker.decode(c.get("body")),
          "author": snapshot.comment_author(c)} for c in issue_comments], gh.repo)))
    if not args.peek and not args.all and not args.since:
        window = started - CURSOR_OVERLAP
        # Earlier ids matter only while the earlier read's window overlaps the new one.
        previous = seen_ids if stored and review.parse_time(stored) >= window else []
        keep = previous + [item["id"] for item in items if item.get("id") is not None
                           and (review.parse_time(item["at"]) or started) >= window]
        cache.store_cursor(gh.repo, me, snapshot.iso(started), keep)
    return EXIT_OK


def cmd_ci(args: argparse.Namespace, gh: GitHub) -> int:
    ref = args.ref or identity.git_branch()
    if not ref:
        raise UsageError("no branch: pass --ref")
    if ref.startswith(train.BATCH_PREFIX):
        raise UsageError(f"{ref} is a merge-train batch; only the train dispatches its gate")
    gh.dispatch_workflow("ci.yml", ref, {"lanes": args.lanes})
    low = getattr(gh, "budget_low", None)
    if low:
        # The dispatch is the point and one call; reading the queue back would be several.
        print(f"requested ci.yml lanes={args.lanes} on {ref}. Not reading the Tycho queue back: {low}.")
        return EXIT_OK
    cache.invalidate_snapshot(gh.repo)
    try:
        runs = snapshot.fetch_runs(gh)
    except AppAuthError as error:
        print(f"requested ci.yml lanes={args.lanes} on {ref}. Not reading the Tycho queue back: {app_words(error)}.")
        return EXIT_OK
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
    verdict, reason = train.eligibility(pr, workflow_token=train.VIEWS_ASSUME_WORKFLOW_TOKEN)
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
    post_comment(gh, args.pr, text, {"kind": "train-add", "v": 1, "agent": me, "pr": args.pr, "head": pr["head"],
                                      **({"fallback": args.owner_fallback} if args.owner_fallback else {})})
    train_step.ensure_labels(gh)
    gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels", method="POST", body={"labels": [train.TRAIN_LABEL]})
    cache.invalidate_snapshot(gh.repo)
    try:
        gh.dispatch_workflow("merge-train.yml", "main")
    except AppAuthError:
        raise
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


FULL_SHA_WORDS = "give the full 40-character SHA of the head you reviewed or approved"


def head_sha(text: str) -> str:
    """argparse type for `--head`: a full 40-hex commit SHA, case-folded. Never a prefix:
    an author can grind a commit that shares a short prefix with the reviewed head
    (about 2^28 hashes for 7 digits; Codex P1 on #1054)."""
    value = text.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError(f"{FULL_SHA_WORDS} (got {text!r})")
    return value


def attested_head(gh: GitHub, number: int, given: str) -> Optional[str]:
    """The PR's current head when it EQUALS the head the attestation names (`--head`);
    otherwise None, after saying why. The head is read when the command runs, not when the
    review or approval happened, so without this a push in between would be recorded as
    reviewed or approved (review of #1054)."""
    pr = gh.rest(f"repos/{{repo}}/pulls/{number}")
    head = str((pr.get("head") or {}).get("sha") or "").lower()
    if not head or head != given:
        if head.startswith(given):
            why = f"{FULL_SHA_WORDS}, not a prefix"
        else:
            why = "its head moved after what you are recording was done; review or approve the new head"
        print(f"coord: #{number} is at {head or 'an unknown head'}, not {given}: {why}. Nothing was recorded.",
              file=sys.stderr)
        return None
    return head


def cmd_train_review(args: argparse.Namespace, gh: GitHub) -> int:
    """Record that a clean review of the PR's head `--head` was done, and by which agent
    (Alex, 2026-09-26: a clean review counts no matter what agent does it). Refuses unless
    `--head` is still the PR's head. The train counts it for this head only. `train
    substitute-review` runs this too, `--by` optional."""
    me = identity.agent_id()
    head = attested_head(gh, args.pr, args.head)
    if head is None:
        return EXIT_CONFLICT
    by = f" by `{args.by}`" if args.by else ""
    text = (f"**Clean review** of head `{head[:9]}`{by}, recorded by `{me}`.\n\n{args.note}\n\n"
            "It counts as the review of this head only; a new push needs a new review.")
    payload = {"kind": "review-clean", "v": 1, "agent": me, "pr": args.pr, "head": head}
    if args.by:
        payload["by"] = args.by
    # The record first, the label second: the record is what the train reads.
    post_comment(gh, args.pr, text, payload)
    train_step.ensure_labels(gh)
    gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels", method="POST", body={"labels": [train.REVIEWED_LABEL]})
    cache.invalidate_snapshot(gh.repo)
    print(f"recorded a clean review of #{args.pr} at {head[:9]}" + (f" by {args.by}" if args.by else ""))
    return EXIT_OK


def cmd_train_approve(args: argparse.Namespace, gh: GitHub) -> int:
    """Alex approves a needs-alex PR at the head he approved (`--head`), which must still
    be its head. Every agent posts as Alex's login, so this is procedure, not
    authentication: run it only on Alex's word."""
    me = identity.agent_id()
    head = attested_head(gh, args.pr, args.head)
    if head is None:
        return EXIT_CONFLICT
    text = (f"**Approved by Alex** for head `{head[:9]}` (recorded by `{me}`).\n\n{args.note}\n\n"
            "This covers this head only; a new push needs a new approval.")
    post_comment(gh, args.pr, text, {"kind": "alex-approve", "v": 1, "agent": me, "pr": args.pr, "head": head})
    gh.rest(f"repos/{{repo}}/issues/{args.pr}/labels", method="POST", body={"labels": [train.ALEX_APPROVED_LABEL]})
    cache.invalidate_snapshot(gh.repo)
    print(f"recorded Alex's approval of #{args.pr} at {head[:9]}")
    return EXIT_OK


def cmd_train_status(args: argparse.Namespace, gh: GitHub) -> int:
    snap = get_snapshot(gh, args.max_age)
    prs = {p["number"]: p for p in snap["prs"]}
    low = getattr(gh, "budget_low", None) or getattr(gh, "app_unavailable", None)
    # No trailing slash: the cloud proxy rejects `…/heads/train/` ("Request path could not
    # be canonicalized", probe 4). Without it GitHub also matches `training…`, so keep
    # only refs under refs/heads/train/. With the budget low the batch is not read at all.
    try:
        refs = [] if low else gh.rest("repos/{repo}/git/matching-refs/heads/" + train.BATCH_PREFIX.rstrip("/")) or []
    except AppAuthError as error:
        mark_app_unavailable(gh, error)
        low = gh.app_unavailable
        print(f"coord: {low}; the queue is from the cached snapshot", file=sys.stderr)
        refs = []
    except GitHubError as error:
        if not mark_exhausted(gh, error):
            raise
        low = gh.budget_low
        print(f"coord: {low}; the queue is from the cached snapshot", file=sys.stderr)
        refs = []
    refs = [ref for ref in refs if ref.get("ref", "").startswith("refs/heads/" + train.BATCH_PREFIX)]
    batches = []
    try:
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
    except AppAuthError as error:  # the read token failed after the refs (on any page of the runs)
        mark_app_unavailable(gh, error)
        low = gh.app_unavailable
        print(f"coord: {low}; the queue is from the cached snapshot", file=sys.stderr)
        batches = []
    except GitHubError as error:  # the budget ran out on a page of the runs
        if not mark_exhausted(gh, error):
            raise
        low = gh.budget_low
        print(f"coord: {low}; the queue is from the cached snapshot", file=sys.stderr)
        batches = []
    queue = []
    for entry in snap["train"]:
        pr = prs.get(entry["pr"])
        verdict, reason = (train.eligibility(pr, entry.get("headAtAdd"), workflow_token=train.VIEWS_ASSUME_WORKFLOW_TOKEN)
                           if pr else (train.DROP, "not open"))
        queue.append(dict(entry, verdict=verdict, reason=reason))
    if args.json:
        print(json.dumps({"schema": 1, "status": "stale" if low else "ok", "reason": low or None,
                          "generatedAt": snap.get("generatedAt"), "stale": snap.get("stale"),
                          "batches": None if low else batches, "queue": queue}, indent=2))
        return EXIT_OK
    why = "API budget low" if getattr(gh, "budget_low", None) else "GitHub App token unavailable"
    print("batch: " + (f"not read ({why})" if low else "none" if not batches else ""))
    for batch in batches:
        run = batch["run"]
        state = "CI not started" if run is None else f"CI {run['status']}" + (
            f" / {run['conclusion']}" if run.get("conclusion") else "") + f" (attempt {run['attempt']}) {run['url']}"
        print(f"  {batch['branch']} at {batch['tip'][:9]}: {state}")
    print("queue: " + ("empty" if not queue else ""))
    for position, entry in enumerate(queue, start=1):
        print(f"  {position}. #{entry['pr']} {entry['verdict']}" + ("" if entry["verdict"] == train.READY else f": {entry['reason']}"))
    return EXIT_OK


def cmd_train_report(args: argparse.Namespace, gh: GitHub) -> int:
    if not 1 <= args.days <= 30:
        raise UsageError("--days must be between 1 and 30")
    report = train_metrics.report(gh, days=args.days)
    print(json.dumps(report, indent=2) if args.json else train_metrics.render(report))
    return EXIT_OK


def cmd_token_status(args: argparse.Namespace, gh: GitHub) -> int:
    """App mode and the cached tokens, from the config and the cache files. No call, no
    Keychain read, and never a token."""
    mode = getattr(gh, "app_mode", None) or apptoken.detect(gh.repo)
    print(f"app mode: {'on' if mode.on else 'off'} ({mode.reason})")
    if mode.error:
        print(f"  every app call refuses: {redact(mode.error)}")
    if mode.config:
        print(f"app: {mode.config.get('app_login') or '?'} (client id {mode.config['client_id']}, "
              f"installed on {mode.config['repo']})")
    app = getattr(gh, "app", None)
    if app is None and mode.config is not None:
        app = apptoken.AppTokens(mode.config, apptoken.token_cache_dir())  # to read the cache only
    if app is None:
        return EXIT_OK
    installation = app.installation_id()
    print(f"installation: {installation or 'not yet known (no token minted)'}")
    if getattr(app, "cache_dir", None):
        print(f"token cache: {app.cache_dir}")
    for kind in apptoken.KINDS:
        info = app.describe(kind)
        line = f"{kind} token: {info['state']}"
        if info.get("expires_at"):
            line += f"; expires {info['expires_at']}"
            if info.get("minutes_left") is not None:
                line += f" ({info['minutes_left']} min left)"
        if info.get("permissions"):
            line += "; permissions " + ", ".join(f"{k}:{v}" for k, v in sorted(info["permissions"].items()))
        print(line)
        held = app.failure(kind) if hasattr(app, "failure") else None
        if held:
            print(f"  a mint failed {int(time.time() - held['at'])}s ago and is not retried before "
                  f"{time.strftime('%H:%M:%SZ', time.gmtime(held['until']))}: {redact(held['cause'])}")
    if installation:
        print(budget.header_words(gh.repo, principal=budget.app_principal(installation)))
    return EXIT_OK


# Tokens gh and other GitHub tools read before, or instead of, GH_TOKEN. The parent's may be
# Alex's own, so none of them reaches a `token exec` child.
INHERITED_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def exec_environment(parent: Dict[str, str], token: str) -> Dict[str, str]:
    """The `token exec` child's environment: the parent's without any inherited GitHub token,
    and GH_TOKEN set to the app token. GH_CONFIG_DIR is left alone: with GH_TOKEN set gh uses
    it, but the child can still run `gh auth token` and reach Alex's keyring. This is a
    convenience wrapper, not a sandbox."""
    env = {name: value for name, value in parent.items() if name not in INHERITED_TOKENS}
    env["GH_TOKEN"] = token
    return env


def cmd_token_exec(args: argparse.Namespace, gh: GitHub) -> int:
    """Run a command with the app's token in ITS environment (GH_TOKEN) and nowhere else;
    exit with its exit code. `read` unless the caller asks for `--kind ops` by name."""
    command = list(args.cmd or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise UsageError("name the command to run after --, e.g. `coord token exec -- gh pr list`")
    if getattr(gh, "app", None) is None:
        mode = getattr(gh, "app_mode", None) or apptoken.detect(gh.repo)
        print(f"coord: app mode is off ({mode.reason}); `token exec` runs only on the app", file=sys.stderr)
        return EXIT_USAGE
    low = getattr(gh, "budget_low", None)
    if low:
        print(f"coord: {low}", file=sys.stderr)
    token = gh.app.token(args.kind)  # AppAuthError: refused in run(), the command never starts
    env = exec_environment(os.environ, token)
    try:
        result = subprocess.run(command, env=env)
    except OSError as error:
        print(f"coord: {command[0]}: {error.strerror}", file=sys.stderr)
        return 127
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def cmd_train_step(args: argparse.Namespace, gh: GitHub) -> int:
    # merge-train.yml passes the inisa-sidechain app's installation token here, and only
    # here, when it could mint one; without it workflow PRs are dropped for a hand merge.
    app_token = os.environ.get(train.WORKFLOW_TOKEN_ENV) or ""
    merger = GitHub(repo=gh.repo, transport="http", token=app_token) if app_token else None
    if merger is None:
        print(f"coord: no {train.WORKFLOW_TOKEN_ENV}: PRs that change .github/workflows/ are dropped "
              "for a hand merge", file=sys.stderr)
    step = train_step.Step(gh, train_step.Git(args.repo_dir), args.mode, agent=identity.agent_id(), merger=merger)
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


def cmd_outage_start(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    now = snapshot.now_utc()
    until = review.parse_time(args.until) if args.until else outage.next_weekly_reset(now)
    if until <= now:
        raise UsageError(f"--until {args.until} is not in the future")
    if until > now + dt.timedelta(days=8):
        raise UsageError(f"--until {args.until} is more than 8 days out; no usage limit lasts past a weekly reset")
    me = identity.agent_id()
    issue = conductor.conductor_issue(gh)
    posted = conductor.post_record(gh, issue, (
        f"**{args.harness} agents are down** until {snapshot.iso(until)} (declared by `{me}`)"
        + (f": {args.note}" if args.note else "") + "."), {
        "kind": "outage", "v": 1, "event": "start", "agent": me, "harness": args.harness,
        "until": snapshot.iso(until), "reason": args.note or ""})
    cache.invalidate_snapshot(gh.repo)
    print(f"declared a {args.harness} outage until {snapshot.iso(until)}: {posted.get('html_url')}")
    return EXIT_OK


def cmd_outage_end(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    me = identity.agent_id()
    issue = conductor.conductor_issue(gh)
    posted = conductor.post_record(gh, issue, f"**{args.harness} agents are back** (declared by `{me}`)."
                                   + (f"\n\n{args.note}" if args.note else ""),
                                   {"kind": "outage", "v": 1, "event": "end", "agent": me, "harness": args.harness})
    cache.invalidate_snapshot(gh.repo)
    print(f"ended the {args.harness} outage: {posted.get('html_url')}")
    return EXIT_OK


def cmd_conductor_tick(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    config = conductor.Config.from_env()
    os.environ["COORD_AGENT"] = config.agent  # every coord call in this process speaks as the conductor
    lines = conductor.Watcher(config, gh).tick(dry_run=args.dry_run, wake=not args.no_wake)
    stamp = snapshot.iso(snapshot.now_utc())
    for line in lines:
        print(f"{stamp} {line}")
    return EXIT_OK


def cmd_conductor_diagnose(args: argparse.Namespace, gh: GitHub) -> int:
    """One explicitly requested diagnosis. It cannot approve, merge or mark work done."""
    from . import conductor, runtime
    if not 1 <= args.timeout <= 3600:
        raise UsageError("--timeout must be between 1 and 3600 seconds")
    executable = shutil.which(args.runtime)
    if executable is None:
        raise UsageError(f"{args.runtime} is not installed or is not on PATH")
    if args.input:
        try:
            with open(args.input, encoding="utf-8") as handle:
                context = handle.read(160_001)
        except (OSError, UnicodeError) as error:
            raise UsageError(f"cannot read diagnosis input: {error}") from error
    else:
        snap = get_snapshot(gh, DEFAULT_MAX_AGE)
        context = json.dumps({key: value for key, value in snap.items() if key != "claimComments"})
    if len(context) > 160_000:
        raise UsageError("diagnosis input exceeds 160,000 characters; provide a focused --input file")
    prompt = runtime.diagnosis_prompt(context)
    # Linux limits each argv string to about 128 KiB, independently of ARG_MAX.
    # JSON escaping can expand a small Unicode input twelvefold (surrogate pairs).
    if len(prompt.encode("utf-8")) > 120_000:
        raise UsageError("diagnosis prompt exceeds 120,000 encoded bytes; provide a focused --input file")
    # A clean cwd avoids loading this checkout's agent/configuration files. Model
    # authentication stays with its CLI; no credential is read or copied by coord.
    with tempfile.TemporaryDirectory(prefix="coord-diagnose-") as scratch:
        command = (runtime.codex_command(executable, prompt, args.model) if args.runtime == "codex"
                   else runtime.claude_command(executable, prompt, args.model, scratch, [], role="diagnose"))
        env = {key: value for key, value in os.environ.items()
               if key not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                              "CONDUCTOR_WAKE", "COORD_AGENT", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")}
        started = time.monotonic()
        completed = conductor.default_runner(command, scratch, env, args.timeout)
        result = runtime.diagnosis_outcome(args.runtime, completed.returncode, completed.stdout, completed.stderr)
    if not result["ok"]:
        return report_error(args, EXIT_BACKEND, "runtime", result["error"])
    report = {"schema": 1, "status": "ok", "runtime": args.runtime,
              "model": args.model or "runtime default", "seconds": round(time.monotonic() - started, 2),
              "diagnosis": result["diagnosis"], "usage": result["usage"], "handoff": None}
    if args.handoff and result["diagnosis"]["needsRepair"]:
        diagnosis = result["diagnosis"]
        me = identity.agent_id()
        target = find_or_create_mailbox(gh)
        body = (f"**Read-only diagnosis from** `{me}` using {args.runtime}. This is advisory analysis, "
                "not an approval or a verified repair. Recheck the current evidence under your standing orders.\n\n"
                + diagnosis["summary"] + "\n\n" + diagnosis["reason"])
        posted = post_comment(gh, target, body, {"kind": "msg", "v": 1, "agent": me, "to": ["conductor@*"]})
        cache.invalidate_snapshot(gh.repo)
        report["handoff"] = {"to": "conductor@*", "url": posted.get("html_url")}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report["diagnosis"]["summary"])
        print("Repair needed: " + ("yes" if report["diagnosis"]["needsRepair"] else "no"))
        print(report["diagnosis"]["reason"])
        if report["handoff"]:
            print(f"Sent to conductor: {report['handoff']['url']}")
    return EXIT_OK


def cmd_conductor_status(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    config = conductor.Config.from_env()
    snap = get_snapshot(gh, args.max_age)
    now = snapshot.now_utc()
    # A board served from the cache says so first, as `board` and the brief do.
    lines = [render.stale_words(snap)] if snap.get("stale") else []
    lines += [line for window in render.active_outages(snap, now) for line in [render.outage_line(window, now)]]
    lines += render.conductor_lines(snap, now)
    wakes = conductor.read_wakes(config)
    day = [w for w in wakes if (review.parse_time(w.get("at")) or now) > now - dt.timedelta(days=1)]
    if wakes:
        tokens = {k: sum((w.get("usage") or {}).get(k) or 0 for w in day)
                  for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
        lines += ["", f"THIS MACHINE ({config.agent}, state in {config.state_dir})",
                  f"  last 24 h: {len(day)} wakes, {sum(1 for w in day if w.get('ok'))} ok; tokens "
                  f"in {tokens['input_tokens']}, cache read {tokens['cache_read_input_tokens']}, "
                  f"cache write {tokens['cache_creation_input_tokens']}, out {tokens['output_tokens']}"]
        for wake in wakes[-args.last:]:
            lines.append(f"  {wake.get('at')} {wake.get('model')} {'ok' if wake.get('ok') else 'FAILED'} "
                         f"{wake.get('seconds')}s: {render._clip(wake.get('summary') or '', 90)}")
    else:
        lines += ["", f"THIS MACHINE ({config.agent}): no wakes recorded in {config.state_dir}"]
    reviews = conductor.read_wakes(config, name="reviews.jsonl")
    if reviews:
        lines += ["", "REVIEWS (@inisa-sidechain review; newest last)"]
        for entry in reviews[-args.last:]:
            lines.append(f"  {entry.get('at')} #{entry.get('pr')} {str(entry.get('head') or '')[:9]} {entry.get('model')} "
                         f"{'ok' if entry.get('ok') else 'FAILED'} {entry.get('seconds')}s")
    print("\n".join(lines))
    return EXIT_OK


def cmd_conductor_pause(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    try:
        until = snapshot.now_utc() + conductor.parse_duration(args.duration)
    except ValueError as error:
        raise UsageError(str(error))
    me = identity.agent_id()
    posted = conductor.post_record(gh, conductor.conductor_issue(gh),
                                   f"**Conductor paused** until {snapshot.iso(until)} by `{me}`" + (f": {args.note}" if args.note else "."),
                                   {"kind": "conductor", "v": 1, "event": "pause", "agent": me,
                                    "until": snapshot.iso(until), "note": args.note or ""})
    cache.invalidate_snapshot(gh.repo)
    print(f"paused the conductor until {snapshot.iso(until)}: {posted.get('html_url')}")
    return EXIT_OK


def cmd_conductor_resume(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    me = identity.agent_id()
    posted = conductor.post_record(gh, conductor.conductor_issue(gh), f"**Conductor resumed** by `{me}`.",
                                   {"kind": "conductor", "v": 1, "event": "resume", "agent": me})
    cache.invalidate_snapshot(gh.repo)
    print(f"resumed the conductor: {posted.get('html_url')}")
    return EXIT_OK


def cmd_conductor_escalate(args: argparse.Namespace, gh: GitHub) -> int:
    from . import conductor
    me = identity.agent_id()
    issue = conductor.conductor_issue(gh)
    posted = conductor.post_record(gh, issue, args.text, {"kind": "conductor", "v": 1, "event": "escalate", "agent": me})
    cache.invalidate_snapshot(gh.repo)
    notified = conductor.notify_alex(gh, issue, f"{args.text} {posted.get('html_url')}")
    print(f"escalated to Alex (on the board for 24 h" + (", and to his phone" if notified else
          "; Notify Alex could not be dispatched") + f"): {posted.get('html_url')}")
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

    preflight = sub.add_parser("doctor", help="read-only environment, identity, transport and cache preflight")
    preflight.add_argument("--json", action="store_true")
    preflight.add_argument("--online", action="store_true", help="verify repository read access (no writes or model runs)")
    preflight.set_defaults(func=cmd_doctor)

    claim = sub.add_parser("claim", help="announce that you are working on an issue")
    claim.add_argument("issue", type=int)
    claim.add_argument("--branch", help="default: the current branch")
    claim.add_argument("--files", nargs="+", metavar="PATH",
                       help="files or directories you expect to touch, as paths from the repository root "
                            "(space- or comma-separated)")
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
    add.add_argument("--owner-fallback", help="the agent that hears the train's held/removed notices when you "
                     "have stopped (your dispatching lead)")
    add.set_defaults(func=cmd_train_add)
    remove = train_sub.add_parser("remove", help="take a PR out of the train")
    remove.add_argument("pr", type=int)
    remove.add_argument("--note")
    remove.set_defaults(func=cmd_train_remove)
    head_help = "the full 40-character SHA of the head that was reviewed or approved; refused unless it is the PR's head"
    reviewed = train_sub.add_parser("review", help="record a clean review of a PR's head by an agent")
    reviewed.add_argument("pr", type=int)
    reviewed.add_argument("--head", required=True, type=head_sha, metavar="SHA", help=head_help)
    reviewed.add_argument("--by", required=True, metavar="AGENT", help="the agent that did the review")
    reviewed.add_argument("--note", required=True, help="where the review is and what it covered")
    reviewed.set_defaults(func=cmd_train_review)
    # The old name, kept for the sessions and docs that still use it; its callers pass no --by.
    substitute = train_sub.add_parser("substitute-review", help="the old name of `train review`; --by is optional here")
    substitute.add_argument("pr", type=int)
    substitute.add_argument("--head", required=True, type=head_sha, metavar="SHA", help=head_help)
    substitute.add_argument("--by", metavar="AGENT", help="the agent that did the review")
    substitute.add_argument("--note", required=True, help="who reviewed and where the review is")
    substitute.set_defaults(func=cmd_train_review)
    approve = train_sub.add_parser("approve", help="record Alex's approval of a needs-alex PR at a head")
    approve.add_argument("pr", type=int)
    approve.add_argument("--head", required=True, type=head_sha, metavar="SHA", help=head_help)
    approve.add_argument("--note", required=True, help="Alex's words, or where he approved")
    approve.set_defaults(func=cmd_train_approve)
    status = train_sub.add_parser("status", help="the current batch and every queued PR's readiness")
    status.add_argument("--json", action="store_true")
    status.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    status.set_defaults(func=cmd_train_status)

    report = train_sub.add_parser("report", help="on-demand train efficiency and CI attempt report")
    report.add_argument("--days", type=int, default=7, help="report window, 1 to 30 days (default 7)")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_train_report)
    step = train_sub.add_parser("step", help="advance the train by one step (merge-train.yml runs this)")
    step.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    step.add_argument("--repo-dir", default=".", help="a clone with push access (default: current directory)")
    step.set_defaults(func=cmd_train_step)

    outage_parser = sub.add_parser("outage", help="declare or end a harness outage (a usage limit: every agent of a kind down)")
    outage_sub = outage_parser.add_subparsers(dest="outage_command", required=True)
    start = outage_sub.add_parser("start", help="declare that a harness's agents are down until a time")
    start.add_argument("harness", help="claude (covers claude@, claude-cloud@ and conductor@) or codex")
    start.add_argument("--until", help="ISO time (default: the next weekly reset, COORD_CLAUDE_WEEKLY_RESET)")
    start.add_argument("--note", help="why, in a few words")
    start.set_defaults(func=cmd_outage_start)
    end = outage_sub.add_parser("end", help="declare that a harness's agents are back")
    end.add_argument("harness")
    end.add_argument("--note")
    end.set_defaults(func=cmd_outage_end)

    conductor_parser = sub.add_parser("conductor", help="the conductor: the watcher that wakes a model when there is work")
    conductor_sub = conductor_parser.add_subparsers(dest="conductor_command", required=True)
    tick = conductor_sub.add_parser("tick", help="one watcher pass (launchd runs this every few minutes)")
    tick.add_argument("--dry-run", action="store_true", help="show what it would wake for; write nothing")
    tick.add_argument("--no-wake", action="store_true", help="beat and record, but start no model run")
    tick.set_defaults(func=cmd_conductor_tick)

    diagnose = conductor_sub.add_parser("diagnose", help="one read-only model diagnosis; optional repair handoff")
    diagnose.add_argument("--runtime", choices=("codex", "claude"), default="codex")
    diagnose.add_argument("--model", help="use this model; omitted uses the runtime's default")
    diagnose.add_argument("--input", help="read a focused context file instead of the board")
    diagnose.add_argument("--timeout", type=int, default=300, help="maximum run seconds (1 to 3600)")
    diagnose.add_argument("--handoff", action="store_true", help="send a repair diagnosis to the existing conductor")
    diagnose.add_argument("--json", action="store_true")
    diagnose.set_defaults(func=cmd_conductor_diagnose)
    cstatus = conductor_sub.add_parser("status", help="hosts, pause, outages, escalations, and this machine's wakes")
    cstatus.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    cstatus.add_argument("--last", type=int, default=5, help="how many recent wakes to list")
    cstatus.set_defaults(func=cmd_conductor_status)
    pause = conductor_sub.add_parser("pause", help="stop every watcher from waking a model for a while")
    pause.add_argument("--for", dest="duration", required=True, help="45m, 2h, 1d")
    pause.add_argument("--note")
    pause.set_defaults(func=cmd_conductor_pause)
    resume = conductor_sub.add_parser("resume", help="end a pause")
    resume.set_defaults(func=cmd_conductor_resume)
    escalate = conductor_sub.add_parser("escalate", help="put something in front of Alex on the board (24 h)")
    escalate.add_argument("text")
    escalate.set_defaults(func=cmd_conductor_escalate)

    token = sub.add_parser("token", help="the GitHub App's tokens: status, or run a command with one (never printed)")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_status = token_sub.add_parser("status", help="app mode and why, the installation, each cached token's "
                                                       "expiry and permissions (no call, no secret)")
    token_status.set_defaults(func=cmd_token_status)
    token_exec = token_sub.add_parser("exec", help="run a command with GH_TOKEN set to an app token in its "
                                                   "environment only; exits with its exit code")
    token_exec.add_argument("--kind", choices=apptoken.KINDS, default="read",
                            help="read (default): every permission read-only; ops: coord's write token, only by name")
    token_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command and its arguments")
    token_exec.set_defaults(func=cmd_token_exec)
    return top


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
    except SystemExit as error:
        if error.code and "--json" in argv:
            return report_error(argparse.Namespace(json=True), EXIT_USAGE, "usage", "invalid arguments; see stderr")
        raise
    try:
        gh = GitHub(args.repo)
    except GitHubError as error:
        return report_error(args, EXIT_BACKEND, "github", f"GitHub error: {error}")
    attach_app(gh)
    return run(args, gh)


def attach_app(gh: GitHub) -> None:
    """App mode for this command (apptoken.detect): the token source goes on `gh`, so its
    calls use the app. Nothing is minted here; the first call that needs a token does."""
    mode = apptoken.detect(gh.repo)
    gh.app_mode = mode
    gh.app = apptoken.for_mode(mode) if mode.on else None


def report_error(args: argparse.Namespace, code: int, kind: str, message: str) -> int:
    """Machine clients get one parseable failure object; diagnostics remain on stderr."""
    message = redact(message)
    print(f"coord: {message}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"schema": 1, "status": "error", "error": {
            "code": code, "kind": kind, "message": message}}, indent=2))
    return code


def run(args: argparse.Namespace, gh: GitHub) -> int:
    """The budget guard, then the command, with every error mapped to its exit code.

    In app mode the USER_IDENTITY commands run on Alex's identity, by choice of command.
    An app failure is never retried as Alex: reads that have a cache serve it
    (`get_snapshot`), everything else refuses here, naming the cause and COORD_APP=off.
    """
    command = budget.command_name(args)
    if getattr(gh, "app", None) is not None and command in USER_IDENTITY:
        gh = gh.as_user()
    gh.cache_read = command in budget.CACHE_READS  # type: ignore[attr-defined]
    try:
        refused = gate(args, gh)
        if refused is not None:
            return refused
        return args.func(args, gh)
    except BudgetRefused as error:
        return report_error(args, EXIT_BUDGET, "budget", str(error))
    except AppRefused as error:
        return report_error(args, EXIT_BACKEND, "authentication", str(error))
    except UsageError as error:
        return report_error(args, EXIT_USAGE, "usage", str(error))
    except identity.IdentityError as error:
        return report_error(args, EXIT_USAGE, "identity", str(error))
    except train_metrics.BudgetConfigurationError as error:
        return report_error(args, EXIT_USAGE, "configuration", str(error))
    except AppAuthError as error:
        return report_error(args, EXIT_BACKEND, "authentication", f"{app_words(error)}; not running `coord {command}`")
    except NotFound as error:
        return report_error(args, EXIT_NOT_FOUND, "not_found", f"not found: {error}")
    except GitHubError as error:
        return report_error(args, EXIT_BACKEND, "github", f"GitHub error: {error}")

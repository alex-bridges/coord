"""What the reviewer and CI say about a PR, computed from one GraphQL PR node.

Pure functions over the node shape PR_FIELDS (snapshot.py) asks for, so the
rules can be tested against recorded responses.

Codex approval (memory: reference_codex_review_signals, re-checked 2026-09-24
on #975/#984/#986):
- Codex approves with a 👍 reaction on the PR itself. It posts a review object
  ONLY when it has findings, so "a review exists at the head commit" is NOT part
  of approval — the approved heads of #984 and #986 have no review object.
- On a new push it removes the 👍, shows 👀 while re-reviewing, then re-adds 👍.
  So: approved = bot 👍 present, no bot 👀, and the 👍 is newer than the head
  was pushed. Push time is taken from the head commit's earliest check suite
  (created within seconds of the push); the commit date is the fallback, and it
  can be older than the push, never newer.

Unanswered findings: a review thread the bot started that is neither resolved
nor answered by a non-bot comment. The merge rule wants a fix commit or a filed
issue behind every finding; the reply is where that is recorded, and a script
cannot judge the reply's content — only that one exists.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional

CODEX_LOGIN_PREFIX = "chatgpt-codex-connector"
VERDICT_CHECK = "CI verdict"

# CANCELLED is absent on purpose: a cancelled check is a superseded run, not a red.
_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
# A job GitHub refused to start concludes `failure` with this as its one annotation
# (#1026, check run 10000099: "The job was not started because an Actions budget is
# preventing further use.", started and completed 2 s apart, no step run). It never
# ran, so it is not evidence either way: NOT RUN, neither red nor green. Only this
# prefix; any other annotation on a failed run leaves it red.
NOT_RUN_PREFIX = "The job was not started because"
# Xcode Cloud checks are billing-dead (memory: reference_xcode_cloud_out_of_credits)
# and report failure on every PR; they are not CI for this repository.
_IGNORED_CHECK_PREFIXES = ("LegacyCI | ",)



def _nodes(connection: Any) -> List[Dict[str, Any]]:
    """A GraphQL connection's nodes, without the nulls GitHub sometimes returns in them
    (a reaction whose user it will not show, with no `errors` entry). One null reaction
    crashed the merge train's step on 2026-09-30 (run 10000098)."""
    return [n for n in ((connection or {}).get("nodes") or []) if isinstance(n, dict)]

def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def is_bot(login: Optional[str]) -> bool:
    login = login or ""
    return login.endswith("[bot]") or login.startswith(CODEX_LOGIN_PREFIX)


def _head_commit(pr: Dict[str, Any]) -> Dict[str, Any]:
    nodes = ((pr.get("commits") or {}).get("nodes")) or []
    return ((nodes[-1] or {}).get("commit") or {}) if nodes else {}


def head_pushed_at(pr: Dict[str, Any]) -> Optional[dt.datetime]:
    commit = _head_commit(pr)
    suites = _nodes(commit.get("checkSuites"))
    times = [parse_time(s.get("createdAt")) for s in suites if s.get("createdAt")]
    times = [t for t in times if t is not None]
    if times:
        return min(times)
    return parse_time(commit.get("committedDate"))


SUMMARY_MARKER = "codex-pull-request-review-summary"
_SUMMARY_ROW = re.compile(r"\*\*Code Review\*\*\s*\|(?P<status>[^|]*)\|\s*`(?P<sha>[0-9a-f]{7,40})`")


def summary_reviewed_commit(pr: Dict[str, Any]) -> Optional[str]:
    """The commit Codex's pinned summary comment says it finished reviewing, or None.

    Codex edits one summary comment in place; its Code Review row names the reviewed
    commit and says Completed when the review is done (on #975/#984/#986 it names
    exactly the approved head). None while a review runs or when there is no summary.
    """
    for comment in _nodes(pr.get("comments")):
        login = (comment.get("author") or {}).get("login") or ""
        body = comment.get("body") or ""
        if login.startswith(CODEX_LOGIN_PREFIX) and SUMMARY_MARKER in body:
            match = _SUMMARY_ROW.search(body)
            if match and "Completed" in match.group("status"):
                return match.group("sha")
            return None
    return None


def codex_state(pr: Dict[str, Any]) -> Dict[str, Any]:
    """Approved = a bot 👍, no bot 👀, the 👍 newer than the head's first check suite,
    AND Codex's summary naming the current head as reviewed and completed.

    The summary check is what binds the 👍 to this head. The push-time test alone
    trusted the head's earliest check suite, which can predate the PR (a SHA pushed to
    another branch, or a force-push back to an older head), so an old 👍 could pass
    for a head Codex never reviewed (substitute review of #989).
    """
    reactions = _nodes(pr.get("reactions"))
    bot = [r for r in reactions if ((r.get("user") or {}).get("login") or "").startswith(CODEX_LOGIN_PREFIX)]
    thumbs = [parse_time(r.get("createdAt")) for r in bot if r.get("content") == "THUMBS_UP"]
    eyes = any(r.get("content") == "EYES" for r in bot)
    pushed = head_pushed_at(pr)
    newest_thumb = max((t for t in thumbs if t is not None), default=None)
    fresh = newest_thumb is not None and (pushed is None or newest_thumb >= pushed)
    reviewed = summary_reviewed_commit(pr)
    head = pr.get("headRefOid") or ""
    reviewed_head = bool(reviewed) and head.startswith(reviewed)
    return {
        "approved": bool(fresh and reviewed_head and not eyes),
        "thumbsUp": newest_thumb is not None,
        "stale": newest_thumb is not None and not (fresh and reviewed_head),
        "reviewing": eyes,
        "reviewedCommit": reviewed,
    }


def unanswered_findings(pr: Dict[str, Any]) -> int:
    threads = _nodes(pr.get("reviewThreads"))
    count = 0
    for thread in threads:
        if thread.get("isResolved"):
            continue
        comments = ((thread.get("comments") or {}).get("nodes")) or []
        if not comments:
            continue
        if not isinstance(comments[0], dict):
            # Who started the thread is unknown, and the next comment did not start it:
            # count it rather than let a null hide a finding (Codex on #1218).
            count += 1
            continue
        starter = (comments[0].get("author") or {}).get("login")
        if not is_bot(starter):
            continue
        answered = any(isinstance(c, dict) and not is_bot((c.get("author") or {}).get("login"))
                       for c in comments[1:])
        if not answered:
            count += 1
    return count


def is_ignored_check(name: Optional[str]) -> bool:
    return (name or "").startswith(_IGNORED_CHECK_PREFIXES)


def needs_first_annotation(context: Dict[str, Any]) -> bool:
    """Whether a rollup node's first annotation can change what ci_state says about it: a
    completed check run with a failure-class conclusion that is not an ignored check. The
    snapshot reads annotations only for these, so a green head costs no call."""
    return (context.get("__typename") == "CheckRun" and context.get("status") == "COMPLETED"
            and context.get("conclusion") in _FAILED and not is_ignored_check(context.get("name")))


def first_annotation_is_refusal(context: Dict[str, Any]) -> bool:
    """Whether a check run's FIRST annotation says GitHub did not start the job."""
    nodes = ((context.get("annotations") or {}).get("nodes")) or []
    message = (nodes[0] or {}).get("message") if nodes else None
    return isinstance(message, str) and message.startswith(NOT_RUN_PREFIX)


# Set on a refused run by snapshot.attach_first_annotations, and only there, once it
# has read every other attempt of the same check name at the commit and found none
# that failed for real.
NO_OTHER_ATTEMPT_FAILED = "noOtherAttemptFailed"


def never_started(context: Dict[str, Any]) -> bool:
    """A check run GitHub did not start, with no real failure hiding behind it.

    The rollup and REST's default `filter=latest` show only the newest attempt of a
    check. A real failure on attempt 1 followed by a re-run the budget refused would
    otherwise read as not run, and the red would vanish (review of #1032). So a refusal
    counts only when the snapshot has also cleared the other attempts
    (NO_OTHER_ATTEMPT_FAILED is True). Fails closed: without that, the run stays red.
    """
    return first_annotation_is_refusal(context) and context.get(NO_OTHER_ATTEMPT_FAILED) is True


def ci_state(pr: Dict[str, Any]) -> Dict[str, Any]:
    """The CI picture at the head commit.

    `verdict` is the `CI verdict` aggregate job when present (success, failure,
    pending, skipped, cancelled, or none). `failing`/`pending` list the individual checks,
    minus the ignored Xcode Cloud ones. `not_run` lists failed check runs GitHub never
    started (NOT_RUN_PREFIX); they are neither red nor green, so nothing reads them as
    a reason to hold or to merge.
    """
    rollup = _head_commit(pr).get("statusCheckRollup") or {}
    contexts = _nodes(rollup.get("contexts"))
    failing: List[str] = []
    pending: List[str] = []
    started: List[str] = []
    not_run: List[str] = []
    verdict = "none"
    for context in contexts:
        refused = False
        if context.get("__typename") == "CheckRun":
            name = context.get("name") or ""
            status = context.get("status")
            conclusion = context.get("conclusion")
            done = status == "COMPLETED"
            failed = done and conclusion in _FAILED
            refused = failed and never_started(context)
            if status == "IN_PROGRESS" and not is_ignored_check(name):
                started.append(name)
            # A verdict job that never ran says nothing; the verdict stays as it was.
            if name == VERDICT_CHECK and not refused:
                if not done:
                    verdict = "pending"
                elif conclusion == "SUCCESS":
                    verdict = "success"
                elif conclusion in ("SKIPPED", "CANCELLED"):
                    verdict = conclusion.lower()
                else:
                    verdict = "failure"
        else:
            name = context.get("context") or ""
            state = context.get("state")
            done = state not in ("PENDING", "EXPECTED")
            failed = state in _FAILED
        if is_ignored_check(name):
            continue
        if refused:
            not_run.append(name)
        elif failed:
            failing.append(name)
        elif not done:
            pending.append(name)
    # `started` is the part of `pending` a runner has picked up; the rest waits in the
    # queue. The board says "running" only when something started (probe 5: a PR row
    # said "running" while the header, which counts workflow runs, said 0 running).
    # A name that also failed for real is red, never "not run".
    return {"verdict": verdict, "failing": sorted(set(failing)), "pending": sorted(set(pending)),
            "started": sorted(set(started)), "not_run": sorted(set(not_run) - set(failing))}


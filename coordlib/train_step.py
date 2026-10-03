"""One step of the merge train, run by .github/workflows/merge-train.yml.

Each step reads the state from GitHub and git, does ONE thing, and exits; the
workflow's concurrency group guarantees one step at a time. A step never waits on a
gate: ci.yml's `wake-train` job dispatches the next step when a batch's CI has its
verdict (GitHub's `workflow_run` does not fire for runs the Actions token dispatched),
a step that closes a batch dispatches the next one (`wake_next`), and a cron tick is
the backstop. The one wait is `settle`: up to two minutes for a
batch run whose jobs have all finished to be marked completed. Holding a runner slot
while a gate runs is exactly the waste #915 removed.

State lives only in GitHub:
- the queue: open PRs labelled `train`, ordered by their `train-add` comment;
- the batch: the branch train/<stamp> (main + one --no-ff merge per PR);
- the gate: the newest ci.yml run on that branch at its tip;
- who was told a queued PR is held: the train's own `held` comments on it (#1034).

Modes: `dry-run` does everything except moving main (a green batch is reported
and the PRs leave the queue); `live` fast-forwards main to the tested commit.

No working tree: merges are computed with `git merge-tree --write-tree` (git 2.38+)
and recorded with `git commit-tree`, so the step runs in a blob-less clone and
fetches only the blobs a merge actually needs, instead of checking out main's
~280 MB tree every time.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import time
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional

from . import marker, review, snapshot, train, train_metrics
from .gh import AppAuthError, GitHub, GitHubError, NotFound

BOT_NAME = "github-actions[bot]"
# The Actions train's agent id (COORD_AGENT in merge-train.yml). A step run by hand
# posts as alex-bridges under its own id.
TRAIN_AGENT = "train@actions"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
DISPATCH_GRACE_MINUTES = 10
# ci.yml's verdict job wakes the train as its last step, so the step can start before
# GitHub marks the batch run completed. It waits this long, but only while nothing but
# that bookkeeping is left (every job done, or only the verdict job still running).
SETTLE_SECONDS = 120
SETTLE_POLL_SECONDS = 10
VERDICT_JOB = "CI verdict"
WAKE_JOB = "Wake the merge train"
FINAL_JOBS = (VERDICT_JOB, WAKE_JOB)
# A comment that asks Codex for a review: the mention at the start of a line, as people
# and the train write it (a sentence quoting it mid-line is not a request).
_CODEX_REQUEST = re.compile(r"(?m)^[ \t]*@codex[ \t]+review\b", re.IGNORECASE)


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, cwd: str):
        self.cwd = cwd

    def run(self, *args: str, check: bool = True, env: Optional[Dict[str, str]] = None) -> "subprocess.CompletedProcess[str]":
        full_env = None
        if env:
            full_env = dict(os.environ)
            full_env.update(env)
        result = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, text=True, env=full_env)
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def out(self, *args: str, env: Optional[Dict[str, str]] = None) -> str:
        return self.run(*args, env=env).stdout.strip()


class Step:
    def __init__(self, gh: GitHub, git: Git, mode: str, now: Optional[dt.datetime] = None,
                 agent: str = TRAIN_AGENT, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, requester: Optional[GitHub] = None):
        if mode not in ("dry-run", "live"):
            raise ValueError(f"mode must be dry-run or live, not {mode!r}")
        self.gh = gh
        self.git = git
        self.mode = mode
        self.now = now or snapshot.now_utc()
        self.agent = agent
        self.log = log
        self.sleep = sleep
        # Who posts `@codex review` (#1101): the step's own identity unless told otherwise.
        self.requester = requester or gh
        self.closed_batch = False
        self.lines: List[str] = []
        self._labels_ready = False
        self._receipt: Optional[Dict[str, Any]] = None

    def note(self, text: str) -> None:
        self.lines.append(text)
        self.log(text)

    # MARK: - GitHub helpers

    def comment(self, number: int, text: str, event: str, via: Optional[GitHub] = None, **fields: Any) -> None:
        payload = {"kind": "train", "v": 1, "agent": self.agent, "event": event, "pr": number, "mode": self.mode,
                   "at": snapshot.iso(self.now)}
        payload.update(fields)
        if self._receipt and fields.get("batch") == self._receipt["batch"]:
            payload["receipt"] = dict(self._receipt, outcome=event)
        (via or self.gh).rest(f"repos/{{repo}}/issues/{number}/comments", method="POST",
                              body={"body": marker.compose(text, payload)})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.gh.rest(f"repos/{{repo}}/issues/{number}/labels/{label.replace(':', '%3A')}", method="DELETE")
        except NotFound:
            pass

    def add_label(self, number: int, label: str) -> None:
        if not self._labels_ready:
            ensure_labels(self.gh)
            self._labels_ready = True
        self.gh.rest(f"repos/{{repo}}/issues/{number}/labels", method="POST", body={"labels": [label]})

    def delete_branch(self, name: str) -> None:
        try:
            self.gh.rest(f"repos/{{repo}}/git/refs/heads/{name}", method="DELETE")
            self.closed_batch = True
        except (NotFound, GitHubError) as error:
            self.note(f"could not delete {name}: {error}")

    def runs_at(self, name: str, tip: str) -> List[Dict[str, Any]]:
        pages = self.gh.rest_paginated(
            f"repos/{{repo}}/actions/workflows/{train.CI_WORKFLOW}/runs?branch={urllib.parse.quote(name, safe='')}&per_page=20")
        return [r for page in pages for r in page.get("workflow_runs", []) if r.get("head_sha") == tip]

    def daily_budget_left(self, kind: str = train.HEAVY) -> int:
        return train_metrics.budget_remaining(self.gh, self.now, kind)

    def request_ci(self, action: str, run: Optional[Dict[str, Any]] = None) -> bool:
        """Reserve one attempt before dispatch/retry, including ambiguous API failures.

        The one batch-level record covers lost dispatches and reruns of a batch
        created before today's window. No admission is made against unreadable state.
        """
        assert self._receipt is not None
        kind = train_metrics.plan_class(self._receipt)
        if self.daily_budget_left(kind) <= 0:
            self.note(f"{self._receipt['batch']}: the daily budget of {kind} CI attempts is used up; waiting")
            return False
        self.comment(self._receipt["members"][0]["pr"],
                     f"Merge train: reserving one {kind} CI attempt ({action}) for `{self._receipt['batch']}`.",
                     "ci-requested", batch=self._receipt["batch"], run=(run or {}).get("id"),
                     request={"id": str(uuid.uuid4()), "action": action,
                              "attempt": train_metrics.attempts(run) + 1 if run else 1})
        return True

    def record_outcome(self, event: str, text: str) -> None:
        """One durable terminal receipt for an abandoned/rebuilt batch before deletion."""
        assert self._receipt is not None
        number, batch = self._receipt["members"][0]["pr"], self._receipt["batch"]
        try:
            if not self.already_posted(number, event, batch):
                self.comment(number, text, event, batch=batch, run=(self._receipt.get("run") or {}).get("id"))
        except GitHubError as error:
            # A missing report outcome must not stop a safe rebuild. The run still
            # carries its immutable plan; the report explicitly exposes missing receipts.
            self.note(f"{batch}: could not record {event} receipt ({error})")

    def main_ci(self, main: str) -> Dict[str, Any]:
        """The newest finished ci.yml run at main's exact commit: {conclusion, failed, url}.

        {} when main's commit has no finished run yet (a hand merge whose run is still
        queued): unknown is not red, so it does not stop the train.
        """
        pages = self.gh.rest_paginated(f"repos/{{repo}}/actions/workflows/{train.CI_WORKFLOW}/runs?head_sha={main}&per_page=20")
        runs = [r for page in pages for r in page.get("workflow_runs", [])
                if r.get("status") == "completed" and r.get("conclusion") in ("success", "failure")]
        if not runs:
            return {}
        newest = max(runs, key=lambda r: r["id"])
        failed = self.failed_jobs(newest["id"]) if newest["conclusion"] == "failure" else []
        return {"conclusion": newest["conclusion"], "failed": failed, "url": newest.get("html_url")}

    def settle(self, name: str, tip: str, run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Give a batch run that is only finishing its bookkeeping time to be marked done.

        ci.yml's wake-train job starts this step as the run's last act, so the run can
        still read "in progress". Wait (SETTLE_SECONDS at most) only when that is all it
        is: every job completed, or only the verdict and wake jobs left and actually
        running. Never while a lane runs, nor while a final job is still queued (it may be
        queued behind THIS step on the one light runner), nor before any job is listed.
        """
        waited = 0
        announced = False
        while run is not None and run.get("status") != "completed" and waited < SETTLE_SECONDS:
            pages = self.gh.rest_paginated(f"repos/{{repo}}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
            jobs = [j for page in pages for j in page.get("jobs", [])]
            if not jobs:
                break
            unfinished = [j for j in jobs if j.get("status") != "completed"]
            if any(j.get("name") not in FINAL_JOBS or j.get("status") != "in_progress" for j in unfinished):
                break  # real work, or a final job not yet started: an ordinary wait
            if not announced:
                self.note(f"{name}: run {run['id']} has finished its jobs; waiting up to {SETTLE_SECONDS}s "
                          "for GitHub to mark it completed")
                announced = True
            self.sleep(SETTLE_POLL_SECONDS)
            waited += SETTLE_POLL_SECONDS
            fresh = self.batch_run(name, tip)
            run = fresh if fresh is not None else run
        return run

    def batch_run(self, name: str, tip: str) -> Optional[Dict[str, Any]]:
        """The batch's gate: the newest ci.yml run at the tip that THE TRAIN dispatched.

        Any other run on the branch (someone's narrow `lanes=docs` dispatch) must never
        count as the gate, or a green partial run could move main while the real gate
        is red (Codex P1 on #989). The train dispatches with the Actions token, so its
        runs' initial actor is the Actions bot; `actor` stays that on a re-run, where
        `triggering_actor` would change.
        """
        runs = [r for r in self.runs_at(name, tip) if train.is_gate_run(r, tip)]
        return max(runs, key=lambda r: r["id"]) if runs else None

    def failed_jobs(self, run_id: int) -> List[str]:
        pages = self.gh.rest_paginated(f"repos/{{repo}}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        return [j["name"] for page in pages for j in page.get("jobs", [])
                if j.get("conclusion") in ("failure", "timed_out", "startup_failure")]

    def cancel_branch_runs(self, branch: str) -> bool:
        """Cancel every run of a merged PR's branch that has not completed. True when done.

        ONE unfiltered snapshot, like cancel-merged-branch-runs.yml: filtering by
        status would miss runs in `requested`, `waiting` or `pending` and race runs
        moving between states (Codex on #989). Each cancel is tried on its own; a run
        that finished in between answers 409 and is fine; any other failure returns
        False so the caller keeps the batch and the next step tries again.
        """
        try:
            # Encoded: a branch named `feature#1` or `a&b` must not change the query.
            pages = self.gh.rest_paginated(
                f"repos/{{repo}}/actions/runs?branch={urllib.parse.quote(branch, safe='')}&per_page=100")
        except GitHubError as error:
            self.note(f"could not list runs of {branch}: {error}")
            return False
        done = True
        for run in [r for page in pages for r in page.get("workflow_runs", [])]:
            # The branch filter also matches a FORK's branch of the same name; leave
            # other repositories' runs alone, as cancel-merged-branch-runs.yml does.
            if (run.get("head_repository") or {}).get("full_name") != self.gh.repo:
                continue
            if run.get("status") == "completed":
                continue
            try:
                self.gh.rest(f"repos/{{repo}}/actions/runs/{run['id']}/cancel", method="POST")
            except GitHubError as error:
                if "409" in str(error):
                    continue
                self.note(f"could not cancel run {run['id']} of {branch}: {error}")
                done = False
        return done

    def open_prs(self) -> Dict[int, Dict[str, Any]]:
        _, nodes = snapshot.fetch_prs(self.gh)
        prs = {}
        for node in nodes:
            pr = snapshot.summarize_pr(node)
            pr["headPushedAt"] = review.head_pushed_at(node)  # when a review of this head became due
            prs[node["number"]] = pr
        return prs

    def train_added(self, pr: Dict[str, Any]) -> Dict[str, Any]:
        """The newest train-add marker on the PR (when it joined, at which head, by whom).
        Also sets on `pr` the heads its newest clean-review (review-clean, or the older
        review-substitute) and alex-approve comments name, counting only those a trusted
        user posted (snapshot.trusted_attestation, the snapshot's own rule), who added it
        (`trainAddedBy`), the (head, hold class) of every `held` notice the train has
        posted on it (`heldNotices`), and `overtakenBy`: the light batch that went ahead
        of it since it joined, if any. For #1101 it also sets `reviewRequests`, when anyone
        (the train included) commented `@codex review`, and `readdedAtNewHead`: the newest
        add names a different head than the add before it, so the train dropped it and it
        was added back."""
        number = pr["number"]
        latest: Dict[str, Any] = {}
        held = set()
        blocked = set()
        requests = []
        add_heads = []
        pr["overtakenBy"] = None
        for comment in self.gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?per_page=100"):
            login = snapshot.comment_author(comment).get("login") or ""
            if _CODEX_REQUEST.search(comment.get("body") or "") and not login.startswith(review.CODEX_LOGIN_PREFIX):
                at = review.parse_time(comment.get("created_at"))
                if at is not None:
                    requests.append(at)
            payload = marker.decode(comment.get("body"))
            if not payload or payload.get("pr") != number:
                continue
            blocks = snapshot.sidechain_blocks(comment, payload)
            if blocks:
                blocked.add(blocks)  # from the PR's whole history, not a comment window (Codex on #1049)
            if payload.get("kind") == "train-add":
                latest = {"at": comment.get("created_at"), "head": payload.get("head"), "agent": payload.get("agent"),
                          "fallback": payload.get("fallback")}
                add_heads.append(payload.get("head"))
                pr["overtakenBy"] = None  # a new queue entry: its own one overtake
            elif payload.get("kind") in snapshot.HEAD_RECORDS:
                if snapshot.trusted_attestation(snapshot.comment_author(comment), self.gh.repo):
                    pr[snapshot.HEAD_RECORDS[payload["kind"]]] = payload.get("head")
            elif payload.get("event") == "held" and self.own_record(comment, payload):
                held.add((payload.get("head"), payload.get("hold")))
            elif payload.get("event") == train.OVERTAKEN_EVENT and self.own_record(comment, payload):
                # Only the train's own: a forged record would give heavy its turn early.
                pr["overtakenBy"] = payload.get("batch") or "?"
        pr["trainAddedBy"] = latest.get("agent")
        pr["trainAddedAt"] = latest.get("at")
        pr["ownerFallback"] = latest.get("fallback")
        pr["heldNotices"] = held
        pr["sidechainBlockedHeads"] = sorted(blocked)
        pr["reviewRequests"] = requests
        pr["readdedAtNewHead"] = len(add_heads) >= 2 and add_heads[-1] != add_heads[-2]
        return latest

    def own_record(self, comment: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """Whether a `kind: train` record counts as already said (a held notice, a receipt).

        (a) The Actions train's record: the marker names TRAIN_AGENT AND GitHub lists
            github-actions[bot] as its author. Every agent posts as alex-bridges (or, in
            app mode, as the GitHub App) and can write any marker, so neither half alone
            proves anything (review of #1037).
        (b) On a hand run only (this step's agent is not TRAIN_AGENT): the run's own
            earlier records, by marker agent, so hand runs repeat nothing either.
            Never on the Actions train, where it would let a `train@actions` marker from
            any login silence it again.

        So a thing is said at most once by the Actions train and once per hand-run agent
        id: the Actions train may repeat, once, what a hand run said first; a hand run
        never repeats what the Actions train said.
        """
        if payload.get("kind") != "train":
            return False
        if payload.get("agent") == TRAIN_AGENT and (comment.get("user") or {}).get("login") == BOT_NAME:
            return True
        return self.agent != TRAIN_AGENT and payload.get("agent") == self.agent

    def recipients(self, pr: Optional[Dict[str, Any]]) -> List[str]:
        """Who hears when the train holds or takes out a PR: the agent that added it (when
        this step read it), the fallback it named at `train add --owner-fallback` (the
        dispatching lead: a one-shot owner has stopped by then, #1148), and the conductor,
        which keeps the train moving (conductor.py).

        A PR dict straight from `open_prs()` (the ejection path) has not had its train-add
        marker read yet: read it here, or the notice reaches the conductor alone (Codex on
        #1157). Best effort: a read error leaves the conductor as the recipient."""
        pr = pr or {}
        if pr.get("number") is not None and "heldNotices" not in pr:
            try:
                self.train_added(pr)
            except GitHubError as error:
                self.note(f"#{pr['number']}: could not read who added it ({error}); telling the conductor only")
        return list(dict.fromkeys(a for a in (pr.get("trainAddedBy"), pr.get("ownerFallback"), train.CONDUCTOR)
                                  if isinstance(a, str) and a))

    def notify_held(self, pr: Dict[str, Any], head: str, hold: str, reason: str) -> None:
        """Tell whoever is still responsible that a queued PR is held (#1034), once per
        (PR, head, hold class), ever.

        A worker stops at `train add`, so a hold nobody hears about sits in the queue
        until someone happens to look. The notice goes to the agent that added the PR
        and to the conductor (both see it in `coord inbox`); a hold only Alex can clear
        (train.OWNER_HOLDS) also @-mentions him, which reaches his GitHub notifications
        when no agent is running.

        "Already told" is read from the train's own earlier `held` comments on the PR
        (`own_record`; `train_added` collects them in the read it already makes, and
        reads them here only for a PR it has not read yet), which live on GitHub: no
        runner state, so a restart never repeats one. The key is the hold's CLASS, never
        its wording (which names the failing checks, the count of findings) or the time.
        A new head or a different class is a new notice.

        Best effort: a notice never changes what the step decides. Any GitHub error,
        reading or posting, is noted and the next step tries again; callers act on the
        hold first and notify last.
        """
        number = pr["number"]
        try:
            if "heldNotices" not in pr:
                self.train_added(pr)
            if (head, hold) in pr["heldNotices"]:
                return
            to = self.recipients(pr)
            text = (f"**Held in the merge train** at head `{head[:9]}`: {reason}.\n\n"
                    + train.HOLD_ACTIONS[hold].format(pr=number) + "\n\n"
                    + "To " + ", ".join(f"`{agent}`" for agent in to)
                    + ". The train says this once per head and reason.")
            if hold in train.OWNER_HOLDS:
                text = f"{train.OWNER_MENTION}: this hold is yours to clear.\n\n" + text
            self.comment(number, text, "held", head=head, hold=hold, reason=reason, to=to)
        except GitHubError as error:
            self.note(f"#{number}: could not post the held notice ({error}); the next step tries again")
            return
        pr["heldNotices"].add((head, hold))

    def request_review(self, pr: Dict[str, Any]) -> None:
        """Ask Codex to review the queued PR's head when nobody has (#1101; the rule is
        train.review_request_due). The comment is the request and the train's record of
        it, so it is made once per head. Dry runs ask too: a request moves nothing.

        Posted through `requester`. A hand-run step is Alex, whose `@codex review` Codex
        answers (on #1088 within four minutes). The Actions train posts as
        github-actions[bot]; the docs name Alex as the identity for review requests, and
        whether Codex answers a bot is not established. Best effort: an error is noted and
        the next step tries again."""
        reason = train.review_request_due(pr, self.now)
        if not reason:
            return
        number, head = pr["number"], pr["head"]
        text = (f"{train.CODEX_REQUEST}\n\n_Merge train: {reason} (head `{head[:9]}`), so the train asks "
                "once for this head._")
        try:
            self.comment(number, text, train.REVIEW_REQUEST_EVENT, via=self.requester, head=head, reason=reason)
        except GitHubError as error:
            self.note(f"#{number}: could not ask Codex to review ({error}); the next step tries again")
            return
        pr.setdefault("reviewRequests", []).append(self.now)
        self.note(f"#{number}: asked Codex to review {head[:9]}: {reason}")

    def all_files(self, pr: Dict[str, Any]) -> Optional[List[str]]:
        if not pr.get("filesTruncated"):
            return None
        return [f["filename"] for f in self.gh.rest_paginated(f"repos/{{repo}}/pulls/{pr['number']}/files?per_page=100")]

    def lane_classes(self, main: str, ready: List[Dict[str, Any]]) -> Dict[int, str]:
        """LIGHT or HEAVY for each ready PR (train.LaneClassifier), judged by the lane
        selector at `main`. Empty for fewer than two: one ready PR is the batch whatever
        its class, and an empty map is the old order."""
        if len(ready) < 2:
            return {}
        classifier = train.LaneClassifier(lambda path: self.git.run("show", f"{main}:{path}").stdout)
        if classifier.error:
            self.note(f"every ready PR counts as heavy: {classifier.error}")
            return {}
        classes = {}
        for pr in ready:
            kind, reason = classifier.classify(self.changed_paths(main, pr["number"], pr["head"]))
            classes[pr["number"]] = kind
            self.note(f"#{pr['number']} is {kind}: {reason}")
        return classes

    def changed_paths(self, main: str, number: int, head: str) -> Optional[List[str]]:
        """The PR's changed paths as ci.yml's selector computes them (merge base with main
        to head, both sides of a rename), or None when they cannot be established."""
        try:
            self.git.out("fetch", "--quiet", "origin", f"+refs/pull/{number}/head:refs/coord/pr/{number}")
            if self.git.out("rev-parse", f"refs/coord/pr/{number}") != head:
                return None
            base = self.git.out("merge-base", main, head)
            diff = self.git.run("diff", "--name-only", "--no-renames", "-z", base, head, "--").stdout
        except (GitError, OSError, ValueError):
            return None
        return [path for path in diff.split("\0") if path]

    # MARK: - Step

    def run(self) -> List[str]:
        self.git.out("fetch", "--quiet", "--prune", "origin",
                     "+refs/heads/main:refs/remotes/origin/main",
                     "+refs/heads/train/*:refs/remotes/origin/train/*")
        main = self.git.out("rev-parse", "refs/remotes/origin/main")
        refs = self.git.out("for-each-ref", "--sort=refname", "--format=%(refname:strip=3) %(objectname)",
                            "refs/remotes/origin/train/")
        batches = []
        for line in refs.splitlines():
            if not line.strip():
                continue
            name, tip = line.split()
            # Only branches the train built: their tip carries its Train-Batch trailer. A
            # person's `train/fix-cache` is left alone, never deleted (substitute review).
            trailer = self.git.out("log", "-1", "--format=%(trailers:key=Train-Batch,valueonly,separator=%x2C)", tip)
            if trailer.strip() == name:
                batches.append((name, tip))
            else:
                self.note(f"{name} is not a batch (no Train-Batch trailer); ignoring it")
        prs = self.open_prs()
        if batches:
            name, tip = batches[0]
            self.advance(name, tip, main, prs)
            if self.closed_batch:
                self.wake_next()
        else:
            self.build(main, prs)
        return self.lines

    def wake_next(self) -> None:
        """A batch just ended (merged, ejected, split, abandoned): start the next step now
        instead of leaving the queue to the late cron (stress test, 2026-09-25). Bounded:
        only a step that closed a batch dispatches, and a step that finds nothing ready
        closes nothing."""
        try:
            self.gh.dispatch_workflow("merge-train.yml", "main")
            self.note("dispatched the next train step")
        except GitHubError as error:
            self.note(f"could not dispatch the next train step ({error}); the cron will")

    def already_posted(self, number: int, event: str, batch: str) -> bool:
        for comment in self.gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?per_page=100"):
            payload = marker.decode(comment.get("body"))
            if payload and self.own_record(comment, payload) and payload.get("event") == event \
                    and payload.get("batch") == batch:
                return True
        return False

    def finish(self, name: str, tip: str, members: List[Dict[str, Any]], prs: Dict[int, Dict[str, Any]],
               run: Optional[Dict[str, Any]]) -> None:
        """Everything after a green batch: labels off, a receipt on each PR, (live) the
        merged branches' runs cancelled, then the batch branch deleted.

        Each member is handled on its own and every step is safe to repeat, so a failure
        halfway (after main already moved) leaves the batch branch in place and the next
        step finishes the job without a second receipt (Codex on #989).
        """
        numbers = ", ".join(f"#{m['pr']}" for m in members)
        url = (run or {}).get("html_url", "")
        event = "merged" if self.mode == "live" else "green-dry-run"
        failures = []
        for member in members:
            number = member["pr"]
            try:
                self.remove_label(number, train.TRAIN_LABEL)
                self.remove_label(number, train.SOLO_LABEL)
                if not self.already_posted(number, event, name):
                    if self.mode == "live":
                        text = (f"**Merged by the merge train** in batch `{name}` ({numbers}): main is now "
                                f"`{tip[:9]}`, the exact commit CI tested: {url}")
                    else:
                        text = (f"**Merge train (dry run):** batch `{name}` ({numbers}) passed CI at `{tip[:9]}`: "
                                f"{url}\n\nThe train is in dry-run mode, so main was not changed and this PR left "
                                f"the queue. Merge it the usual way.")
                    self.comment(number, text, event, batch=name, run=(run or {}).get("id"), tip=tip)
                if self.mode == "live":
                    self.close_issues(number, prs.get(number), name)
                    branch = (prs.get(number) or {}).get("branch") \
                        or ((self.gh.rest(f"repos/{{repo}}/pulls/{number}") or {}).get("head") or {}).get("ref")
                    if branch and not self.cancel_branch_runs(branch):
                        failures.append(f"#{number}: some runs of {branch} were not cancelled")
            except GitHubError as error:
                failures.append(f"#{number}: {error}")
        if failures:
            self.note(f"{name}: clean-up incomplete ({'; '.join(failures)}); the next step finishes it")
            return
        self.delete_branch(name)

    def close_issues(self, number: int, pr: Optional[Dict[str, Any]], batch: str) -> None:
        """Close the issues a merged PR says it closes (`Closes #N`), once each.

        GitHub does not do it for a PR the train merged: main moves by a ref update, not a
        PR merge, and auto-close fires only on the merge. Measured 2026-09-26: 13 of the 15
        issues named by train-merged PRs were still open, while #1036, merged by hand, closed
        #929. That also left their coord claims standing on the board. An issue already
        closed (by hand, or by an earlier step that was interrupted) is left alone, so a
        repeated step closes nothing twice. `pr` is the open PR as this step read it; when
        a later step finishes the batch the PR is already merged, so its body is read (that
        path sees only `Closes #N` in the text, not issues linked in the sidebar).

        Best effort (substitute review of #1050): a failure is noted and never added to the
        batch's clean-up failures, because a permanent one (a typo'd `Fixes #9999`, a locked
        issue) would keep the batch branch and stall the train at every step. A reference
        to another repository, or a number that is a pull request, is never closed."""
        refs = (pr or {}).get("closesRefs")
        if refs is None:
            if pr is not None and pr.get("closes") is not None:
                refs = [{"number": n} for n in pr["closes"]]
            else:
                pull = self.gh.rest(f"repos/{{repo}}/pulls/{number}") or {}
                refs = [{"number": int(n)} for n in snapshot._CLOSES.findall(pull.get("body") or "")]
        issues = [r["number"] for r in refs if isinstance(r.get("number"), int)
                  and (not r.get("repo") or r["repo"].lower() == self.gh.repo.lower())]
        for issue in dict.fromkeys(issues):
            try:
                current = self.gh.rest(f"repos/{{repo}}/issues/{issue}") or {}
                if current.get("state") != "open" or current.get("pull_request"):
                    continue
                # Close first, then the receipt: a lost receipt is less harm than a second one.
                self.gh.rest(f"repos/{{repo}}/issues/{issue}", method="PATCH",
                             body={"state": "closed", "state_reason": "completed"})
                payload = {"kind": "train", "v": 1, "agent": self.agent, "event": "closed-issue", "pr": number,
                           "issue": issue, "batch": batch, "mode": self.mode}
                self.gh.rest(f"repos/{{repo}}/issues/{issue}/comments", method="POST", body={"body": marker.compose(
                    f"**Closed by the merge train:** #{number}, which closes this issue, merged in batch `{batch}`.",
                    payload)})
                self.note(f"#{number}: closed #{issue}")
            except GitHubError as error:
                self.note(f"#{number}: could not close #{issue} ({str(error)[:120]}); left for a person")

    def advance(self, name: str, tip: str, main: str, prs: Dict[int, Dict[str, Any]]) -> None:
        base, members = train.parse_batch(
            self.git.out("log", "--first-parent", f"--max-count={train.BATCH_CAP + 1}",
                         f"--format={train.LOG_FORMAT}", tip), name)
        if not members:
            self.note(f"{name}: its tip names the batch but no batch merges were found; leaving it for a person")
            return
        numbers = ", ".join(f"#{m['pr']}" for m in members)
        run = self.settle(name, tip, self.batch_run(name, tip))
        message = self.git.out("log", "-1", "--format=%B", tip)
        plan = train.read_plan(message, name) or {
            "version": 1, "batch": name, "base": base, "batchCap": train.BATCH_CAP,
            "builtAt": snapshot.iso(dt.datetime.fromtimestamp(int(self.git.out("log", "-1", "--format=%ct", tip)), dt.timezone.utc)),
            "members": [{"pr": member["pr"], "head": member["sha"], "queuedAt": None} for member in members],
            "lanePlan": {"class": train.HEAVY, "lanes": None, "source": "unavailable", "at": base,
                         "reason": "historical batch has no persisted lane plan"},
        }
        self._receipt = dict(plan, tip=tip, run=None if run is None else {
            "id": run["id"], "attempt": train_metrics.attempts(run), "status": run.get("status"),
            "conclusion": run.get("conclusion"), "url": run.get("html_url")})
        if self.git.run("merge-base", "--is-ancestor", tip, "refs/remotes/origin/main", check=False).returncode == 0:
            # Main already holds the batch: an earlier step moved it and was interrupted.
            self.note(f"{name} ({numbers}): main already contains {tip[:9]}; finishing the clean-up")
            self.finish(name, tip, members, prs, run)
            return
        action = train.run_action(run, len(members))
        if action == "dispatch":
            others = self.runs_at(name, tip)
            if others:
                # A run exists but is not recognised as the train's own: never dispatch
                # again on top of it (that would repeat a full CI run every tick). Wait for
                # a person to look (substitute review of #989).
                actors = sorted({(r.get("actor") or {}).get("login", "?") for r in others})
                self.note(f"{name} ({numbers}): {len(others)} ci.yml run(s) at {tip[:9]} by {', '.join(actors)}, "
                          "none dispatched by the train; not dispatching again")
                return
            built = dt.datetime.fromtimestamp(int(self.git.out("log", "-1", "--format=%ct", tip)), dt.timezone.utc)
            if self.now - built < dt.timedelta(minutes=DISPATCH_GRACE_MINUTES):
                self.note(f"{name} ({numbers}): CI dispatched {int((self.now - built).total_seconds())}s ago, not registered yet")
                return
            if not self.request_ci("redispatch"):
                return
            self.gh.dispatch_workflow(train.CI_WORKFLOW, name, {"lanes": train.CI_LANES})
            self.note(f"{name} ({numbers}): no CI run at {tip[:9]} at all (the dispatch was lost); dispatched once more")
            return
        url = run["html_url"]
        if action == "wait":
            self.note(f"{name} ({numbers}): CI {run.get('status')} {url}")
            return
        if action == "rerun":
            if not self.request_ci("rerun", run):
                return
            try:
                self.gh.rest(f"repos/{{repo}}/actions/runs/{run['id']}/rerun-failed-jobs", method="POST")
            except GitHubError as error:
                self.note(f"{name} ({numbers}): CI red and the re-run was refused ({error}); deleting the batch")
                self.record_outcome("abandoned", f"Merge train: retry refused for `{name}`; the batch will be rebuilt.")
                self.delete_branch(name)
                return
            self.note(f"{name} ({numbers}): CI red on attempt {run.get('run_attempt')}; re-running the failed jobs once {url}")
            return
        if action == "abandon":
            self.note(f"{name} ({numbers}): CI {run.get('conclusion')}; deleting the batch, the next step rebuilds")
            self.record_outcome("abandoned", f"Merge train: `{name}` ended with {run.get('conclusion')}; rebuilding later.")
            self.delete_branch(name)
            return
        if action in ("eject", "split"):
            failed = self.failed_jobs(run["id"])
            lanes = [job for job in failed if job not in FINAL_JOBS]  # the aggregate verdict adds nothing
            jobs = ", ".join((lanes or failed)[:5]) or "see the run"
            main_state = self.main_ci(main)
            # Lane jobs only: the aggregate verdict is red whenever anything is, so it
            # would always look "shared" between two red runs.
            shared = sorted((set(failed) & set(main_state.get("failed") or [])) - set(FINAL_JOBS))
            if shared:
                # Red on main too: not these PRs' fault. Removing them one by one would
                # empty the queue while main is broken (substitute review of #989).
                self.note(f"{name} ({numbers}): CI red in {', '.join(shared[:5])}, which is red on main too "
                          f"({main_state.get('url')}); not blaming the PRs, the queue waits for main")
                self.record_outcome("abandoned", f"Merge train: `{name}` shares failing lanes with main; waiting for main.")
                self.delete_branch(name)
                return
            for member in members:
                number = member["pr"]
                if action == "eject":
                    self.remove_label(number, train.TRAIN_LABEL)
                    self.remove_label(number, train.SOLO_LABEL)
                    self.comment(number, (
                        f"**Removed from the merge train:** CI failed on this PR alone, twice "
                        f"({jobs}): {url}\n\nFix it and add it again with `Tools/coord/coord train add {number}`."
                    ), "ejected", batch=name, run=run["id"], failed=failed[:10],
                       to=self.recipients(prs.get(number) or {"number": number}))
                else:
                    self.add_label(number, train.SOLO_LABEL)
                    self.comment(number, (
                        f"Batch `{name}` ({numbers}) failed CI twice ({jobs}): {url}\n\n"
                        f"Each PR in it will now be tested on its own; this one stays in the train."
                    ), "split", batch=name, run=run["id"], failed=failed[:10])
            self.note(f"{name} ({numbers}): CI red twice ({jobs}); {action}")
            self.delete_branch(name)
            return
        # action == "merge": check everything again; the batch was green at `tip`.
        if main != base:
            if self.mode == "live":
                self.note(f"{name} ({numbers}): green, but main moved from {base[:9]} to {main[:9]}; rebuilding")
                self.record_outcome("rebuilt", f"Merge train: `{name}` passed, but main moved; a new batch must be tested.")
                self.delete_branch(name)
                return
            # Dry run: report the green result instead of re-testing on every hand merge,
            # which on a busy day would mean a dry-run batch never reports at all.
            self.note(f"{name} ({numbers}): green on {base[:9]}; main has since moved to {main[:9]} "
                      "(live mode would rebuild and re-test)")
        # Fresh state for the final check: a push or a `train remove` since this step
        # started must stop the merge (Codex on #989). What main receives is still the
        # tested tip; the window left is the second between this read and the update.
        prs = self.open_prs()
        for member in members:
            number = member["pr"]
            pr = prs.get(number)
            problem = None
            hold, reason = "", ""
            if pr is None:
                problem = "is no longer open"
            elif pr["head"] != member["sha"]:
                problem = f"moved from {member['sha'][:9]} to {pr['head'][:9]}"
                if train.TRAIN_LABEL in pr["labels"]:
                    hold, reason = train.HOLD_HEAD_MOVED, f"its head moved from {member['sha'][:9]} to {pr['head'][:9]}"
            elif train.TRAIN_LABEL not in pr["labels"]:
                problem = "was taken out of the train"
            else:
                self.train_added(pr)  # refresh the recorded-review and approval heads for the final check
                verdict, hold, reason = train.assess(pr, all_files=self.all_files(pr))
                if verdict != train.READY:
                    problem = f"is no longer ready ({reason})"
            if problem:
                self.note(f"{name} ({numbers}): green, but #{number} {problem}; rebuilding without it")
                self.record_outcome("rebuilt", f"Merge train: `{name}` passed, but #{number} {problem}; rebuilding.")
                self.delete_branch(name)  # the decision first; the notice can never stop it
                if hold:  # still queued and now held (a DROP is commented on by the next build)
                    self.notify_held(pr, pr["head"], hold, reason)
                return
        if self.mode == "live":
            try:
                self.gh.rest("repos/{repo}/git/refs/heads/main", method="PATCH", body={"sha": tip, "force": False})
            except GitHubError as error:
                # The update may have been applied with its response lost. The batch
                # branch is the only record of what to clean up, so look before deleting
                # it, and keep it when even that cannot be told (Codex on #989).
                try:
                    now_main = ((self.gh.rest("repos/{repo}/git/ref/heads/main") or {}).get("object") or {}).get("sha")
                except GitHubError as reread:
                    self.note(f"{name} ({numbers}): main update failed ({error}) and main cannot be re-read "
                              f"({reread}); keeping the batch for the next step")
                    return
                if now_main != tip:
                    self.note(f"{name} ({numbers}): green, but main could not be fast-forwarded ({error}); rebuilding")
                    self.record_outcome("rebuilt", f"Merge train: `{name}` passed, but main could not be fast-forwarded; rebuilding.")
                    self.delete_branch(name)
                    return
                self.note(f"{name} ({numbers}): the main update reported {error}, but main is at {tip[:9]}")
        verb = "main fast-forwarded to" if self.mode == "live" else "dry run: would have fast-forwarded main to"
        self.note(f"{name} ({numbers}): CI green; {verb} {tip[:9]}")
        self.finish(name, tip, members, prs, run)

    def build(self, main: str, prs: Dict[int, Dict[str, Any]]) -> None:
        queued = [pr for pr in prs.values() if train.TRAIN_LABEL in pr["labels"]]
        if not queued:
            self.note("queue empty")
            return
        entries = []
        verdicts = {}
        holds = {}
        for pr in queued:
            added = self.train_added(pr)
            entries.append({"pr": pr["number"], "at": added.get("at") or "9999"})
            verdict, holds[pr["number"]], reason = train.assess(pr, added.get("head"), self.all_files(pr))
            verdicts[pr["number"]] = (verdict, reason)
        entries.sort(key=lambda e: (e["at"], e["pr"]))
        classes = self.lane_classes(main, [prs[n] for n in verdicts if verdicts[n][0] == train.READY])
        chosen = train.plan(entries, verdicts, {n: prs[n]["labels"] for n in verdicts}, classes=classes,
                            overtaken=[n for n in verdicts if prs[n].get("overtakenBy")])
        for number, reason in chosen["drops"]:
            self.remove_label(number, train.TRAIN_LABEL)
            self.comment(number, f"**Removed from the merge train:** {reason}.", "removed", reason=reason,
                         to=self.recipients(prs.get(number)))
            self.note(f"#{number} removed: {reason}")
        for number, reason in chosen["holds"]:
            self.note(f"#{number} held: {reason}")
            self.notify_held(prs[number], prs[number]["head"], holds[number], reason)
        for pr in queued:
            if verdicts[pr["number"]][0] != train.DROP:
                self.request_review(pr)
        if not chosen["batch"]:
            self.note("nothing ready")
            return

        main_state = self.main_ci(main)
        if main_state.get("conclusion") == "failure":
            self.note(f"main {main[:9]} is red ({', '.join(main_state.get('failed', [])[:3]) or 'see the run'}, "
                      f"{main_state.get('url')}); not building a batch on it")
            return
        name = train.batch_name(self.now)
        identity = {"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
                    "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}
        tip = main
        merged = []
        member_heads = []
        for number in chosen["batch"]:
            pr = prs[number]
            self.git.out("fetch", "--quiet", "origin", f"+refs/pull/{number}/head:refs/coord/pr/{number}")
            head = self.git.out("rev-parse", f"refs/coord/pr/{number}")
            if head != pr["head"]:
                self.note(f"#{number} held: its head changed while the batch was built")
                self.notify_held(pr, head, train.HOLD_HEAD_MOVED,
                                 f"its head moved from {pr['head'][:9]} to {head[:9]} while a batch was being built")
                continue
            result = self.git.run("merge-tree", "--write-tree", "--no-messages", tip, head, check=False)
            if result.returncode == 1:
                reason = f"it does not merge cleanly on top of {', '.join('#%d' % m for m in merged) or 'main'}"
                self.note(f"#{number} held: {reason}")
                self.notify_held(pr, head, train.HOLD_CONFLICT, reason)
                continue
            if result.returncode != 0:
                raise GitError(f"git merge-tree: {result.stderr.strip()}")
            tree = result.stdout.split()[0]
            paragraphs = [arg for text in train.merge_message(pr, self.gh.owner, name) for arg in ("-m", text)]
            last_parent = tip
            last_head = head
            tip = self.git.out("commit-tree", tree, "-p", last_parent, "-p", head, *paragraphs, env=identity)
            merged.append(number)
            member_heads.append({"pr": number, "head": head, "queuedAt": pr.get("trainAddedAt")})
        if not merged:
            self.note("nothing merged cleanly; no batch")
            return
        paths = [path for path in self.git.run("diff", "--name-only", "--no-renames", "-z", main, tip, "--").stdout.split("\0") if path]
        lane_plan = train.LaneClassifier(lambda path: self.git.run("show", f"{main}:{path}").stdout).lane_plan(paths)
        lane_plan.update(source="main", at=main)
        plan = {"version": 1, "batch": name, "base": main, "members": member_heads,
                "builtAt": snapshot.iso(self.now), "batchCap": train.BATCH_CAP, "lanePlan": lane_plan}
        # Add the plan to the final merge message before publishing anything. Keeping
        # its two parents preserves parse_batch and GitHub's normal merged-PR history.
        tip = self.git.out("commit-tree", tree, "-p", last_parent, "-p", last_head, *paragraphs[:-2],
                           "-m", paragraphs[-1] + "\n" + train.plan_trailer(plan), env=identity)
        self._receipt = dict(plan, tip=tip, run=None)
        if not self.request_ci("dispatch"):
            return
        if chosen["overtakes"]:
            # Recorded BEFORE the push: a failure between the two can only give heavy work
            # its turn early, never let a second light batch ahead. A comment, not a merge
            # trailer: it holds whether or not this batch ever reaches main.
            numbers = ", ".join(f"#{n}" for n in merged)
            self.comment(chosen["overtakes"], (
                f"Merge train: light batch `{name}` ({numbers}) goes ahead of this PR, whose changes select "
                f"heavy CI lanes. At most one light batch goes ahead of ready heavy work; heavy goes next."
            ), train.OVERTAKEN_EVENT, batch=name, members=merged)
            self.note(f"{name}: light, ahead of heavy #{chosen['overtakes']} (its one overtake)")
        self.git.out("push", "--quiet", "origin", f"{tip}:refs/heads/{name}")
        self.gh.dispatch_workflow(train.CI_WORKFLOW, name, {"lanes": train.CI_LANES})
        numbers = ", ".join(f"#{n}" for n in merged)
        for number in merged:
            self.comment(number, (
                f"Merge train: testing in batch `{name}` ({numbers}) at `{tip[:9]}` "
                f"({'live' if self.mode == 'live' else 'dry run'})."
            ), "testing", batch=name, tip=tip, members=merged)
        self.note(f"{name}: built {numbers} on main {main[:9]} -> {tip[:9]}; dispatched ci.yml lanes={train.CI_LANES}")


def ensure_labels(gh: GitHub) -> None:
    for name, (color, description) in train.LABELS.items():
        try:
            gh.rest("repos/{repo}/labels", method="POST", body={"name": name, "color": color, "description": description})
        except AppAuthError:
            raise  # no app token is not "the label exists"
        except GitHubError:
            pass  # exists

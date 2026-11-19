"""The merge train: land ready PRs on main in batches, one CI run per batch.

GitHub's merge queue is not available for a private repository owned by a personal
account, so this is built from parts that are: a label (the queue), a batch branch
`train/<stamp>` (the batch and its state), a `ci.yml` dispatch with `lanes=changed`
(the gate), and a fast-forward of main to exactly the tested commit (the merge).
`.github/workflows/merge-train.yml` runs `coord train step` one at a time on the
light runner; each step reads the state from GitHub, does one thing, and exits.

The pure rules (eligibility, planning, what to do with a finished run, reading a
batch back from git) are separate from the step that performs them, so tests can
cover every branch of the state machine without GitHub.

Merge rule (memory: feedback-pr-train-protocol), re-checked when a batch is built
AND again just before main moves — the `train` label is intent, not approval:
- the current head is reviewed: Codex 👍 on it (review.codex_state), or a clean review
  of it by any agent (Alex, 2026-09-26), recorded by `coord train review` in a comment
  naming that head. That record, like Alex's approval below, counts only when a
  trusted user posted it, never a bot (snapshot.trusted_attestation). It counts while
  Codex is still re-reviewing the head, too (Alex, 2026-09-29, #1181): the train waits
  for Codex only on a head with no clean review recorded;
- no unanswered Codex findings, clean review or not. Findings that land while the PR
  is in a batch take it out at the final check, before main moves;
- CI at the head not red (pending is fine: the batch runs the gates anyway). A check
  GitHub never started (review.NOT_RUN_PREFIX, e.g. refused by the Actions budget) is
  in `ci.not_run`, not `ci.failing`: it neither holds the PR nor counts for it;
- not a carve-out: sound-character changes, persistence changes without a
  migration and public API breaks carry `needs-alex` until Alex approves THE
  CURRENT HEAD (`coord train approve`, which records the head and adds
  `alex-approved`). Adding a PR to the train asserts it is not a carve-out.

Order (Alex, 2026-09-25: bounded light priority): each ready PR is light or heavy by
main's own CI lane selector (LaneClassifier). A batch never mixes the two, and while
both are ready at most one light batch goes ahead of ready heavy work (plan).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import types
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config, marker

TRAIN_LABEL = "train"
SOLO_LABEL = "train:solo"
NEEDS_ALEX_LABEL = "needs-alex"
ALEX_APPROVED_LABEL = "alex-approved"
REVIEWED_LABEL = "reviewed"
BATCH_PREFIX = "train/"
BATCH_CAP = 3


class NotConfigured(ValueError):
    """The repo has not configured the merge train (.coord/config.json)."""


NOT_CONFIGURED_MESSAGE = ("merge train is not configured for this repo: set 'ci_workflow' and 'train_workflow' "
                          "in .coord/config.json (board, claim, send and inbox work without them)")


def is_configured() -> bool:
    return bool(config.get("ci_workflow")) and bool(config.get("train_workflow"))


def _required(key: str) -> str:
    value = config.get(key)
    if not value:
        raise NotConfigured(NOT_CONFIGURED_MESSAGE)
    return value


def ci_workflow() -> str:
    """The CI workflow file the train dispatches and reads (config `ci_workflow`)."""
    return _required("ci_workflow")


def train_workflow() -> str:
    """The workflow that runs `coord train step` (config `train_workflow`)."""
    return _required("train_workflow")


def ci_lanes() -> Optional[str]:
    """The `lanes` input the train dispatches the gate with; None sends no inputs."""
    return config.get("ci_lanes") or None


def ci_dispatch_inputs(lanes: Optional[str] = None) -> Optional[Dict[str, str]]:
    lanes = lanes or ci_lanes()
    return {"lanes": lanes} if lanes else None


def verdict_job() -> Optional[str]:
    return config.get("verdict_job") or None


def wake_job() -> Optional[str]:
    return config.get("wake_job") or None


def final_jobs() -> Tuple[str, ...]:
    """The CI workflow's aggregate jobs (verdict, wake): red whenever any lane is."""
    return tuple(job for job in (verdict_job(), wake_job()) if job)


def owner_login(repo: Optional[str] = None) -> Optional[str]:
    """The login that approves carve-outs: config `owner_login`, else the repo's owner."""
    return config.get("owner_login") or (repo.split("/", 1)[0] if repo else None)


def owner_mention(repo: Optional[str] = None) -> str:
    """What a notice @-mentions to reach the owner; "" when there is nobody to mention."""
    mention = config.get("owner_mention")
    if mention:
        return mention
    login = owner_login(repo)
    return "@" + login if login else ""


def owner_only_paths() -> Tuple[str, ...]:
    """Path prefixes whose changes wait for the owner's approval of the head (config)."""
    return tuple(config.get("owner_only_paths") or ())


_default_branch_cache: Dict[str, str] = {}
_last_default_branch: List[str] = []


def default_branch(gh: Any = None) -> str:
    """The branch the train builds on: config `default_branch`, else the repository's
    default branch from GitHub (one call, cached per repo). Without a GitHub handle it
    reuses the last branch detected in this process, then falls back to "main"."""
    configured = config.get("default_branch")
    if configured:
        return configured
    repo = getattr(gh, "repo", None)
    if repo and repo in _default_branch_cache:
        return _default_branch_cache[repo]
    if gh is not None and repo:
        from .gh import AppAuthError, GitHubError
        try:
            branch = (gh.rest("repos/{repo}") or {}).get("default_branch")
        except AppAuthError:
            raise
        except GitHubError:
            branch = None
        if branch:
            _default_branch_cache[repo] = branch
            _last_default_branch[:] = [branch]
            return branch
    return _last_default_branch[0] if _last_default_branch else "main"


def lane_classifier_files() -> Optional[Tuple[str, str]]:
    """(selector script, rules file) paths at the default branch, or None: every PR is then one lane."""
    cfg = config.get("lane_classifier") or {}
    script, rules = cfg.get("script"), cfg.get("rules")
    return (script, rules) if script and rules else None


_LAZY_NAMES = {
    # Compatibility names for importers that read these as constants; each reads config on access.
    "CI_WORKFLOW": lambda: config.get("ci_workflow"),
    "CI_LANES": ci_lanes,
    "VERDICT_JOB": verdict_job,
    "WAKE_JOB": wake_job,
    "FINAL_JOBS": final_jobs,
    "OWNER_MENTION": owner_mention,
    "ALEX_ONLY_PATHS": owner_only_paths,
    "OWNER_ONLY_PATHS": owner_only_paths,
    "LANE_SELECTOR": lambda: (lane_classifier_files() or (None, None))[0],
    "LANE_RULES": lambda: (lane_classifier_files() or (None, None))[1],
}


def __getattr__(name: str) -> Any:
    lazy = _LAZY_NAMES.get(name)
    if lazy is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return lazy()


WORKFLOW_DIR = ".github/workflows/"
# The inisa-sidechain app's installation token, minted by merge-train.yml with contents
# and workflows write, for the batch pushes and main moves that carry workflow changes.
# Unset: workflow PRs are dropped for a hand merge, as before the app had the permission.
WORKFLOW_TOKEN_ENV = "COORD_TRAIN_APP_TOKEN"
# What coord's own views (`train add`, `train status`, the board, the conductor's stuck
# check) assume, since they cannot see whether the Actions step will hold that token:
# that it will (Alex granted the app workflows write, 2026-10-04). Only the step decides;
# without the token it takes the PR out with the hand-merge comment.
VIEWS_ASSUME_WORKFLOW_TOKEN = True
# What governs the unattended conductor: its standing orders, the settings, hooks and
# deny list its runs load, and the watcher that starts them. The conductor must not be
# able to widen its own powers through the train, so a repo lists those paths in config
# `owner_only_paths` and they wait for the owner's approval of the head like a
# needs-alex PR, whoever queued them. Default: none.
MAX_ATTEMPTS = 2
# Each light/heavy pool admits at most twelve attempts per 24 hours by default.
# The former twelve-run bound assumed ~1 h per heavy run; counting retries makes
# that bound honest, and a separate light pool keeps cheap docs moving. An explicit
# COORD_TRAIN_DAILY_RUNS still imposes its combined cap (now including attempts).
DAILY_RUNS_DEFAULT = 12

READY, HOLD, DROP = "ready", "hold", "drop"

# Why a queued PR is held: a fixed set. The train's `held` notice (#1034) is keyed on
# (PR, head, class), never on the reason's wording, which changes from step to step
# (the failing checks it lists, the number of findings).
HOLD_DRAFT = "draft"
HOLD_PARTIAL_DATA = "partial-data"
HOLD_NEEDS_ALEX = "needs-alex"
HOLD_NO_REVIEW = "no-review"  # neither a Codex 👍 nor a recorded clean review at the head (Codex re-reviewing or not)
HOLD_UNANSWERED_FINDINGS = "unanswered-findings"
HOLD_CI_RED = "ci-red"
HOLD_HEAD_MOVED = "head-moved"  # the step's own checks, not eligibility: the head moved under a batch
HOLD_CONFLICT = "conflict"      # the step's own checks: `git merge-tree` found a conflict
HOLD_MERGE_CHECK = "merge-check"  # GitHub reports a conflict; the checkout must verify it
HOLD_CLASSES = (HOLD_DRAFT, HOLD_PARTIAL_DATA, HOLD_NEEDS_ALEX, HOLD_NO_REVIEW, HOLD_UNANSWERED_FINDINGS,
                HOLD_CI_RED, HOLD_HEAD_MOVED, HOLD_CONFLICT, HOLD_MERGE_CHECK)
# Holds only Alex can clear: an approval of the head, or (the train never merges a PR it
# cannot read whole) a hand merge. Their notice @-mentions him, so it reaches his GitHub
# notifications even when no agent is running.
OWNER_HOLDS = frozenset({HOLD_NEEDS_ALEX, HOLD_PARTIAL_DATA})
# Every held notice also goes to the conductor, whichever session that is.
CONDUCTOR = "conductor@*"
# What clears each hold; `{pr}` is the PR number.
HOLD_ACTIONS = {
    HOLD_DRAFT: "Mark it ready for review, or take it out with `coord train remove {pr}`.",
    HOLD_PARTIAL_DATA: ("The train never merges a PR it cannot read whole, so this one stays held at this head. "
                        "Alex decides: merge it by hand, or take it out of the train."),
    # `--head` is a placeholder, never the current head: copying the current head would
    # attest whatever is there now, not the head that was reviewed or approved.
    HOLD_NEEDS_ALEX: ("Alex: approve this head (`coord train approve {pr} "
                      "--head <full-sha-Alex-approved> --note \"…\"`, or tell an agent to), or take it out of the "
                      "train. A new push needs a new approval."),
    HOLD_NO_REVIEW: ("It needs Codex's 👍 on this head, or a clean review of this head by any agent, recorded "
                     "with `coord train review {pr} --head <full-reviewed-sha> --by <agent> "
                     "--note \"…\"`."),
    HOLD_UNANSWERED_FINDINGS: ("Answer every finding on the PR. If answering means a push, add the new head "
                               "again with `coord train add {pr}` once it is reviewed."),
    HOLD_CI_RED: ("Fix the failing checks, or re-run them if they flaked. A fix is a new push: add the new head "
                  "again with `coord train add {pr}` once it is reviewed."),
    HOLD_HEAD_MOVED: ("The train takes it out at its next step. Add the new head again with "
                      "`coord train add {pr}` once it is reviewed."),
    HOLD_CONFLICT: ("If it conflicts with main, merge main into it (or rebase) and add it again with "
                    "`coord train add {pr}`. If it conflicts only with a PR ahead of it in the batch, "
                    "it waits for the next batch."),
    HOLD_MERGE_CHECK: ("The train verifies this head against its fetched main with git before removing it for a "
                       "conflict. It stays queued while that check is unavailable."),
}

LABELS = {
    TRAIN_LABEL: ("0e8a16", "coord: queued for the merge train"),
    SOLO_LABEL: ("fbca04", "coord: the merge train tests this PR on its own (it was in a red batch)"),
    REVIEWED_LABEL: ("c2e0c6", "coord: informational; a clean review is recorded (coord train review records which head)"),
    NEEDS_ALEX_LABEL: ("b60205", "coord: a carve-out; the train holds it until Alex approves its head"),
    ALEX_APPROVED_LABEL: ("0e8a16", "coord: Alex approved a head (coord train approve records which)"),
}


def eligibility(pr: Dict[str, Any], head_at_add: Optional[str] = None,
                all_files: Optional[Sequence[str]] = None, workflow_token: bool = False,
                base_branch: Optional[str] = None) -> Tuple[str, str]:
    """(READY | HOLD | DROP, reason). HOLD waits and retries; DROP removes the label.

    `all_files` is the complete file list when the summary's list was truncated.
    """
    verdict, _, reason = assess(pr, head_at_add, all_files, workflow_token, base_branch=base_branch)
    return verdict, reason


def assess(pr: Dict[str, Any], head_at_add: Optional[str] = None,
           all_files: Optional[Sequence[str]] = None, workflow_token: bool = False,
           *, merge_verified: bool = False, base_branch: Optional[str] = None) -> Tuple[str, str, str]:
    """(verdict, hold class, reason): `eligibility` plus, for a HOLD, which of
    HOLD_CLASSES it is ("" for READY and DROP).

    `workflow_token`: the step holds the inisa-sidechain app's installation token, which
    may push workflow files (merge-train.yml mints it). Without it a PR that changes
    `.github/workflows/` is dropped, since the Actions token cannot move main onto it;
    with it, such a PR waits for Alex's approval of its head, like owner_only_paths.
    `base_branch` is the default branch (default_branch() when omitted).
    `merge_verified` is used only by Step after git confirms the exact head merges
    cleanly with its pinned main. Read-only views never override GitHub's metadata."""
    files = list(all_files) if all_files is not None else pr.get("files", [])
    labels = set(pr.get("labels", []))
    base = base_branch or default_branch()
    if pr.get("base") != base:
        return DROP, "", f"its base is {pr.get('base')}, not {base}"
    if pr.get("fromFork"):
        return DROP, "", "it comes from a fork; the train only builds same-repository branches on CI"
    if head_at_add and head_at_add != pr.get("head"):
        return DROP, "", (f"its head moved from {head_at_add[:9]} to {str(pr.get('head'))[:9]} after it was added; "
                          "add it again once the new head is reviewed")
    touched = [f for f in files if f.startswith(WORKFLOW_DIR)]
    if touched and not workflow_token:
        return DROP, "", (f"it changes {touched[0]}: the Actions token cannot push workflow changes, "
                          "so this PR is merged by hand")
    approved_at_head = ALEX_APPROVED_LABEL in labels and pr.get("alexApprovedHead") == pr.get("head")
    if touched and not approved_at_head:
        # CI itself, and the train's own workflow: the app token can merge it, Alex decides.
        return HOLD, HOLD_NEEDS_ALEX, (f"it changes {touched[0]}, a workflow file the train merges only at a head "
                                       "Alex approved (`coord train approve`)")
    guarded = [f for f in files if any(f == p or f.startswith(p) for p in owner_only_paths())]
    if guarded and not approved_at_head:
        return HOLD, HOLD_NEEDS_ALEX, (f"it changes {guarded[0]}, which governs the unattended conductor; "
                                       "Alex approves its head (`coord train approve`)")
    if pr.get("isDraft"):
        return HOLD, HOLD_DRAFT, "it is a draft"
    if pr.get("incomplete"):
        return HOLD, HOLD_PARTIAL_DATA, ("GitHub returned only part of its " + ", ".join(pr["incomplete"])
                                         + "; the train does not decide on partial data")
    if NEEDS_ALEX_LABEL in labels:
        # Alex's approval covers the head he approved, not later pushes: labels persist
        # across pushes (substitute review of #989, P1). `coord train approve` records it.
        if ALEX_APPROVED_LABEL not in labels:
            return HOLD, HOLD_NEEDS_ALEX, "it is labelled needs-alex and waits for Alex (`coord train approve`)"
        if pr.get("alexApprovedHead") != pr.get("head"):
            return HOLD, HOLD_NEEDS_ALEX, (f"alex-approved does not name the current head {str(pr.get('head'))[:9]}; "
                                           "Alex's approval covers only the head he approved")
    if pr.get("head") and pr.get("head") in (pr.get("sidechainBlockedHeads") or []):
        # Checked before Codex: its 👍 does not answer another reviewer's P0-P2 (Codex on #1049).
        return HOLD, HOLD_UNANSWERED_FINDINGS, ("an inisa-sidechain review requested changes at this head "
                                                f"{str(pr.get('head'))[:9]}; only a new push answers it")
    codex = pr.get("codex") or {}
    # A clean review recorded for this exact head is its review, whether or not Codex is
    # still looking (Alex, 2026-09-29, #1181: "Yes, clean review suffices"). It releases
    # only the wait for Codex: findings on the head still hold it, just below.
    clean_at_head = bool(pr.get("reviewedHead")) and pr.get("reviewedHead") == pr.get("head")
    if codex.get("reviewing") and not clean_at_head:
        return HOLD, HOLD_NO_REVIEW, "Codex is re-reviewing it"
    if pr.get("unansweredFindings"):
        # Checked before the 👍: Codex answering a head with findings (a COMMENTED review,
        # no 👍) is a different hold from waiting for a first review, so the train posts a
        # new notice for it and the board says so (#1148).
        count = pr["unansweredFindings"]
        return HOLD, HOLD_UNANSWERED_FINDINGS, (f"{count} unanswered Codex finding" + ("s" if count != 1 else "")
                                                + f" on head {str(pr.get('head'))[:9]}")
    if not codex.get("approved"):
        # A clean review by any agent counts too (Alex, 2026-09-26), for the one head its
        # record names. No label is needed: labels persist across pushes, the record does
        # not (Codex P1 on #989).
        reviewed = pr.get("reviewedHead")
        head = str(pr.get("head"))[:9]
        if not reviewed:
            return HOLD, HOLD_NO_REVIEW, (f"no Codex 👍 on its current head {head} and no clean review of it recorded "
                                          f"(`coord train review {pr.get('number')} --head <full-reviewed-sha> "
                                          "--by <agent>`)")
        if reviewed != pr.get("head"):
            return HOLD, HOLD_NO_REVIEW, (f"no Codex 👍 on its current head {head}, and the recorded clean review "
                                          f"covers {str(reviewed)[:9]}, an older head; a new push needs a new review")
    failing = (pr.get("ci") or {}).get("failing") or []
    if failing:
        return HOLD, HOLD_CI_RED, "CI is red at its head: " + ", ".join(failing[:3])
    if pr.get("mergeable") == "CONFLICTING" and not merge_verified:
        return HOLD, HOLD_MERGE_CHECK, "GitHub reports a conflict with main; awaiting the train's git verification"
    return READY, "", "ready"


CODEX_REQUEST = "@codex review"
REVIEW_REQUEST_EVENT = "review-requested"
REVIEW_REQUEST_AFTER = dt.timedelta(minutes=30)


def review_request_due(pr: Dict[str, Any], now: dt.datetime) -> Optional[str]:
    """Why the train should ask Codex to review the queued PR's head now, or None (#1101).

    Codex reviews some new heads on its own and not others, and a queued PR then sits
    held on "no Codex 👍" until someone notices. Due when nobody has reviewed the head
    (no Codex 👍 or summary naming it, no review under way, no recorded clean review, no
    inisa-sidechain verdict on it), nobody has asked since it was pushed (`reviewRequests`:
    when anyone commented `@codex review` after the push, the train's own record included),
    and either the head is REVIEW_REQUEST_AFTER old or the PR was added again at a new
    head (`readdedAtNewHead`: the train dropped it and its author did what the drop said,
    so there is nothing to wait for).

    Once per head: the guard is the record of asking, never the answer, so the train
    never loops and a person can always ask again by hand.
    """
    head = pr.get("head")
    pushed = pr.get("headPushedAt")
    codex = pr.get("codex") or {}
    reviewed = codex.get("reviewedCommit")
    if not head or pr.get("isDraft") or pushed is None:
        return None
    if codex.get("approved") or codex.get("reviewing") or (reviewed and head.startswith(reviewed)):
        return None
    if pr.get("reviewedHead") == head or head in (pr.get("sidechainBlockedHeads") or []):
        return None
    if any(at >= pushed for at in pr.get("reviewRequests") or []):
        return None
    if pr.get("readdedAtNewHead"):
        return "it was added again to the train at a new head"
    waited = now - pushed
    if waited < REVIEW_REQUEST_AFTER:
        return None
    return f"its head has had no Codex review for {int(waited.total_seconds() // 60)} minutes"


LIGHT, HEAVY = "light", "heavy"
# The train comment (event) recording that a light batch went ahead of a heavy PR.
OVERTAKEN_EVENT = "overtaken"
NO_CLASSIFIER_REASON = "no lane classifier is configured, so every PR is one lane"


class _EveryApp:
    """The app scope the classifier hands the selector. Which apps an `apps` hit builds
    does not matter here: any app build is heavy. The real scope module needs a working
    tree the train's clone does not have."""

    @staticmethod
    def scope_for_paths(root: Any, paths: Sequence[str]) -> Tuple[List[str], str]:
        return ["(any app)"], "the train does not scope apps"


class LaneClassifier:
    """Light or heavy for a PR, by the CI lane selector itself: a PR is heavy when its
    changed paths select any lane outside the selector's LIGHT_LANES, which includes a
    change to CI infrastructure (the selector's `everything` rule). No second path map.

    `show(path)` returns a file's text AT MAIN'S COMMIT (the step passes `git show
    <main>:<path>`; the blob-less clone fetches the blob on demand), so a PR's own
    selector is never executed. Any doubt is heavy, the selector's own rule: a selector
    or rules file that will not load (`error`), an unknown or empty diff, or a failure
    while selecting.
    """

    def __init__(self, show: Callable[[str], str]):
        self.error: Optional[str] = None
        files = lane_classifier_files()
        # Without a configured classifier nothing is executed: every PR is one lane.
        self.configured = files is not None
        if files is None:
            return
        selector, rules = files
        try:
            module = types.ModuleType("select_ci_lanes")
            # Read at import (its REPO constant), used by nothing called here; absolute,
            # so the process's working directory never matters.
            module.__file__ = "/" + selector
            exec(compile(show(selector), "main:" + selector, "exec"), module.__dict__)
            with tempfile.TemporaryDirectory(prefix="coord-lanes-") as scratch:
                rules_file = os.path.join(scratch, "ci-lanes.json")
                with open(rules_file, "w") as handle:
                    handle.write(show(rules))
                self.rules = module.load_rules(rules_file)
            self.selector = module
            self.light_lanes = tuple(module.LIGHT_LANES)
        except Exception as error:  # noqa: BLE001 - any failure classifies everything heavy
            self.error = f"main's lane selector did not load ({type(error).__name__}: {error})"

    def classify(self, paths: Optional[Sequence[str]]) -> Tuple[str, str]:
        """(LIGHT | HEAVY, reason) for a PR whose diff from main's merge base is `paths`
        (None when it could not be established)."""
        plan = self.lane_plan(paths)
        return plan["class"], plan["reason"]

    def lane_plan(self, paths: Optional[Sequence[str]]) -> Dict[str, Any]:
        """Main's planned lane names, not a claim about what a later CI run executed.

        App scope does not affect whether the apps lane is heavy. Unknown plans have
        null lanes and charge the heavy budget, never the cheap one.
        """
        if not self.configured:
            return {"class": HEAVY, "lanes": None, "reason": NO_CLASSIFIER_REASON}
        if self.error:
            return {"class": HEAVY, "lanes": None, "reason": self.error}
        if not paths:
            return {"class": HEAVY, "lanes": None, "reason": "its diff is unknown or empty"}
        try:
            selection = self.selector.Selection([])
            self.selector.select_for_paths(selection, list(paths), self.rules, None, _EveryApp)
            lanes = [lane for lane, on in selection.lanes.items() if on]
            heavy = [lane for lane in lanes if lane not in self.light_lanes]
            if heavy:
                return {"class": HEAVY, "lanes": lanes,
                        "reason": f"it selects {', '.join(heavy)} ({selection.why[heavy[0]]})"}
        except Exception as error:  # noqa: BLE001
            return {"class": HEAVY, "lanes": None,
                    "reason": f"lane selection failed ({type(error).__name__}: {error})"}
        return {"class": LIGHT, "lanes": lanes,
                "reason": "it selects " + (", ".join(lanes) if lanes else "no lane")}


def plan(queue: Sequence[Dict[str, Any]], verdicts: Dict[int, Tuple[str, str]],
         labels: Dict[int, Sequence[str]], cap: int = BATCH_CAP,
         classes: Optional[Dict[int, str]] = None, overtaken: Sequence[int] = ()) -> Dict[str, Any]:
    """Pick the next batch from the queue (already in train order).

    A PR labelled train:solo was in a red batch and is tested alone when it reaches
    the front; others are batched up to `cap`, skipping solo ones behind the front.

    Bounded light priority (Alex, 2026-09-25). `classes` maps a ready PR to LIGHT or
    HEAVY (LaneClassifier; a PR missing from it is heavy). A batch never mixes them.
    With only one class ready the queue order decides, as it always has. With both
    ready, a light batch goes first unless a ready heavy PR is in `overtaken` (a light
    batch already went ahead of it: the step reads that from the PR's comments); then
    heavy goes. `overtakes` names the oldest ready heavy PR a light batch goes ahead
    of, for the step to record before it pushes the batch.
    """
    ready = [q["pr"] for q in queue if verdicts[q["pr"]][0] == READY]
    candidates, overtakes = ready, None
    if classes is not None:
        light = [n for n in ready if classes.get(n) == LIGHT]
        heavy = [n for n in ready if classes.get(n) != LIGHT]
        if light and heavy:
            if any(n in overtaken for n in heavy):
                candidates = heavy
            else:
                candidates, overtakes = light, heavy[0]
    batch: List[int] = []
    if candidates:
        if SOLO_LABEL in labels.get(candidates[0], ()):
            batch = [candidates[0]]
        else:
            batch = [n for n in candidates if SOLO_LABEL not in labels.get(n, ())][:cap]
    return {
        "batch": batch,
        "overtakes": overtakes,
        "drops": [(q["pr"], verdicts[q["pr"]][1]) for q in queue if verdicts[q["pr"]][0] == DROP],
        "holds": [(q["pr"], verdicts[q["pr"]][1]) for q in queue if verdicts[q["pr"]][0] == HOLD],
    }


_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) ")
BATCH_TRAILER = "Train-Batch"
PLAN_TRAILER = "Train-Plan"
# One line per first-parent commit: sha, parents, the Train-Batch trailer, subject.
LOG_FORMAT = "%H%x09%P%x09%(trailers:key=Train-Batch,valueonly,separator=%x2C)%x09%s"


def plan_trailer(plan: Dict[str, Any]) -> str:
    """Persist the batch plan in its final merge commit, including failed batches.

    Actions exposes this message in head_commit, so accounting does not need a git
    fetch or a separate comment read for every historical run. The tip cannot be in
    its own commit message; the full event receipt supplies it from the actual ref.
    """
    return PLAN_TRAILER + ": " + json.dumps(plan, separators=(",", ":"), sort_keys=True)


def read_plan(message: str, batch: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(message, str):
        return None
    lines = [line[len(PLAN_TRAILER) + 2:] for line in message.splitlines()
             if line.startswith(PLAN_TRAILER + ": ")]
    if len(lines) != 1:
        return None
    try:
        plan = marker.parse_json(lines[0])
    except (TypeError, ValueError, RecursionError):
        return None
    return plan if valid_plan(plan, batch) else None


def valid_plan(plan: Any, batch: Optional[str] = None) -> bool:
    """Validate external receipt/trailer fields before accounting interprets them."""
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1 \
            or not isinstance(plan.get("batch"), str) or not plan["batch"].startswith(BATCH_PREFIX) \
            or (batch is not None and plan["batch"] != batch) \
            or not isinstance(plan.get("base"), str) or not re.fullmatch(r"[0-9a-f]{40}", plan["base"]):
        return False
    members, lanes = plan.get("members"), plan.get("lanePlan")
    if not isinstance(members, list) or not members or not isinstance(lanes, dict):
        return False
    for member in members:
        if not isinstance(member, dict) or type(member.get("pr")) is not int or member["pr"] <= 0 \
                or not isinstance(member.get("head"), str) or not re.fullmatch(r"[0-9a-f]{40}", member["head"]) \
                or (member.get("queuedAt") is not None and not isinstance(member["queuedAt"], str)) \
                or ("closes" in member and (not isinstance(member["closes"], list)
                    or len(member["closes"]) > 100
                    or any(type(number) is not int or number <= 0 for number in member["closes"]))):
            return False
    selected = lanes.get("lanes")
    if lanes.get("class") not in (LIGHT, HEAVY) \
            or (selected is not None and (not isinstance(selected, list)
                                         or any(not isinstance(lane, str) for lane in selected))) \
            or (lanes.get("class") == LIGHT and selected is None):
        return False
    if len({member["pr"] for member in members}) != len(members):
        return False
    if "batchCap" in plan and (type(plan["batchCap"]) is not int or plan["batchCap"] < len(members)):
        return False
    for name in ("builtAt", "outcome"):
        if name in plan and not isinstance(plan[name], str):
            return False
    if "tip" in plan and (not isinstance(plan["tip"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["tip"])):
        return False
    run = plan.get("run")
    if run is not None:
        if not isinstance(run, dict) or any(type(run.get(name)) is not int or run[name] < 1 for name in ("id", "attempt")):
            return False
        if any(run.get(name) is not None and not isinstance(run[name], str) for name in ("status", "conclusion", "url")):
            return False
    return True


def parse_batch(log: str, name: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """(base, members oldest first) of batch `name`, from
    `git log --first-parent --format=LOG_FORMAT <tip>`.

    The batch is the run of commits from the tip down whose Train-Batch trailer names
    it; each one's second parent is the PR head it merged, and the first parent of the
    earliest is the main the batch was built on. The trailer, not a diff against the
    current main, identifies the batch: after main is fast-forwarded to the tip that
    diff is empty, and a step interrupted between the fast-forward and the clean-up
    must still find the PRs it merged (Codex on #989). It also keeps earlier batches'
    merges, already on main, out of this one.
    """
    members: List[Dict[str, Any]] = []
    base: Optional[str] = None
    for line in log.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            break
        _, parents, batch, subject = parts
        parent_list = parents.split()
        match = _MERGE_SUBJECT.match(subject)
        if batch.strip() != name or not match or len(parent_list) != 2:
            break
        members.append({"pr": int(match.group(1)), "sha": parent_list[1]})
        base = parent_list[0]
    members.reverse()
    return base, members


GATE_ACTOR = "github-actions[bot]"


def is_gate_run(run: Dict[str, Any], tip: str) -> bool:
    """A ci.yml run the train itself dispatched for this batch tip."""
    return (run.get("head_sha") == tip
            and run.get("event") == "workflow_dispatch"
            and ((run.get("actor") or {}).get("login") == GATE_ACTOR))


# The CI workflow's aggregate jobs (config verdict_job, wake_job; final_jobs()): red
# whenever any lane is, so they say nothing about why.

# Why a red batch run failed (classify), judged by the signals GitHub leaves on each job,
# never by how long it ran: an 8-second exit 1 is a real failure.
REAL, TIMEOUT, INFRA, USER_CANCEL = "real", "timeout", "infra", "user-cancel"
# The job conclusions classify reads. A job that hits `timeout-minutes` ends `cancelled`,
# not `timed_out`, and takes its run to `cancelled` with it.
CLASSIFIED_JOB_CONCLUSIONS = ("failure", "timed_out", "startup_failure", "cancelled")
USER_CANCEL_SIGNAL = "canceled by @"  # "The run was canceled by @alex-bridges."
TIMEOUT_SIGNAL = "exceeded the maximum execution time"
# A job the runner never ran or lost: refused by billing, the runner shut down or vanished.
INFRA_SIGNALS = ("job was not started because", "received a shutdown signal", "lost communication")
SETUP_STEP = "Set up job"
# Reruns an infrastructure failure gets beyond MAX_ATTEMPTS before the batch is abandoned.
INFRA_RETRIES = 2


def job_detail(job: Dict[str, Any], annotations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What classify reads of one job (GitHub's jobs API) and its check-run annotations."""
    steps = job.get("steps") or []
    failed = next((step.get("name") for step in steps
                   if step.get("conclusion") in ("failure", "cancelled", "timed_out")), None)
    return {"id": job.get("id"), "name": job.get("name"), "conclusion": job.get("conclusion"),
            "runner_id": job.get("runner_id"), "runner_name": job.get("runner_name"), "steps": len(steps),
            "failed_step": failed, "annotations": [str(a.get("message") or "") for a in annotations]}


def job_class(job: Dict[str, Any]) -> str:
    """REAL, TIMEOUT, INFRA or USER_CANCEL for one failed or cancelled job (job_detail)."""
    notes = [message.lower() for message in job.get("annotations") or []]
    if any(USER_CANCEL_SIGNAL in note for note in notes):
        return USER_CANCEL
    if job.get("conclusion") == "timed_out" or any(TIMEOUT_SIGNAL in note for note in notes):
        return TIMEOUT
    if (job.get("runner_id") == 0 or not job.get("steps") or job.get("failed_step") == SETUP_STEP
            or any(signal in note for note in notes for signal in INFRA_SIGNALS)):
        return INFRA
    return REAL


def classify(jobs: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Why a red run failed, from its failed and cancelled jobs (job_detail), or None when
    no job is left to judge once the verdict and wake jobs are set aside (a run that never
    started a job, or whose only red job is the verdict): run_action then goes by the
    run's conclusion alone, as it did before jobs were read.

    Precedence: a person's cancel of any job decides the run (cancelling a run also stops
    its queued jobs, which look like infrastructure); then any real failure, so a mix of
    real and infrastructure counts as real; then a timeout; and only a run whose every
    judged job is infrastructure counts as infrastructure.
    """
    classes = {job_class(job) for job in jobs if job.get("name") not in final_jobs()}
    for kind in (USER_CANCEL, REAL, TIMEOUT, INFRA):
        if kind in classes:
            return kind
    return None


def evidence(jobs: Sequence[Dict[str, Any]], kind: str) -> str:
    """The jobs classify judged `kind`, each with its runner and why, for notes and receipts."""
    parts = []
    for job in jobs:
        if job.get("name") in final_jobs() or job_class(job) != kind:
            continue
        notes = [note for note in job.get("annotations") or [] if note]
        why = notes[0] if notes else (f"failed in {job['failed_step']}" if job.get("failed_step") else "no step ran")
        parts.append(f"{job.get('name')} on {job.get('runner_name') or 'no runner'}: {why[:160]}")
    return "; ".join(parts[:3]) + (f"; and {len(parts) - 3} more" if len(parts) > 3 else "")


def run_action(run: Optional[Dict[str, Any]], batch_size: int, failure: Optional[str] = None) -> str:
    """What to do with the batch's CI run: dispatch, wait, merge, rerun, eject, split,
    abandon, or abandon with a receipt naming why (`timed-out`, `infra`).

    `failure` is classify's verdict on a red run's jobs (None when they were not read or
    none was left to judge). An infrastructure failure or a timeout never splits a batch
    or ejects a PR: a timeout is re-run once and an infrastructure failure up to
    INFRA_RETRIES more times than MAX_ATTEMPTS, then the batch is abandoned for a later
    rebuild. A real failure retries then splits or ejects, as always, and now also when the
    run ended `cancelled` or `timed_out`. With no job to judge, only a `failure` conclusion
    takes that path; every other conclusion abandons the batch instead of blaming ordinary
    PRs (substitute review of #989). Step handles one narrower case separately: `startup_failure` on a
    workflow-changing batch removes its workflow members for manual gate review, so the
    same broken workflow is not rebuilt forever.
    """
    if run is None:
        return "dispatch"
    if run.get("status") != "completed":
        return "wait"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "merge"
    attempt = run.get("run_attempt") or 1
    if conclusion in ("failure", "cancelled", "timed_out"):
        if failure == USER_CANCEL:
            return "abandon"
        if failure == TIMEOUT:
            return "rerun" if attempt < MAX_ATTEMPTS else "timed-out"
        if failure == INFRA:
            return "rerun" if attempt < MAX_ATTEMPTS + INFRA_RETRIES else "infra"
        # A real failure is real whatever the run's conclusion: a run whose docs job also
        # timed out ends `cancelled`, and abandoning it would rebuild the same red batch until
        # the budget ran out. rerun-failed-jobs re-runs its cancelled jobs too.
        if failure == REAL or conclusion == "failure":
            if attempt < MAX_ATTEMPTS:
                return "rerun"
            return "eject" if batch_size == 1 else "split"
    return "abandon"  # startup_failure, skipped, neutral, stale; cancelled or timed_out with no job to judge


def batch_name(now: dt.datetime) -> str:
    return BATCH_PREFIX + now.astimezone(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def merge_message(pr: Dict[str, Any], owner: str, batch: str) -> List[str]:
    """Paragraphs for `git commit-tree -m …`: the subject GitHub's merge button writes (so
    history reads the same), the PR title, and the Train-Batch trailer."""
    return [f"Merge pull request #{pr['number']} from {owner}/{pr['branch']}",
            pr.get("title") or f"#{pr['number']}",
            f"{BATCH_TRAILER}: {batch}"]

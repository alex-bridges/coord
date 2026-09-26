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
  trusted user posted it, never a bot (snapshot.trusted_attestation);
- no unanswered Codex findings;
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
import os
import re
import tempfile
import types
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TRAIN_LABEL = "train"
SOLO_LABEL = "train:solo"
NEEDS_ALEX_LABEL = "needs-alex"
ALEX_APPROVED_LABEL = "alex-approved"
REVIEWED_LABEL = "reviewed"
BATCH_PREFIX = "train/"
BATCH_CAP = 3
CI_WORKFLOW = "ci.yml"
CI_LANES = "changed"
WORKFLOW_DIR = ".github/workflows/"
# What governs the unattended conductor: its standing orders, the settings, hooks and
# deny list its runs load, and the watcher that starts them. The conductor must not be
# able to widen its own powers through the train, so these wait for Alex's approval of
# the head like a needs-alex PR, whoever queued them.
# Everything the tick imports is under Tools/coord/, and CLAUDE.md is loaded into every run
# (substitute review of #1043), so all of it is guarded, this list included.
ALEX_ONLY_PATHS = (".claude/agents/", ".claude/settings.json", ".claude/hooks/", "Tools/coord/", "CLAUDE.md")
MAX_ATTEMPTS = 2
# The most batch CI runs the train will start in 24 hours, whatever goes wrong: a
# loop, a red main, a lost dispatch. At ~1 h of heavy runner time each, 12 is the
# heavy runner's whole day. `COORD_TRAIN_DAILY_RUNS` (a repository variable passed by
# merge-train.yml) overrides it.
DAILY_RUNS_DEFAULT = 12

READY, HOLD, DROP = "ready", "hold", "drop"

# Why a queued PR is held: a fixed set. The train's `held` notice (#1034) is keyed on
# (PR, head, class), never on the reason's wording, which changes from step to step
# (the failing checks it lists, the number of findings).
HOLD_DRAFT = "draft"
HOLD_PARTIAL_DATA = "partial-data"
HOLD_NEEDS_ALEX = "needs-alex"
HOLD_NO_REVIEW = "no-review"  # Codex re-reviewing, or neither a Codex 👍 nor a recorded clean review at the head
HOLD_UNANSWERED_FINDINGS = "unanswered-findings"
HOLD_CI_RED = "ci-red"
HOLD_HEAD_MOVED = "head-moved"  # the step's own checks, not eligibility: the head moved under a batch
HOLD_CONFLICT = "conflict"      # the step's own checks: `git merge-tree` found a conflict
HOLD_CLASSES = (HOLD_DRAFT, HOLD_PARTIAL_DATA, HOLD_NEEDS_ALEX, HOLD_NO_REVIEW, HOLD_UNANSWERED_FINDINGS,
                HOLD_CI_RED, HOLD_HEAD_MOVED, HOLD_CONFLICT)
# Holds only Alex can clear: an approval of the head, or (the train never merges a PR it
# cannot read whole) a hand merge. Their notice @-mentions him, so it reaches his GitHub
# notifications even when no agent is running.
OWNER_HOLDS = frozenset({HOLD_NEEDS_ALEX, HOLD_PARTIAL_DATA})
OWNER_MENTION = "@alex-bridges"
# Every held notice also goes to the conductor, whichever session that is.
CONDUCTOR = "conductor@*"
# What clears each hold; `{pr}` is the PR number.
HOLD_ACTIONS = {
    HOLD_DRAFT: "Mark it ready for review, or take it out with `Tools/coord/coord train remove {pr}`.",
    HOLD_PARTIAL_DATA: ("The train never merges a PR it cannot read whole, so this one stays held at this head. "
                        "Alex decides: merge it by hand, or take it out of the train."),
    # `--head` is a placeholder, never the current head: copying the current head would
    # attest whatever is there now, not the head that was reviewed or approved.
    HOLD_NEEDS_ALEX: ("Alex: approve this head (`Tools/coord/coord train approve {pr} "
                      "--head <full-sha-Alex-approved> --note \"…\"`, or tell an agent to), or take it out of the "
                      "train. A new push needs a new approval."),
    HOLD_NO_REVIEW: ("It needs Codex's 👍 on this head, or a clean review of this head by any agent, recorded "
                     "with `Tools/coord/coord train review {pr} --head <full-reviewed-sha> --by <agent> "
                     "--note \"…\"`."),
    HOLD_UNANSWERED_FINDINGS: ("Answer every finding on the PR. If answering means a push, add the new head "
                               "again with `Tools/coord/coord train add {pr}` once it is reviewed."),
    HOLD_CI_RED: ("Fix the failing checks, or re-run them if they flaked. A fix is a new push: add the new head "
                  "again with `Tools/coord/coord train add {pr}` once it is reviewed."),
    HOLD_HEAD_MOVED: ("The train takes it out at its next step. Add the new head again with "
                      "`Tools/coord/coord train add {pr}` once it is reviewed."),
    HOLD_CONFLICT: ("If it conflicts with main, merge main into it (or rebase) and add it again with "
                    "`Tools/coord/coord train add {pr}`. If it conflicts only with a PR ahead of it in the batch, "
                    "it waits for the next batch."),
}

LABELS = {
    TRAIN_LABEL: ("0e8a16", "coord: queued for the merge train"),
    SOLO_LABEL: ("fbca04", "coord: the merge train tests this PR on its own (it was in a red batch)"),
    REVIEWED_LABEL: ("c2e0c6", "coord: informational; a clean review is recorded (coord train review records which head)"),
    NEEDS_ALEX_LABEL: ("b60205", "coord: a carve-out; the train holds it until Alex approves its head"),
    ALEX_APPROVED_LABEL: ("0e8a16", "coord: Alex approved a head (coord train approve records which)"),
}


def eligibility(pr: Dict[str, Any], head_at_add: Optional[str] = None,
                all_files: Optional[Sequence[str]] = None) -> Tuple[str, str]:
    """(READY | HOLD | DROP, reason). HOLD waits and retries; DROP removes the label.

    `all_files` is the complete file list when the summary's list was truncated.
    """
    verdict, _, reason = assess(pr, head_at_add, all_files)
    return verdict, reason


def assess(pr: Dict[str, Any], head_at_add: Optional[str] = None,
           all_files: Optional[Sequence[str]] = None) -> Tuple[str, str, str]:
    """(verdict, hold class, reason): `eligibility` plus, for a HOLD, which of
    HOLD_CLASSES it is ("" for READY and DROP)."""
    files = list(all_files) if all_files is not None else pr.get("files", [])
    labels = set(pr.get("labels", []))
    if pr.get("base") != "main":
        return DROP, "", f"its base is {pr.get('base')}, not main"
    if pr.get("fromFork"):
        return DROP, "", "it comes from a fork; the train only builds same-repository branches on Tycho"
    if head_at_add and head_at_add != pr.get("head"):
        return DROP, "", (f"its head moved from {head_at_add[:9]} to {str(pr.get('head'))[:9]} after it was added; "
                          "add it again once the new head is reviewed")
    touched = [f for f in files if f.startswith(WORKFLOW_DIR)]
    if touched:
        return DROP, "", (f"it changes {touched[0]}: the Actions token cannot push workflow changes, "
                          "so this PR is merged by hand")
    guarded = [f for f in files if any(f == p or f.startswith(p) for p in ALEX_ONLY_PATHS)]
    if guarded and not (ALEX_APPROVED_LABEL in labels and pr.get("alexApprovedHead") == pr.get("head")):
        return HOLD, HOLD_NEEDS_ALEX, (f"it changes {guarded[0]}, which governs the unattended conductor; "
                                       "Alex approves its head (`coord train approve`)")
    if pr.get("mergeable") == "CONFLICTING":
        return DROP, "", "it conflicts with main; merge main into it (or rebase) and add it again"
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
    codex = pr.get("codex") or {}
    if codex.get("reviewing"):
        return HOLD, HOLD_NO_REVIEW, "Codex is re-reviewing it"
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
    if pr.get("unansweredFindings"):
        count = pr["unansweredFindings"]
        return HOLD, HOLD_UNANSWERED_FINDINGS, f"{count} unanswered Codex finding" + ("s" if count != 1 else "")
    failing = (pr.get("ci") or {}).get("failing") or []
    if failing:
        return HOLD, HOLD_CI_RED, "CI is red at its head: " + ", ".join(failing[:3])
    return READY, "", "ready"


LIGHT, HEAVY = "light", "heavy"
# The train comment (event) recording that a light batch went ahead of a heavy PR.
OVERTAKEN_EVENT = "overtaken"
LANE_SELECTOR = ".github/scripts/select-ci-lanes.py"
LANE_RULES = ".github/ci-lanes.json"


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
        try:
            module = types.ModuleType("select_ci_lanes")
            # Read at import (its REPO constant), used by nothing called here; absolute,
            # so the process's working directory never matters.
            module.__file__ = "/" + LANE_SELECTOR
            exec(compile(show(LANE_SELECTOR), "main:" + LANE_SELECTOR, "exec"), module.__dict__)
            with tempfile.TemporaryDirectory(prefix="coord-lanes-") as scratch:
                rules_file = os.path.join(scratch, "ci-lanes.json")
                with open(rules_file, "w") as handle:
                    handle.write(show(LANE_RULES))
                self.rules = module.load_rules(rules_file)
            self.selector = module
            self.light_lanes = tuple(module.LIGHT_LANES)
        except Exception as error:  # noqa: BLE001 - any failure classifies everything heavy
            self.error = f"main's lane selector did not load ({type(error).__name__}: {error})"

    def classify(self, paths: Optional[Sequence[str]]) -> Tuple[str, str]:
        """(LIGHT | HEAVY, reason) for a PR whose diff from main's merge base is `paths`
        (None when it could not be established)."""
        if self.error:
            return HEAVY, self.error
        if not paths:
            return HEAVY, "its diff is unknown or empty"
        try:
            selection = self.selector.Selection([])
            self.selector.select_for_paths(selection, list(paths), self.rules, None, _EveryApp)
            lanes = [lane for lane, on in selection.lanes.items() if on]
            heavy = [lane for lane in lanes if lane not in self.light_lanes]
            if heavy:
                return HEAVY, f"it selects {', '.join(heavy)} ({selection.why[heavy[0]]})"
        except Exception as error:  # noqa: BLE001
            return HEAVY, f"lane selection failed ({type(error).__name__}: {error})"
        return LIGHT, "it selects " + (", ".join(lanes) if lanes else "no lane")


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
# One line per first-parent commit: sha, parents, the Train-Batch trailer, subject.
LOG_FORMAT = "%H%x09%P%x09%(trailers:key=Train-Batch,valueonly,separator=%x2C)%x09%s"


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


def run_action(run: Optional[Dict[str, Any]], batch_size: int) -> str:
    """What to do with the batch's CI run: dispatch, wait, merge, rerun, eject, split, abandon.

    Only a plain `failure` can be the PRs' fault. `timed_out` and `startup_failure` are
    what a lost runner, a full disk or a hung machine look like, so they abandon the
    batch (rebuild later, within the daily budget) instead of splitting it and
    ejecting PRs one by one (substitute review of #989).
    """
    if run is None:
        return "dispatch"
    if run.get("status") != "completed":
        return "wait"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "merge"
    if conclusion == "failure":
        if (run.get("run_attempt") or 1) < MAX_ATTEMPTS:
            return "rerun"
        return "eject" if batch_size == 1 else "split"
    return "abandon"  # timed_out, startup_failure, cancelled, skipped, neutral, stale


def batch_name(now: dt.datetime) -> str:
    return BATCH_PREFIX + now.astimezone(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def merge_message(pr: Dict[str, Any], owner: str, batch: str) -> List[str]:
    """Paragraphs for `git commit-tree -m …`: the subject GitHub's merge button writes (so
    history reads the same), the PR title, and the Train-Batch trailer."""
    return [f"Merge pull request #{pr['number']} from {owner}/{pr['branch']}",
            pr.get("title") or f"#{pr['number']}",
            f"{BATCH_TRAILER}: {batch}"]

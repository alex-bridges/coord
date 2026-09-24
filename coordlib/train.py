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
- Codex 👍 on the current head (review.codex_state), or, during a Codex quota
  outage (Alex's rule), the `codex-unavailable` label PLUS a `review-substitute`
  comment naming the current head (`coord train substitute-review`);
- no unanswered Codex findings;
- CI at the head not red (pending is fine: the batch runs the gates anyway);
- not a carve-out: sound-character changes, persistence changes without a
  migration and public API breaks carry `needs-alex` until Alex approves THE
  CURRENT HEAD (`coord train approve`, which records the head and adds
  `alex-approved`). Adding a PR to the train asserts it is not a carve-out.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

TRAIN_LABEL = "train"
SOLO_LABEL = "train:solo"
NEEDS_ALEX_LABEL = "needs-alex"
ALEX_APPROVED_LABEL = "alex-approved"
CODEX_UNAVAILABLE_LABEL = "codex-unavailable"
BATCH_PREFIX = "train/"
BATCH_CAP = 3
CI_WORKFLOW = "ci.yml"
CI_LANES = "changed"
WORKFLOW_DIR = ".github/workflows/"
MAX_ATTEMPTS = 2
# The most batch CI runs the train will start in 24 hours, whatever goes wrong: a
# loop, a red main, a lost dispatch. At ~1 h of heavy runner time each, 12 is the
# heavy runner's whole day. `COORD_TRAIN_DAILY_RUNS` (a repository variable passed by
# merge-train.yml) overrides it.
DAILY_RUNS_DEFAULT = 12

READY, HOLD, DROP = "ready", "hold", "drop"

LABELS = {
    TRAIN_LABEL: ("0e8a16", "coord: queued for the merge train"),
    SOLO_LABEL: ("fbca04", "coord: the merge train tests this PR on its own (it was in a red batch)"),
    CODEX_UNAVAILABLE_LABEL: ("d93f0b", "coord: Codex out of quota; a substitute review is recorded per head"),
}


def eligibility(pr: Dict[str, Any], head_at_add: Optional[str] = None,
                all_files: Optional[Sequence[str]] = None) -> Tuple[str, str]:
    """(READY | HOLD | DROP, reason). HOLD waits and retries; DROP removes the label.

    `all_files` is the complete file list when the summary's list was truncated.
    """
    files = list(all_files) if all_files is not None else pr.get("files", [])
    labels = set(pr.get("labels", []))
    if pr.get("base") != "main":
        return DROP, f"its base is {pr.get('base')}, not main"
    if pr.get("fromFork"):
        return DROP, "it comes from a fork; the train only builds same-repository branches on Tycho"
    if head_at_add and head_at_add != pr.get("head"):
        return DROP, (f"its head moved from {head_at_add[:9]} to {str(pr.get('head'))[:9]} after it was added; "
                      "add it again once the new head is reviewed")
    touched = [f for f in files if f.startswith(WORKFLOW_DIR)]
    if touched:
        return DROP, (f"it changes {touched[0]}: the Actions token cannot push workflow changes, "
                      "so this PR is merged by hand")
    if pr.get("mergeable") == "CONFLICTING":
        return DROP, "it conflicts with main; merge main into it (or rebase) and add it again"
    if pr.get("isDraft"):
        return HOLD, "it is a draft"
    if pr.get("incomplete"):
        return HOLD, ("GitHub returned only part of its " + ", ".join(pr["incomplete"])
                      + "; the train does not decide on partial data")
    if NEEDS_ALEX_LABEL in labels:
        # Alex's approval covers the head he approved, not later pushes: labels persist
        # across pushes (substitute review of #989, P1). `coord train approve` records it.
        if ALEX_APPROVED_LABEL not in labels:
            return HOLD, "it is labelled needs-alex and waits for Alex (`coord train approve`)"
        if pr.get("alexApprovedHead") != pr.get("head"):
            return HOLD, (f"alex-approved does not name the current head {str(pr.get('head'))[:9]}; "
                          "Alex's approval covers only the head he approved")
    codex = pr.get("codex") or {}
    if codex.get("reviewing"):
        return HOLD, "Codex is re-reviewing it"
    if not codex.get("approved"):
        if CODEX_UNAVAILABLE_LABEL not in labels:
            return HOLD, "no Codex 👍 on its current head"
        # The label persists across pushes; the substitute review covers one head only,
        # the one its `review-substitute` comment names (Codex P1 on #989).
        reviewed = pr.get("substituteReviewHead")
        if reviewed != pr.get("head"):
            return HOLD, ("codex-unavailable is set, but no substitute review names the current head "
                          f"{str(pr.get('head'))[:9]} (`coord train substitute-review {pr.get('number')}`)")
    if pr.get("unansweredFindings"):
        count = pr["unansweredFindings"]
        return HOLD, f"{count} unanswered Codex finding" + ("s" if count != 1 else "")
    failing = (pr.get("ci") or {}).get("failing") or []
    if failing:
        return HOLD, "CI is red at its head: " + ", ".join(failing[:3])
    return READY, "ready"


def plan(queue: Sequence[Dict[str, Any]], verdicts: Dict[int, Tuple[str, str]],
         labels: Dict[int, Sequence[str]], cap: int = BATCH_CAP) -> Dict[str, Any]:
    """Pick the next batch from the queue (already in train order).

    A PR labelled train:solo was in a red batch and is tested alone when it reaches
    the front; others are batched up to `cap`, skipping solo ones behind the front.
    """
    ready = [q["pr"] for q in queue if verdicts[q["pr"]][0] == READY]
    batch: List[int] = []
    if ready:
        if SOLO_LABEL in labels.get(ready[0], ()):
            batch = [ready[0]]
        else:
            batch = [n for n in ready if SOLO_LABEL not in labels.get(n, ())][:cap]
    return {
        "batch": batch,
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

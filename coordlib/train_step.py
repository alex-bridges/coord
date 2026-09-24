"""One step of the merge train, run by .github/workflows/merge-train.yml.

Each step reads the state from GitHub and git, does ONE thing, and exits; the
workflow's concurrency group guarantees one step at a time. A step never waits on
CI: the workflow is woken again when a CI run on a train/ branch completes, and a
cron tick catches anything that event misses. Holding a runner slot while a gate
runs is exactly the waste #915 removed.

State lives only in GitHub:
- the queue: open PRs labelled `train`, ordered by their `train-add` comment;
- the batch: the branch train/<stamp> (main + one --no-ff merge per PR);
- the gate: the newest ci.yml run on that branch at its tip.

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
import subprocess
from typing import Any, Callable, Dict, List, Optional

from . import marker, snapshot, train
from .gh import GitHub, GitHubError, NotFound

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
DISPATCH_GRACE_MINUTES = 10


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
                 agent: str = "train@actions", log: Callable[[str], None] = print):
        if mode not in ("dry-run", "live"):
            raise ValueError(f"mode must be dry-run or live, not {mode!r}")
        self.gh = gh
        self.git = git
        self.mode = mode
        self.now = now or snapshot.now_utc()
        self.agent = agent
        self.log = log
        self.lines: List[str] = []
        self._labels_ready = False

    def note(self, text: str) -> None:
        self.lines.append(text)
        self.log(text)

    # MARK: - GitHub helpers

    def comment(self, number: int, text: str, event: str, **fields: Any) -> None:
        payload = {"kind": "train", "v": 1, "agent": self.agent, "event": event, "pr": number, "mode": self.mode}
        payload.update(fields)
        self.gh.rest(f"repos/{{repo}}/issues/{number}/comments", method="POST",
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
        except (NotFound, GitHubError) as error:
            self.note(f"could not delete {name}: {error}")

    def batch_run(self, name: str, tip: str) -> Optional[Dict[str, Any]]:
        """The batch's gate: the newest ci.yml run at the tip that THE TRAIN dispatched.

        Any other run on the branch (someone's narrow `lanes=docs` dispatch) must never
        count as the gate, or a green partial run could move main while the real gate
        is red (Codex P1 on #989). The train dispatches with the Actions token, so its
        runs' initial actor is the Actions bot; `actor` stays that on a re-run, where
        `triggering_actor` would change.
        """
        pages = self.gh.rest_paginated(
            f"repos/{{repo}}/actions/workflows/{train.CI_WORKFLOW}/runs?branch={name}&per_page=20")
        runs = [r for page in pages for r in page.get("workflow_runs", []) if train.is_gate_run(r, tip)]
        return max(runs, key=lambda r: r["id"]) if runs else None

    def failed_jobs(self, run_id: int) -> List[str]:
        pages = self.gh.rest_paginated(f"repos/{{repo}}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        return [j["name"] for page in pages for j in page.get("jobs", [])
                if j.get("conclusion") in ("failure", "timed_out", "startup_failure")]

    def cancel_branch_runs(self, branch: str) -> None:
        for status in ("in_progress", "queued"):
            try:
                pages = self.gh.rest_paginated(f"repos/{{repo}}/actions/runs?branch={branch}&status={status}&per_page=50")
                for run in [r for page in pages for r in page.get("workflow_runs", [])]:
                    self.gh.rest(f"repos/{{repo}}/actions/runs/{run['id']}/cancel", method="POST")
            except GitHubError as error:
                self.note(f"could not cancel runs of {branch}: {error}")

    def open_prs(self) -> Dict[int, Dict[str, Any]]:
        _, nodes = snapshot.fetch_prs(self.gh)
        return {node["number"]: snapshot.summarize_pr(node) for node in nodes}

    def train_added(self, number: int) -> Dict[str, Any]:
        """The newest train-add marker on the PR: when it joined and at which head."""
        latest: Dict[str, Any] = {}
        for comment in self.gh.rest_paginated(f"repos/{{repo}}/issues/{number}/comments?per_page=100"):
            payload = marker.decode(comment.get("body"))
            if payload and payload.get("kind") == "train-add" and payload.get("pr") == number:
                latest = {"at": comment.get("created_at"), "head": payload.get("head"), "agent": payload.get("agent")}
        return latest

    def all_files(self, pr: Dict[str, Any]) -> Optional[List[str]]:
        if not pr.get("filesTruncated"):
            return None
        return [f["filename"] for f in self.gh.rest_paginated(f"repos/{{repo}}/pulls/{pr['number']}/files?per_page=100")]

    # MARK: - Step

    def run(self) -> List[str]:
        self.git.out("fetch", "--quiet", "--prune", "origin",
                     "+refs/heads/main:refs/remotes/origin/main",
                     "+refs/heads/train/*:refs/remotes/origin/train/*")
        main = self.git.out("rev-parse", "refs/remotes/origin/main")
        refs = self.git.out("for-each-ref", "--sort=refname", "--format=%(refname:strip=3) %(objectname)",
                            "refs/remotes/origin/train/")
        batches = [line.split() for line in refs.splitlines() if line.strip()]
        prs = self.open_prs()
        if batches:
            name, tip = batches[0]
            self.advance(name, tip, main, prs)
        else:
            self.build(main, prs)
        return self.lines

    def advance(self, name: str, tip: str, main: str, prs: Dict[int, Dict[str, Any]]) -> None:
        base = self.git.out("merge-base", tip, "refs/remotes/origin/main")
        members = train.parse_members(self.git.out(
            "log", "--first-parent", "--merges", "--format=%H%x09%P%x09%s", f"{base}..{tip}"))
        if not members:
            self.note(f"{name}: no train merges on it; deleting")
            self.delete_branch(name)
            return
        numbers = ", ".join(f"#{m['pr']}" for m in members)
        run = self.batch_run(name, tip)
        action = train.run_action(run, len(members))
        if action == "dispatch":
            built = dt.datetime.fromtimestamp(int(self.git.out("log", "-1", "--format=%ct", tip)), dt.timezone.utc)
            if self.now - built < dt.timedelta(minutes=DISPATCH_GRACE_MINUTES):
                self.note(f"{name} ({numbers}): CI dispatched {int((self.now - built).total_seconds())}s ago, not registered yet")
                return
            self.gh.dispatch_workflow(train.CI_WORKFLOW, name, {"lanes": train.CI_LANES})
            self.note(f"{name} ({numbers}): no CI run found at {tip[:9]}; dispatched ci.yml again")
            return
        url = run["html_url"]
        if action == "wait":
            self.note(f"{name} ({numbers}): CI {run.get('status')} {url}")
            return
        if action == "rerun":
            self.gh.rest(f"repos/{{repo}}/actions/runs/{run['id']}/rerun-failed-jobs", method="POST")
            self.note(f"{name} ({numbers}): CI red on attempt {run.get('run_attempt')}; re-running the failed jobs once {url}")
            return
        if action == "abandon":
            self.note(f"{name} ({numbers}): CI {run.get('conclusion')}; deleting the batch, the next step rebuilds")
            self.delete_branch(name)
            return
        if action in ("eject", "split"):
            failed = self.failed_jobs(run["id"])
            jobs = ", ".join(failed[:5]) or "see the run"
            for member in members:
                number = member["pr"]
                if action == "eject":
                    self.remove_label(number, train.TRAIN_LABEL)
                    self.remove_label(number, train.SOLO_LABEL)
                    self.comment(number, (
                        f"**Removed from the merge train:** CI failed on this PR alone, twice "
                        f"({jobs}): {url}\n\nFix it and add it again with `Tools/coord/coord train add {number}`."
                    ), "ejected", batch=name, run=run["id"], failed=failed[:10])
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
            self.note(f"{name} ({numbers}): green, but main moved from {base[:9]} to {main[:9]}; rebuilding")
            self.delete_branch(name)
            return
        for member in members:
            number = member["pr"]
            pr = prs.get(number)
            problem = None
            if pr is None:
                problem = "is no longer open"
            elif pr["head"] != member["sha"]:
                problem = f"moved from {member['sha'][:9]} to {pr['head'][:9]}"
            elif train.TRAIN_LABEL not in pr["labels"]:
                problem = "was taken out of the train"
            else:
                verdict, reason = train.eligibility(pr, all_files=self.all_files(pr))
                if verdict != train.READY:
                    problem = f"is no longer ready ({reason})"
            if problem:
                self.note(f"{name} ({numbers}): green, but #{number} {problem}; rebuilding without it")
                self.delete_branch(name)
                return
        if self.mode == "live":
            try:
                self.gh.rest("repos/{repo}/git/refs/heads/main", method="PATCH", body={"sha": tip, "force": False})
            except GitHubError as error:
                self.note(f"{name} ({numbers}): green, but main could not be fast-forwarded ({error}); rebuilding")
                self.delete_branch(name)
                return
        for member in members:
            number = member["pr"]
            pr = prs[number]
            self.remove_label(number, train.TRAIN_LABEL)
            self.remove_label(number, train.SOLO_LABEL)
            if self.mode == "live":
                self.comment(number, (
                    f"**Merged by the merge train** in batch `{name}` ({numbers}): main is now `{tip[:9]}`, "
                    f"the exact commit CI tested: {url}"
                ), "merged", batch=name, run=run["id"], tip=tip)
                self.cancel_branch_runs(pr["branch"])
            else:
                self.comment(number, (
                    f"**Merge train (dry run):** batch `{name}` ({numbers}) passed CI at `{tip[:9]}`: {url}\n\n"
                    f"The train is in dry-run mode, so main was not changed and this PR left the queue. "
                    f"Merge it the usual way."
                ), "green-dry-run", batch=name, run=run["id"], tip=tip)
        verb = "main fast-forwarded to" if self.mode == "live" else "dry run: would have fast-forwarded main to"
        self.note(f"{name} ({numbers}): CI green; {verb} {tip[:9]}")
        self.delete_branch(name)

    def build(self, main: str, prs: Dict[int, Dict[str, Any]]) -> None:
        queued = [pr for pr in prs.values() if train.TRAIN_LABEL in pr["labels"]]
        if not queued:
            self.note("queue empty")
            return
        entries = []
        verdicts = {}
        for pr in queued:
            added = self.train_added(pr["number"])
            entries.append({"pr": pr["number"], "at": added.get("at") or "9999"})
            verdicts[pr["number"]] = train.eligibility(pr, added.get("head"), self.all_files(pr))
        entries.sort(key=lambda e: (e["at"], e["pr"]))
        chosen = train.plan(entries, verdicts, {n: prs[n]["labels"] for n in verdicts})
        for number, reason in chosen["drops"]:
            self.remove_label(number, train.TRAIN_LABEL)
            self.comment(number, f"**Removed from the merge train:** {reason}.", "removed", reason=reason)
            self.note(f"#{number} removed: {reason}")
        for number, reason in chosen["holds"]:
            self.note(f"#{number} held: {reason}")
        if not chosen["batch"]:
            self.note("nothing ready")
            return

        name = train.batch_name(self.now)
        identity = {"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
                    "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}
        tip = main
        merged = []
        for number in chosen["batch"]:
            pr = prs[number]
            self.git.out("fetch", "--quiet", "origin", f"+refs/pull/{number}/head:refs/coord/pr/{number}")
            head = self.git.out("rev-parse", f"refs/coord/pr/{number}")
            if head != pr["head"]:
                self.note(f"#{number} held: its head changed while the batch was built")
                continue
            result = self.git.run("merge-tree", "--write-tree", "--no-messages", tip, head, check=False)
            if result.returncode == 1:
                self.note(f"#{number} held: it does not merge cleanly on top of "
                          f"{', '.join('#%d' % m for m in merged) or 'main'}")
                continue
            if result.returncode != 0:
                raise GitError(f"git merge-tree: {result.stderr.strip()}")
            tree = result.stdout.split()[0]
            subject, body = train.merge_message(pr, self.gh.owner)
            tip = self.git.out("commit-tree", tree, "-p", tip, "-p", head, "-m", subject, "-m", body or subject,
                               env=identity)
            merged.append(number)
        if not merged:
            self.note("nothing merged cleanly; no batch")
            return
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
        except GitHubError:
            pass  # exists

"""`coord claim`'s narrow read (#1109) and claims whose PR has merged (#1110).

#1109: in REST-only mode (every cloud session) `claim` built the whole board, about 250
calls and 100 s on 2026-09-28, most of them issue and branch reads for every claim record
in the 14-day window and per-PR review and CI reads a claim never uses. It now reads
open PRs' files and the claim/release records only.

#1110: a claim whose branch's PR merged with `Refs #N` (not `Closes`) stayed in flight and
kept producing overlap warnings. It is now stale: out of `claims`, overlaps and in-flight,
kept under `staleClaims`, and a new claim on its issue retires it.

Run: python3 -m unittest tests.test_claim_speed_and_refs (from Tools/coord)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cli, marker, snapshot  # noqa: E402
from coordlib.gh import NotFound  # noqa: E402
import example_config  # noqa: E402

example_config.use()  # example-repo workflow/job/lane names are config, not defaults


def setUpModule():
    example_config.use()

ME = "claude-cloud@issue-5"
OTHER = "claude-cloud@issue-7"
NOW = snapshot.now_utc()


def ago(hours: float) -> str:
    return snapshot.iso(NOW - dt.timedelta(hours=hours))


def pull(number, branch, files=(), merged_hours=None, updated_hours=1.0, fork=False):
    return {"number": number, "title": f"PR {number}", "html_url": f"https://x/pull/{number}", "draft": False,
            "head": {"ref": branch, "sha": f"{number:040d}", "repo": {"full_name": "other/r" if fork else "o/r"}},
            "base": {"ref": "main"}, "labels": [], "body": "", "created_at": ago(90), "updated_at": ago(updated_hours),
            "merged_at": ago(merged_hours) if merged_hours is not None else None, "_files": list(files)}


class FakeGitHub:
    """REST-only GitHub: open and closed PRs, claim records, closed issues. Counts calls."""

    repo, owner, name = "o/r", "o", "r"
    graphql_blocked = True

    def __init__(self, open_prs=(), closed_prs=(), records=(), closed_issues=()):
        self.open_prs = list(open_prs)
        self.closed_prs = list(closed_prs)
        self.records = list(records)  # (issue, payload, hours ago)
        self.closed_issues = set(closed_issues)
        self.paths = []
        self.posted = []

    @property
    def calls(self):
        return len(self.paths)

    def raw_comments(self):
        return [{"id": index + 1, "issue_url": f"https://api/repos/o/r/issues/{issue}",
                 "created_at": ago(hours), "html_url": f"https://x/issues/{issue}#c{index + 1}",
                 "body": marker.compose("record", payload), "user": {"login": "alex"}}
                for index, (issue, payload, hours) in enumerate(self.records)]

    def rest_paginated(self, path):
        self.paths.append(path)
        if path.startswith("repos/{repo}/pulls?state=open"):
            return [dict(p) for p in self.open_prs]
        for p in self.open_prs + self.closed_prs:
            if path.startswith(f"repos/{{repo}}/pulls/{p['number']}/files"):
                return [{"filename": f} for f in p["_files"]]
        if path.startswith("repos/{repo}/issues/comments"):
            return self.raw_comments()
        if path.startswith("repos/{repo}/issues?state=closed"):
            return [{"number": n, "state": "closed"} for n in sorted(self.closed_issues)]
        for issue in {r[0] for r in self.records}:
            if path.startswith(f"repos/{{repo}}/issues/{issue}/comments"):
                return [c for c in self.raw_comments() if c["issue_url"].endswith(f"/issues/{issue}")]
        return []

    def rest(self, path, method="GET", body=None):
        self.paths.append(path)
        if method != "GET":
            self.posted.append((path, body))
            number = len(self.records) + 1
            issue = int(path.split("/issues/")[1].split("/")[0])
            self.records.append((issue, marker.decode(body["body"]), 0))
            return {"id": number, "html_url": f"https://x/issues/{issue}#c{number}"}
        if path.startswith("repos/{repo}/pulls?state=closed"):
            page = int(path.rsplit("page=", 1)[1])
            ordered = sorted(self.closed_prs, key=lambda p: p["updated_at"], reverse=True)
            return [dict(p) for p in ordered[(page - 1) * 100: page * 100]]
        if path.startswith("repos/{repo}/issues/"):
            number = int(path.rsplit("/", 1)[1])
            return {"number": number, "title": f"issue {number}", "html_url": f"https://x/issues/{number}",
                    "state": "closed" if number in self.closed_issues else "open"}
        if path.startswith("repos/{repo}/branches/"):
            raise NotFound(path)
        return {}


def claim_payload(issue, agent, branch, files):
    return {"kind": "claim", "v": 1, "agent": agent, "issue": issue, "branch": branch, "files": files}


def run_claim(gh, argv):
    """`coord claim` REST-only against `gh`, with a cold cache; (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as cache_dir, \
            mock.patch.dict(os.environ, {"COORD_AGENT": ME, "COORD_REST_ONLY": "1", "COORD_CACHE_DIR": cache_dir}), \
            mock.patch.object(cli, "_commits_ahead_of_main", return_value=3), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.cmd_claim(cli.parser().parse_args(["claim", *argv]), gh)
    return code, out.getvalue(), err.getvalue()


class ClaimCallCountTests(unittest.TestCase):
    """#1109: a claim reads open PRs' files and the claim records, not the board."""

    def busy_repo(self):
        open_prs = [pull(100 + i, f"b{i}", [f"src/f{i}.py"]) for i in range(10)]
        # 40 claims on issues a `Closes` merge has since closed, and 5 live ones.
        records = [(200 + i, claim_payload(200 + i, f"agent{i}", f"old{i}", ["x"]), 30) for i in range(40)]
        records += [(300 + i, claim_payload(300 + i, f"live{i}", f"b{i}", [f"src/f{i}.py"]), 20) for i in range(5)]
        return FakeGitHub(open_prs=open_prs, records=records, closed_issues=range(200, 240))

    def test_a_rest_only_dry_run_claim_makes_a_bounded_number_of_calls(self):
        gh = self.busy_repo()
        code, out, err = run_claim(gh, ["5", "--branch", "x", "--files", "x", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertEqual(marker.decode(out)["issue"], 5)
        # The target issue, the open PR list, one file list per open PR, the record
        # window, the closed issues since the oldest record, one page of closed PRs.
        self.assertLessEqual(gh.calls, 1 + 1 + 10 + 1 + 1 + 1, "\n".join(gh.paths))

    def test_the_count_grows_with_open_prs_only(self):
        gh = self.busy_repo()
        run_claim(gh, ["5", "--branch", "x", "--files", "x", "--dry-run"])
        self.assertFalse([p for p in gh.paths if "/branches/" in p or "check-" in p or "/reactions" in p
                          or "/status" in p or "actions/runs" in p], "\n".join(gh.paths))


class GraphQLClaimViewTests(unittest.TestCase):
    """Where GraphQL works, the open PRs and their files are one query; a rename or a
    list cut at 100 files is completed over REST."""

    def test_open_prs_come_from_one_query(self):
        gh = FakeGitHub(closed_prs=[pull(61, "r", ["new.md", "old.md"])])
        gh.graphql_blocked = False
        queries = []

        def graphql(query, variables):
            queries.append(query)
            nodes = [{"number": 60, "title": "t", "url": "u", "isCrossRepository": False, "headRefName": "a",
                      "updatedAt": ago(1), "files": {"totalCount": 1, "nodes": [{"path": "a.md", "changeType": "MODIFIED"}]}},
                     {"number": 61, "title": "t", "url": "u", "isCrossRepository": False, "headRefName": "r",
                      "updatedAt": ago(1), "files": {"totalCount": 1, "nodes": [{"path": "new.md", "changeType": "RENAMED"}]}}]
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}

        gh.graphql = graphql
        with mock.patch.dict(os.environ, {"COORD_REST_ONLY": "0", "CLAUDE_CODE_REMOTE": "false"}):
            view = snapshot.claim_view(gh, 5, NOW)
        self.assertEqual(len(queries), 1)
        self.assertEqual({p["number"]: p["files"] for p in view["prs"]}, {60: ["a.md"], 61: ["new.md", "old.md"]})
        self.assertFalse([p for p in gh.paths if "pulls?state=open" in p or "/pulls/60/" in p], gh.paths)


class MergedPrClaimTests(unittest.TestCase):
    """#1110: a claim whose branch's PR merged after the claim is stale."""

    def repo(self, merged_hours=2.0, fork=False, open_on_branch=False):
        # Issue 7's claim declared shared.md; its `Refs #7` PR merged. PR 50 touches shared.md.
        closed = [pull(40, "cloud/issue-7", ["shared.md"], merged_hours=merged_hours, updated_hours=1.5, fork=fork)]
        open_prs = [pull(50, "cloud/issue-9", ["shared.md"])]
        if open_on_branch:
            open_prs.append(pull(41, "cloud/issue-7", ["other.md"]))
        records = [(7, claim_payload(7, OTHER, "cloud/issue-7", ["shared.md"]), 10)]
        return FakeGitHub(open_prs=open_prs, closed_prs=closed, records=records)

    def build(self, gh):
        with mock.patch.dict(os.environ, {"COORD_REST_ONLY": "1"}):
            return snapshot.build(gh, NOW)

    def test_a_claim_whose_pr_merged_is_neither_in_flight_nor_overlapping(self):
        snap = self.build(self.repo())
        self.assertEqual([c["issue"] for c in snap["claims"]], [])
        self.assertEqual(snap["overlaps"], [])
        [stale] = snap["staleClaims"]
        self.assertEqual((stale["issue"], stale["mergedPr"], stale["stale"]), (7, 40, True))

    def test_a_branch_reused_after_its_merge_keeps_the_new_claim_live(self):
        snap = self.build(self.repo(merged_hours=30.0))  # merged before the claim (10 h ago)
        self.assertEqual([c["issue"] for c in snap["claims"]], [7])
        self.assertEqual(len(snap["overlaps"]), 1)

    def test_a_forks_merged_pr_with_the_same_branch_name_does_not_match(self):
        snap = self.build(self.repo(fork=True))
        self.assertEqual([c["issue"] for c in snap["claims"]], [7])

    def test_an_open_pr_on_the_branch_wins(self):
        snap = self.build(self.repo(open_on_branch=True))
        self.assertEqual([(c["issue"], c["pr"]) for c in snap["claims"]], [(7, 41)])

    def test_claim_warns_of_no_overlap_with_a_merged_claim(self):
        gh = self.repo()
        gh.open_prs = []  # only the merged claim declares shared.md
        code, out, err = run_claim(gh, ["5", "--branch", "cloud/issue-5", "--files", "shared.md"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("overlaps", out)

    def test_a_new_claim_on_the_issue_retires_the_merged_claim_and_is_not_withdrawn(self):
        gh = self.repo()
        code, out, err = run_claim(gh, ["7", "--branch", "cloud/issue-7b", "--files", "a.md"])
        self.assertEqual(code, 0, err)
        self.assertIn("claimed #7", out)
        [(path, body)] = gh.posted
        self.assertEqual(marker.decode(body["body"])["takesOverFrom"], [OTHER])
        self.assertIn("PR #40 merged", err)

    def test_a_live_claim_still_blocks(self):
        gh = self.repo(merged_hours=30.0)
        code, _, err = run_claim(gh, ["7", "--branch", "cloud/issue-7b", "--files", "a.md", "--dry-run"])
        self.assertEqual(code, cli.EXIT_CONFLICT)
        self.assertIn(f"claimed by {OTHER}", err)


if __name__ == "__main__":
    unittest.main()

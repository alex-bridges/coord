"""`coord claim` usability: comma-separated --files (#1083), the hint for a branch with no
work (#1084), and overlap warnings that name what the other work declared (#1069).

Run: python3 -m unittest tests.test_claim_ux (from Tools/coord)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import cli, marker, render, snapshot  # noqa: E402

ME = "claude-cloud@issue-5"


class FakeGitHub:
    repo, owner, name = "o/r", "o", "r"


def run_claim(argv, claims=(), prs=(), branch="feature/x", ahead=0):
    """Run `coord claim` offline; returns (exit code, stdout, stderr)."""
    snap = {"claims": list(claims), "prs": list(prs)}
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {"COORD_AGENT": ME}), \
            mock.patch.object(cli.identity, "git_branch", return_value=branch), \
            mock.patch.object(cli, "_commits_ahead_of_main", return_value=ahead, create=True), \
            mock.patch.object(cli.snapshot, "fetch_targets",
                              return_value={"issues": {5: {"state": "OPEN"}}, "branches": {}}), \
            mock.patch.object(cli, "get_snapshot", return_value=snap), \
            mock.patch.object(cli, "post_comment", return_value={"html_url": "h"}), \
            mock.patch.object(cli, "_issue_claims", return_value=[]), \
            mock.patch.object(cli.cache, "invalidate_snapshot"), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.cmd_claim(cli.parser().parse_args(["claim", "5", *argv]), FakeGitHub())
    return code, out.getvalue(), err.getvalue()


def dry_run_payload(stdout):
    return marker.decode(stdout)


class CommaFilesTests(unittest.TestCase):
    """#1083: `--files a,b` reserved one path named "a,b"."""

    def test_a_comma_separated_list_is_split(self):
        code, out, _ = run_claim(["--files", "a.swift,b.swift", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(dry_run_payload(out)["files"], ["a.swift", "b.swift"])

    def test_commas_and_spaces_mix_and_blanks_drop(self):
        code, out, _ = run_claim(["--files", "a.swift,", "b.swift, c/d.swift", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(dry_run_payload(out)["files"], ["a.swift", "b.swift", "c/d.swift"])


class NoWorkBranchHintTests(unittest.TestCase):
    """#1084: a claim on HEAD with no commits ahead of origin/main names a branch with no work."""

    HINT = "which has no work yet"

    def test_a_claim_on_a_branch_equal_to_main_prints_the_hint(self):
        code, _, err = run_claim(["--dry-run"], branch="claude/harness-start", ahead=0)
        self.assertEqual(code, 0)
        self.assertIn("claiming on claude/harness-start, which has no work yet; "
                      "pass --branch <name> if you'll work elsewhere", err)

    def test_a_claim_with_branch_does_not(self):
        _, _, err = run_claim(["--branch", "cloud/issue-5", "--dry-run"], branch="claude/harness-start", ahead=0)
        self.assertNotIn(self.HINT, err)

    def test_a_branch_with_commits_does_not(self):
        _, _, err = run_claim(["--dry-run"], branch="feature/x", ahead=2)
        self.assertNotIn(self.HINT, err)

    def test_a_branch_with_a_pr_does_not(self):
        pr = {"number": 9, "branch": "feature/x", "files": [], "fromFork": False}
        _, _, err = run_claim(["--dry-run"], prs=[pr], branch="feature/x", ahead=0)
        self.assertNotIn(self.HINT, err)

    def test_unknown_ahead_count_does_not(self):
        _, _, err = run_claim(["--dry-run"], branch="feature/x", ahead=None)
        self.assertNotIn(self.HINT, err)


class OverlapNamesDeclaredPathTests(unittest.TestCase):
    """#1069: the warning named the claimant's file, not the directory the other claim declared."""

    DIR = "Tests/CoreKitRTAllocationTests"
    FILE = DIR + "/AUInstrumentBypassRTAllocationTests.swift"

    def other_claim(self, files):
        return {"issue": 1022, "agent": "fix-1022", "pr": None, "branch": "cloud/issue-1022", "files": files}

    def test_claim_warning_names_the_other_claims_directory(self):
        code, out, _ = run_claim(["--files", self.FILE], claims=[self.other_claim([self.DIR])])
        self.assertEqual(code, 0)
        self.assertIn(f"touches or expects: {self.DIR}/ (directory), which contains: AUInstrumentBypassRTAllocationTests.swift", out)

    def test_claim_warning_for_the_same_file_names_it_once(self):
        _, out, _ = run_claim(["--files", self.FILE], claims=[self.other_claim([self.FILE])])
        self.assertIn(f"touches or expects: {self.FILE}\n", out)

    def test_claim_warning_when_mine_is_the_directory(self):
        _, out, _ = run_claim(["--files", self.DIR], claims=[self.other_claim([self.FILE])])
        self.assertIn(f"touches or expects: {self.FILE} (inside {self.DIR}/)", out)

    def test_board_line_names_which_side_declared_the_directory(self):
        claims = [dict(self.other_claim([self.DIR + "/"])),
                  {"issue": 1020, "agent": "fix-1020", "pr": None, "branch": "b", "files": [self.FILE]}]
        found = snapshot.overlaps([], claims)
        board = render.board({"schema": 1, "generatedAt": "2026-09-24T17:00:00Z", "repo": "o/r",
                              "main": {"sha": "f" * 40}, "prs": [], "claims": [], "overlaps": found,
                              "train": [], "runs": {"running": [], "queued": []}}, "me")
        self.assertIn(f"claim #1022 (fix-1022) and claim #1020 (fix-1020): {self.DIR}/ (directory, claim #1022), "
                      "which contains: AUInstrumentBypassRTAllocationTests.swift", board)

    def test_board_line_for_the_same_file_is_unchanged(self):
        claims = [self.other_claim([self.FILE]),
                  {"issue": 1020, "agent": "fix-1020", "pr": None, "branch": "b", "files": [self.FILE]}]
        found = snapshot.overlaps([], claims)
        self.assertEqual(found, [{"a": "claim #1022 (fix-1022)", "b": "claim #1020 (fix-1020)", "files": [self.FILE]}])
        json.dumps(found)


if __name__ == "__main__":
    unittest.main()

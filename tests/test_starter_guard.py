"""The PreToolUse hook shipped in examples/, invoked exactly as a stdin filter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


GUARD = Path(__file__).resolve().parents[1] / "examples" / "conductor-guard.py"
CONDUCTOR = "conductor@host:watcher"
REVIEWER = "reviewer@host:review"


class StarterGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name, "checkout")
        self.project.mkdir()
        self.state = Path(temp.name, "state")
        self.env = dict(os.environ, HOME=temp.name, USERPROFILE=temp.name,
                        CLAUDE_PROJECT_DIR=str(self.project), CONDUCTOR_STATE_DIR=str(self.state),
                        COORD_KEYCHAIN_SERVICE="coord-github-app", COORD_STATE_DIR_NAME="coord")

    def check(self, body, status, agent=CONDUCTOR, wake=""):
        result = subprocess.run([sys.executable, str(GUARD)], cwd=self.project, input=body, text=True,
                                capture_output=True, env=dict(self.env, COORD_AGENT=agent, CONDUCTOR_WAKE=wake),
                                timeout=10)
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertEqual(result.stdout, "")
        if status == 0:
            self.assertEqual(result.stderr, "")
        else:
            self.assertIn("conductor-guard:", result.stderr)

    def call(self, tool, data):
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": data,
                           "cwd": str(self.project)})

    def test_activation_and_role_specific_shell_permissions(self):
        for agent, wake, command, status in [
            ("human@host", "", "gh pr merge 7", 0),
            ("human@host", "1", "gh pr merge 7", 2),
            (CONDUCTOR, "", "gh pr merge 7", 2),
            (CONDUCTOR, "", "git push origin review-fix", 0),
            (CONDUCTOR, "", "tee CLAUDE.md", 2),
            (CONDUCTOR, "", "cat CLAUDE.md", 0),
            (REVIEWER, "", "git push origin review-fix", 2),
            (REVIEWER, "", "gh pr view 7", 0),
        ]:
            with self.subTest(agent=agent, wake=wake, command=command):
                self.check(self.call("Bash", {"command": command}), status, agent, wake)

    def test_conductor_edits_only_block_control_files_and_directories(self):
        for path, status in [
            (".claude/hooks/check.py", 2), ("CLAUDE.md", 2), ("CLAUDE.md.example", 0),
            (str(Path(self.env["HOME"], ".claude", "settings.json")), 2), ("src/module.py", 0),
        ]:
            with self.subTest(path=path):
                self.check(self.call("Edit", {"file_path": path}), status)
        self.check(self.call("Read", {"file_path": "CLAUDE.md"}), 0)

    def test_reviewer_writes_stay_in_review_worktrees_and_scratch(self):
        for path, status in [
            (self.state / "reviews" / "pr-7" / "notes.ipynb", 0),
            (self.state / "reviews" / "pr-7-scratch" / "notes.ipynb", 0),
            (self.project / "notes.ipynb", 2),
        ]:
            with self.subTest(path=path):
                self.check(self.call("NotebookEdit", {"notebook_path": str(path)}), status, REVIEWER)

    def test_active_guard_refuses_unusable_hook_input(self):
        for body in ["invalid json", "[]", "{}", self.call("Bash", []),
                     self.call("Bash", {}), self.call("Write", {"file_path": None})]:
            with self.subTest(body=body):
                self.check(body, 2)
        self.check("invalid json", 0, agent="human@host")


if __name__ == "__main__":
    unittest.main()

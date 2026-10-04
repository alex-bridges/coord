"""`send --on <mailbox issue>` without `--to` addresses nobody, so coord refuses it (#1070)."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coordlib import EXIT_OK, EXIT_USAGE, cache, cli, marker, snapshot
from coordlib.gh import GitHub

MAILBOX = 1001
PLAIN = 1070
# This machine's cached board: a message sent without --on, so it was posted on the mailbox.
BOARD = {"messages": [{"on": MAILBOX, "author": {"login": "o", "type": "User"},
                       "payload": {"kind": "msg", "v": 1, "agent": "claude@x:y",
                                                  "to": ["conductor@*"]}}]}


class SendOnMailboxTests(unittest.TestCase):
    def send(self, argv):
        gh = GitHub("o/r", transport="http")
        calls = []

        def rest(path, method="GET", body=None):
            calls.append((method, path))
            return {"html_url": "https://example.invalid/c"}

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(gh, "rest", side_effect=rest), \
                mock.patch.object(cli, "gate", return_value=None), \
                mock.patch.object(cli.cache, "load_snapshot", return_value=BOARD), \
                mock.patch.object(cli.identity, "agent_id", return_value="conductor@test"), \
                mock.patch.object(cli.cache, "invalidate_snapshot"), \
                mock.patch.object(cli, "down_warnings", return_value=[]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.run(cli.parser().parse_args(["send", "hi"] + argv), gh)
        return code, calls, stderr.getvalue()

    def test_on_mailbox_without_to_is_refused_and_points_at_to(self):
        code, calls, stderr = self.send(["--on", str(MAILBOX)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(calls, [])  # refused without spending a call
        self.assertIn("--to", stderr)

    def test_on_mailbox_with_to_is_sent(self):
        code, calls, _ = self.send(["--on", str(MAILBOX), "--to", "claude@x:y"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([m for m, _ in calls], ["POST"])

    def test_on_a_real_issue_without_to_is_sent(self):
        code, calls, _ = self.send(["--on", str(PLAIN)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([m for m, _ in calls], ["POST"])


class MailboxLearnedByInboxTests(unittest.TestCase):
    """Codex on #1098: the board was cached less than a minute before `inbox` and held no
    mailbox message from the last 14 days. `inbox` then reads the mailbox's first message
    directly; the next `send --on <mailbox>` without --to must still be refused."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"COORD_CACHE_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = GitHub("o/r", transport="http")
        now = snapshot.iso(snapshot.now_utc())
        # A fresh board with no mailbox message: `inbox` serves it from the cache.
        cache.store_snapshot(self.gh.repo, {"schema": 1, "generatedAt": now, "claims": [], "prs": [],
                                            "messages": []})
        self.mailbox_comment = {
            "id": 77, "created_at": now, "html_url": "https://example.invalid/m",
            "issue_url": f"https://api.github.com/repos/o/r/issues/{MAILBOX}",
            "user": {"login": "o", "type": "User"},
            "body": "**From** `claude@x:y` **to** `conductor@*`\n\nhello\n\n"
                    + marker.encode({"kind": "msg", "v": 1, "agent": "claude@x:y", "to": ["conductor@*"]}),
        }

    def run_cli(self, argv, comments=()):
        calls = []

        def rest(path, method="GET", body=None):
            calls.append((method, path))
            return {"html_url": "https://example.invalid/c"}

        def rest_paginated(path):
            calls.append(("GET", path))
            return list(comments) if "issues/comments" in path else []

        out = io.StringIO()
        with mock.patch.object(self.gh, "rest", side_effect=rest), \
                mock.patch.object(self.gh, "rest_paginated", side_effect=rest_paginated), \
                mock.patch.object(cli, "gate", return_value=None), \
                mock.patch.object(cli.identity, "agent_id", return_value="conductor@test"), \
                mock.patch.object(cli.identity, "git_branch", return_value=""), \
                mock.patch.object(cli, "down_warnings", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run(cli.parser().parse_args(argv), self.gh)
        return code, calls, out.getvalue()

    def test_mailbox_read_by_inbox_is_refused_without_to(self):
        code, calls, out = self.run_cli(["inbox"], [self.mailbox_comment])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 item", out)  # the inbox delivered the mailbox message
        self.assertTrue(all("issues/comments" in p or "pulls/comments" in p for _, p in calls))  # board from cache
        code, calls, _ = self.run_cli(["send", "hi", "--on", str(MAILBOX)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(calls, [])

    def test_a_real_issue_read_by_inbox_stays_sendable(self):
        comment = dict(self.mailbox_comment, issue_url=f"https://api.github.com/repos/o/r/issues/{PLAIN}",
                       body="reply\n\n" + marker.encode({"kind": "msg", "v": 1, "agent": "claude@x:y",
                                                            "to": ["conductor@*"], "on": PLAIN}))
        self.run_cli(["inbox"], [comment])
        code, calls, _ = self.run_cli(["send", "hi", "--on", str(PLAIN)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([m for m, _ in calls], ["POST"])

    def test_forged_mailbox_marker_on_real_issue_is_not_remembered(self):
        comment = dict(self.mailbox_comment, issue_url=f"https://api.github.com/repos/o/r/issues/{PLAIN}",
                       user={"login": "outsider", "type": "User"})
        self.run_cli(["inbox"], [comment])
        code, calls, _ = self.run_cli(["send", "hi", "--on", str(PLAIN)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([m for m, _ in calls], ["POST"])

    def test_a_send_to_the_mailbox_remembers_it(self):
        with mock.patch.object(cli, "find_or_create_mailbox", return_value=MAILBOX):
            code, _, _ = self.run_cli(["send", "hi", "--to", "conductor@*"])
        self.assertEqual(code, EXIT_OK)
        code, calls, _ = self.run_cli(["send", "hi", "--on", str(MAILBOX)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

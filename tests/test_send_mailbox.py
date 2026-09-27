"""`send --on <mailbox issue>` without `--to` addresses nobody, so coord refuses it (#1070)."""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coordlib import EXIT_OK, EXIT_USAGE, cli
from coordlib.gh import GitHub

MAILBOX = 1001
PLAIN = 1070
# This machine's cached board: a message sent without --on, so it was posted on the mailbox.
BOARD = {"messages": [{"on": MAILBOX, "payload": {"kind": "msg", "v": 1, "agent": "claude@x:y",
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


if __name__ == "__main__":
    unittest.main()

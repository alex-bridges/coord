"""Diagnosis stays advisory; only an explicit handoff can post a coordination message."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from coordlib import cli, conductor
from coordlib.gh import GitHub


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "report.json")
        with open(self.source, "w") as handle:
            handle.write('{"ci":"red","head":"example"}')
        self.result = {"summary": "CI failed", "needsRepair": True, "reason": "Inspect the failed test log."}

    def completed(self):
        return subprocess.CompletedProcess([], 0, "\n".join(json.dumps(event) for event in [
            {"type": "thread.started", "thread_id": "diagnostic"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(self.result)}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 8}},
        ]), "")

    def invoke(self, extra=(), completed=None):
        gh = GitHub("o/r", transport="http")
        gh.rest = mock.Mock(side_effect=AssertionError("unexpected GitHub call"))
        args = cli.parser().parse_args(["conductor", "diagnose", "--input", self.source, "--json", *extra])
        stdout = io.StringIO()
        with mock.patch.object(cli, "gate", return_value=None), \
                mock.patch.object(cli.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(conductor, "default_runner", return_value=completed or self.completed()) as runner, \
                mock.patch.object(cli, "find_or_create_mailbox", return_value=10) as mailbox, \
                mock.patch.object(cli, "post_comment", return_value={"html_url": "https://example.test/comment"}) as post, \
                mock.patch.object(cli.cache, "invalidate_snapshot"), \
                mock.patch.object(cli.identity, "agent_id", return_value="codex@host:task"), \
                mock.patch.dict(os.environ, {"GH_TOKEN": "must-not-reach-runtime", "CODEX_THREAD_ID": "parent"}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.run(args, gh)
        return code, json.loads(stdout.getvalue()), runner, mailbox, post

    def test_default_cannot_post_and_uses_clean_temporary_context(self):
        code, result, runner, mailbox, post = self.invoke()
        self.assertEqual(code, 0)
        self.assertIsNone(result["handoff"])
        mailbox.assert_not_called()
        post.assert_not_called()
        command, cwd, env, timeout = runner.call_args.args
        self.assertIn("read-only", command)
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertFalse(os.path.exists(cwd), "diagnostic scratch directory is cleaned")
        self.assertEqual(timeout, 300)

    def test_explicit_handoff_posts_one_advisory_message(self):
        code, result, _, mailbox, post = self.invoke(["--handoff"])
        self.assertEqual(code, 0)
        mailbox.assert_called_once()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[3]["kind"], "msg")
        self.assertEqual(post.call_args.args[3]["to"], ["conductor@*"])
        self.assertIn("not an approval", post.call_args.args[2])
        self.assertEqual(result["handoff"]["to"], "conductor@*")

    def test_argv_byte_limit_is_checked_after_unicode_escaping(self):
        for context in ("x" * 121_000, "🙂" * 11_000):
            with self.subTest(characters=len(context)):
                with open(self.source, "w", encoding="utf-8") as handle:
                    handle.write(context)
                code, result, runner, mailbox, post = self.invoke(["--handoff"])
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn("encoded bytes", result["error"]["message"])
                runner.assert_not_called()
                mailbox.assert_not_called()
                post.assert_not_called()

    def test_no_handoff_for_no_repair_or_invalid_output(self):
        self.result["needsRepair"] = False
        _, _, _, mailbox, post = self.invoke(["--handoff"])
        mailbox.assert_not_called()
        post.assert_not_called()
        for output in (subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 124, "", "timeout")):
            code, result, _, mailbox, post = self.invoke(["--handoff"], output)
            self.assertNotEqual(code, 0)
            self.assertEqual(result["error"]["kind"], "runtime")
            mailbox.assert_not_called()
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()

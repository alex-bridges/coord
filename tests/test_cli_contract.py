"""Machine clients can parse every supported read's degraded/error response."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coordlib import EXIT_BACKEND, EXIT_BUDGET, EXIT_NOT_FOUND, EXIT_USAGE, budget, cli, identity, train_metrics
from coordlib.gh import AppAuthError, GitHub, GitHubError, NotFound
import inisa_config  # noqa: E402

inisa_config.use()  # INISA workflow/job/lane names are config, not defaults


def setUpModule():
    inisa_config.use()


class JsonContractTests(unittest.TestCase):
    def invoke(self, args, gh):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.run(args, gh)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_all_read_errors_are_objects_with_nonzero_exit(self):
        for argv in (["board", "--json"], ["inbox", "--json"], ["train", "status", "--json"],
                     ["doctor", "--online", "--json"]):
            for error, expected in ((GitHubError("offline"), EXIT_BACKEND), (NotFound("missing"), EXIT_NOT_FOUND),
                                    (AppAuthError("token missing"), EXIT_BACKEND),
                                    (identity.IdentityError("cannot persist id"), EXIT_USAGE),
                                    (train_metrics.BudgetConfigurationError("invalid attempt limit"), EXIT_USAGE),
                                    (cli.BudgetRefused("low"), EXIT_BUDGET), (cli.UsageError("bad"), EXIT_USAGE)):
                with self.subTest(argv=argv, error=type(error).__name__):
                    args = cli.parser().parse_args(argv)
                    args.func = mock.Mock(side_effect=error)
                    with mock.patch.object(cli, "gate", return_value=None):
                        code, result, stderr = self.invoke(args, GitHub("o/r", transport="http"))
                    self.assertEqual(code, expected)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], expected)
                    self.assertIn("coord:", stderr)

    def test_inbox_unavailable_is_not_an_empty_inbox_and_does_not_advance_cursor(self):
        snap = {"claims": [], "prs": [], "messages": [], "generatedAt": "2026-09-20T00:00:00Z",
                "stale": {"ago": "1d", "reason": "quota"}}
        for reason in ("budget_low", "app_unavailable"):
            gh = GitHub("o/r", transport="http")
            setattr(gh, reason, "unavailable")
            args = cli.parser().parse_args(["inbox", "--json"])
            stdout = io.StringIO()
            with mock.patch.object(cli, "get_snapshot", return_value=snap), \
                    mock.patch.object(cli.cache, "load_cursor", return_value=None), \
                    mock.patch.object(cli.cache, "load_seen_ids", return_value=[]), \
                    mock.patch.object(cli.cache, "store_cursor") as store, contextlib.redirect_stdout(stdout):
                cli.inbox_from_snapshot(args, gh, "codex@test:task")
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "stale")
            self.assertIsNone(report["items"])
            self.assertFalse(report["markedRead"])
            store.assert_not_called()

    def test_online_doctor_refuses_low_budget_without_api_call(self):
        gh = GitHub("o/r", transport="http")
        with mock.patch.object(budget, "current", return_value={}), \
                mock.patch.object(budget, "decision", return_value=(budget.LOW, "low quota")), \
                mock.patch.object(gh, "rest") as rest:
            code, result, _ = self.invoke(cli.parser().parse_args(["doctor", "--online", "--json"]), gh)
        self.assertEqual(code, EXIT_BUDGET)
        self.assertEqual(result["error"]["kind"], "budget")
        rest.assert_not_called()

    def test_json_argument_error_and_repo_error(self):
        for argv, error in ((["board", "--json", "--unknown"], None), (["board", "--json"], GitHubError("no repo"))):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "GitHub", side_effect=error), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], code)


if __name__ == "__main__":
    unittest.main()

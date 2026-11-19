"""coord in app mode, end to end through cli.main (#1035): which identity each call uses,
no fallback to Alex's token, `coord token`, and no secret in any output.
Run: python3 -m unittest discover -s Tools/coord/tests

Every command runs in-process exactly as the `coord` script runs it, against the fake
`security`, the fake GitHub and the recording `gh` runner (apptestkit.py). One class also
runs the real script as a subprocess, with a pre-seeded token cache so nothing is minted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import INSTALLATION, REPO, AppTestCase, FakeResponse  # noqa: E402
from coordlib import EXIT_BACKEND, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, apptoken, budget, cache, cli, conductor  # noqa: E402
import example_config  # noqa: E402

example_config.use()  # example-repo workflow names, state dir and weekly reset are config


def setUpModule():
    example_config.use()


USER_TOKEN = "user-token-that-must-never-be-sent"
DEEP_JSON = "[" * 1_000_000 + "]" * 1_000_000  # too deep for 3.9 and 3.14's parsers alike
COORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coord")


def is_write(request):
    return request["method"] != "GET" and request["path"] != "graphql"


def parser_commands():
    """Every (command,) or (command, subcommand) the parser knows."""
    names = set()
    for action in cli.parser()._subparsers._group_actions:  # noqa: SLF001
        for name, sub in action.choices.items():
            nested = [a for a in sub._actions if a.__class__.__name__ == "_SubParsersAction"]  # noqa: SLF001
            names |= {(name, child) for child in nested[0].choices} if nested else {(name,)}
    return names


class AppCliTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        os.environ["COORD_TRANSPORT"] = "gh"  # Alex's identity goes through the (fake) gh runner
        self.no_repo = os.path.join(self.root, "not-a-clone")
        os.makedirs(self.no_repo)
        # The conductor's commands keep their state, checkout and model command inside the
        # test directory.
        for name, value in (("CONDUCTOR_HOME", self.no_repo), ("CONDUCTOR_STATE_DIR", os.path.join(self.root, "state")),
                            ("CONDUCTOR_CLAUDE", os.path.join(self.root, "no-claude")),
                            ("CONDUCTOR_UPDATE_CHECKOUT", "0")):
            self.addCleanup(os.environ.pop, name, None)
            os.environ[name] = value

    def writes(self):
        return [r for r in self.server.api_requests() if is_write(r)]

    def reads(self):
        return [r for r in self.server.api_requests() if not is_write(r)]

    def runner_writes(self):
        return [call for call in self.runner.calls if "--method" in call and call[call.index("--method") + 1] != "GET"]


# MARK: - Identity per write


class RoutingTests(AppCliTestCase):
    """The identity table: operational writes on the app's ops token, reads on its read
    token, attestations and a hand-run train step on Alex's gh."""

    OPERATIONAL = {
        "claim": (["claim", "55", "--branch", "feature/x"], {("POST", "repos/o/r/issues/55/comments")}),
        "release": (["release", "55", "--everyone"], {("POST", "repos/o/r/issues/55/comments")}),
        "send (mailbox)": (["send", "hello"], {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                                               ("POST", "repos/o/r/issues/900/comments")}),
        "send --on": (["send", "hello", "--on", "101"], {("POST", "repos/o/r/issues/101/comments")}),
        "train add": (["train", "add", "101"], {("POST", "repos/o/r/issues/101/comments"), ("POST", "repos/o/r/labels"),
                                                ("POST", "repos/o/r/issues/101/labels"),
                                                ("POST", "repos/o/r/actions/workflows/merge-train.yml/dispatches")}),
        "train remove": (["train", "remove", "101"], {("DELETE", "repos/o/r/issues/101/labels/train"),
                                                      ("POST", "repos/o/r/issues/101/comments")}),
        "ci": (["ci", "mk4", "--ref", "feature/x"], {("POST", "repos/o/r/actions/workflows/ci.yml/dispatches")}),
        "outage start": (["outage", "start", "claude", "--note", "limit"],
                         {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                          ("POST", "repos/o/r/issues/900/comments")}),
        "outage end": (["outage", "end", "claude"],
                       {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                        ("POST", "repos/o/r/issues/900/comments")}),
    }

    CONTROLS = {
        "conductor pause": (["conductor", "pause", "--for", "1h"],
                            {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                             ("POST", "repos/o/r/issues/900/comments")}),
        "conductor resume": (["conductor", "resume"],
                             {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                              ("POST", "repos/o/r/issues/900/comments")}),
        "conductor escalate": (["conductor", "escalate", "look at #101"],
                               {("POST", "repos/o/r/labels"), ("POST", "repos/o/r/issues"),
                                ("POST", "repos/o/r/issues/900/comments"),
                                ("POST", "repos/o/r/actions/workflows/notify-owner.yml/dispatches")}),
    }

    def test_operational_writes_use_the_ops_token_and_reads_the_read_token(self):
        self.keychain("key")
        for name, (argv, expected) in self.OPERATIONAL.items():
            with self.subTest(command=name):
                self.server.requests.clear()
                self.runner.calls.clear()
                code, out, err = self.main(argv)
                self.assertEqual(code, EXIT_OK, err)
                self.assertEqual({(r["method"], r["path"].split("?")[0]) for r in self.writes()}, expected)
                self.assertEqual({r["kind"] for r in self.writes()}, {"ops"})
                self.assertEqual({r["kind"] for r in self.reads()} - {"read"}, set(), "every read on the read token")
                self.assertEqual(self.runner.calls, [], "nothing on Alex's gh")
        self.assertEqual(self.server.mints, 2, "one read and one ops token served every command")

    def test_conductor_control_records_use_the_owner_login(self):
        for name, (argv, expected) in self.CONTROLS.items():
            with self.subTest(command=name):
                self.runner.calls.clear()
                code, _, err = self.main(argv)
                self.assertEqual(code, EXIT_OK, err)
                writes = {(call[call.index("--method") + 1], call[2]) for call in self.runner_writes()}
                self.assertEqual(writes, expected)
        self.assertEqual(self.server.requests, [])

    def test_a_review_run_cannot_select_the_owner_identity(self):
        os.environ["COORD_AGENT"] = "reviewer@test"
        for argv in (["conductor", "pause", "--for", "1h"], ["train", "approve", "101", "--head", "a" * 40,
                                                             "--note", "review"]):
            with self.subTest(argv=argv):
                code, _, err = self.main(argv)
                self.assertEqual(code, EXIT_BACKEND)
                self.assertIn("no GitHub credential", err)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.server.requests, [])

    def test_attestations_stay_on_alex(self):
        self.keychain("key")
        for argv, label in ((["train", "approve", "101", "--head", "a" * 40, "--note", "Alex said so"], "owner-approved"),
                            (["train", "review", "101", "--head", "a" * 40, "--by", "agy@h:gemini", "--note", "n"], "reviewed"),
                            (["train", "substitute-review", "101", "--head", "a" * 40, "--note", "reviewed by X"], "reviewed")):
            with self.subTest(argv=argv[:2]):
                self.runner.calls.clear()
                code, out, err = self.main(argv)
                self.assertEqual(code, EXIT_OK, err)
                paths = [call[2] for call in self.runner_writes()]
                self.assertIn("repos/o/r/issues/101/comments", paths)
                self.assertIn("repos/o/r/issues/101/labels", paths)
        self.assertEqual(self.server.requests, [], "no app token was minted or used for an attestation")
        self.assertEqual(self.security_calls(), [])

    def test_the_command_decides_the_identity(self):
        seen = {}

        def capture(args, gh):
            seen[budget.command_name(args)] = gh.app is not None
            return EXIT_OK

        argvs = [["board"], ["whoami"], ["doctor"], ["claim", "5", "--branch", "b"], ["release", "5"], ["send", "x"],
                 ["inbox"], ["ci", "mk4"], ["train", "report"], ["train", "add", "7"], ["train", "remove", "7"], ["train", "status"],
                 ["train", "approve", "7", "--head", "a" * 40, "--note", "n"],
                 ["train", "review", "7", "--head", "a" * 40, "--by", "x", "--note", "n"],
                 ["train", "substitute-review", "7", "--head", "a" * 40, "--note", "n"], ["train", "step"], ["outage", "start", "claude"], ["outage", "end", "claude"],
                 ["conductor", "tick"], ["conductor", "status"], ["conductor", "diagnose"], ["conductor", "pause", "--for", "1h"],
                 ["conductor", "resume"], ["conductor", "escalate", "x"],
                 ["token", "status"], ["token", "exec", "--", "true"], ["token", "import-key", "x.pem"]]
        self.assertEqual({tuple(argv[:2]) if argv[0] in ("train", "outage", "conductor", "token") else (argv[0],)
                          for argv in argvs}, parser_commands(), "every subcommand is decided here")
        for argv in argvs:
            args = cli.parser().parse_args(argv)
            args.func = capture
            gh = cli.GitHub(REPO)
            cli.attach_app(gh)
            self.assertIsNotNone(gh.app)
            cli.run(args, gh)
        self.assertEqual({name for name, on_app in seen.items() if not on_app}, set(cli.USER_IDENTITY))
        self.assertEqual(cli.USER_IDENTITY, {"train approve", "train review", "train substitute-review", "train step",
                                             "conductor tick", "conductor pause", "conductor resume", "conductor escalate"})
        self.assertLessEqual({"train review", "train substitute-review"}, cli.ATTESTATIONS)

    def test_conductor_status_reads_on_the_app(self):
        self.keychain("key")
        code, out, err = self.main(["conductor", "status"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertTrue(self.server.api_requests())
        self.assertEqual({r["kind"] for r in self.server.api_requests()}, {"read"})
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.runner.calls, [], "nothing on Alex's gh")

    def test_conversation_records_move_to_alex_with_one_line(self):
        # P3-6: if Alex ever wants claims and messages posted as himself again, the change is
        # `USER_IDENTITY = ATTESTATIONS | NEEDS_ALEX | CONVERSATION`. Prove that line is enough.
        self.patch(cli, "USER_IDENTITY", cli.ATTESTATIONS | cli.NEEDS_ALEX | cli.CONVERSATION)
        self.keychain("key")
        for argv in (["claim", "55", "--branch", "feature/x"], ["release", "55", "--everyone"], ["send", "hi"],
                     ["send", "hi", "--on", "101"], ["outage", "start", "claude"], ["outage", "end", "claude"]):
            with self.subTest(argv=argv):
                self.runner.calls.clear()
                code, _, err = self.main(argv)
                self.assertEqual(code, EXIT_OK, err)
                self.assertTrue(self.runner_writes(), "posted through Alex's gh")
        self.assertEqual(self.server.requests, [], "no app token minted or used")
        self.assertEqual(self.security_calls(), [])

    def test_a_hand_run_train_step_never_touches_the_app(self):
        code, _, err = self.main(["train", "step", "--repo-dir", self.no_repo])
        self.assertEqual(code, EXIT_BACKEND)
        self.assertIn("git", err)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.security_calls(), [])

    def test_a_missing_permission_is_named_in_the_error(self):
        # The live check's first question: does issues:write cover a PR number? If GitHub
        # says no, its X-Accepted-GitHub-Permissions header names what it wants.
        self.keychain("key")
        self.server.forbid_writes = "issues=write; pull_requests=write"
        code, _, err = self.main(["send", "app check", "--on", "101"])
        self.assertEqual(code, EXIT_BACKEND)
        self.assertIn("HTTP 403", err)
        self.assertIn("(accepted permissions: issues=write; pull_requests=write)", err)
        self.assertEqual(self.runner.calls, [], "a refused app write is not retried as Alex")
        self.assert_no_secret(err)

    def test_graphql_is_a_read_and_every_other_post_is_a_write(self):
        from coordlib.gh import token_kind
        self.assertEqual(token_kind("POST", "graphql"), "read")
        self.assertEqual(token_kind("POST", "http://127.0.0.1:9/graphql"), "read")
        self.assertEqual(token_kind("GET", "repos/o/r/issues"), "read")
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(token_kind(method, "repos/o/r/issues/1/comments"), "ops")

    def test_whoami_names_the_identity_and_mints_nothing(self):
        code, out, _ = self.main(["whoami"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("githubIdentity: example-reviewer[bot] (installation not yet known)", out)
        self.assertIn("user for `conductor escalate`, `conductor pause`, `conductor resume`, `conductor tick`, "
                      "`train approve`, `train review`, `train step`, "
                      "`train substitute-review`", out)
        self.assertIn("apiBudget: API (app): no current reading", out)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.security_calls(), [])


class ModeOffTests(AppCliTestCase):
    """App mode off: exactly today's behaviour, Alex's identity for everything."""

    def switch_off(self, how):
        if how == "actions":
            os.environ["GITHUB_ACTIONS"] = "true"
        elif how == "override":
            os.environ["COORD_APP"] = "off"
        else:
            os.environ["COORD_APP_CONFIG"] = os.path.join(self.root, "no-config.json")

    def test_off_means_the_gh_path_and_no_app(self):
        for how in ("actions", "override", "no config"):
            with self.subTest(off=how):
                self.switch_off(how)
                self.runner.calls.clear()
                code, out, err = self.main(["send", "hi", "--on", "101"])
                self.assertEqual(code, EXIT_OK, err)
                self.assertIn(["gh", "api", "repos/o/r/issues/101/comments", "--method", "POST", "--input", "-"],
                              self.runner.calls)
                self.assertEqual(self.server.requests, [])
                self.assertEqual(self.security_calls(), [])
                code, out, _ = self.main(["whoami"])
                self.assertIn("githubIdentity: user's gh/keyring login", out)
                for name in ("GITHUB_ACTIONS", "COORD_APP"):
                    os.environ.pop(name, None)
                os.environ["COORD_APP_CONFIG"] = self.config_path

    def test_off_over_https_uses_the_environment_token_as_before(self):
        os.environ["COORD_APP"] = "off"
        os.environ["COORD_TRANSPORT"] = "http"
        os.environ["GH_TOKEN"] = USER_TOKEN
        self.server.tokens[USER_TOKEN] = "user"
        code, _, err = self.main(["send", "hi", "--on", "101"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual([r["bearer"] for r in self.server.requests], [USER_TOKEN])


# MARK: - No fallback


class NoFallbackTests(AppCliTestCase):
    """After an app failure nothing is sent as Alex: the gh runner is never called and no
    request carries his token. Reads with a cache serve it; everything else refuses."""

    FAILURES = ("keychain", "mint 401", "mint 403", "broken config", "deep config")

    def break_app(self, how):
        os.environ["GH_TOKEN"] = USER_TOKEN  # a fallback would find it right here
        os.environ["GITHUB_TOKEN"] = USER_TOKEN
        self.server.tokens[USER_TOKEN] = "user"
        if how == "keychain":
            self.keychain("missing")
        elif how.startswith("mint"):
            self.keychain("key")
            self.server.mint_status = int(how.split()[1])
        elif how == "broken config":
            self.write_config("{broken")
        else:
            # Codex on a774ba186 (P2): nested too deep for any Python's parser (RecursionError).
            # It must end as a broken config does: refused, never Alex's token.
            self.write_config(DEEP_JSON)

    def assert_nothing_as_alex(self):
        self.assertEqual(self.runner.calls, [], "the gh runner was called after an app failure")
        self.assertEqual([r for r in self.server.requests if r["bearer"] == USER_TOKEN], [])
        self.assertEqual(self.server.api_requests(), [], "no API call went out at all")

    def test_writes_refuse_naming_the_cause_and_the_override(self):
        for how in self.FAILURES:
            for argv in (["claim", "55", "--branch", "feature/x"], ["release", "55", "--everyone"], ["send", "hi"],
                         ["send", "hi", "--on", "101"], ["train", "add", "101"], ["train", "remove", "101"],
                         ["ci", "mk4", "--ref", "feature/x"]):
                with self.subTest(failure=how, argv=argv):
                    self.break_app(how)
                    code, out, err = self.main(argv)
                    self.assertEqual(code, EXIT_BACKEND, err)
                    self.assertIn("GitHub App token unavailable", err)
                    self.assertIn("COORD_APP=off", err)
                    self.assertIn("nothing was sent as user", err)
                    self.assert_nothing_as_alex()

    def test_the_board_serves_its_cache_with_a_notice(self):
        self.seed_snapshot(age_seconds=600)
        for how in self.FAILURES:
            for argv in (["board"], ["board", "--fresh"], ["board", "--brief"], ["board", "--json"]):
                with self.subTest(failure=how, argv=argv):
                    self.break_app(how)
                    code, out, err = self.main(argv)
                    self.assertEqual(code, EXIT_OK, err)
                    self.assertIn("GitHub App token unavailable", err)
                    self.assertIn("showing the snapshot from 10m ago", err)
                    self.assertIn("COORD_APP=off", err)
                    self.assertTrue(out)
                    self.assert_nothing_as_alex()

    def test_no_cache_to_serve_refuses(self):
        for how in self.FAILURES:
            for argv in (["board"], ["train", "status"], ["inbox"]):
                with self.subTest(failure=how, argv=argv):
                    self.break_app(how)
                    code, out, err = self.main(argv)
                    self.assertEqual(code, EXIT_BACKEND)
                    self.assertIn("no cached snapshot on this machine to show", err)
                    self.assertIn("COORD_APP=off", err)
                    self.assert_nothing_as_alex()

    def test_inbox_and_train_status_read_the_cache(self):
        self.seed_snapshot()
        self.break_app("keychain")
        code, out, err = self.main(["inbox"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("0 new in that snapshot; run `coord inbox` once the app token is back", out)
        self.assertIsNone(cache.load_cursor(REPO, "claude@test:app"), "nothing marked read")
        code, out, err = self.main(["train", "status"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("batch: not read (GitHub App token unavailable)", out)
        self.assert_nothing_as_alex()

    def test_a_claim_never_acts_on_a_stale_board(self):
        self.seed_snapshot()
        self.break_app("keychain")
        code, _, err = self.main(["claim", "55", "--branch", "feature/x"])
        self.assertEqual(code, EXIT_BACKEND)
        self.assertNotIn("showing the snapshot", err)
        self.assert_nothing_as_alex()

    def test_a_failure_between_the_two_tokens_posts_nothing(self):
        self.seed_token("read")  # reads work; the ops mint fails
        self.break_app("keychain")
        code, _, err = self.main(["send", "hi"])
        self.assertEqual(code, EXIT_BACKEND, err)
        self.assertEqual(self.writes(), [], "not even the mailbox label")
        self.assertEqual({r["kind"] for r in self.server.api_requests()}, {"read"})
        self.assertEqual(self.runner.calls, [])

    def test_token_exec_never_runs_its_command(self):
        marker = os.path.join(self.root, "ran")
        self.break_app("keychain")
        code, _, err = self.main(["token", "exec", "--", "touch", marker])
        self.assertEqual(code, EXIT_BACKEND)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("GitHub App token unavailable", err)
        self.assert_nothing_as_alex()

    def test_one_keychain_attempt_per_command(self):
        self.seed_snapshot()
        self.break_app("keychain")
        self.main(["train", "status"])
        self.assertEqual(len(self.security_calls()), 1, "the failure is not retried within the command")


class TrainStatusBatchTests(AppCliTestCase):
    """Codex on e470b61da (P2): the read token failing after the refs call, on the batch's
    workflow runs (its first page or a later one), took `train status` down with a backend
    error. It reports the queue from the snapshot and does not read the batch, as for refs."""

    def setUp(self):
        super().setUp()
        os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"] = USER_TOKEN  # a fallback would find it here
        self.server.tokens[USER_TOKEN] = "user"
        self.seed_token("read")
        now = time.time()
        cache.store_snapshot(REPO, {
            "schema": 1, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "repo": REPO,
            "main": {"sha": "f" * 40}, "prs": [], "claims": [], "overlaps": [],
            "train": [{"pr": 101, "headAtAdd": "a" * 40}], "runs": {"running": [], "queued": []}, "messages": [],
            "apiCalls": 9})
        self.runs_calls = 0
        real = self.server._api_call

        def api_call(method, path, body):
            route = path.split("?", 1)[0]
            if route.endswith("/git/matching-refs/heads/train"):
                return FakeResponse([{"ref": "refs/heads/train/batch-1", "object": {"sha": "b" * 40}}], {})
            if "/actions/workflows/" in route and route.endswith("/runs"):
                self.runs_calls += 1
                if self.runs_calls < self.fail_on_page:
                    link = f"<https://api.github.com/{route}?page={self.runs_calls + 1}>; rel=\"next\""
                    return FakeResponse({"workflow_runs": []}, {"Link": link})
                raise self.server._error(f"https://api.github.com/{path}", 401, {"message": "Bad credentials"})
            return real(method, path, body)

        self.patch(self.server, "_api_call", api_call)

    def test_a_refused_runs_request_reports_the_queue_and_skips_the_batch(self):
        for page in (1, 2):
            for argv in (["train", "status"], ["train", "status", "--json"]):
                with self.subTest(page=page, argv=argv):
                    self.fail_on_page, self.runs_calls = page, 0
                    self.seed_token("read")  # the 401 dropped the last one
                    code, out, err = self.main(argv)
                    self.assertEqual(code, EXIT_OK, err)
                    self.assertEqual(self.runs_calls, page, "the refs succeeded; the runs were refused on this page")
                    self.assertIn("GitHub App token unavailable", err)
                    self.assertIn("the queue is from the cached snapshot", err)
                    if "--json" in argv:
                        data = json.loads(out)
                        self.assertIsNone(data["batches"])
                        self.assertEqual([entry["pr"] for entry in data["queue"]], [101])
                    else:
                        self.assertIn("batch: not read (GitHub App token unavailable)", out)
                        self.assertIn("1. #101", out)
                    self.assertEqual(self.runner.calls, [], "the gh runner was never called")
                    self.assertEqual([r for r in self.server.requests if r["bearer"] == USER_TOKEN], [],
                                     "nothing was sent as user")


class ConfigPathTests(AppCliTestCase):
    """Codex on fb63aef6e (P1): os.path.lexists is False on EACCES, so a config whose folder
    cannot be searched read as "no app config" and turned app mode off: every command ran as
    Alex. Only a path that does not exist is absent; any other failure to check it is a broken
    config, app mode on with an error."""

    def use_config_at(self, path):
        os.environ["COORD_APP_CONFIG"] = path
        os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"] = USER_TOKEN  # a fallback would find it here
        self.server.tokens[USER_TOKEN] = "user"

    def assert_nothing_as_alex(self):
        self.assertEqual(self.runner.calls, [], "the gh runner was called")
        self.assertEqual([r for r in self.server.requests if r["bearer"] == USER_TOKEN], [])
        self.assertEqual(self.server.api_requests(), [], "no API call went out at all")

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                     "requires POSIX non-root chmod semantics")
    def test_a_config_folder_that_cannot_be_searched_is_broken_not_absent(self):
        folder = os.path.join(self.root, "locked")
        os.makedirs(folder)
        with open(os.path.join(folder, "github-app.json"), "w", encoding="utf-8") as handle:
            json.dump({"client_id": "Iv23test", "repo": REPO}, handle)
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, 0o700)  # before the temporary root is removed
        self.use_config_at(os.path.join(folder, "github-app.json"))
        mode = apptoken.detect(REPO)
        self.assertTrue(mode.on, mode.reason)
        self.assertIn("cannot be checked (PermissionError", mode.error)
        code, _, err = self.main(["send", "hi"])
        self.assertEqual(code, EXIT_BACKEND, err)
        self.assertIn("GitHub App token unavailable", err)
        self.assertIn("COORD_APP=off", err)
        self.assert_nothing_as_alex()
        code, _, err = self.main(["board"])
        self.assertEqual(code, EXIT_BACKEND, err)
        self.assertIn("no cached snapshot on this machine to show", err)
        self.assert_nothing_as_alex()
        code, out, _ = self.main(["token", "status"])
        self.assertIn("app mode: on (the app config's path cannot be checked)", out)

    def test_a_path_that_does_not_exist_is_absent(self):
        plain = os.path.join(self.root, "regular-file")
        with open(plain, "w", encoding="utf-8") as handle:
            handle.write("x")
        for name, path in (("missing", os.path.join(self.root, "nowhere", "github-app.json")),
                           ("under a regular file", os.path.join(plain, "github-app.json"))):
            with self.subTest(name):
                self.use_config_at(path)
                mode = apptoken.detect(REPO)
                self.assertFalse(mode.on)
                self.assertIn("no app config at", mode.reason)


class StaleWriteGuardTests(AppCliTestCase):
    """A write never acts on a board served from the cache (review of #1046, P3-2 / M5).

    The read token fails and the ops token works: exactly the case where only
    get_snapshot's guard (a cached board serves READS only) stands between a 3-hour-old
    board and real writes. Round one's test failed earlier (in fetch_targets) or was
    stopped by a failure memory shared across kinds, so removing the guard passed it.
    """

    def setUp(self):
        super().setUp()
        from apptestkit import pr_node
        from coordlib import snapshot
        self.seed_snapshot(age_seconds=3 * 3600)
        stale = cache.load_snapshot(REPO, float("inf"))
        stale["prs"] = [snapshot.summarize_pr(pr_node(101))]
        stale.pop("cacheAgeSeconds", None)
        cache.store_snapshot(REPO, stale)
        os.utime(cache.snapshot_path(REPO), (time.time() - 3 * 3600,) * 2)
        self.keychain("key")
        self.server.refuse_kinds = {"read"}

    def test_writes_that_read_the_board_refuse_instead_of_using_a_stale_one(self):
        for argv in (["train", "add", "101"], ["claim", "55", "--branch", "feature/x"], ["send", "hi"]):
            with self.subTest(argv=argv):
                code, out, err = self.main(argv)
                self.assertEqual(code, EXIT_BACKEND, err)
                self.assertEqual(self.writes(), [], "no write from a stale board")
                self.assertNotIn("showing the snapshot", err)
                self.assertIn("GitHub App token unavailable", err)
                self.assertEqual(self.runner.calls, [])
        self.assertTrue(self.server.refuse_kinds, "the read mint really was refused throughout")

    def test_the_ops_token_itself_works_here(self):
        # The control: writes that read nothing (no board, no issue) go through on ops,
        # so the refusals above come from the guard, not from a dead ops token.
        for argv in (["train", "remove", "101"], ["send", "hi", "--on", "101"]):
            with self.subTest(argv=argv):
                self.server.requests.clear()
                code, _, err = self.main(argv)
                self.assertEqual(code, EXIT_OK, err)
                self.assertTrue(self.writes())
                self.assertEqual({r["kind"] for r in self.server.api_requests()}, {"ops"})


class RevokedTokenTests(AppCliTestCase):
    """A cached token GitHub answers 401 to (revoked, or the installation suspended) is
    dropped from the cache and refused; nothing goes to Alex (review of #1046)."""

    def test_a_401_evicts_the_kind_and_reads_serve_the_cache(self):
        self.seed_snapshot(age_seconds=7200)
        revoked = self.seed_token("read")
        del self.server.tokens[revoked]  # GitHub no longer knows it
        os.environ["GH_TOKEN"] = USER_TOKEN
        code, out, err = self.main(["board"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("GitHub refused the app's read token (HTTP 401", err)
        self.assertIn("COORD_APP=off", err)
        self.assertIn("showing the snapshot from 2h00m ago", err)
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "read.json")), "the refused token is gone")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual([r for r in self.server.requests if r["bearer"] == USER_TOKEN], [])
        self.assertEqual(len(self.server.api_requests()), 1, "one refused call, then the cache")
        self.assert_no_secret(out, err)
        # The next command mints a fresh token.
        self.keychain("key")
        code, _, err = self.main(["board"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(self.server.mints, 1)

    def test_a_401_on_a_write_refuses(self):
        self.seed_token("read")
        revoked = self.seed_token("ops")
        del self.server.tokens[revoked]
        code, _, err = self.main(["send", "hi", "--on", "101"])
        self.assertEqual(code, EXIT_BACKEND)
        self.assertIn("GitHub refused the app's ops token (HTTP 401", err)
        self.assertTrue(os.path.exists(os.path.join(self.token_dir, "read.json")), "only the refused kind goes")
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "ops.json")))
        self.assertEqual(self.runner.calls, [])


class StaleBoardTests(AppCliTestCase):
    """A board served from the cache after an app failure says how old it is, first."""

    def test_the_board_and_the_brief_lead_with_the_age_and_the_cause(self):
        self.seed_snapshot(age_seconds=3 * 3600)
        self.keychain("missing")
        code, out, _ = self.main(["board"])
        self.assertEqual(code, EXIT_OK)
        self.assertRegex(out.splitlines()[0], r" · STALE: snapshot from 3h00m ago; GitHub App token unavailable: "
                                              r"the Keychain did not return the app key.*COORD_APP=off")
        code, out, _ = self.main(["board", "--brief"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(out.startswith("coord (claude@test:app): STALE: snapshot from 3h00m ago; "
                                       "GitHub App token unavailable: "), out)
        code, out, _ = self.main(["board", "--json"])
        self.assertEqual(json.loads(out)["stale"]["ago"], "3h00m")

    def test_a_fresh_board_says_nothing_of_the_kind(self):
        self.keychain("key")
        code, out, _ = self.main(["board", "--brief"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("STALE", out)
        code, out, _ = self.main(["conductor", "status"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("STALE", out)

    def test_conductor_status_leads_with_the_age_too(self):
        # Round three of the #1046 review (P3-3): conductor status serves the cache like board.
        self.seed_snapshot(age_seconds=3 * 3600)
        self.keychain("missing")
        code, out, err = self.main(["conductor", "status"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertTrue(out.startswith("STALE: snapshot from 3h00m ago; GitHub App token unavailable: "), out)
        self.assertIn("showing the snapshot from 3h00m ago", err)
        self.assertEqual(self.runner.calls, [])


class EchoedSecretTests(AppCliTestCase):
    """An error body that repeats the bearer token and a JWT never reaches the terminal:
    every printed error passes through gh.redact (review of #1046, mutant M18b)."""

    def test_no_command_prints_an_echoed_token_or_jwt(self):
        self.keychain("key")
        errors = []
        for status in (400, 404, 409, 422):
            for argv in (["send", "hi", "--on", "101"], ["claim", "55", "--branch", "feature/x"], ["board", "--fresh"],
                         ["train", "add", "101"], ["ci", "mk4", "--ref", "feature/x"], ["inbox"],
                         ["outage", "end", "claude"], ["token", "exec", "--", "true"]):
                with self.subTest(status=status, argv=argv):
                    self.server.echo_secrets = status
                    code, out, err = self.main(argv)
                    errors.append(err)
                    self.assert_no_secret(out, err)
        self.assertGreater(self.server.mints, 0, "real tokens were minted and echoed back")
        self.assertIn("[redacted]", "".join(errors), "the echoed secrets reached the printing path and were redacted")


# MARK: - coord token


class TokenStatusTests(AppCliTestCase):
    def test_a_deeply_nested_cache_document_is_a_miss(self):
        # Codex on 141d38f62 (P2): 10,000 levels of nesting raised RecursionError out of the
        # token cache's reader and took coord down. The token file and the failure record are
        # misses now, and the next call mints.
        os.makedirs(self.token_dir, exist_ok=True)
        for name, key in (("read.json", "permissions"), ("read.failed.json", "cause"), ("ops.json", "permissions"),
                          ("ops.failed.json", "cause")):
            path = os.path.join(self.token_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f'{{"{key}":' * 10000 + "{}" + "}" * 10000)
            os.chmod(path, 0o600)
        code, out, err = self.main(["token", "status"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("read token: ", out)
        self.assertNotIn("Traceback", err)
        tokens = self.tokens()
        self.assertIsNone(tokens.failure("read"))
        self.assertIsNone(tokens.cached("ops"))
        self.keychain("key")
        self.assertTrue(tokens.token("read").startswith("ghs_"), "a miss: minted afresh")
        # Python 3.14's parser takes 10,000 levels; a million is too deep for every version.
        deep = "[" * 1_000_000 + "]" * 1_000_000
        with self.assertRaises(RecursionError, msg="the fixture must really be too deep to parse"):
            json.loads(deep)
        with open(os.path.join(self.token_dir, "ops.json"), "w", encoding="utf-8") as handle:
            handle.write(deep)
        self.assertIsNone(tokens.cached("ops"))
        self.assertEqual(self.main(["token", "status"])[0], EXIT_OK)

    def test_status_with_app_mode_off(self):
        os.environ["COORD_APP"] = "off"
        code, out, _ = self.main(["token", "status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("app mode: off (COORD_APP=off: the deliberate override", out)
        os.environ.pop("COORD_APP")
        os.environ["COORD_APP_CONFIG"] = os.path.join(self.root, "none.json")
        self.assertIn("app mode: off (no app config at", self.main(["token", "status"])[1])

    def test_status_shows_each_kind_without_minting_or_printing_a_token(self):
        code, out, _ = self.main(["token", "status"])
        self.assertIn("app mode: on", out)
        self.assertIn("installation: not yet known", out)
        self.assertIn("read token: none cached", out)
        token = self.seed_token("read")
        self.seed_token("ops", permissions=apptoken.READ_PERMISSIONS)
        code, out, err = self.main(["token", "status"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(f"installation: {INSTALLATION}", out)
        self.assertRegex(out, r"read token: reusable; expires \S+Z \(\d+ min left\); permissions actions:read, "
                              r"checks:read, contents:read, issues:read, metadata:read, pull_requests:read, statuses:read")
        self.assertIn("ops token: permissions differ from the kind (not used)", out)
        self.assertNotIn(token, out + err)
        self.assert_no_secret(out, err)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.security_calls(), [])

    def test_status_with_a_broken_config(self):
        self.write_config("[]")
        code, out, _ = self.main(["token", "status"])
        self.assertIn("app mode: on (the app config exists but cannot be used)", out)
        self.assertIn("every app call refuses: the app config", out)
        with self.assertRaises(RecursionError, msg="the fixture must really be too deep to parse"):
            json.loads(DEEP_JSON)
        self.write_config(DEEP_JSON)
        code, out, err = self.main(["token", "status"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("app mode: on (the app config exists but cannot be used)", out)
        self.assertIn("cannot be read (RecursionError)", out)

    def test_a_cached_token_whose_expiry_is_not_a_string_is_a_miss(self):
        # The review of 2d13f6b1e..752135947 (P2): review.parse_time calls .strip(), so an
        # `expires_at` of 1 or true raised AttributeError out of token status, cached() and token().
        self.keychain("key")
        for value in (1, True, [1]):
            with self.subTest(expires_at=value):
                for kind in apptoken.KINDS:
                    self.seed_token(kind)
                    path = os.path.join(self.token_dir, f"{kind}.json")
                    with open(path, encoding="utf-8") as handle:
                        entry = json.load(handle)
                    entry["expires_at"] = value
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(entry, handle)
                code, out, err = self.main(["token", "status"])
                self.assertEqual(code, EXIT_OK, err)
                self.assertNotIn("Traceback", err)
                for kind in apptoken.KINDS:
                    self.assertIn(f"{kind} token: expired or within five minutes of expiry (the next call mints)", out)
                    self.assertIsNone(self.tokens().cached(kind))
                mints = self.server.mints
                self.assertTrue(self.tokens().token("read").startswith("ghs_"))
                self.assertEqual(self.server.mints, mints + 1, "a miss: the next call mints")

    def test_a_group_readable_cached_token_is_not_reported_reusable(self):
        # Codex on a774ba186 (P2): describe() said `reusable` for a 0644 file that cached()
        # refuses, so `token status` disagreed with what the next call does.
        self.seed_token("read", mode=0o644)
        self.assertIsNone(self.tokens().cached("read"))
        self.assertEqual(self.tokens().describe("read")["state"], "file not private to this user (not used)")
        self.assertIn("read token: file not private to this user (not used)", self.main(["token", "status"])[1])

    def test_status_names_another_api_url_as_the_reason(self):
        os.environ["GITHUB_API_URL"] = "http://127.0.0.1:9"
        code, out, _ = self.main(["token", "status"])
        self.assertIn("app mode: off (GITHUB_API_URL is http://127.0.0.1:9, not https://api.github.com; "
                      "app credentials go to GitHub only)", out)
        self.assertIn("githubIdentity: user's gh/keyring login", self.main(["whoami"])[1])

    def test_status_shows_a_held_mint_failure(self):
        self.keychain("missing")
        self.main(["board"])
        code, out, err = self.main(["token", "status"])
        self.assertRegex(out, r"read token: none cached\n  a mint failed \d+s ago and is not retried before "
                              r"\d\d:\d\d:\d\dZ: the Keychain did not return the app key")
        self.assert_no_secret(out, err)


class TokenExecTests(AppCliTestCase):
    CHECK = ("import hashlib, os, sys; token = os.environ.get('GH_TOKEN', ''); "
             "sys.exit(0 if hashlib.sha256(token.encode()).hexdigest() == sys.argv[1] else 7)")

    def test_the_child_gets_the_read_token_and_the_parent_never_does(self):
        self.keychain("key")
        code, out, err = self.main(["token", "exec", "--", sys.executable, "-c", "import os, sys; "
                                    "sys.exit(0 if os.environ['GH_TOKEN'].startswith('ghs_') else 7)"])
        self.assertEqual(code, EXIT_OK, err)
        (token, kind), = self.server.tokens.items()
        self.assertEqual(kind, "read")
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(self.main(["token", "exec", "--", sys.executable, "-c", self.CHECK, digest])[0], 0)
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assert_no_secret(out, err)

    def test_the_child_inherits_no_other_github_token(self):
        # Codex on 2d13f6b1e (P2): the child inherited Alex's GITHUB_TOKEN, which gh and
        # other tools read before GH_TOKEN. Every inherited GitHub token is removed.
        parent = {"PATH": "/usr/bin", "GITHUB_TOKEN": "x", "GH_TOKEN": "y", "GH_ENTERPRISE_TOKEN": "z",
                  "GITHUB_ENTERPRISE_TOKEN": "w", "GH_CONFIG_DIR": "/tmp/gh"}
        self.assertEqual(cli.exec_environment(parent, "app"),
                         {"PATH": "/usr/bin", "GH_TOKEN": "app", "GH_CONFIG_DIR": "/tmp/gh"})
        for name in ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ[name] = USER_TOKEN
        os.environ["GITHUB_TOKEN"] = USER_TOKEN
        self.keychain("key")
        check = ("import os, sys; names = ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'); "
                 "sys.exit(7 if any(n in os.environ for n in names) else 0 if os.environ['GH_TOKEN'].startswith('ghs_') "
                 "else 8)")
        code, out, err = self.main(["token", "exec", "--", sys.executable, "-c", check])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(os.environ["GITHUB_TOKEN"], USER_TOKEN, "the parent's environment is unchanged")
        self.assert_no_secret(out, err)

    def test_ops_only_by_name(self):
        self.keychain("key")
        self.assertEqual(self.main(["token", "exec", "--kind", "ops", "--", "true"])[0], EXIT_OK)
        self.assertEqual(sorted(self.server.tokens.values()), ["ops"])
        self.assertEqual(cli.parser().parse_args(["token", "exec", "--", "true"]).kind, "read")

    def test_the_exit_code_is_the_childs(self):
        self.keychain("key")
        self.assertEqual(self.main(["token", "exec", "--", sys.executable, "-c", "raise SystemExit(3)"])[0], 3)
        self.assertEqual(self.main(["token", "exec", "--", os.path.join(self.root, "no-such-command")])[0], 127)

    def test_app_mode_off_refuses_and_runs_nothing(self):
        os.environ["COORD_APP"] = "off"
        marker = os.path.join(self.root, "ran")
        code, _, err = self.main(["token", "exec", "--", "touch", marker])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("app mode is off", err)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.runner.calls, [], "no probe either")

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(self.main(["token", "exec"])[0], EXIT_USAGE)
        self.assertEqual(self.main(["token", "exec", "--"])[0], EXIT_USAGE)

    def test_below_the_hard_floor_exec_is_refused(self):
        now = int(time.time())
        principal = budget.app_principal(INSTALLATION)
        self.seed_token("read")
        budget.record(REPO, {"x-ratelimit-remaining": "0", "x-ratelimit-used": "4900", "x-ratelimit-limit": "5000",
                             "x-ratelimit-reset": str(now + 600)}, "core", principal)
        code, _, err = self.main(["token", "exec", "--", "true"])
        self.assertEqual(code, 5)
        self.assertIn("not running `coord token exec`", err)


class ScriptTests(AppCliTestCase):
    """The real `coord` script as a subprocess, with a seeded token cache (the cached path
    runs neither `security` nor a mint)."""

    def run_script(self, *argv):
        env = {name: value for name, value in os.environ.items() if name not in ("GH_TOKEN", "GITHUB_TOKEN")}
        # The subprocess does not inherit this process's patches: point its `security` at
        # the tripwire (honoured because COORD_APP_CONFIG is a test config), so it can never
        # reach /usr/bin/security, and its GitHub at nothing it could reach.
        env["COORD_SECURITY_BIN"] = apptoken.SECURITY
        env["FAKE_SECURITY_MODE"] = "tripwire"
        return subprocess.run([sys.executable, COORD, *argv], capture_output=True, text=True, env=env, timeout=60)

    def test_exec_hands_the_child_the_cached_token_and_prints_none(self):
        token = self.seed_token("read")
        digest = hashlib.sha256(token.encode()).hexdigest()
        result = self.run_script("token", "exec", "--", sys.executable, "-c", TokenExecTests.CHECK, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_secret(result.stdout, result.stderr)
        status = self.run_script("token", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("read token: reusable", status.stdout)
        self.assert_no_secret(status.stdout, status.stderr)
        self.assertEqual(self.security_calls(), [])

    def test_a_subprocess_that_must_mint_reaches_only_the_tripwire(self):
        # No cached token: the script has to mint, so it asks `security` for the key. The
        # tripwire answers (and logs the call); /usr/bin/security never runs, and nothing
        # reaches the network because the key is refused before any request is built.
        result = self.run_script("token", "exec", "--", "true")
        self.assertEqual(result.returncode, EXIT_BACKEND, result.stderr)
        self.assertIn("tripwire", result.stderr)
        self.assertEqual(len(self.security_calls()), 1, "the fake saw the one call; the real one saw none")
        self.assert_no_secret(result.stdout, result.stderr)


# MARK: - No secret in any output


class NoLeakTests(AppCliTestCase):
    """Every subcommand, with throwaway credentials minted for real by the fake GitHub, on
    success and on failure: no `ghs_`, no JWT-shaped string, no key material, and none of
    the tokens or JWTs the fake saw, in stdout or stderr."""

    def setUp(self):
        super().setUp()
        self.diagnosis_fails = False
        self.diagnosis_calls = []
        self.context = os.path.join(self.root, "diagnosis.json")
        with open(self.context, "w", encoding="utf-8") as handle:
            handle.write("{}")
        which = cli.shutil.which
        self.patch(cli.shutil, "which", lambda name: f"/fake/{name}" if name in ("codex", "claude") else which(name))
        # The every-command sweep must never start an installed model CLI. Exercise
        # both protocol success and failure using only synthetic JSON responses.
        self.patch(conductor, "default_runner", self.diagnose)

    def diagnose(self, command, cwd, env, timeout):
        self.diagnosis_calls.append(command)
        secret = "ghs_" + "x" * 60
        report = {"summary": "diagnosis " + secret, "needsRepair": False, "reason": "checked input"}
        if command[0].endswith("codex"):
            events = ([{"type": "error", "message": "runtime failure " + secret}] if self.diagnosis_fails else [
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}},
                {"type": "turn.completed", "usage": {}},
            ])
            output = "\n".join(json.dumps(event) for event in events)
        else:
            output = json.dumps({"type": "result", "is_error": self.diagnosis_fails,
                                 "result": "runtime failure " + secret if self.diagnosis_fails else json.dumps(report)})
        return subprocess.CompletedProcess(command, int(self.diagnosis_fails), output, "")

    def argvs(self):
        return [
            ["board"], ["board", "--brief"], ["board", "--json"], ["board", "--fresh"], ["whoami"], ["whoami", "--json"],
            ["doctor"], ["doctor", "--json"],
            ["claim", "55", "--branch", "feature/x"], ["claim", "56", "--branch", "feature/y", "--dry-run"],
            ["release", "55", "--everyone"], ["release", "57"], ["send", "hello"], ["send", "hi", "--on", "101"],
            ["inbox"], ["inbox", "--json", "--all"], ["ci", "mk4", "--ref", "feature/x"],
            ["train", "add", "101"], ["train", "remove", "101"], ["train", "status"], ["train", "status", "--json"],
            ["train", "report"], ["train", "report", "--json"],
            ["train", "approve", "101", "--head", "a" * 40, "--note", "n"],
            ["train", "review", "101", "--head", "a" * 40, "--by", "x", "--note", "n"],
            ["train", "substitute-review", "101", "--head", "a" * 40, "--note", "n"],
            ["train", "step", "--repo-dir", self.no_repo],
            ["outage", "start", "claude", "--note", "limit"], ["outage", "end", "claude"],
            ["conductor", "tick", "--dry-run", "--no-wake"], ["conductor", "status"],
            ["conductor", "diagnose", "--input", self.context],
            ["conductor", "diagnose", "--input", self.context, "--json"],
            ["conductor", "diagnose", "--input", self.context, "--runtime", "claude", "--json"],
            ["conductor", "pause", "--for", "1h"], ["conductor", "resume"], ["conductor", "escalate", "look"],
            ["token", "status"], ["token", "exec", "--", "true"], ["token", "exec", "--kind", "ops", "--", "true"],
            ["token", "import-key", os.path.join(self.root, "missing.pem")],
        ]

    def test_no_subcommand_prints_a_secret(self):
        commands = {tuple(argv[:2]) if argv[0] in ("train", "outage", "conductor", "token") else (argv[0],)
                    for argv in self.argvs()}
        for scenario in ("minting", "cached", "mint refused", "keychain fails"):
            self.diagnosis_fails = scenario in ("mint refused", "keychain fails")
            self.server.mint_status = None
            if scenario in ("mint refused", "keychain fails"):
                for name in os.listdir(self.token_dir):
                    os.remove(os.path.join(self.token_dir, name))  # nothing cached: each command must mint
            # minting and mint refused: the key is there; cached: the tripwire proves no
            # command reached the Keychain; keychain fails: `security` finds no item.
            self.keychain({"minting": "key", "cached": "tripwire", "mint refused": "key",
                           "keychain fails": "missing"}[scenario])
            if scenario == "mint refused":
                self.server.mint_status = 401
            calls_before = len(self.security_calls())
            for argv in self.argvs():
                with self.subTest(scenario=scenario, argv=argv):
                    code, out, err = self.main(argv)
                    self.assert_no_secret(out, err)
            if scenario == "minting":
                self.assertEqual(self.server.mints, 2, "both kinds were really minted")
            if scenario == "cached":
                self.assertEqual(len(self.security_calls()), calls_before, "the cached pass never ran security")
            if scenario in ("mint refused", "keychain fails"):
                self.assertGreater(len(self.security_calls()), calls_before, "the failing passes really tried")
        self.assertEqual(parser_commands() - commands, set(), "a subcommand is missing from the no-leak sweep")
        self.assertEqual(len(self.diagnosis_calls), 12, "both runtime protocols exercised without a real model")

    def test_redact_catches_every_shape(self):
        from coordlib.gh import redact
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjIzIn0.c2lnbmF0dXJlLWJ5dGVz"
        text = redact(f"a ghs_{'x' * 380} b {jwt} c -----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY----- "
                      f"d {'QUJD' * 40} e gho_abc ghp_def github_pat_ghi")
        self.assert_no_secret(text)
        for fragment in ("gho_abc", "ghp_def", "github_pat_ghi", "AAAA"):
            self.assertNotIn(fragment, text)
        self.assertEqual(redact("HTTP 404 GET https://api.github.com/repos/o/r/issues/1: Not Found"),
                         "HTTP 404 GET https://api.github.com/repos/o/r/issues/1: Not Found")


if __name__ == "__main__":
    unittest.main()

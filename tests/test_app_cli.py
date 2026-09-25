"""coord in app mode, end to end through cli.main (#1035): which identity each call uses,
no fallback to Alex's token, `coord token`, and no secret in any output.
Run: python3 -m unittest discover -s Tools/coord/tests

Every command runs in-process exactly as the `coord` script runs it, against the fake
`security`, the fake GitHub and the recording `gh` runner (apptestkit.py). One class also
runs the real script as a subprocess, with a pre-seeded token cache so nothing is minted.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import INSTALLATION, REPO, AppTestCase  # noqa: E402
from coordlib import EXIT_BACKEND, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, apptoken, budget, cache, cli  # noqa: E402

USER_TOKEN = "user-token-that-must-never-be-sent"
COORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coord")


def is_write(request):
    return request["method"] != "GET" and request["path"] != "graphql"


class AppCliTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        os.environ["COORD_TRANSPORT"] = "gh"  # Alex's identity goes through the (fake) gh runner
        self.no_repo = os.path.join(self.root, "not-a-clone")
        os.makedirs(self.no_repo)

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

    def test_attestations_stay_on_alex(self):
        self.keychain("key")
        for argv, label in ((["train", "approve", "101", "--note", "Alex said so"], "alex-approved"),
                            (["train", "substitute-review", "101", "--note", "reviewed by X"], "codex-unavailable")):
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

        argvs = [["board"], ["whoami"], ["claim", "5", "--branch", "b"], ["release", "5"], ["send", "x"],
                 ["inbox"], ["ci", "mk4"], ["train", "add", "7"], ["train", "remove", "7"], ["train", "status"],
                 ["train", "approve", "7", "--note", "n"], ["train", "substitute-review", "7", "--note", "n"],
                 ["train", "step"], ["token", "status"], ["token", "exec", "--", "true"]]
        for argv in argvs:
            args = cli.parser().parse_args(argv)
            args.func = capture
            gh = cli.GitHub(REPO)
            cli.attach_app(gh)
            self.assertIsNotNone(gh.app)
            cli.run(args, gh)
        self.assertEqual({name for name, on_app in seen.items() if not on_app}, set(cli.USER_IDENTITY))
        self.assertEqual(cli.USER_IDENTITY, {"train approve", "train substitute-review", "train step"})

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
        self.assertIn("githubIdentity: inisa-sidechain[bot] (installation not yet known)", out)
        self.assertIn("Alex for `train approve`, `train step`, `train substitute-review`", out)
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
                self.assertIn("githubIdentity: Alex's gh/keyring login", out)
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

    FAILURES = ("keychain", "mint 401", "mint 403", "broken config")

    def break_app(self, how):
        os.environ["GH_TOKEN"] = USER_TOKEN  # a fallback would find it right here
        os.environ["GITHUB_TOKEN"] = USER_TOKEN
        self.server.tokens[USER_TOKEN] = "user"
        if how == "keychain":
            self.keychain("missing")
        elif how.startswith("mint"):
            self.keychain("key")
            self.server.mint_status = int(how.split()[1])
        else:
            self.write_config("{broken")

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
                    self.assertIn("nothing was sent as Alex", err)
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


# MARK: - coord token


class TokenStatusTests(AppCliTestCase):
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


# MARK: - No secret in any output


class NoLeakTests(AppCliTestCase):
    """Every subcommand, with throwaway credentials minted for real by the fake GitHub, on
    success and on failure: no `ghs_`, no JWT-shaped string, no key material, and none of
    the tokens or JWTs the fake saw, in stdout or stderr."""

    def argvs(self):
        return [
            ["board"], ["board", "--brief"], ["board", "--json"], ["board", "--fresh"], ["whoami"], ["whoami", "--json"],
            ["claim", "55", "--branch", "feature/x"], ["claim", "56", "--branch", "feature/y", "--dry-run"],
            ["release", "55", "--everyone"], ["release", "57"], ["send", "hello"], ["send", "hi", "--on", "101"],
            ["inbox"], ["inbox", "--json", "--all"], ["ci", "mk4", "--ref", "feature/x"],
            ["train", "add", "101"], ["train", "remove", "101"], ["train", "status"], ["train", "status", "--json"],
            ["train", "approve", "101", "--note", "n"], ["train", "substitute-review", "101", "--note", "n"],
            ["train", "step", "--repo-dir", self.no_repo], ["token", "status"],
            ["token", "exec", "--", "true"], ["token", "exec", "--kind", "ops", "--", "true"],
        ]

    def test_no_subcommand_prints_a_secret(self):
        commands = {tuple(argv[:2]) if argv[0] in ("train", "token") else (argv[0],) for argv in self.argvs()}
        for scenario in ("minting", "cached", "mint refused", "keychain fails"):
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
        # Every subcommand the parser knows is in the sweep (15 today).
        parser = cli.parser()
        names = set()
        for action in parser._subparsers._group_actions:  # noqa: SLF001
            for name, sub in action.choices.items():
                nested = [a for a in sub._actions if a.__class__.__name__ == "_SubParsersAction"]  # noqa: SLF001
                if nested:
                    names |= {(name, child) for child in nested[0].choices}
                else:
                    names.add((name,))
        self.assertEqual(names - commands, set(), "a subcommand is missing from the no-leak sweep")

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

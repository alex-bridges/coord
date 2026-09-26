"""GitHub App tokens (#1035): the JWT, the key's path to openssl, minting, the token cache,
and when app mode is on. Run: python3 -m unittest discover -s Tools/coord/tests

A throwaway key, a fake `security` and a fake GitHub (apptestkit.py). Nothing here reads
the real Keychain item, writes the real token cache or reaches GitHub.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import (API, CLIENT_ID, INSTALLATION, OPENSSLS, PEM, PEM_B64, PUBLIC_PEM, REPO,  # noqa: E402
                        AppTestCase, file_mode, jwt_parts, read_json)
from coordlib import apptoken  # noqa: E402
from coordlib.gh import AppAuthError  # noqa: E402


class JWTTests(AppTestCase):
    def verify(self, verifier: str, jwt: str) -> "subprocess.CompletedProcess[bytes]":
        header, payload, signature = jwt.split(".")
        work = tempfile.mkdtemp(dir=self.root)
        with open(os.path.join(work, "public.pem"), "wb") as handle:
            handle.write(PUBLIC_PEM)
        with open(os.path.join(work, "signature.bin"), "wb") as handle:
            handle.write(apptoken.base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
        return subprocess.run([verifier, "dgst", "-sha256", "-verify", os.path.join(work, "public.pem"),
                               "-signature", os.path.join(work, "signature.bin")],
                              input=f"{header}.{payload}".encode(), capture_output=True)

    def test_the_jwt_verifies_with_every_openssl_on_this_machine(self):
        self.assertIn("/usr/bin/openssl", OPENSSLS)
        for signer in OPENSSLS:
            jwt = apptoken.make_jwt(CLIENT_ID, PEM, time.time(), openssl=signer)
            for verifier in OPENSSLS:
                with self.subTest(signer=signer, verifier=verifier):
                    result = self.verify(verifier, jwt)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(b"Verified OK", result.stdout)

    def test_a_tampered_jwt_does_not_verify(self):
        jwt = apptoken.make_jwt(CLIENT_ID, PEM, time.time())
        header, payload, signature = jwt.split(".")
        forged = apptoken.b64url(json.dumps({"iat": 1, "exp": 2, "iss": "someone"}).encode())
        self.assertNotEqual(self.verify("/usr/bin/openssl", f"{header}.{forged}.{signature}").returncode, 0)

    def test_the_claims(self):
        now = 1_790_000_000.7
        jwt = apptoken.make_jwt(CLIENT_ID, PEM, now)
        self.assertNotIn("=", jwt)
        header, payload = jwt_parts(jwt)
        self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(payload, {"iat": 1_790_000_000 - 60, "exp": 1_790_000_000 + 540, "iss": CLIENT_ID})
        self.assertLessEqual(payload["exp"] - now, 600, "GitHub refuses a JWT that lives more than 10 minutes")

    def test_the_key_reaches_openssl_through_an_inherited_pipe(self):
        seen = []
        original = apptoken.subprocess.Popen

        def spy(args, **kwargs):
            seen.append((list(args), kwargs))
            return original(args, **kwargs)

        self.patch(apptoken.subprocess, "Popen", spy)
        apptoken.make_jwt(CLIENT_ID, PEM, time.time())
        (args, kwargs), = seen
        self.assertEqual(args[:4], [apptoken.OPENSSL, "dgst", "-sha256", "-sign"])
        self.assertRegex(args[4], r"^/dev/fd/\d+$")
        self.assertEqual(kwargs["pass_fds"], (int(args[4].rsplit("/", 1)[1]),))
        self.assertNotIn(PEM.decode(), " ".join(args))
        self.assertIs(kwargs["stdin"], subprocess.PIPE, "the JWT goes on stdin, the key on the pipe")

    def test_an_openssl_failure_names_no_key_material(self):
        with self.assertRaises(AppAuthError) as caught:
            apptoken.make_jwt(CLIENT_ID, b"-----BEGIN RSA PRIVATE KEY-----\nbm90IGEga2V5\n-----END RSA PRIVATE KEY-----\n",
                              time.time())
        self.assertIn("could not sign the app JWT", str(caught.exception))
        self.assertNotIn("bm90IGEga2V5", str(caught.exception))
        with self.assertRaises(AppAuthError):
            apptoken.make_jwt(CLIENT_ID, PEM, time.time(), openssl=os.path.join(self.root, "no-openssl"))


class MintTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.keychain("key")

    def test_mint_discovers_the_installation_and_narrows_the_token(self):
        tokens = self.tokens()
        token = tokens.token("read")
        discover, mint = self.server.requests
        self.assertEqual((discover["method"], discover["path"]), ("GET", f"repos/{REPO}/installation"))
        self.assertEqual((mint["method"], mint["path"]), ("POST", f"app/installations/{INSTALLATION}/access_tokens"))
        self.assertEqual(mint["body"], {"repositories": ["r"], "permissions": apptoken.READ_PERMISSIONS})
        self.assertTrue(all(value == "read" for value in mint["body"]["permissions"].values()))
        header, payload = jwt_parts(mint["bearer"])
        self.assertEqual(payload["iss"], CLIENT_ID)
        self.assertEqual(self.server.tokens[token], "read")
        self.assertEqual(self.security_calls(),
                         [f"find-generic-password -s example-coord-github-app -a {CLIENT_ID} -w"])
        entry = read_json(os.path.join(self.token_dir, "read.json"))
        self.assertEqual({k: entry[k] for k in ("token", "installation_id", "permissions")},
                         {"token": token, "installation_id": INSTALLATION, "permissions": apptoken.READ_PERMISSIONS})
        self.assertEqual(file_mode(os.path.join(self.token_dir, "read.json")), 0o600)
        self.assertEqual(tokens.installation_id(), INSTALLATION)

    def test_the_ops_token_carries_only_the_operational_writes(self):
        # Issues AND pull_requests write: GitHub enforces the one matching the number
        # (measured 2026-09-25 on PR #1046 and issue #991); actions write for dispatches.
        self.tokens().token("ops")
        mint = self.server.requests[-1]
        self.assertEqual(mint["body"]["permissions"],
                         {"actions": "write", "issues": "write", "metadata": "read", "pull_requests": "write"})

    def test_one_jwt_and_one_keychain_read_serve_both_kinds_in_one_process(self):
        tokens = self.tokens()
        tokens.token("read")
        tokens.token("ops")
        self.assertEqual(len(self.security_calls()), 1)
        self.assertEqual(self.server.discoveries, 1, "the second mint reuses the installation")
        self.assertEqual(self.server.mints, 2)

    def test_a_cached_token_is_reused_without_security_or_a_call(self):
        first = self.tokens().token("read")
        self.keychain("tripwire")
        again = self.tokens().token("read")  # a new process: a new AppTokens
        self.assertEqual(first, again)
        self.assertEqual(self.server.mints, 1)
        self.assertEqual(len(self.security_calls()), 1, "the cached path never runs security")

    def test_a_token_with_five_minutes_or_less_left_is_minted_again(self):
        first = self.tokens().token("read")
        expires = apptoken.expiry_epoch(read_json(os.path.join(self.token_dir, "read.json")))
        self.assertEqual(self.tokens(clock=lambda: expires - 301).token("read"), first, "5 min 1 s left: reused")
        again = self.tokens(clock=lambda: expires - 300).token("read")
        self.assertNotEqual(again, first, "5 min left: minted again")
        self.assertEqual(self.server.mints, 2)
        self.assertEqual(self.server.discoveries, 1, "the installation id comes from the expired entry")

    def test_a_cached_installation_that_is_gone_is_found_again_once(self):
        self.seed_token("read", expires_in=-10)
        self.server.installation = 999
        self.tokens().token("read")
        self.assertEqual(self.server.discoveries, 1)
        self.assertEqual(self.tokens().installation_id(), 999)

    def test_a_cached_token_whose_permissions_differ_is_never_used(self):
        wrong = {"read": dict(apptoken.READ_PERMISSIONS, issues="write"), "ops": apptoken.READ_PERMISSIONS}
        for kind, permissions in wrong.items():
            with self.subTest(kind=kind):
                seeded = self.seed_token(kind, permissions=permissions)
                minted = self.tokens().token(kind)
                self.assertNotEqual(minted, seeded)
                self.assertEqual(self.server.tokens[minted], kind)
        # And the read cache never answers for ops, nor ops for read.
        read = self.tokens().token("read")
        self.assertNotEqual(self.tokens().token("ops"), read)

    def test_a_cache_file_others_can_read_is_not_used(self):
        seeded = self.seed_token("read", mode=0o644)
        self.assertNotEqual(self.tokens().token("read"), seeded)
        self.assertEqual(file_mode(os.path.join(self.token_dir, "read.json")), 0o600, "the mint rewrote it private")

    def test_a_cache_entry_for_another_app_config_is_not_used(self):
        seeded = self.seed_token("read")
        self.write_config({"client_id": "Iv23other", "repo": REPO})
        self.assertNotEqual(self.tokens().token("read"), seeded)

    def test_the_token_length_is_never_assumed(self):
        for length in (40, 93, 390, 1200):
            with self.subTest(length=length):
                self.server.token_length = length
                for name in os.listdir(self.token_dir) if os.path.isdir(self.token_dir) else []:
                    os.remove(os.path.join(self.token_dir, name))
                self.assertEqual(len(self.tokens().token("read")), length)

    def test_github_granting_other_permissions_is_refused(self):
        self.server.grant = dict(apptoken.READ_PERMISSIONS, issues="write")
        with self.assertRaises(AppAuthError) as caught:
            self.tokens().token("read")
        message = str(caught.exception)
        self.assertIn("issues:write", message)
        self.assertIn("issues:read", message)
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "read.json")), "never cached")

    def test_failures_name_the_cause_and_are_not_retried_in_one_process(self):
        cases = {
            "missing": "the Keychain did not return the app key",
            "garbage": "not the base64 of a PEM key",
        }
        for mode, words in cases.items():
            with self.subTest(mode=mode):
                self.keychain(mode)
                open(os.environ["FAKE_SECURITY_LOG"], "w").close()
                self.forget_failures()
                tokens = self.tokens()
                with self.assertRaises(AppAuthError) as caught:
                    tokens.token("read")
                self.assertIn(words, str(caught.exception))
                with self.assertRaises(AppAuthError):
                    tokens.token("ops")
                self.assertEqual(len(self.security_calls()), 1, "the failure is remembered, not retried")
        self.keychain("key")
        for status in (401, 403):
            with self.subTest(status=status):
                self.server.mint_status = status
                self.forget_failures()
                with self.assertRaises(AppAuthError) as caught:
                    self.tokens().token("read")
                self.assertIn(f"answered HTTP {status}", str(caught.exception))

    def test_no_security_binary_is_a_failure_not_a_fallback(self):
        self.patch(apptoken, "SECURITY", os.path.join(self.root, "no-such-security"))
        with self.assertRaises(AppAuthError) as caught:
            self.tokens().token("read")
        self.assertIn("login Keychain", str(caught.exception))
        self.assertEqual(self.server.requests, [])

    def test_the_pem_never_touches_disk(self):
        scratch = os.path.join(self.root, "tmpdir")
        os.makedirs(scratch)
        os.environ["TMPDIR"] = scratch
        self.patch(tempfile, "tempdir", scratch)
        self.tokens().token("read")
        self.tokens().token("ops")
        self.assertEqual(os.listdir(scratch), [], "no temporary file")
        self.assertEqual(sorted(os.listdir(self.token_dir)), ["ops.json", "ops.lock", "read.json", "read.lock"])
        needles = [b"PRIVATE KEY", PEM_B64[:60].encode()] + [line.encode() for line in PEM.decode().splitlines()[1:4]]
        for directory, _, names in os.walk(self.root):
            for name in names:
                if name == "security":
                    continue  # the fake binary itself (it holds no key: the key is in its environment)
                with open(os.path.join(directory, name), "rb") as handle:
                    content = handle.read()
                for needle in needles:
                    self.assertNotIn(needle, content, os.path.join(directory, name))


class LockTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.keychain("key")

    def test_concurrent_callers_mint_once(self):
        self.server.mint_delay = 0.3
        results, errors = [], []

        def worker():
            try:
                results.append(self.tokens().token("read"))  # each its own AppTokens, like its own process
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.server.mints, 1)
        self.assertEqual(len(self.security_calls()), 1)

    def test_a_lock_held_too_long_is_a_failure_not_a_wait_forever(self):
        os.makedirs(self.token_dir, exist_ok=True)
        fd = os.open(os.path.join(self.token_dir, "read.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.patch(apptoken, "LOCK_TIMEOUT_SECONDS", 0.3)
        with self.assertRaises(AppAuthError) as caught:
            self.tokens().token("read")
        self.assertIn("held the read token lock", str(caught.exception))
        self.assertEqual(self.server.mints, 0)
        self.assertEqual(self.security_calls(), [])

    def test_the_lock_is_per_kind(self):
        os.makedirs(self.token_dir, exist_ok=True)
        fd = os.open(os.path.join(self.token_dir, "read.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.patch(apptoken, "LOCK_TIMEOUT_SECONDS", 0.3)
        self.assertTrue(self.tokens().token("ops"))


class ModeTests(AppTestCase):
    def test_on_with_the_config(self):
        mode = apptoken.detect(REPO)
        self.assertTrue(mode.on)
        self.assertEqual(mode.config["client_id"], CLIENT_ID)
        self.assertIsInstance(apptoken.for_mode(mode), apptoken.AppTokens)

    def test_off_in_github_actions(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        mode = apptoken.detect(REPO)
        self.assertFalse(mode.on)
        self.assertIn("GitHub Actions", mode.reason)

    def test_off_with_coord_app_off(self):
        os.environ["COORD_APP"] = "off"
        mode = apptoken.detect(REPO)
        self.assertFalse(mode.on)
        self.assertIn("COORD_APP=off", mode.reason)
        for value in ("on", "0", "", "OFF"):
            with self.subTest(value=value):
                os.environ["COORD_APP"] = value
                self.assertTrue(apptoken.detect(REPO).on, "only the exact word `off` turns it off")

    def test_off_without_a_config(self):
        os.environ["COORD_APP_CONFIG"] = os.path.join(self.root, "nowhere", "github-app.json")
        mode = apptoken.detect(REPO)
        self.assertFalse(mode.on)
        self.assertIn("no app config", mode.reason)

    def test_off_for_another_repository(self):
        mode = apptoken.detect("someone/else")
        self.assertFalse(mode.on)
        self.assertIn(f"installed on {REPO} only", mode.reason)
        self.assertTrue(apptoken.detect(REPO.upper()).on, "GitHub names are case-insensitive")

    def test_a_broken_config_is_on_and_refuses(self):
        for content in ("not json", "[]", {"client_id": "", "repo": REPO}, {"client_id": CLIENT_ID, "repo": "norepo"}):
            with self.subTest(content=content):
                self.write_config(content)
                mode = apptoken.detect(REPO)
                self.assertTrue(mode.on)
                self.assertTrue(mode.error)
                source = apptoken.for_mode(mode)
                with self.assertRaises(AppAuthError):
                    source.token("read")
                self.assertIsNone(source.installation_id())
        self.assertEqual(self.security_calls(), [])

    def test_the_default_paths_and_binaries(self):
        self.assertEqual(apptoken.config_path({}), os.path.expanduser("~/.config/inisa-coord/github-app.json"))
        self.assertEqual(apptoken.token_cache_dir({}), os.path.expanduser("~/Library/Caches/inisa-coord"))
        self.assertEqual(apptoken.OPENSSL, "/usr/bin/openssl")
        with open(apptoken.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('\nSECURITY = "/usr/bin/security"\n', source, "absolute, never from PATH or the environment")
        self.assertNotIn("environ.get(\"COORD_OPENSSL", source)


class DescribeTests(AppTestCase):
    def test_describe_never_returns_the_token(self):
        token = self.seed_token("read")
        info = self.tokens().describe("read")
        self.assertEqual(info["state"], "reusable")
        self.assertNotIn(token, json.dumps(info))
        self.assertEqual(self.tokens().describe("ops"), {"state": "none cached"})
        self.seed_token("ops", permissions=apptoken.READ_PERMISSIONS)
        self.assertIn("permissions differ", self.tokens().describe("ops")["state"])


class APIHostTests(AppTestCase):
    """App credentials go to https://api.github.com and nowhere else (review of #1046)."""

    def test_any_other_api_url_turns_app_mode_off(self):
        for url in ("http://127.0.0.1:9", "https://proxy.example/api", "http://api.github.com",
                    "https://api.github.com.evil.example"):
            with self.subTest(url=url):
                os.environ["GITHUB_API_URL"] = url
                mode = apptoken.detect(REPO)
                self.assertFalse(mode.on)
                self.assertIn(f"GITHUB_API_URL is {url}", mode.reason)
                self.assertIn("app credentials go to GitHub only", mode.reason)
        for url in ("https://api.github.com", "https://api.github.com/"):
            with self.subTest(url=url):
                os.environ["GITHUB_API_URL"] = url
                self.assertTrue(apptoken.detect(REPO).on)

    def test_the_token_source_talks_to_github_only(self):
        self.assertEqual(self.tokens().api, "https://api.github.com")

    def test_an_app_token_is_never_sent_off_github(self):
        from coordlib.gh import GitHub

        class Source:
            asked = []

            def token(self, kind):
                self.asked.append(kind)
                return "ghs_must_not_leave"

            def installation_id(self):
                return INSTALLATION

        gh = GitHub(REPO, app=Source())
        for url in ("https://evil.example/repos/o/r/issues", "http://api.github.com/repos/o/r/issues",
                    "https://api.github.com.evil.example/x"):
            with self.subTest(url=url):
                with self.assertRaises(AppAuthError):
                    gh._http("GET", url)  # noqa: SLF001
        self.assertEqual(Source.asked, [], "no token was even fetched")
        self.assertEqual(self.server.requests, [])


class FailureMemoryTests(AppTestCase):
    """A failed mint is remembered across processes for FAILURE_HOLD_SECONDS, in a 0600
    file that holds the cause and the time and never a secret (review of #1046)."""

    def test_a_keychain_failure_holds_both_kinds_without_asking_again(self):
        self.keychain("missing")
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")
        self.assertEqual(len(self.security_calls()), 1)
        for kind in apptoken.KINDS:
            path = os.path.join(self.token_dir, f"{kind}.failed.json")
            self.assertEqual(file_mode(path), 0o600)
            record = read_json(path)
            self.assertEqual(set(record), {"at", "cause", "stage", "client_id", "repo"})
            self.assertIn("the Keychain did not return the app key", record["cause"])
            self.assert_no_secret(json.dumps(record))
        for kind in apptoken.KINDS:  # the next processes
            with self.subTest(kind=kind):
                with self.assertRaises(AppAuthError) as caught:
                    self.tokens().token(kind)
                self.assertIn("a mint failed", str(caught.exception))
                self.assertIn("the Keychain did not return the app key", str(caught.exception))
        self.assertEqual(len(self.security_calls()), 1, "no second Keychain prompt")
        self.assertEqual(self.server.requests, [])

    def test_the_hold_ends_after_five_minutes_and_a_success_clears_it(self):
        self.keychain("missing")
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")
        self.keychain("key")
        later = time.time() + apptoken.FAILURE_HOLD_SECONDS - 5
        with self.assertRaises(AppAuthError):
            self.tokens(clock=lambda: later).token("read")
        self.assertEqual(len(self.security_calls()), 1)
        after = time.time() + apptoken.FAILURE_HOLD_SECONDS + 1
        self.assertTrue(self.tokens(clock=lambda: after).token("read"))
        self.assertEqual(len(self.security_calls()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "read.failed.json")))
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "ops.failed.json")),
                         "a key failure is cleared for both kinds once the key works")

    def test_a_refused_mint_holds_only_its_kind(self):
        self.keychain("key")
        self.server.grant = dict(apptoken.READ_PERMISSIONS, issues="write")  # refused for read only
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")
        self.server.grant = None
        self.assertTrue(os.path.exists(os.path.join(self.token_dir, "read.failed.json")))
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "ops.failed.json")))
        self.assertTrue(self.tokens().token("ops"))
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")

    def test_a_hold_belongs_to_its_config(self):
        # Two app configs share one token cache directory (review of #1046, P3-4).
        self.keychain("missing")
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")
        other = apptoken.AppTokens({"client_id": "Iv23otherapp", "repo": "o/other", "app_login": "", "app_id": None},
                                   self.token_dir)
        self.assertIsNone(other.failure("read"), "another config's hold does not stop this one")
        self.keychain("key")
        self.assertTrue(other.token("read"), "and it mints")
        self.assertIsNotNone(self.tokens().failure("read"), "its success does not clear the first config's hold")
        with self.assertRaises(AppAuthError) as caught:
            self.tokens().token("read")
        self.assertIn("a mint failed", str(caught.exception))

    def test_a_record_without_its_config_is_ignored(self):
        os.makedirs(self.token_dir, exist_ok=True)
        path = os.path.join(self.token_dir, "read.failed.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"at": time.time(), "cause": "x", "stage": "key"}, handle)  # the shape before P3-4
        os.chmod(path, 0o600)
        self.assertIsNone(self.tokens().failure("read"))

    def test_a_refused_read_mint_does_not_stop_the_ops_token_in_the_same_process(self):
        self.keychain("key")
        self.server.refuse_kinds = {"read"}
        tokens = self.tokens()
        with self.assertRaises(AppAuthError):
            tokens.token("read")
        self.assertTrue(tokens.token("ops"), "a mint refused for one kind holds only that kind")
        with self.assertRaises(AppAuthError):
            tokens.token("read")
        self.assertEqual(len(self.security_calls()), 1)

    def test_a_lock_timeout_is_not_remembered(self):
        os.makedirs(self.token_dir, exist_ok=True)
        fd = os.open(os.path.join(self.token_dir, "read.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.patch(apptoken, "LOCK_TIMEOUT_SECONDS", 0.2)
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")
        self.assertFalse(os.path.exists(os.path.join(self.token_dir, "read.failed.json")))

    def test_a_marker_others_can_read_or_from_the_future_is_ignored(self):
        os.makedirs(self.token_dir, exist_ok=True)
        path = os.path.join(self.token_dir, "read.failed.json")
        for at, mode in ((time.time(), 0o644), (time.time() + 3600, 0o600), ("now", 0o600)):
            with self.subTest(at=at, mode=mode):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump({"at": at, "cause": "x", "stage": "key"}, handle)
                os.chmod(path, mode)
                self.assertIsNone(self.tokens().failure("read"))


class MintErrorTests(AppTestCase):
    """Anything the mint path can raise becomes an AppAuthError, never a traceback."""

    def setUp(self):
        super().setUp()
        self.keychain("key")

    def failing_urlopen(self, error=None, body=None):
        def urlopen(request, timeout=None):
            if error is not None:
                raise error

            class Response:
                headers = {}

                def read(self):
                    return body

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()
        return urlopen

    def test_every_transport_error_is_an_app_auth_error(self):
        import http.client
        cases = {
            "IncompleteRead": dict(error=http.client.IncompleteRead(b"partial", 100)),
            "BadStatusLine": dict(error=http.client.BadStatusLine("garbage")),
            "RemoteDisconnected": dict(error=http.client.RemoteDisconnected("closed")),
            "not UTF-8": dict(body=b"\xff\xfe\xfd"),
            "not JSON": dict(body=b"<html>"),
            "timeout": dict(error=TimeoutError("timed out")),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                self.forget_failures()
                self.patch(apptoken.gh, "app_open", self.failing_urlopen(**kwargs))
                with self.assertRaises(AppAuthError) as caught:
                    self.tokens().token("read")
                self.assert_no_secret(str(caught.exception))

    def test_an_unexpected_error_in_the_mint_is_contained(self):
        def broken(*args, **kwargs):
            raise KeyError("a bug")
        self.patch(apptoken.AppTokens, "_mint_with", broken)
        with self.assertRaises(AppAuthError) as caught:
            self.tokens().token("read")
        self.assertIn("minting failed (KeyError", str(caught.exception))


class RedirectTests(unittest.TestCase):
    """App credentials never follow a redirect to another origin (review of #1046, P3-1).

    Two REAL local HTTP servers, no patched urlopen: A answers 302 (or 307) to B, and B
    records every Authorization header it receives. urllib's default redirect handler
    copies add_header headers to any host; the app's requests must not reach B at all.
    """

    def setUp(self):
        import http.server
        self.servers = []
        for _ in range(2):
            server = http.server.HTTPServer(("127.0.0.1", 0), self.handler())
            server.seen = []
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.servers.append(server)
        self.a, self.b = self.servers
        # A fresh opener built with no proxy from the environment: 127.0.0.1 must be reached directly.
        saved = {name: os.environ.pop(name) for name in list(os.environ) if name.lower().endswith("_proxy")}
        self.addCleanup(os.environ.update, saved)
        from coordlib import gh
        self.gh = gh
        original = gh._APP_OPENER  # noqa: SLF001
        gh._APP_OPENER = None  # noqa: SLF001
        self.addCleanup(setattr, gh, "_APP_OPENER", original)

    def url(self, server, path):
        return f"http://127.0.0.1:{server.server_address[1]}{path}"

    def handler(self):
        test = self

        class Handler(__import__("http.server").server.BaseHTTPRequestHandler):
            def answer(self):
                self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if self.path.startswith("/away"):
                    code = 307 if "307" in self.path else 302
                    self.send_response(code)
                    self.send_header("Location", test.url(test.b, "/stolen"))
                elif self.path.startswith("/same"):
                    self.send_response(302)
                    self.send_header("Location", "/final")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        return Handler

    def test_the_harness_can_see_a_leak(self):
        # The same redirect through plain urlopen with add_header DOES hand B the token:
        # the leak the review demonstrated, so the checks below can fail.
        request = urllib.request.Request(self.url(self.a, "/away"))
        request.add_header("Authorization", "Bearer ghs_THROWAWAY_read")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(request, timeout=5).read()
        self.assertEqual(self.b.seen, [("GET", "/stolen", "Bearer ghs_THROWAWAY_read")])

    def test_an_app_token_never_reaches_another_origin(self):
        for path, method, data in (("/away", "GET", None), ("/away-307", "POST", b"{}")):
            with self.subTest(path=path):
                request = self.gh.app_request(method, self.url(self.a, path), "ghs_THROWAWAY_read", data)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.gh.app_open(request, timeout=5)
                caught.exception.close()
                self.assertIn("refused a redirect", str(caught.exception.reason))
        self.assertEqual(self.b.seen, [], "B was never reached, with or without a credential")
        self.assertEqual([seen[2] for seen in self.a.seen], ["Bearer ghs_THROWAWAY_read"] * 2)

    def test_the_jwt_never_reaches_another_origin(self):
        tokens = apptoken.AppTokens({"client_id": CLIENT_ID, "repo": REPO, "app_login": "", "app_id": None},
                                    tempfile.mkdtemp())
        tokens.api = self.url(self.a, "")  # the one test-only change: point the mint path at A
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjIzdGVzdCJ9.c2lnbmF0dXJlLWJ5dGVz"
        # The installation lookup is a GET: a 302 there is what urllib's own handler follows.
        for method, path, body, code in (("GET", "away/repos/o/r/installation", None, 302),
                                         ("POST", "away-307/app/installations/1/access_tokens", {"x": 1}, 307)):
            with self.subTest(method=method):
                with self.assertRaises(AppAuthError) as caught:
                    tokens._call(method, path, jwt, body)  # noqa: SLF001
                self.assertIn(f"answered HTTP {code}", str(caught.exception))
                self.assertNotIn(jwt, str(caught.exception))
        self.assertEqual(self.b.seen, [], "B was never reached")

    def test_a_redirect_within_the_origin_keeps_the_credential(self):
        request = self.gh.app_request("GET", self.url(self.a, "/same"), "ghs_THROWAWAY_read")
        self.gh.app_open(request, timeout=5).read()
        self.assertEqual(self.a.seen, [("GET", "/same", "Bearer ghs_THROWAWAY_read"),
                                       ("GET", "/final", "Bearer ghs_THROWAWAY_read")])

    def test_a_downgrade_to_http_on_the_same_host_is_refused(self):
        request = self.gh.app_request("GET", "https://api.github.com/repos/o/r", "ghs_THROWAWAY_read")
        for target in ("http://api.github.com/repos/o/r", "https://api.github.com:8443/x", "https://evil.example/x"):
            with self.subTest(target=target):
                with self.assertRaises(urllib.error.HTTPError):
                    self.gh.SameOriginRedirects().redirect_request(request, None, 302, "Found", {}, target)

    def test_the_credential_is_never_a_redirectable_header(self):
        request = self.gh.app_request("GET", "https://api.github.com/x", "ghs_THROWAWAY_read")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer ghs_THROWAWAY_read")


class SecurityOverrideTests(AppTestCase):
    """COORD_SECURITY_BIN reaches the `coord` script in a subprocess, which module patches
    cannot. It is refused for the default config by ANY spelling of its path, and for any
    config carrying the default config's client id (review of #1046, P3-3)."""

    REAL_ID = "Iv23realdefault"

    def setUp(self):
        super().setUp()
        self.patch(apptoken, "SECURITY", "/usr/bin/security")
        self.fake = os.path.join(self.root, "bin", "security")
        # This test's stand-in for ~/.config/inisa-coord/github-app.json, holding "the real"
        # client id (never the real file: the test must mean the same on every machine).
        home = os.path.join(self.root, "Home")
        self.default = os.path.join(home, ".config", "inisa-coord", "github-app.json")
        os.makedirs(os.path.dirname(self.default))
        with open(self.default, "w", encoding="utf-8") as handle:
            json.dump({"client_id": self.REAL_ID, "repo": REPO}, handle)
        self.patch(apptoken, "default_config_path", lambda: self.default)

    def binary(self, configured, client_id=CLIENT_ID):
        env = {"COORD_SECURITY_BIN": self.fake}
        if configured is not None:
            env["COORD_APP_CONFIG"] = configured
        return apptoken.security_binary(env, client_id)

    def test_a_test_config_with_a_test_client_id_may_use_the_override(self):
        self.assertEqual(self.binary(self.config_path), self.fake)

    def test_no_config_named_means_the_default(self):
        self.assertEqual(self.binary(None), "/usr/bin/security")

    def test_every_spelling_of_the_default_path_is_refused(self):
        folder = os.path.dirname(self.default)
        link = os.path.join(self.root, "link.json")
        os.symlink(self.default, link)
        linked_dir = os.path.join(self.root, "linked-dir")
        os.symlink(folder, linked_dir)
        spellings = {
            "itself": self.default,
            "a symlink to the file": link,
            "through a symlinked folder": os.path.join(linked_dir, "github-app.json"),
            "with ..": os.path.join(folder, "..", "inisa-coord", "github-app.json"),
            "with ./": os.path.join(folder, ".", "github-app.json"),
            "in other letter case": self.default.replace("/Home/", "/HOME/").replace("github-app", "GitHub-App"),
        }
        for name, path in spellings.items():
            with self.subTest(spelling=name):
                self.assertEqual(self.binary(path), "/usr/bin/security")

    def test_the_default_client_id_is_refused_from_any_config(self):
        copy = os.path.join(self.root, "copy.json")
        with open(copy, "w", encoding="utf-8") as handle:
            json.dump({"client_id": self.REAL_ID, "repo": REPO}, handle)
        self.assertEqual(self.binary(copy, client_id=self.REAL_ID), "/usr/bin/security")
        self.assertEqual(self.binary(self.config_path, client_id=None), "/usr/bin/security",
                         "no client id named: no override")

    def test_the_mint_passes_its_client_id(self):
        seen = []
        self.patch(apptoken, "security_binary", lambda env=None, client_id=None: seen.append(client_id) or self.fake)
        with self.assertRaises(AppAuthError):
            self.tokens().token("read")  # the fake trips (tripwire mode); the argument is what matters
        self.assertEqual(seen, [CLIENT_ID])


if __name__ == "__main__":
    unittest.main()

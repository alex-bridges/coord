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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import (API, CLIENT_ID, INSTALLATION, OPENSSLS, PEM, PEM_B64, PUBLIC_PEM, REPO,  # noqa: E402
                        AppTestCase, file_mode, jwt_parts)
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
        entry = json.load(open(os.path.join(self.token_dir, "read.json"), encoding="utf-8"))
        self.assertEqual({k: entry[k] for k in ("token", "installation_id", "permissions")},
                         {"token": token, "installation_id": INSTALLATION, "permissions": apptoken.READ_PERMISSIONS})
        self.assertEqual(file_mode(os.path.join(self.token_dir, "read.json")), 0o600)
        self.assertEqual(tokens.installation_id(), INSTALLATION)

    def test_the_ops_token_carries_only_the_operational_writes(self):
        self.tokens().token("ops")
        mint = self.server.requests[-1]
        self.assertEqual(mint["body"]["permissions"], {"actions": "write", "issues": "write", "metadata": "read"})

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
        expires = apptoken.expiry_epoch(json.load(open(os.path.join(self.token_dir, "read.json"))))
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
        self.assertIsInstance(apptoken.for_mode(mode, API), apptoken.AppTokens)

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
                source = apptoken.for_mode(mode, API)
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


if __name__ == "__main__":
    unittest.main()

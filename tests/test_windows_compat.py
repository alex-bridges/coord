"""Cross-platform and native Windows compatibility tests.

Exercises:
- Pure-Python RS256 signer (PKCS#1 and PKCS#8 keys, cross-checked against OpenSSL when available)
- Cross-platform file locking (`compat.try_lock` / `compat.unlock`)
- Isolated process-tree timeout termination (POSIX killpg / Windows Job Object)
- Windows path normalization in `snapshot.normalize_path`
- `doctor.inspect` platform support and `reviewer.isolated_env` / `run_settings` on Windows
- On Windows: real Win32 DPAPI protect/unprotect, `apptoken.store_private_key`,
  end-to-end `AppTokens` minting & caching, and `conductor/install.ps1` PowerShell syntax
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coordlib import apptoken, compat, conductor, doctor, identity, outage, reviewer, snapshot  # noqa: E402
from coordlib.gh import GitHub  # noqa: E402


def _openssl_binary():
    for candidate in ("/usr/bin/openssl", "/opt/homebrew/bin/openssl", shutil.which("openssl")):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


class PurePythonRS256Tests(unittest.TestCase):
    def test_pkcs1_and_pkcs8_signatures_verify_with_openssl(self):
        openssl = _openssl_binary()
        if not openssl:
            self.skipTest("openssl binary not available to generate/verify throwaway key")
        pkcs1 = subprocess.run([openssl, "genrsa", "2048"], capture_output=True, check=True).stdout
        pkcs8 = subprocess.run([openssl, "pkcs8", "-topk8", "-nocrypt"], input=pkcs1,
                               capture_output=True, check=True).stdout
        public = subprocess.run([openssl, "rsa", "-pubout"], input=pkcs1,
                                capture_output=True, check=True).stdout
        message = b"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJJdjIzdGVzdCJ9"
        sig1 = compat.rs256_sign_pem(message, pkcs1)
        sig8 = compat.rs256_sign_pem(message, pkcs8)
        self.assertEqual(sig1, sig8, "PKCS#1 and PKCS#8 of the same key produce identical deterministic RS256 signatures")
        with tempfile.TemporaryDirectory() as tmp:
            pub_path = os.path.join(tmp, "pub.pem")
            sig_path = os.path.join(tmp, "sig.bin")
            with open(pub_path, "wb") as handle:
                handle.write(public)
            with open(sig_path, "wb") as handle:
                handle.write(sig1)
            verify = subprocess.run([openssl, "dgst", "-sha256", "-verify", pub_path, "-signature", sig_path],
                                    input=message, capture_output=True)
            self.assertEqual(verify.returncode, 0, verify.stderr.decode(errors="replace"))

    def test_rejects_corrupt_or_encrypted_pem(self):
        for bad in (b"not a pem", b"-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----\n"):
            with self.assertRaises(ValueError):
                compat.rs256_sign_pem(b"hello", bad)


class CrossPlatformPrimitivesTests(unittest.TestCase):
    def test_windows_assignment_failure_never_releases_the_command(self):
        kernel = mock.Mock()
        kernel.AssignProcessToJobObject.return_value = False
        launcher = mock.Mock(_handle=2)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(compat, "_win_create_kill_job", return_value=1), \
                mock.patch.object(compat, "_kernel32", kernel, create=True), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
                mock.patch.object(subprocess, "Popen", return_value=launcher) as popen:
            with self.assertRaisesRegex(OSError, "assign"):
                compat.run_isolated_process(["target"], tmp, {}, timeout=5)
            self.assertEqual(popen.call_args.args[0][:4], [sys.executable, "-I", "-S", "-c"])
            self.assertNotIn("target", popen.call_args.args[0])
            launcher.communicate.assert_not_called()
            launcher.kill.assert_called_once()
            kernel.CloseHandle.assert_called_once_with(1)

    def test_windows_teardown_waits_when_kill_and_pipe_close_race_with_job_exit(self):
        kernel = mock.Mock()
        launcher = mock.Mock(_handle=2, returncode=7)
        launcher.kill.side_effect = PermissionError("launcher already exiting")
        launcher.stdin.close.side_effect = OSError("pipe already disconnected")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(compat, "_win_create_kill_job", return_value=1), \
                mock.patch.object(compat, "_kernel32", kernel, create=True), \
                mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
                mock.patch.object(subprocess, "Popen", return_value=launcher):
            for timed_out, expected in ((False, 7), (True, 124)):
                with self.subTest(timed_out=timed_out):
                    launcher.reset_mock()
                    launcher.communicate.side_effect = subprocess.TimeoutExpired("target", 1) if timed_out else None
                    completed = compat.run_isolated_process(["target"], tmp, {}, timeout=1)
                    self.assertEqual(completed.returncode, expected)
                    launcher.stdin.close.assert_called_once()
                    launcher.wait.assert_called_once_with(timeout=5)
            launcher.wait.side_effect = subprocess.TimeoutExpired("launcher", 5)
            with self.assertRaisesRegex(OSError, "launcher did not terminate"):
                compat.run_isolated_process(["target"], tmp, {}, timeout=1)

    def test_file_lock_blocks_second_holder_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "test.lock")
            fd1 = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                compat.try_lock(fd1)
                # Verify from a child process (portable across POSIX flock and Windows msvcrt)
                probe = (
                    "import os, sys; sys.path.insert(0, sys.argv[1]); from coordlib import compat; "
                    "fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600)\n"
                    "try:\n  compat.try_lock(fd)\n  sys.exit(0)\n"
                    "except OSError:\n  sys.exit(42)\n"
                    "finally:\n  os.close(fd)\n"
                )
                repo_root = str(Path(__file__).resolve().parents[1])
                contended = subprocess.run([sys.executable, "-c", probe, repo_root, lock_path], capture_output=True)
                self.assertEqual(contended.returncode, 42, contended.stderr.decode(errors="replace"))
            finally:
                compat.unlock(fd1)
                os.close(fd1)
            acquired = subprocess.run([sys.executable, "-c", probe, repo_root, lock_path], capture_output=True)
            self.assertEqual(acquired.returncode, 0, acquired.stderr.decode(errors="replace"))

    def test_run_isolated_process_kills_grandchild_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "grandchild-survived.txt")
            grandchild = f"import time; time.sleep(2.5); open({marker!r}, 'w').write('alive')"
            parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
            done = compat.run_isolated_process([sys.executable, "-c", parent], tmp, dict(os.environ), timeout=1)
            self.assertEqual(done.returncode, 124)
            time.sleep(3.0)
            self.assertFalse(os.path.exists(marker), "grandchild process should have been killed with the process tree")

    def test_normalize_path_handles_windows_separators_and_rejects_drives(self):
        self.assertEqual(snapshot.normalize_path(r"src\foo\bar.py"), "src/foo/bar.py")
        self.assertEqual(snapshot.normalize_path(r".\src\a\..\b.py"), "src/b.py")
        self.assertEqual(snapshot.normalize_path(r"C:\Users\dev\secret.txt"), "")
        self.assertEqual(snapshot.normalize_path("d:/outside/file.py"), "")

    def test_doctor_reports_supported_on_current_platform(self):
        gh = GitHub("owner/repo", token="ghp_testtoken12345678901234567890")
        with mock.patch.object(doctor.identity, "agent_id", return_value="test@host:repo"), \
                mock.patch("subprocess.run", side_effect=AssertionError("offline doctor must not run subprocesses")):
            report = doctor.inspect(gh)
        self.assertTrue(report["environment"]["supported"])
        self.assertIn("supported", report["capabilities"]["nativeWindows"])

    def test_windows_reviewer_env_and_settings_shape(self):
        with mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(reviewer.runtime, "setting", return_value=".claude/hooks/conductor-guard.py"):
            env = reviewer.isolated_env(
                {"SystemRoot": r"C:\Windows", "PATH": r"C:\Windows\System32", "GH_TOKEN": "secret", "OPENAI_API_KEY": "k"},
                r"C:\Temp\empty",
            )
            self.assertIn("SystemRoot", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertIn("COORD_WINDOWS_KEY_DIR", env)
            settings = reviewer.run_settings(tempfile.gettempdir(), [], tempfile.gettempdir())
            cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn(sys.executable, cmd)

    def test_identity_detects_codex_worktree_with_windows_backslashes(self):
        self.assertEqual(identity.detect_harness({}, r"C:\Users\dev\.codex\worktrees\abc\repo"), "codex")
        self.assertEqual(identity.detect_harness({}, r"C:\Users\dev\Projects\repo"), "human")

    def test_outage_us_zone_fallback_matches_dst_and_standard_time(self):
        eastern = outage._FALLBACK_ZONES["America/New_York"]
        with mock.patch.object(outage, "_zone", return_value=eastern):
            summer = outage.next_weekly_reset(dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc),
                                              "Thu 12:00 America/New_York")
            winter = outage.next_weekly_reset(dt.datetime(2026, 1, 12, 12, 0, tzinfo=dt.timezone.utc),
                                              "Thu 12:00 America/New_York")
        self.assertEqual(summer, dt.datetime(2026, 10, 1, 16, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(winter, dt.datetime(2026, 1, 15, 17, 0, tzinfo=dt.timezone.utc))
        # Also verify transition instants (spring forward + fall back with fold)
        before_spring = dt.datetime(2026, 3, 8, 6, 30, tzinfo=dt.timezone.utc).astimezone(eastern)
        after_spring = dt.datetime(2026, 3, 8, 7, 30, tzinfo=dt.timezone.utc).astimezone(eastern)
        self.assertEqual((before_spring.hour, before_spring.utcoffset()), (1, dt.timedelta(hours=-5)))
        self.assertEqual((after_spring.hour, after_spring.utcoffset()), (3, dt.timedelta(hours=-4)))
        before_fall = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc).astimezone(eastern)
        after_fall = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc).astimezone(eastern)
        self.assertEqual((before_fall.hour, before_fall.fold, before_fall.utcoffset()), (1, 0, dt.timedelta(hours=-4)))
        self.assertEqual((after_fall.hour, after_fall.fold, after_fall.utcoffset()), (1, 1, dt.timedelta(hours=-5)))


@unittest.skipUnless(os.name == "nt", "Windows-native DPAPI and PowerShell installer tests")
class NativeWindowsTests(unittest.TestCase):
    def test_isolated_command_preserves_output_arguments_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "-c", "import sys; print(sys.argv[1]); print('error', file=sys.stderr); sys.exit(3)",
                       'argument with "quotes" and spaces']
            result = compat.run_isolated_process(command, tmp, dict(os.environ), timeout=10)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout.strip(), command[-1])
            self.assertEqual(result.stderr.strip(), "error")
            missing = compat.run_isolated_process([os.path.join(tmp, "missing.exe")], tmp, dict(os.environ), timeout=10)
            self.assertEqual(missing.returncode, 127)
            self.assertTrue(missing.stderr)

    def test_fast_exiting_parent_leaves_no_live_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "survived")
            worker = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).touch()"
            parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {worker!r}])"
            result = compat.run_isolated_process([sys.executable, "-c", parent], tmp, dict(os.environ), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            time.sleep(2)
            self.assertFalse(marker.exists())

    def test_dpapi_round_trip(self):
        secret = b"-----BEGIN RSA PRIVATE KEY-----\ntest-payload\n-----END RSA PRIVATE KEY-----\n"
        blob = compat.dpapi_protect(secret)
        self.assertNotIn(b"test-payload", blob)
        self.assertEqual(compat.dpapi_unprotect(blob), secret)

    def test_windows_store_and_mint_with_dpapi_and_pure_python_rs256(self):
        openssl = _openssl_binary()
        if not openssl:
            self.skipTest("openssl needed to generate throwaway RSA key")
        pem = subprocess.run([openssl, "genrsa", "2048"], capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory() as tmp:
            key_dir = os.path.join(tmp, "keys")
            tok_dir = os.path.join(tmp, "tokens")
            cfg_path = os.path.join(tmp, "github-app.json")
            with open(cfg_path, "w", encoding="utf-8") as handle:
                json.dump({"client_id": "Iv23win0test", "repo": "o/r", "app_login": "win-bot[bot]"}, handle)
            with mock.patch.dict(os.environ, {
                "COORD_WINDOWS_KEY_DIR": key_dir,
                "COORD_TOKEN_CACHE_DIR": tok_dir,
                "COORD_APP_CONFIG": cfg_path,
            }):
                os.environ.pop("COORD_SECURITY_BIN", None)
                dest = apptoken.store_private_key(pem, "Iv23win0test")
                self.assertIn("Windows DPAPI", dest)
                tokens = apptoken.AppTokens(apptoken.load_config(cfg_path), cache_dir=tok_dir)
                recovered = tokens._private_key()
                self.assertEqual(recovered, pem)
                jwt = apptoken.make_jwt("Iv23win0test", recovered, time.time())
                parts = jwt.split(".")
                self.assertEqual(len(parts), 3)
                header = json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
                self.assertEqual(header["alg"], "RS256")

    def test_install_ps1_parses_cleanly(self):
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if not ps:
            self.skipTest("PowerShell not available")
        script = str(Path(__file__).resolve().parents[1] / "conductor" / "install.ps1")
        check = (
            f"$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        result = subprocess.run([ps, "-NoProfile", "-Command", check], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

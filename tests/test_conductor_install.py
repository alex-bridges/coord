"""Exercise installer preflight and plist refresh with fake launchd and tools only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "conductor" / "install.sh"


class ConductorInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Both XML and shell metacharacters must survive as literal paths.
        self.home = self.root / "home & <operator> ' $(false)"
        self.checkout = self.root / "checkout & <repo> ' $(false)"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.checkout.mkdir()
        (self.checkout / ".git").write_text("fake checkout")
        self.plist = self.home / "Library/LaunchAgents/com.coord.conductor.owner-repo.plist"
        self.events = self.root / "events.jsonl"
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin),
                        FAKE_EVENTS=str(self.events), FAKE_CHECKOUT=str(self.checkout))
        # A closed PATH proves --no-wake does not accidentally find the real CLI.
        (self.bin / "python3").symlink_to(sys.executable)
        for name in ("dirname", "id", "mktemp", "rm", "mkdir", "mv", "sed"):
            (self.bin / name).symlink_to(shutil.which(name))
        fake_tool = self.root / "fake_tool.py"
        fake_tool.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json, os, pathlib, sys
            name = pathlib.Path(sys.argv[0]).name
            args = sys.argv[1:]
            with open(os.environ['FAKE_EVENTS'], 'a') as handle:
                handle.write(json.dumps({'tool': name, 'args': args}) + '\\n')
            if name == 'git':
                if '--show-toplevel' in args:
                    print(os.environ['FAKE_CHECKOUT'])
                elif 'rev-parse' in args:
                    print('abc1234')
                elif 'symbolic-ref' in args:
                    sys.exit(1)
            elif name == 'gh':
                sys.exit(int(os.environ.get('FAKE_AUTH_EXIT', '0')))
            elif name == 'scutil':
                print('FakeHost')
            elif name == 'claude':
                sys.exit('the installer must never invoke a model')
            """))
        fake_tool.chmod(0o755)
        for name in ("git", "gh", "launchctl", "scutil"):
            (self.bin / name).symlink_to(fake_tool)
        self.fake_tool = fake_tool
        # The installer runs the coord that sits beside its own conductor/ directory.
        self.coord_root = self.root / "coord-src"
        (self.coord_root / "conductor").mkdir(parents=True)
        self.installer = self.coord_root / "conductor" / "install.sh"
        shutil.copy(INSTALLER, self.installer)
        coord = self.coord_root / "coord"
        coord.write_text(textwrap.dedent("""\
            import json, os, sys
            event = {'tool': 'tick', 'args': sys.argv[1:], 'cwd': os.getcwd(),
                     'env': {key: value for key, value in os.environ.items()
                             if key.startswith('CONDUCTOR_') or key in ('HOME', 'PATH', 'COORD_AGENT')}}
            with open(os.environ['FAKE_EVENTS'], 'a') as handle:
                handle.write(json.dumps(event) + '\\n')
            print(os.environ.get('FAKE_TICK_OUTPUT', 'dry-run ok'))
            sys.exit(int(os.environ.get('FAKE_TICK_EXIT', '0')))
            """))

    def run_install(self, *args):
        return subprocess.run(["/bin/bash", str(self.installer), "--repo", "owner/repo", "--home", str(self.checkout), *args],
                              cwd=self.root, env=self.env, text=True, capture_output=True, timeout=10)

    def read_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def read_plist(self):
        return plistlib.loads(self.plist.read_bytes())

    def existing(self, no_wake=True, **environment):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        arguments = ["/old/python", "/old/coord", "conductor", "tick"]
        if no_wake:
            arguments.append("--no-wake")
        self.plist.write_bytes(plistlib.dumps({
            "ProgramArguments": arguments,
            "EnvironmentVariables": {
                "HOME": "/old/home", "PATH": "/old/bin", "COORD_AGENT": "old-agent",
                "CONDUCTOR_HOME": "/old/checkout", "CONDUCTOR_TRAIN": "0",
                "CONDUCTOR_REVIEWER": "0", "CONDUCTOR_WAKE": "custom & <wake> ' $(false)",
                "CONDUCTOR_WAKE_LIMIT": "2", "OTHER_OPERATOR_SETTING": "keep me",
                **environment,
            },
        }))

    def assert_installed(self, result, no_wake):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate = self.read_plist()
        self.assertEqual("--no-wake" in candidate["ProgramArguments"], no_wake)
        self.assertEqual(candidate["WorkingDirectory"], str(self.checkout))
        self.assertEqual(candidate["ProgramArguments"][:4],
                         [str(self.bin / "python3"), str(self.coord_root / "coord"), "conductor", "tick"])
        environment = candidate["EnvironmentVariables"]
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertEqual(environment["CONDUCTOR_HOME"], str(self.checkout))
        self.assertEqual(environment["COORD_REPO"], "owner/repo")
        self.assertEqual(candidate["Label"], "com.coord.conductor.owner-repo")
        self.assertTrue(candidate["StandardOutPath"].endswith("coord-conductor-owner-repo.log"))
        self.assertEqual(environment["COORD_AGENT"], "conductor@FakeHost:conductor")
        self.assertNotEqual(environment["PATH"], "/old/bin")
        events = self.read_events()
        ticks = [event for event in events if event["tool"] == "tick"]
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0]["args"], candidate["ProgramArguments"][2:] + ["--dry-run"])
        for key, value in environment.items():
            if key.startswith("CONDUCTOR_") or key in ("HOME", "PATH", "COORD_AGENT"):
                self.assertEqual(ticks[0]["env"][key], value)
        self.assertLess(events.index(ticks[0]), next(i for i, event in enumerate(events) if event["tool"] == "launchctl"))
        self.assertEqual([event["args"][0] for event in events if event["tool"] == "launchctl"],
                         ["bootout", "bootstrap"])
        self.assertFalse(any(event["tool"] == "claude" for event in events))
        return candidate

    def test_no_wake_installs_without_claude_and_escapes_paths(self):
        candidate = self.assert_installed(self.run_install("--no-wake", "--interval", "45"), True)
        self.assertEqual(candidate["StartInterval"], 45)

    def test_installed_path_runs_checkout_update_with_git_from_a_separate_directory(self):
        git_bin = self.root / "custom git bin"
        git_bin.mkdir()
        (self.bin / "git").unlink()
        (git_bin / "git").symlink_to(self.fake_tool)
        self.env["PATH"] = os.pathsep.join((str(git_bin), str(self.bin)))

        candidate = self.assert_installed(self.run_install("--no-wake"), True)
        runtime_env = dict(self.env, **candidate["EnvironmentVariables"])
        self.assertEqual(shutil.which("git", path=runtime_env["PATH"]), str(git_bin / "git"))
        event_count = len(self.read_events())
        # The installer's dry-run skips self-update. Exercise the real update
        # path separately with the persisted environment and only our fake Git.
        update = textwrap.dedent("""\
            import sys
            sys.path.insert(0, sys.argv[1])
            from coordlib.conductor import Config, Watcher
            config = Config(home=sys.argv[2], state_dir=sys.argv[3], claude='unused',
                            agent='conductor@FakeHost:conductor', host='FakeHost')
            Watcher(config, gh=None).update_checkout()
            """)
        result = subprocess.run(
            [candidate["ProgramArguments"][0], "-c", update, str(INSTALLER.parents[1]),
             str(self.checkout), str(self.root / "state")],
            cwd=candidate["WorkingDirectory"], env=runtime_env,
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["args"] for event in self.read_events()[event_count:]], [
            ["-C", str(self.checkout), "status", "--porcelain"],
            ["-C", str(self.checkout), "symbolic-ref", "-q", "HEAD"],
            ["-C", str(self.checkout), "fetch", "-q", "origin", "main"],
            ["-C", str(self.checkout), "checkout", "-q", "--detach", "FETCH_HEAD"],
        ])

    def test_refresh_preserves_no_wake_and_operator_environment(self):
        self.existing(CONDUCTOR_CLAUDE="/missing/claude-is-unneeded-with-no-wake")
        before = self.read_plist()["EnvironmentVariables"]
        candidate = self.assert_installed(self.run_install(), True)
        managed = {"HOME", "PATH", "COORD_AGENT", "CONDUCTOR_HOME", "COORD_REPO"}
        for key, value in before.items():
            if key not in managed:
                self.assertEqual(candidate["EnvironmentVariables"][key], value)

    def test_explicit_wake_changes_existing_no_wake_without_invoking_claude(self):
        self.existing()
        (self.bin / "claude").symlink_to(self.fake_tool)
        self.assert_installed(self.run_install("--wake"), False)

    def test_wake_refresh_accepts_configured_executable_without_ambient_claude(self):
        custom_bin = self.root / "custom model & <bin> ' $(false)"
        custom_bin.mkdir()
        custom_claude = custom_bin / "claude"
        custom_claude.symlink_to(self.fake_tool)
        self.existing(no_wake=False, CONDUCTOR_CLAUDE=str(custom_claude))
        self.assertIsNone(shutil.which("claude", path=self.env["PATH"]))

        candidate = self.assert_installed(self.run_install(), False)
        self.assertEqual(candidate["EnvironmentVariables"]["CONDUCTOR_CLAUDE"],
                         str(custom_claude))
        self.assertEqual(shutil.which("claude", path=candidate["EnvironmentVariables"]["PATH"]),
                         str(custom_claude))

    def test_wake_refresh_rejects_invalid_configured_executable_despite_ambient_claude(self):
        (self.bin / "claude").symlink_to(self.fake_tool)
        for executable in ("missing", "not-executable"):
            with self.subTest(executable=executable):
                configured = self.root / executable
                if executable == "not-executable":
                    configured.write_text("not an executable")
                self.existing(no_wake=False, CONDUCTOR_CLAUDE=str(configured))
                before = self.plist.read_bytes()

                result = self.run_install()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("CONDUCTOR_CLAUDE", result.stderr)
                self.assertEqual(self.plist.read_bytes(), before)
                self.assertFalse(any(event["tool"] in ("tick", "launchctl", "claude")
                                     for event in self.read_events()))

    def test_explicit_no_wake_changes_existing_wake_without_claude(self):
        self.existing(no_wake=False)
        self.assert_installed(self.run_install("--no-wake"), True)

    def test_new_install_and_existing_wake_require_claude(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.existing(no_wake=False)
                before = self.plist.read_bytes() if existing else None
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("claude is not on PATH", result.stderr)
                self.assertEqual(self.plist.read_bytes() if self.plist.exists() else None, before)
        self.assertFalse(any(event["tool"] in ("tick", "launchctl") for event in self.read_events()))

    def test_failed_preflight_does_not_stop_or_replace_existing_job(self):
        self.existing()
        before = self.plist.read_bytes()
        cases = ({"FAKE_AUTH_EXIT": "1"}, {"FAKE_TICK_EXIT": "3"},
                 {"FAKE_TICK_OUTPUT": "another tick is running"})
        for settings in cases:
            with self.subTest(settings=settings):
                self.env.update(settings)
                try:
                    result = self.run_install()
                finally:
                    for key in settings:
                        self.env.pop(key)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.plist.read_bytes(), before)
                self.assertFalse(any(event["tool"] == "launchctl" for event in self.read_events()))
                self.assertEqual(list(self.plist.parent.glob("*.plist.*")), [])

    def test_repo_is_required_and_validated(self):
        for args in ((), ("--repo", "not-a-repo")):
            with self.subTest(args=args):
                result = subprocess.run(["/bin/bash", str(self.installer), "--home", str(self.checkout), *args],
                                        cwd=self.root, env=self.env, text=True, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--repo owner/name is required", result.stderr)
        self.assertFalse(any(event["tool"] in ("tick", "launchctl") for event in self.read_events()))

    def test_malformed_plist_and_invalid_interval_leave_job_untouched(self):
        self.existing()
        self.plist.write_bytes(b"broken plist")
        for args in ((), ("--no-wake",), ("--interval", "0"), ("--interval", "abc")):
            with self.subTest(args=args):
                result = self.run_install(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.plist.read_bytes(), b"broken plist")
                self.assertFalse(any(event["tool"] == "launchctl" for event in self.read_events()))


_EXAMPLE_CONFIG_PATH = None


def setUpModule():
    global _EXAMPLE_CONFIG_PATH
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conductor_config_fixture
    _EXAMPLE_CONFIG_PATH = conductor_config_fixture.use_example_config()


def tearDownModule():
    import conductor_config_fixture
    conductor_config_fixture.restore(_EXAMPLE_CONFIG_PATH)


if __name__ == "__main__":
    unittest.main()

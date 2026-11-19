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


@unittest.skipIf(os.name == "nt", "tests macOS launchd install.sh")
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
        self.plist = self.home / "Library/LaunchAgents/com.coord.conductor.owner+repo.plist"
        self.events = self.root / "events.jsonl"
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin),
                        FAKE_EVENTS=str(self.events), FAKE_CHECKOUT=str(self.checkout))
        # A closed PATH proves --no-wake does not accidentally find the real CLI.
        (self.bin / "python3").symlink_to(sys.executable)
        for name in ("dirname", "id", "mktemp", "rm", "mkdir", "mv", "sed", "tr"):
            (self.bin / name).symlink_to(shutil.which(name))
        fake_tool = self.root / "fake_tool.py"
        fake_tool.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json, os, pathlib, sys
            name = pathlib.Path(sys.argv[0]).name
            args = sys.argv[1:]
            with open(os.environ['FAKE_EVENTS'], 'a') as handle:
                handle.write(json.dumps({'tool': name, 'args': args}) + '\\n')
            if name == 'git':
                if 'get-url' in args:
                    print(os.environ.get('FAKE_PUSH_URL' if '--push' in args else 'FAKE_FETCH_URL', ''))
                elif '--show-toplevel' in args:
                    print(os.environ['FAKE_CHECKOUT'])
                elif 'check-ref-format' in args:
                    print(args[-1])
                elif 'ls-remote' in args:
                    print('ref: refs/heads/main\tHEAD')
                    print('a' * 40 + '\tHEAD')
                elif 'rev-parse' in args:
                    print('a' * 40)
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
        shutil.copy(INSTALLER.with_name("checkout.py"), self.installer.with_name("checkout.py"))
        (self.coord_root / "coordlib").symlink_to(INSTALLER.parents[1] / "coordlib", target_is_directory=True)
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
        self.assertEqual(candidate["Label"], "com.coord.conductor.owner+repo")
        self.assertTrue(candidate["StandardOutPath"].endswith("coord-conductor-owner+repo.log"))
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

    def test_default_checkouts_for_same_named_repositories_are_distinct(self):
        for owner in ("owner", "other"):
            checkout = self.home / "conductor" / owner / "repo"
            checkout.mkdir(parents=True)
            (checkout / ".git").write_text("fake checkout")
            result = subprocess.run(["/bin/bash", str(self.installer), "--repo", owner + "/repo", "--no-wake"],
                                    env=self.env, cwd=self.root, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            ticks = [event for event in self.read_events() if event["tool"] == "tick"]
            self.assertEqual(Path(ticks[-1]["cwd"]).resolve(), checkout.resolve())

    def test_legacy_job_refresh_and_uninstall_only_touch_its_recorded_repository(self):
        original, other = "my-org/foo", "my/org-foo"
        legacy_label = "com.coord.conductor.my-org-foo"
        other_label = "com.coord.conductor.my+org-foo"
        legacy = self.plist.parent / (legacy_label + ".plist")
        alternate = self.plist.parent / (other_label + ".plist")
        self.plist = legacy
        self.existing(COORD_REPO=original)
        before = legacy.read_bytes()

        def run(repo, *args):
            start = len(self.read_events())
            result = self.run_install("--repo", repo, *args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return [event["args"] for event in self.read_events()[start:] if event["tool"] == "launchctl"]

        actions = run(other, "--no-wake")
        self.assertTrue(actions[0][1].endswith("/" + other_label))
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(plistlib.loads(alternate.read_bytes())["Label"], other_label)
        actions = run(original)
        self.assertTrue(actions[0][1].endswith("/" + legacy_label))
        self.assertIn("--no-wake", self.read_plist()["ProgramArguments"])
        self.assertEqual(self.read_plist()["EnvironmentVariables"]["OTHER_OPERATOR_SETTING"], "keep me")
        actions = run(other, "--uninstall")
        self.assertTrue(actions[0][1].endswith("/" + other_label))
        self.assertFalse(alternate.exists())
        self.assertTrue(legacy.exists())
        actions = run(original, "--uninstall")
        self.assertTrue(actions[0][1].endswith("/" + legacy_label))
        self.assertFalse(legacy.exists())

    def test_mismatched_existing_origin_stops_before_checkout_or_scheduler_changes(self):
        self.existing()
        installed = self.plist.read_bytes()
        for setting in ("FAKE_FETCH_URL", "FAKE_PUSH_URL"):
            with self.subTest(setting=setting):
                self.env.update(FAKE_FETCH_URL="git@github.com:Owner/Repo.git",
                                FAKE_PUSH_URL="git@github.com:Owner/Repo.git", COORD_REPO="owner/repo")
                self.env[setting] = "ssh://git@github.com/other/project.git"
                result = self.run_install("--no-wake")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("targets other/project", result.stderr)
                self.assertEqual(self.plist.read_bytes(), installed)
        events = self.read_events()
        self.assertFalse(any(e["tool"] in ("tick", "launchctl") for e in events))
        self.assertFalse(any(e["tool"] == "git" and ("checkout" in e["args"] or "fetch" in e["args"])
                             for e in events))

    def test_new_checkout_and_refresh_follow_a_non_main_default_branch(self):
        source = self.root / "source"
        git = shutil.which("git")
        subprocess.run([git, "init", "-q", "--initial-branch=trunk", str(source)], check=True)
        def commit():
            subprocess.run([git, "-C", str(source), "-c", "user.name=Coord", "-c", "user.email=coord@example.com",
                            "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "test"], check=True)
        commit()
        (self.bin / "git").unlink()
        (self.bin / "git").symlink_to(git)
        (self.bin / "gh").unlink()
        (self.bin / "gh").write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import os, subprocess, sys
            if sys.argv[1:3] == ['repo', 'clone']:
                sys.exit(subprocess.call(['git', 'clone', '-q', os.environ['FAKE_ORIGIN'], sys.argv[4]]))
            """))
        (self.bin / "gh").chmod(0o755)
        self.env["FAKE_ORIGIN"] = str(source)
        self.env["COORD_CONFIG"] = os.devnull
        shutil.rmtree(self.checkout)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                if attempt:
                    commit()
                result = self.run_install("--no-wake")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                tip = subprocess.check_output([git, "-C", str(source), "rev-parse", "trunk"])
                for ref in ("HEAD", "refs/remotes/origin/trunk"):
                    self.assertEqual(subprocess.check_output([git, "-C", str(self.checkout), "rev-parse", ref]), tip)
                branch = subprocess.run([git, "-C", str(self.checkout), "symbolic-ref", "-q", "HEAD"], check=False)
                self.assertEqual(branch.returncode, 1, "the watcher checkout must be detached")

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
            ["-C", str(self.checkout), "check-ref-format", "--branch", "main"],
            ["-C", str(self.checkout), "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"],
            ["-C", str(self.checkout), "checkout", "-q", "--detach", "refs/remotes/origin/main"],
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


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell unavailable")
class ScheduledTaskIdentityTests(unittest.TestCase):
    def run_powershell(self, script, env):
        command = [shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-NonInteractive"]
        if os.name == "nt":
            command.extend(["-ExecutionPolicy", "Bypass"])
        return subprocess.run([*command, "-Command", script], env=env, text=True, capture_output=True, timeout=20)

    def installer_env(self, temp, installer, checkout):
        env = dict(os.environ, LOCALAPPDATA=temp, GIT_ALLOW_PROTOCOL="file", COORD_CONFIG=os.devnull,
                   COORD_REPO="owner/repo", COORD_TEST_INSTALLER=str(installer), COORD_TEST_CHECKOUT=str(checkout))
        if os.name == "nt":
            python_dir = Path(sys.executable).parent
        else:
            python_dir = Path(temp, "bin")
            python_dir.mkdir()
            (python_dir / "python").symlink_to(sys.executable)
        env["PATH"] = str(python_dir) + os.pathsep + env["PATH"]
        return env

    def test_refresh_advances_the_tracking_branch_and_detached_head(self):
        with tempfile.TemporaryDirectory() as temp:
            source, checkout = Path(temp, "source"), Path(temp, "checkout")
            subprocess.run(["git", "init", "-q", "--initial-branch=trunk", str(source)], check=True)
            commit = ["git", "-C", str(source), "-c", "user.name=Coord", "-c", "user.email=coord@example.com",
                      "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "test"]
            subprocess.run(commit, check=True)
            subprocess.run(["git", "clone", "-q", str(source), str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", "HEAD"], check=True)
            subprocess.run(commit, check=True)

            # Stop at the dry-run boundary so no API call or scheduler mutation is possible.
            tool = Path(temp, "coord-src")
            shutil.copytree(INSTALLER.parent, tool / "conductor")
            shutil.copytree(INSTALLER.parents[1] / "coordlib", tool / "coordlib", ignore=shutil.ignore_patterns("__pycache__"))
            (tool / "coord").write_text("print('checkout refresh finished')\nraise SystemExit(23)\n")
            script = """
            function gh { $global:LASTEXITCODE = 0 }
            & $env:COORD_TEST_INSTALLER -Repo owner/repo -HomeDir $env:COORD_TEST_CHECKOUT -NoWake
            """
            env = self.installer_env(temp, tool / "conductor" / "install.ps1", checkout)
            result = self.run_powershell(script, env)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("checkout refresh finished", result.stdout)
            tip = subprocess.check_output(["git", "-C", str(source), "rev-parse", "trunk"])
            for ref in ("HEAD", "refs/remotes/origin/trunk"):
                self.assertEqual(subprocess.check_output(["git", "-C", str(checkout), "rev-parse", ref]), tip)
            self.assertEqual(subprocess.run(["git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD"]).returncode, 1)

    def test_install_refuses_an_existing_checkout_for_another_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp, "checkout")
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                            "https://github.com/other/project.git"], check=True)
            script = """
            function gh { $global:LASTEXITCODE = 0 }
            & $env:COORD_TEST_INSTALLER -Repo owner/repo -HomeDir $env:COORD_TEST_CHECKOUT -NoWake
            """
            env = self.installer_env(temp, INSTALLER.with_suffix(".ps1"), checkout)
            result = self.run_powershell(script, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("targets other/project", result.stdout + result.stderr)
            self.assertFalse(Path(temp, "coord").exists())
            self.assertFalse((checkout / ".git" / "FETCH_HEAD").exists())

    def test_uninstall_verifies_the_repository_before_selecting_a_legacy_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "coord")
            root.mkdir()
            legacy = root / "conductor-my-org-foo.json"
            legacy.write_text(json.dumps({"repo": "my-org/foo"}))
            current = root / "conductor-my+org-foo.json"
            current.write_text(json.dumps({"repo": "my/org-foo"}))
            env = dict(os.environ, LOCALAPPDATA=temp, COORD_TEST_INSTALLER=str(INSTALLER.with_suffix(".ps1")))
            script = """
            function Get-ScheduledTask { param($TaskName, $ErrorAction) return $true }
            function Unregister-ScheduledTask { param($TaskName, [switch]$Confirm) Write-Output "unregistered $TaskName" }
            & $env:COORD_TEST_INSTALLER -Repo $env:COORD_TEST_REPOSITORY -Uninstall
            """
            for repo, task in [("my/org-foo", "my+org-foo"), ("my-org/foo", "my-org-foo")]:
                result = self.run_powershell(script, dict(env, COORD_TEST_REPOSITORY=repo))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("unregistered coord-conductor-" + task, result.stdout)
                self.assertFalse(current.exists())
                self.assertEqual(legacy.exists(), repo == "my/org-foo")


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

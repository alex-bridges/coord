"""The merge train's per-repo configuration: what a repo with no .coord/config.json gets."""

import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import example_config  # noqa: E402
from test_coord import check, with_checks  # noqa: E402
from coordlib import EXIT_USAGE, cli, config, review, runtime, snapshot, train  # noqa: E402
from coordlib.gh import GitHub, GitHubError  # noqa: E402


class FakeGitHub:
    repo = "acme/widgets"

    def __init__(self, default_branch="trunk"):
        self.default_branch = default_branch
        self.calls = []

    def rest(self, path, method="GET", body=None):
        self.calls.append(path)
        return {"default_branch": self.default_branch}


class UnconfiguredRepoTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")
        os.environ["COORD_CONFIG"] = self.path  # absent: generic defaults
        config._cache.clear()
        runtime._default_branches.clear()

    def tearDown(self):
        self.dir.cleanup()
        example_config.use()

    def test_nothing_is_guarded_and_no_one_is_mentioned_by_default(self):
        self.assertEqual(train.owner_only_paths(), ())
        self.assertEqual(train.final_jobs(), ())
        self.assertFalse(train.is_configured())
        self.assertEqual(train.owner_mention("acme/widgets"), "@acme")
        self.assertEqual(train.owner_mention(), "")

    def test_the_train_workflow_names_are_required_not_defaulted(self):
        with self.assertRaises(train.NotConfigured) as caught:
            train.ci_workflow()
        self.assertIn("merge train is not configured for this repo", str(caught.exception))
        with self.assertRaises(train.NotConfigured):
            train.train_workflow()

    def test_default_branch_is_asked_of_github_once(self):
        fake = FakeGitHub("trunk")
        self.assertEqual(train.default_branch(fake), "trunk")
        self.assertEqual(train.default_branch(fake), "trunk")
        self.assertEqual(fake.calls, ["repos/{repo}"])
        pr = {"base": "trunk", "head": "h", "files": [], "labels": []}
        verdict, _, reason = train.assess(pr, base_branch=train.default_branch(fake))
        self.assertNotIn("its base is", reason)

    def test_a_configured_default_branch_asks_nobody(self):
        with open(self.path, "w") as handle:
            json.dump({"default_branch": "develop"}, handle)
        config._cache.clear()
        fake = FakeGitHub()
        self.assertEqual(train.default_branch(fake), "develop")
        self.assertEqual(fake.calls, [])

    def test_failed_branch_lookup_does_not_guess_or_reuse_another_repos_branch(self):
        self.assertEqual(train.default_branch(FakeGitHub("trunk")), "trunk")
        other = FakeGitHub(None)
        other.repo = "acme/other"
        with self.assertRaises(GitHubError):
            train.default_branch(other)
        other.default_branch = "stable"
        self.assertEqual(train.default_branch(other), "stable")

    def test_queue_uses_the_default_branch_in_the_snapshot(self):
        pr = {"number": 1, "base": "trunk", "head": "a" * 40, "codex": {"approved": True}}
        snap = {"main": {"name": "trunk"}, "prs": [pr], "train": [{"pr": 1}]}
        self.assertEqual(cli.train_queue(snap)[0]["verdict"], train.READY)

    def test_without_a_lane_classifier_every_pr_is_one_heavy_lane_and_nothing_runs(self):
        def show(path):
            raise AssertionError("nothing may be read or executed: " + path)
        classifier = train.LaneClassifier(show)
        self.assertEqual(classifier.classify(["a.txt"]), (train.HEAVY, train.NO_CLASSIFIER_REASON))
        self.assertIsNone(classifier.error)

    def test_train_commands_refuse_clearly_while_the_other_commands_do_not(self):
        gh = GitHub(repo="acme/widgets", runner=lambda args, stdin=None: "[]", transport="gh")
        args = cli.parser().parse_args(["train", "status"])
        with mock.patch("sys.stderr") as stderr:
            code = cli.run(args, gh)
        self.assertEqual(code, EXIT_USAGE)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("merge train is not configured for this repo", written)


class ConfiguredRepoTests(unittest.TestCase):
    def setUp(self):
        example_config.use()

    def test_the_example_values_populate_the_lazy_constants(self):
        self.assertEqual(train.ci_workflow(), "ci.yml")
        self.assertEqual(train.train_workflow(), "merge-train.yml")
        self.assertEqual(train.ci_dispatch_inputs(), {"lanes": "changed"})
        self.assertEqual(train.final_jobs(), ("CI verdict", "Wake the merge train"))
        self.assertEqual(train.owner_mention(), "@example-owner")
        self.assertIn("Tools/coord/", train.owner_only_paths())
        self.assertEqual(train.default_branch(), "main")
        self.assertEqual(train.needs_owner_label(), "needs-owner")
        self.assertEqual(train.owner_approved_label(), "owner-approved")
        self.assertEqual(review.ignored_check_prefixes(), ("LegacyCI | ",))


class StepCheckoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.targets = {}
        for name in ("caller", "target"):
            directory = os.path.join(temp.name, name)
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "remote", "add", "origin", f"https://github.com/team/{name}.git"],
                           check=True)
            os.makedirs(os.path.join(directory, ".coord"))
            with open(os.path.join(directory, ".coord", "config.json"), "w", encoding="utf-8") as handle:
                json.dump({"ci_workflow": f"{name}.yml", "train_workflow": "train.yml"}, handle)
            self.targets[name] = directory
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.targets["caller"])
        environ = mock.patch.dict(os.environ, {"COORD_APP": "off", "COORD_AGENT": "tester@host"})
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("COORD_CONFIG", "COORD_REPO", "GITHUB_STEP_SUMMARY", "COORD_TRAIN_APP_TOKEN"):
            os.environ.pop(name, None)

    def invoke(self, *options):
        seen = []
        def capture(step):
            seen.append((step.gh.repo, os.path.realpath(step.git.cwd), train.ci_workflow()))
            return []
        errors = io.StringIO()
        with mock.patch.object(cli.train_step.Step, "run", capture), contextlib.redirect_stderr(errors):
            code = cli.main([*options, "train", "step", "--repo-dir", self.targets["target"]])
        return code, seen, errors.getvalue()

    def test_checkout_selects_api_and_config_and_preserves_explicit_config(self):
        target = os.path.realpath(self.targets["target"])
        self.assertEqual(self.invoke()[:2], (0, [("team/target", target, "target.yml")]))
        os.environ["COORD_CONFIG"] = ".coord/config.json"
        self.assertEqual(self.invoke()[:2], (0, [("team/target", target, "caller.yml")]))

    def test_explicit_target_must_match_fetch_and_every_push_url(self):
        os.environ["COORD_REPO"] = "team/caller"
        for options in ((), ("--repo", "team/caller")):
            with self.subTest(options=options):
                code, seen, message = self.invoke(*options)
                self.assertNotEqual(code, 0)
                self.assertEqual(seen, [])
                self.assertIn("team/target", message)
        os.environ["COORD_REPO"] = "TEAM/TARGET"
        for repo in ("team/target", "team/other"):
            subprocess.run(["git", "-C", self.targets["target"], "remote", "set-url", "--add", "--push", "origin",
                            f"git@github.com:{repo}.git"], check=True)
        code, seen, message = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(seen, [])
        self.assertIn("push", message)
        self.assertIn("team/other", message)

    def test_explicit_api_repository_with_a_local_origin(self):
        target = self.targets["target"]
        subprocess.run(["git", "-C", target, "remote", "set-url", "origin", self.targets["caller"]], check=True)
        self.assertEqual(self.invoke("--repo", "team/local")[:2], (0, [("team/local", os.path.realpath(target), "target.yml")]))


class ConfigurableLabelsAndChecksTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")
        os.environ["COORD_CONFIG"] = self.path
        config._cache.clear()

    def tearDown(self):
        self.dir.cleanup()
        example_config.use()

    def test_defaults_have_generic_labels_and_no_ignored_check_prefixes(self):
        self.assertEqual(train.needs_owner_label(), "needs-owner")
        self.assertEqual(train.owner_approved_label(), "owner-approved")
        self.assertEqual(review.ignored_check_prefixes(), ())
        self.assertFalse(review.is_ignored_check("LegacyCI | Test"))

    def test_verdict_uses_the_configured_aggregate_job(self):
        with open(self.path, "w") as handle:
            json.dump({"verdict_job": "Build gate"}, handle)
        self.assertEqual(review.ci_state(with_checks(check("CI verdict")))["verdict"], "none")
        self.assertEqual(review.ci_state(with_checks(check("Build gate")))["verdict"], "success")

    def test_unconfigured_verdict_reports_successful_checks(self):
        status = {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"}
        self.assertEqual(review.ci_state(with_checks(status, check("Build")))["verdict"], "success")
        self.assertEqual(review.ci_state(with_checks(status, check("Test", status="QUEUED")))["verdict"], "pending")
        for conclusion in ("NEUTRAL", "SKIPPED", "CANCELLED"):
            self.assertNotEqual(review.ci_state(with_checks(check("Build", conclusion=conclusion)))["verdict"], "success")
        with open(self.path, "w") as handle:
            json.dump({"ignored_check_prefixes": ["External"]}, handle)
        config._cache.clear()
        self.assertEqual(review.ci_state(with_checks(check("External build")))["verdict"], "none")

    def test_custom_labels_and_ignored_check_prefixes_are_honoured(self):
        with open(self.path, "w") as handle:
            json.dump({
                "default_branch": "main",
                "needs_owner_label": "needs-lead",
                "owner_approved_label": "lead-approved",
                "ignored_check_prefixes": ["ExternalCI | "],
            }, handle)
        config._cache.clear()
        self.assertIn("needs-lead", train.LABELS)
        self.assertIn("lead-approved", train.LABELS)
        self.assertTrue(review.is_ignored_check("ExternalCI | Build"))
        self.assertFalse(review.is_ignored_check("GitHub Actions"))
        head = "a" * 40
        pr = {"number": 1, "base": "main", "head": head, "files": [], "labels": ["train", "needs-lead"],
              "codex": {"approved": True}, "ci": {"failing": []}}
        self.assertEqual(train.assess(pr)[:2], (train.HOLD, train.HOLD_NEEDS_OWNER))
        approved = dict(pr, labels=["train", "needs-lead", "lead-approved"], ownerApprovedHead=head)
        self.assertEqual(train.assess(approved)[0], train.READY)

    def test_both_owner_approve_and_legacy_alex_approve_markers_populate_head(self):
        head = "b" * 40
        for kind in ("owner-approve", "alex-approve"):
            prs = [{"number": 7}]
            comments = [{"on": 7, "author": {"login": "acme", "type": "User"},
                         "payload": {"kind": kind, "v": 1, "agent": "a", "pr": 7, "head": head}}]
            snapshot.attach_head_records(prs, comments, "acme/widgets")
            self.assertEqual(prs[0]["ownerApprovedHead"], head, kind)
            self.assertEqual(prs[0]["alexApprovedHead"], head, kind)


if __name__ == "__main__":
    unittest.main()

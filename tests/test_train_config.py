"""The merge train's per-repo configuration: what a repo with no .coord/config.json gets."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import example_config  # noqa: E402
from coordlib import EXIT_USAGE, cli, config, review, snapshot, train  # noqa: E402
from coordlib.gh import GitHub  # noqa: E402


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
        train._default_branch_cache.clear()
        train._last_default_branch.clear()

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

"""The preflight distinguishes configuration from proven access without side effects."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import apptoken, doctor  # noqa: E402
from coordlib.gh import GitHub, GitHubError  # noqa: E402


NOW = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        self.which = self.start_patch(patch("shutil.which", side_effect=lambda name: "/bin/git" if name == "git" else None))
        self.start_patch(patch.object(doctor.identity, "agent_id", return_value="codex@host:INISA-task"))
        self.cached = self.start_patch(patch.object(doctor.cache, "load_snapshot", return_value=None))
        self.start_patch(patch.object(doctor.snapshot, "now_utc", return_value=NOW))
        self.start_patch(patch.object(doctor.budget, "summary", return_value=None))
        self.start_patch(patch("subprocess.run", side_effect=AssertionError("doctor must not start a process")))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def client(self, **kwargs):
        gh = GitHub("owner/repo", **kwargs)
        gh.rest = Mock(side_effect=AssertionError("offline doctor must not call the API"))
        gh.graphql = Mock(side_effect=AssertionError("doctor must not call GraphQL"))
        return gh

    def test_no_gh_or_token_is_reported_unavailable(self):
        gh = self.client()
        report = doctor.inspect(gh)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["transport"])
        self.assertFalse(report["authentication"]["configured"])
        self.assertFalse(report["authentication"]["readVerified"])
        self.assertFalse(report["authentication"]["writeVerified"])
        self.assertIn("neither the gh CLI nor a GH_TOKEN", report["authentication"]["problem"])
        gh.rest.assert_not_called()

    def test_token_selects_http_but_does_not_prove_authorization(self):
        gh = self.client(token=SECRET)
        report = doctor.inspect(gh)
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["transport"], "http")
        self.assertEqual(report["authentication"]["principal"], "user")
        self.assertTrue(report["authentication"]["configured"])
        self.assertFalse(report["authentication"]["readVerified"])
        self.assertFalse(report["authentication"]["writeVerified"])
        self.assertNotIn(SECRET, json.dumps(report) + doctor.render(report))
        gh.rest.assert_not_called()
        gh.graphql.assert_not_called()

    def test_gh_presence_does_not_claim_authenticated_access(self):
        self.which.side_effect = lambda name: f"/bin/{name}" if name in ("git", "gh", "codex") else None
        report = doctor.inspect(self.client())
        self.assertEqual(report["transport"], "gh")
        self.assertEqual(report["status"], "unverified")
        self.assertFalse(report["authentication"]["readVerified"])
        self.assertEqual(report["tools"], {"git": True, "gh": True, "claude": False, "codex": True})

    def test_offline_app_principal_never_requests_a_token(self):
        app = Mock()
        app.installation_id.return_value = 123
        app.token.side_effect = AssertionError("offline doctor must not mint an app token")
        report = doctor.inspect(self.client(app=app))
        self.assertEqual(report["authentication"]["principal"], "app:123")
        self.assertEqual(report["authentication"]["mode"], "app")
        self.assertEqual(report["status"], "unverified")
        app.token.assert_not_called()

    def test_broken_app_configuration_is_unavailable_and_redacted(self):
        error = f"invalid app configuration: {SECRET}"
        gh = self.client(app=apptoken.Unavailable(error))
        gh.app_mode = apptoken.Mode(True, "config found", error=error)
        report = doctor.inspect(gh)
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse(report["authentication"]["configured"])
        self.assertIn("invalid app configuration", report["authentication"]["problem"])
        self.assertNotIn(SECRET, json.dumps(report) + doctor.render(report))
        gh.rest.assert_not_called()

    def test_old_cache_reports_its_age_without_refreshing(self):
        self.cached.return_value = {"generatedAt": "2026-09-25T12:00:00Z", "cacheAgeSeconds": 1}
        gh = self.client(token=SECRET)
        report = doctor.inspect(gh)
        self.assertEqual(report["cache"], {"present": True, "generatedAt": "2026-09-25T12:00:00Z",
                                          "ageSeconds": 172800})
        self.assertEqual(report["status"], "unverified")
        gh.rest.assert_not_called()

    def test_malformed_cache_timestamp_cannot_break_preflight(self):
        for generated in ("not a timestamp", "2026-99-99T01:00:00Z", [], None):
            with self.subTest(generated=generated):
                self.cached.return_value = {"generatedAt": generated}
                report = doctor.inspect(self.client(token=SECRET))
                self.assertTrue(report["cache"]["present"])
                self.assertIsNone(report["cache"]["ageSeconds"])
                self.assertEqual(report["status"], "unverified")

    def test_future_cache_timestamp_never_reports_negative_age(self):
        self.cached.return_value = {"generatedAt": "2026-09-28T12:00:00Z"}
        self.assertEqual(doctor.inspect(self.client(token=SECRET))["cache"]["ageSeconds"], 0)

    def test_online_success_probes_only_repository_read(self):
        gh = self.client(token=SECRET)
        gh.rest.side_effect = None
        gh.rest.return_value = {"full_name": "owner/repo", "permissions": {"push": True}}
        report = doctor.inspect(gh, online=True)
        gh.rest.assert_called_once_with("repos/{repo}")
        gh.graphql.assert_not_called()
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["authentication"]["readVerified"])
        self.assertFalse(report["authentication"]["writeVerified"], "metadata permissions are not a write probe")

    def test_online_app_probe_reports_the_principal_it_discovers(self):
        app = Mock()
        app.installation_id.return_value = None
        gh = self.client(app=app)

        def first_read(path):
            app.installation_id.return_value = 123
            return {"full_name": "owner/repo"}

        gh.rest.side_effect = first_read
        report = doctor.inspect(gh, online=True)
        self.assertEqual(report["authentication"]["principal"], "app:123")
        gh.rest.assert_called_once_with("repos/{repo}")

    def test_online_failure_propagates_to_cli_error_envelope(self):
        gh = self.client(token=SECRET)
        gh.rest.side_effect = GitHubError("HTTP 403: repository access denied")
        with self.assertRaisesRegex(GitHubError, "repository access denied"):
            doctor.inspect(gh, online=True)
        gh.rest.assert_called_once_with("repos/{repo}")
        gh.graphql.assert_not_called()


if __name__ == "__main__":
    unittest.main()

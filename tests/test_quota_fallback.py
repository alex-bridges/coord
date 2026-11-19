"""#1052 slice 2: the read that discovers an exhausted app quota serves the cached board.

The first 403 carrying `X-RateLimit-Remaining: 0` is recorded by `_http` and raised as a
plain GitHubError. A cache read (board, inbox, train status, conductor status) must then
take the stale-snapshot path the next call would take, not exit with the error."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import API, AppTestCase, FakeResponse, rate_headers  # noqa: E402
from coordlib import EXIT_BACKEND, EXIT_OK  # noqa: E402
from coordlib import gh as ghmodule  # noqa: E402


class QuotaFallbackTests(AppTestCase):
    def setUp(self):
        super().setUp()
        os.environ["COORD_TRANSPORT"] = "gh"  # Alex's identity would go through the fake gh runner
        # REST only, as cloud sessions run: the first read is a core one. (A GraphQL 403 is
        # taken for a proxy's refusal and retried over REST: snapshot.with_rest_fallback.)
        os.environ["COORD_REST_ONLY"] = "1"
        self.seed_token("read")
        self.seed_snapshot(age_seconds=2 * 3600)
        self.refused = []

    def refuse_first_read(self, remaining_zero: bool = True, after: int = 0, batch: bool = False) -> None:
        """Answer the first API request after `after` others 403, as GitHub answers an
        exhausted (or, without remaining 0, a merely forbidden) budget; the others reach the
        fake GitHub. `batch`: the train's batch refs list one batch branch."""
        server = self.server
        passed = []

        def urlopen(request, timeout=None):
            path = request.full_url[len(API) + 1:]
            if path.startswith("app/") or path.endswith("/installation") or self.refused:
                return server(request, timeout)
            if batch and "/git/matching-refs/" in path:
                passed.append(path)
                return FakeResponse([{"ref": "refs/heads/train/batch-1", "object": {"sha": "b" * 40}}], {})
            if len(passed) < after:
                passed.append(path)
                return server(request, timeout)
            self.refused.append(path)
            resource = "graphql" if path == "graphql" else "core"
            headers = rate_headers(5000 if remaining_zero else 10, server.reset, resource)
            raise server._error(request.full_url, 403, {"message": "API rate limit exceeded"}, headers)

        self.patch(ghmodule, "app_open", urlopen)

    def test_the_exhausting_read_serves_the_stale_board(self):
        self.refuse_first_read()
        code, out, err = self.main(["board"])
        self.assertEqual(len(self.refused), 1, "the first app read was refused")
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("GitHub API budget low: 0 left on the GitHub App", err)
        self.assertIn("showing the snapshot from 2h00m ago", err)
        self.assertIn("STALE: snapshot from 2h00m ago", out.splitlines()[0])
        self.assertEqual(self.server.api_requests(), [], "no request after the refused one reached GitHub")
        self.assertEqual(self.runner.calls, [], "nothing as Alex")

    def test_a_403_that_leaves_budget_still_errors(self):
        self.refuse_first_read(remaining_zero=False)
        code, _, err = self.main(["board"])
        self.assertEqual(code, EXIT_BACKEND, err)
        self.assertIn("HTTP 403", err)

    # Codex on #1081: inbox and train status call GitHub again after the snapshot (the
    # comments; the batch refs and runs). Served from a fresh cache, the refused request
    # is one of those.

    def test_inbox_counts_from_the_snapshot_when_its_comment_read_exhausts(self):
        self.seed_snapshot(age_seconds=5)
        self.refuse_first_read()
        code, out, err = self.main(["inbox"])
        self.assertEqual(self.refused[0].split("?", 1)[0], "repos/o/r/issues/comments")
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("GitHub API budget low: 0 left on the GitHub App", err)
        self.assertIn("run `coord inbox` after the reset", out)
        self.assertEqual(self.server.api_requests(), [], "no request after the refused one reached GitHub")

    def test_train_status_skips_the_batch_when_its_ref_read_exhausts(self):
        self.seed_snapshot(age_seconds=5)
        self.refuse_first_read()
        code, out, err = self.main(["train", "status"])
        self.assertTrue(self.refused[0].startswith("repos/o/r/git/matching-refs/"), self.refused)
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("GitHub API budget low: 0 left on the GitHub App", err)
        self.assertIn("batch: not read (API budget low)", out)
        self.assertEqual(self.server.api_requests(), [], "no request after the refused one reached GitHub")

    def test_inbox_falls_back_when_its_second_comment_read_exhausts(self):
        self.seed_snapshot(age_seconds=5)
        self.refuse_first_read(after=1)
        code, out, err = self.main(["inbox"])
        self.assertEqual(self.refused[0].split("?", 1)[0], "repos/o/r/pulls/comments")
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("run `coord inbox` after the reset", out)

    def test_train_status_falls_back_when_a_page_of_runs_exhausts(self):
        self.seed_snapshot(age_seconds=5)
        self.refuse_first_read(batch=True)
        code, out, err = self.main(["train", "status"])
        self.assertIn("/actions/workflows/", self.refused[0])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("batch: not read (API budget low)", out)

    # Codex on 5e880ae: when the snapshot build is the exhausting read, inbox must not go
    # on to read the comments; it counts from the stale snapshot it was just served.

    def test_inbox_stops_after_the_snapshot_build_exhausts(self):
        self.refuse_first_read()  # the 2h-old seed: the snapshot build is the first read
        code, out, err = self.main(["inbox"])
        self.assertEqual(len(self.refused), 1, "the first app read was refused")
        self.assertNotIn("comments", self.refused[0])
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("GitHub API budget low: 0 left on the GitHub App", err)
        self.assertIn("run `coord inbox` after the reset", out)
        self.assertEqual(self.server.api_requests(), [], "no request after the refused one reached GitHub")

    def test_a_403_that_leaves_budget_still_errors_after_the_snapshot(self):
        for argv in (["inbox"], ["train", "status"]):
            with self.subTest(argv=argv):
                self.refused.clear()
                self.seed_snapshot(age_seconds=5)
                self.refuse_first_read(remaining_zero=False)
                code, _, err = self.main(argv)
                self.assertEqual(code, EXIT_BACKEND, err)
                self.assertIn("HTTP 403", err)



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

"""#1052 slice 2: the read that discovers an exhausted app quota serves the cached board.

The first 403 carrying `X-RateLimit-Remaining: 0` is recorded by `_http` and raised as a
plain GitHubError. A cache read (board, inbox, train status, conductor status) must then
take the stale-snapshot path the next call would take, not exit with the error."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apptestkit import API, AppTestCase, rate_headers  # noqa: E402
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

    def refuse_first_read(self, remaining_zero: bool = True) -> None:
        """Answer the first API request 403, as GitHub answers an exhausted (or, without
        remaining 0, a merely forbidden) budget; later requests reach the fake GitHub."""
        server = self.server

        def urlopen(request, timeout=None):
            path = request.full_url[len(API) + 1:]
            if not self.refused and not (path.startswith("app/") or path.endswith("/installation")):
                self.refused.append(path)
                resource = "graphql" if path == "graphql" else "core"
                headers = rate_headers(5000 if remaining_zero else 10, server.reset, resource)
                raise server._error(request.full_url, 403, {"message": "API rate limit exceeded"}, headers)
            return server(request, timeout)

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


if __name__ == "__main__":
    unittest.main()

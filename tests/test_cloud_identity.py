"""Stable cloud fallback identity across concurrent first use and cache failures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from coordlib import identity  # noqa: E402


class CloudIdentityTests(unittest.TestCase):
    def test_concurrent_creators_publish_one_complete_identity(self):
        workers = 12
        publish = threading.Barrier(workers)
        staged = []
        real_link = os.link
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            destination = Path(directory) / "container-id"

            def compete(source, target):
                # Every caller has already observed a missing destination and
                # written its own complete candidate before any can publish.
                candidate = Path(source).read_text(encoding="utf-8")
                self.assertRegex(candidate, r"^[0-9a-f]{32}$")
                self.assertFalse(destination.exists())
                staged.append(candidate)
                publish.wait(timeout=10)
                real_link(source, target)

            with patch.object(identity.os, "link", side_effect=compete), ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda _: identity.container_id(), range(workers)))
            self.assertEqual(len(set(staged)), workers)
            self.assertEqual(set(results), {destination.read_text(encoding="utf-8")})
            self.assertEqual(identity.container_id(), results[0])
            self.assertEqual(os.listdir(directory), ["container-id"])

    def test_a_stored_identity_does_not_require_writing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            (Path(directory) / "container-id").write_text("existing-id\n", encoding="utf-8")
            with patch.object(identity.tempfile, "mkstemp", side_effect=AssertionError("must not create")):
                self.assertEqual(identity.container_id(), "existing-id")

    def assert_identity_error(self):
        with self.assertRaisesRegex(identity.IdentityError, "COORD_AGENT or COORD_SESSION_ID") as caught:
            identity.container_id()
        self.assertNotIn("sensitive", str(caught.exception))

    def test_unavailable_cache_raises_instead_of_returning_random_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "not-a-directory"
            blocked.write_text("occupied", encoding="utf-8")
            with patch.dict(os.environ, {"COORD_CACHE_DIR": str(blocked)}):
                self.assert_identity_error()
                self.assert_identity_error()
            self.assertEqual(blocked.read_text(encoding="utf-8"), "occupied")

    def test_unreadable_empty_and_corrupt_stored_identity_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            stored = Path(directory) / "container-id"
            for raw in (b"", b" \n", b"\xff"):
                with self.subTest(raw=raw):
                    stored.write_bytes(raw)
                    self.assert_identity_error()
                    self.assertEqual(stored.read_bytes(), raw)
            stored.unlink()
            stored.mkdir()
            self.assert_identity_error()
            self.assertTrue(stored.is_dir())
            with patch("builtins.open", side_effect=PermissionError("sensitive cache location")):
                self.assert_identity_error()

    def test_failed_creation_or_publication_never_returns_an_unstored_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COORD_CACHE_DIR": directory}):
            for target, method in ((identity.tempfile, "mkstemp"), (identity.os, "fsync"), (identity.os, "link")):
                with self.subTest(operation=method), patch.object(target, method, side_effect=PermissionError("sensitive")):
                    self.assert_identity_error()
                    self.assertEqual(os.listdir(directory), [], "temporary candidate must be cleaned up")

    def test_explicit_addresses_and_sessions_bypass_unavailable_cloud_cache(self):
        with patch.object(identity, "container_id", side_effect=identity.IdentityError("unavailable")), \
                patch.object(identity, "git_toplevel", return_value="/workspace/example-repo"), \
                patch.object(identity, "detect_host", return_value="cloud"):
            flags = {"CLAUDE_CODE_REMOTE": "true"}
            self.assertEqual(identity.agent_id(dict(flags, COORD_AGENT="worker@cloud:fixed")), "worker@cloud:fixed")
            for key in ("COORD_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
                with self.subTest(key=key):
                    first = identity.agent_id(dict(flags, **{key: "session-one"}))
                    self.assertEqual(first, identity.agent_id(dict(flags, **{key: "session-one"})))
                    self.assertTrue(re.fullmatch(r"claude-cloud@cloud:example-repo-[0-9a-f]{16}", first))


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

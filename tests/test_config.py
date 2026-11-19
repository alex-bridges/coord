import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from coordlib import config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        os.environ["COORD_CONFIG"] = self.path
        config._cache.clear()

    def tearDown(self):
        os.environ.pop("COORD_CONFIG", None)
        config._cache.clear()

    def test_missing_file_gives_defaults(self):
        self.assertIsNone(config.get("ci_workflow"))
        self.assertEqual(config.get("owner_only_paths"), [])

    def test_file_overrides_and_nested_merge(self):
        with open(self.path, "w") as handle:
            json.dump({"ci_workflow": "ci.yml", "conductor": {"guard_hook": "g.py"}}, handle)
        self.assertEqual(config.get("ci_workflow"), "ci.yml")
        self.assertEqual(config.get("conductor")["guard_hook"], "g.py")
        self.assertEqual(config.get("conductor")["deny"], [])

    def test_require_raises_when_unset(self):
        with self.assertRaises(ValueError):
            config.require("train_workflow")

    def test_bad_json_raises(self):
        with open(self.path, "w") as handle:
            handle.write("{")
        with self.assertRaises(ValueError):
            config.load()


if __name__ == "__main__":
    unittest.main()

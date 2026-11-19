"""Gives a test module the example per-repo config (tests/fixtures/example-train-config.json).

The merge train, CLI and mailbox tests assert configured workflow names, jobs, lanes and
guarded paths. Call `use()` at import and from `setUpModule`: test_config's tearDown unsets
COORD_CONFIG between modules.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from coordlib import config  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "example-train-config.json")


def use() -> None:
    os.environ["COORD_CONFIG"] = PATH
    config._cache.clear()

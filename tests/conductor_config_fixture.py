"""INISA's per-repo config for the conductor, reviewer, outage and snapshot tests.

Each test module calls `use_inisa_config` from setUpModule and `restore` from tearDownModule,
so COORD_CONFIG points at a temp file holding the values INISA configures.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys_path = os.path.join(HERE, "..")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

INISA_CONFIG = {
    "default_branch": "main",
    "owner_login": "alex-bridges",
    "reviewer_bot_login": "inisa-sidechain[bot]",
    "reviewer_trigger": "@inisa-sidechain review",
    "ci_workflow": "ci.yml",
    "train_workflow": "merge-train.yml",
    "notify_workflow": "notify-alex.yml",
    "owner_only_paths": [".claude/agents/", ".claude/settings.json", ".claude/hooks/", "Tools/coord/", "CLAUDE.md",
                         "Tools/ci-runner/"],
    "runner_variables": ["MK4_RUNNER_MACOS", "MK4_RUNNER_MACOS_LIGHT", "MK4_RUNNER_CONTROL", "MK4_RUNNER_LINUX_ARM64"],
    "runner_control_variable": "MK4_RUNNER_CONTROL",
    "conductor": {"agent_file": ".claude/agents/conductor.md", "instructions_file": "CLAUDE.md",
                  "guard_hook": ".claude/hooks/conductor-guard.py", "deny": []},
    "reviewer": {"agent_file": ".claude/agents/reviewer.md", "guard_hook": ".claude/hooks/conductor-guard.py"},
    "claude_weekly_reset": "Thu 12:00 America/New_York",
    "state_dir_name": "inisa-coord",
    "keychain_service": "example-coord-github-app",
    "legacy_state_dir_names": [],
}

_saved = {}


def use_inisa_config(extra=None):
    """Point COORD_CONFIG at a temp file with INISA's values (merged with `extra`)."""
    from coordlib import config
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(dict(INISA_CONFIG, **(extra or {})), handle)
    handle.close()
    _saved[handle.name] = os.environ.get("COORD_CONFIG")
    os.environ["COORD_CONFIG"] = handle.name
    config._cache.clear()
    return handle.name


def restore(path):
    from coordlib import config
    previous = _saved.pop(path, None)
    if previous is None:
        os.environ.pop("COORD_CONFIG", None)
    else:
        os.environ["COORD_CONFIG"] = previous
    config._cache.clear()
    try:
        os.unlink(path)
    except OSError:
        pass

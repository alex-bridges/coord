"""Per-repo coord configuration.

Read from `.coord/config.json` in the target repository's working tree
(`COORD_CONFIG` overrides the path). A missing file means generic defaults:
board, claim, send and inbox work; the merge train, reviewer and conductor
stay disabled until the repo configures them.

Every value that used to be an INISA hardcode lives in DEFAULTS below.
"""

import json
import os
import subprocess

DEFAULTS = {
    "default_branch": None,          # None: ask GitHub
    "owner_login": None,             # None: repo owner
    "owner_mention": None,           # None: "@" + owner_login
    "reviewer_bot_login": None,      # None: reviewer disabled
    "reviewer_trigger": None,        # e.g. "@my-bot review"
    "ci_workflow": None,             # e.g. "ci.yml"; None: train disabled
    "train_workflow": None,          # e.g. "merge-train.yml"
    "notify_workflow": None,         # e.g. "notify-owner.yml"
    "verdict_job": None,             # e.g. "CI verdict"
    "wake_job": None,                # e.g. "Wake the merge train"
    "ci_lanes": None,                # e.g. "changed"
    "owner_only_paths": [],          # PRs touching these need the owner
    "lane_classifier": None,         # {"script": ..., "rules": ...} or None
    "runner_variables": [],          # repo variables the conductor may set
    "conductor": {
        "agent_file": None,          # path to the conductor prompt
        "instructions_file": None,   # e.g. "CLAUDE.md"
        "guard_hook": None,
        "deny": [],                  # extra deny patterns
    },
    "reviewer": {
        "agent_file": None,
        "guard_hook": None,
    },
    "claude_weekly_reset": None,     # e.g. "Thu 12:00 America/New_York"
    "keychain_service": "coord-github-app",
    "state_dir_name": "coord",       # ~/.cache/<name>, ~/.config/<name>
    "legacy_state_dir_names": [],    # read-only fallbacks, e.g. ["inisa-coord"]
}

CONFIG_RELATIVE_PATH = ".coord/config.json"

_cache = {}


def _repo_root(start=None):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start or os.getcwd(), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def config_path(start=None):
    explicit = os.environ.get("COORD_CONFIG")
    if explicit:
        return explicit
    root = _repo_root(start)
    return os.path.join(root, CONFIG_RELATIVE_PATH) if root else None


def _merge(base, over):
    merged = dict(base)
    for key, value in over.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load(start=None, refresh=False):
    """Return the merged config. Unknown keys are kept; bad JSON raises ValueError."""
    path = config_path(start)
    if not refresh and path in _cache:
        return _cache[path]
    merged = _merge(DEFAULTS, {})
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError as err:
                raise ValueError("invalid coord config %s: %s" % (path, err))
        if not isinstance(data, dict):
            raise ValueError("coord config %s must be a JSON object" % path)
        merged = _merge(merged, data)
    _cache[path] = merged
    return merged


def get(key, start=None):
    return load(start)[key]


def require(key, start=None):
    """Value for a feature that cannot run without it; raises a clear error."""
    value = get(key, start)
    if value in (None, "", []):
        raise ValueError(
            "coord: '%s' is not set; add it to %s to use this feature"
            % (key, CONFIG_RELATIVE_PATH))
    return value

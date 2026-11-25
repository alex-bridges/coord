"""Per-repo coord configuration.

Read from `.coord/config.json` in the target repository's working tree
(`COORD_CONFIG` overrides the path). A missing file means generic defaults:
board, claim, send and inbox work; the merge train, reviewer and conductor
stay disabled until the repo configures them.
"""

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

DEFAULTS = {
    "default_branch": None,          # None: ask GitHub
    "owner_login": None,             # None: repo owner
    "owner_mention": None,           # None: "@" + owner_login
    "needs_owner_label": "needs-owner",       # label holding PRs for owner approval
    "owner_approved_label": "owner-approved", # label recording owner approval
    "reviewer_bot_login": None,      # None: reviewer disabled
    "reviewer_trigger": None,        # e.g. "@my-bot review"
    "ci_workflow": None,             # e.g. "ci.yml"; None: train disabled
    "train_workflow": None,          # e.g. "merge-train.yml"
    "notify_workflow": None,         # e.g. "notify-owner.yml"
    "verdict_job": None,             # e.g. "CI verdict"
    "wake_job": None,                # e.g. "Wake the merge train"
    "ci_lanes": None,                # e.g. "changed"
    "ignored_check_prefixes": [],    # check/status name prefixes to ignore in CI state
    "owner_only_paths": [],          # PRs touching these need the owner
    "lane_classifier": None,         # {"script": ..., "rules": ...} or None
    "runner_variables": [],          # repo variables the conductor may set
    "runner_control_variable": None,
    "coord_app_login": None,         # the GitHub App coord posts as in app mode (e.g. "my-coord-app[bot]")
    "relay_command": [],             # handoff relay argv with a "{prompt}" item; [] = the default (relay.py)
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
    "legacy_state_dir_names": [],    # read-only fallbacks for older state directory names
}

CONFIG_RELATIVE_PATH = ".coord/config.json"

_cache = {}
_checkout = ContextVar("coord_config_checkout", default=None)


class ConfigError(ValueError):
    """An unreadable or invalid repository configuration."""


@contextmanager
def in_checkout(start):
    """Resolve command-wide config reads in the selected checkout without changing cwd."""
    scope = _checkout.set(start)
    try:
        yield
    finally:
        _checkout.reset(scope)


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


def _merge(base, over, path, prefix=""):
    merged = deepcopy(base)
    for key, value in over.items():
        if key in base:
            default = base[key]
            field = prefix + key
            if key == "lane_classifier" and value is not None:
                default = {"script": None, "rules": None}
            if isinstance(default, dict):
                if not isinstance(value, dict):
                    raise ConfigError(f"{path}: {field} must be an object")
                value = _merge(default, value, path, field + ".")
            elif isinstance(default, list):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ConfigError(f"{path}: {field} must be an array of strings")
            elif value is not None and not isinstance(value, str):
                raise ConfigError(f"{path}: {field} must be a string or null")
        merged[key] = value
    return merged


def load(start=None, refresh=False):
    """Return the merged config. Unknown keys are kept; bad JSON raises ValueError."""
    # Cache before discovering the checkout. CI and review rules read configuration
    # for every job and file, and discovery otherwise runs git for every lookup.
    start = start or _checkout.get()
    key = (os.path.abspath(start or os.curdir), os.environ.get("COORD_CONFIG"),
           os.environ.get("GIT_DIR"), os.environ.get("GIT_WORK_TREE"))
    if not refresh and key in _cache:
        return _cache[key]
    path = config_path(start)
    data = {}
    if path and os.path.abspath(path) != os.path.abspath(os.devnull):
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                raise OSError("expected a regular config file")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError) as error:
            raise ConfigError(f"cannot read coord config {path}: {error}") from error
        if not isinstance(data, dict):
            raise ConfigError(f"coord config {path} must be a JSON object")
    merged = _merge(DEFAULTS, data, path)
    _cache[key] = merged
    return merged


def get(key, start=None):
    return load(start)[key]


def require(key, start=None):
    """Value for a feature that cannot run without it; raises a clear error."""
    value = get(key, start)
    if value in (None, "", []):
        raise ConfigError(
            "'%s' is not set; add it to %s to use this feature"
            % (key, CONFIG_RELATIVE_PATH))
    return value

#!/usr/bin/env python3
"""Example CI lane selector used by coord's merge-train classifier tests.

Evaluates path-filter rules from `.github/ci-lanes.json` to classify changes into
light (`docs`, `screenshots`) and heavy lanes. Standard library only.
"""

import json
from pathlib import Path
import re

LIGHT_LANES = ("docs", "screenshots")
HEAVY_LANES = ("core-debug", "core-release", "ui", "apps", "android", "wasm", "plugins")
ON_DEMAND_LANES = ("sanitizers",)
ALL_LANES = LIGHT_LANES + HEAVY_LANES + ON_DEMAND_LANES
PATHS_ONLY_LANES = ("screenshots",)
RULE_TO_LANES = {"core": ("core-debug", "core-release")}


def glob_to_regex(pattern):
    """GitHub path-filter glob: `*` stays in one segment, `**` crosses segments."""
    unsupported = set("?+[]{}") & set(pattern)
    if unsupported:
        raise ValueError(f"unsupported glob syntax {sorted(unsupported)} in {pattern!r}")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def rule_patterns(rule):
    """[(is_include, regex, text)] in file order; entries are strings or {path, why}."""
    patterns = []
    for entry in rule["paths"]:
        text = entry["path"] if isinstance(entry, dict) else entry
        if not isinstance(text, str) or not text or text == "!":
            raise ValueError(f"invalid path entry: {entry!r}")
        include = not text.startswith("!")
        patterns.append((include, glob_to_regex(text if include else text[1:]), text))
    if not any(include for include, _, _ in patterns):
        raise ValueError("a lane needs at least one including pattern")
    return patterns


def included_paths(paths, patterns):
    """Paths whose LAST matching pattern includes them (GitHub's ordering rule)."""
    included = []
    for path in paths:
        verdict = False
        for include, regex, _ in patterns:
            if regex.match(path):
                verdict = include
        if verdict:
            included.append(path)
    return included


def load_rules(lanes_file):
    rules = json.loads(Path(lanes_file).read_text())
    compiled = {"everything": rule_patterns(rules["everything"])}
    for name, rule in rules["lanes"].items():
        for lane in RULE_TO_LANES.get(name, (name,)):
            if lane not in ALL_LANES:
                raise ValueError(f"ci-lanes.json names an unknown lane: {name}")
        compiled[name] = rule_patterns(rule)
    missing = [lane for lane in LIGHT_LANES + HEAVY_LANES
               if not any(lane in RULE_TO_LANES.get(name, (name,)) for name in rules["lanes"])]
    if missing:
        raise ValueError(f"ci-lanes.json has no rule for: {missing}")
    return compiled


class Selection:
    def __init__(self, apps_registered):
        self.lanes = {lane: False for lane in ALL_LANES}
        self.why = {lane: "no changed path selects it" for lane in ALL_LANES}
        self.apps = []
        self.apps_registered = apps_registered
        self.notes = []

    def select(self, lane, why):
        self.lanes[lane] = True
        self.why[lane] = why

    def select_everything(self, why, include_paths_only=False):
        for lane in LIGHT_LANES + HEAVY_LANES:
            if lane in PATHS_ONLY_LANES and not include_paths_only:
                self.why[lane] = "only its own paths or a dispatch naming it select this lane"
                continue
            self.select(lane, why)
        self.apps = list(self.apps_registered)


def select_for_paths(selection, paths, rules, root, scope):
    hit = included_paths(paths, rules["everything"])
    if hit:
        selection.select_everything(f"the CI graph itself changed: {hit[0]}")
        for lane in PATHS_ONLY_LANES:
            owned = included_paths(paths, rules[lane])
            if owned:
                selection.select(lane, f"{owned[0]} changed alongside the CI graph")
        return
    for name, patterns in rules.items():
        if name == "everything":
            continue
        hit = included_paths(paths, patterns)
        if not hit:
            continue
        more = f" and {len(hit) - 1} more" if len(hit) > 1 else ""
        for lane in RULE_TO_LANES.get(name, (name,)):
            selection.select(lane, f"{hit[0]}{more}")
        if name == "apps":
            selection.apps, reason = scope.scope_for_paths(root, hit)
            selection.why["apps"] = reason if hit[0] in reason else f"{reason}: {hit[0]}{more}"

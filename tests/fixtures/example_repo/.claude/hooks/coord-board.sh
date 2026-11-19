#!/bin/bash
# SessionStart: put the coord board summary into the session's context, so every
# session starts knowing what else is in flight — open PRs and who works on them,
# claims without a PR, file overlaps, the CI queue, the merge train, and
# messages addressed to this agent. See docs/agent-coordination.md.
#
# Reuses a board up to 5 minutes old from the per-machine cache, so a burst of
# sessions costs GitHub one snapshot. Fails open and silent: no python3, neither gh
# nor a token, no auth, no network, a slow API — the session starts exactly as it
# would have. Exit 0 always.

EXIT_BUDGET=5  # coordlib/__init__.py

ROOT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || exit 0
COORD="$ROOT/Tools/coord/coord"
[ -x "$COORD" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
command -v gh >/dev/null 2>&1 || [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] || exit 0

errors=$(mktemp "${TMPDIR:-/tmp}/coord-board.XXXXXX" 2>/dev/null) || exit 0
trap 'rm -f "$errors"' EXIT

brief=$(cd "$ROOT" && "$COORD" board --brief --max-age 300 2>"$errors"); rc=$?
budget=$(grep -m 1 'GitHub API budget low' "$errors" 2>/dev/null); budget=${budget#coord: }
case "$rc" in
  0) [ -n "$budget" ] && brief="$budget"$'\n'"$brief" ;;
  "$EXIT_BUDGET") brief=$budget ;;
  *) exit 0 ;;
esac
[ -n "$brief" ] || exit 0

python3 - "$brief" <<'PY' 2>/dev/null || exit 0
import json, sys
brief = sys.argv[1].rstrip()
print(json.dumps({
    "systemMessage": brief.splitlines()[0],
    "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief},
}))
PY
exit 0

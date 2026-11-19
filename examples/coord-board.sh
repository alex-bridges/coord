#!/bin/bash
# Claude Code SessionStart hook: injects `coord board --brief` into each new session's
# context so agents start aware of open PRs, claims, file overlaps, CI, and unread mail.
#
# Reuses a cached board up to 5 minutes old. Fails open and silent if coord, python3,
# auth, or the network is unavailable. Exit 0 always.

EXIT_BUDGET=5

ROOT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || exit 0
COORD="$(command -v coord 2>/dev/null || true)"
[ -n "$COORD" ] || exit 0
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

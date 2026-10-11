#!/bin/bash
# Install (or remove) the conductor's watcher on this Mac: a launchd agent that runs
# `coord conductor tick` every few minutes from a checkout kept at origin/main.
#
#   Tools/coord/conductor/install.sh              install or refresh
#   Tools/coord/conductor/install.sh --uninstall  stop and remove the launchd agent
#
# Options: --home DIR (the conductor's checkout; default ~/conductor/INISA, made as a
# detached worktree of this repository), --interval SECONDS (default 300).
# --no-wake installs model-free ticks and does not require Claude; --wake enables
# model wakes. Refreshes preserve the installed choice; new installs enable wakes.
# Every CI box runs one; the lease in coord makes exactly one of them the conductor.
# See Docs/agent-coordination.md, "The conductor".
set -euo pipefail

LABEL="com.inisa.conductor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/inisa-conductor.log"
HOME_DIR="$HOME/conductor/INISA"
INTERVAL=300
UNINSTALL=0
WAKE_MODE=preserve

while [ $# -gt 0 ]; do
  case "$1" in
    --home|--interval)
      [ $# -ge 2 ] || { echo "install.sh: $1 needs a value" >&2; exit 1; }
      if [ "$1" = --home ]; then HOME_DIR="$2"; else INTERVAL="$2"; fi
      shift 2 ;;
    --no-wake) WAKE_MODE=no-wake; shift ;;
    --wake) WAKE_MODE=wake; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 1 ;;
  esac
done

DOMAIN="gui/$(id -u)"
if [ "$UNINSTALL" = 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (the checkout at $HOME_DIR and the state in ~/.local/state/inisa-conductor are kept)"
  exit 0
fi

[[ "$INTERVAL" =~ ^[1-9][0-9]*$ ]] || { echo "install.sh: interval must be a positive integer" >&2; exit 1; }
for tool in git python3 gh; do
  command -v "$tool" >/dev/null || { echo "install.sh: $tool is not on PATH" >&2; exit 1; }
done
GIT="$(command -v git)"
PY="$(command -v python3)"
WAKE_MODE="$("$PY" - "$PLIST" "$WAKE_MODE" <<'PY'
import pathlib
import plistlib
import sys

path, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
if mode == "preserve":
    installed = plistlib.loads(path.read_bytes()) if path.exists() else {}
    mode = "no-wake" if "--no-wake" in installed.get("ProgramArguments", []) else "wake"
print(mode)
PY
)"
if [ "$WAKE_MODE" = wake ]; then
  command -v claude >/dev/null || { echo "install.sh: claude is not on PATH (use --no-wake for model-free ticks)" >&2; exit 1; }
fi
gh auth status >/dev/null 2>&1 || { echo "install.sh: gh is not authenticated" >&2; exit 1; }

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if [ ! -d "$HOME_DIR/.git" ] && [ ! -f "$HOME_DIR/.git" ]; then
  mkdir -p "$(dirname "$HOME_DIR")"
  git -C "$REPO" fetch -q origin main
  git -C "$REPO" worktree add -q --detach "$HOME_DIR" origin/main
  echo "made the conductor's checkout: $HOME_DIR (detached at origin/main)"
fi

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
AGENT="conductor@$HOST:conductor"
# launchd starts jobs with a bare PATH; retain every required tool's directory.
# Git may live separately from gh/Python and is needed for the watcher's self-update.
JOB_PATH="$(dirname "$GIT"):$(dirname "$(command -v gh)"):$(dirname "$PY"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ "$WAKE_MODE" = wake ]; then
  JOB_PATH="$(dirname "$(command -v claude)"):$JOB_PATH"
fi

# Check the code that will run: bring a clean, detached checkout to origin/main first.
if [ -n "$(git -C "$HOME_DIR" status --porcelain)" ]; then
  echo "install.sh: WARNING: $HOME_DIR has local changes; checking it as it is, not at origin/main" >&2
elif git -C "$HOME_DIR" symbolic-ref -q HEAD >/dev/null; then
  echo "install.sh: WARNING: $HOME_DIR is on a branch; the watcher expects it detached at origin/main" >&2
elif ! { git -C "$HOME_DIR" fetch -q origin main && git -C "$HOME_DIR" checkout -q --detach FETCH_HEAD; }; then
  echo "install.sh: WARNING: could not fetch origin/main; checking the checkout as it is" >&2
fi
echo "checking a dry-run tick as $AGENT at $(git -C "$HOME_DIR" rev-parse --short HEAD) ..."
# Build and check the candidate before stopping the installed job. plistlib keeps
# paths and environment values literal, including XML and shell metacharacters.
mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
TMP_PLIST="$(mktemp "$PLIST.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
"$PY" - "$PLIST" "$TMP_PLIST" "$LABEL" "$PY" "$HOME_DIR" "$JOB_PATH" "$HOME" "$AGENT" "$INTERVAL" "$LOG" "$WAKE_MODE" <<'PY'
import pathlib
import plistlib
import sys

old_path, new_path, label, python, checkout, path, home, agent, interval, log, mode = sys.argv[1:]
old = pathlib.Path(old_path)
installed = plistlib.loads(old.read_bytes()) if old.exists() else {}
# Keep operator settings (including CONDUCTOR_WAKE/CONDUCTOR_TRAIN) on refresh.
environment = dict(installed.get("EnvironmentVariables", {}))
environment.update(PATH=path, HOME=home, COORD_AGENT=agent, CONDUCTOR_HOME=checkout)
arguments = [python, str(pathlib.Path(checkout) / "Tools/coord/coord"), "conductor", "tick"]
if mode == "no-wake":
    arguments.append("--no-wake")
candidate = {
    "Label": label,
    "ProgramArguments": arguments,
    "WorkingDirectory": checkout,
    "EnvironmentVariables": environment,
    "StartInterval": int(interval),
    "RunAtLoad": True,
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
pathlib.Path(new_path).write_bytes(plistlib.dumps(candidate))
PY
CHECK="$("$PY" - "$TMP_PLIST" <<'PY'
import os
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    candidate = plistlib.load(handle)
os.chdir(candidate["WorkingDirectory"])
arguments = candidate["ProgramArguments"] + ["--dry-run"]
os.execve(arguments[0], arguments, dict(os.environ, **candidate["EnvironmentVariables"]))
PY
)"
echo "$CHECK"
case "$CHECK" in
  *"another tick is running"*) echo "install.sh: a tick is running; try again when it ends" >&2; exit 1 ;;
esac

# Only now replace a running installation: a failed check above leaves it as it was.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
mv "$TMP_PLIST" "$PLIST"

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "installed $LABEL: every ${INTERVAL}s ($WAKE_MODE), log $LOG"
echo "status: Tools/coord/coord conductor status   stop: $0 --uninstall"

#!/bin/bash
# Install (or remove) the conductor's watcher on this Mac: a launchd agent that runs
# `coord conductor tick` every few minutes from a checkout kept at the repository's default branch.
#
#   conductor/install.sh --repo owner/name              install or refresh
#   conductor/install.sh --repo owner/name --uninstall  stop and remove the launchd agent
#
# Options: --repo OWNER/NAME (required), --home DIR (the conductor's checkout; default
# ~/conductor/OWNER/NAME, cloned from the repository and kept detached at the default branch),
# --interval SECONDS (default 300). coord runs from this script's own directory.
# --no-wake installs model-free ticks and does not require Claude; --wake enables
# model wakes. Refreshes preserve the installed choice; new installs enable wakes.
# Every CI box runs one; the lease in coord makes exactly one of them the conductor.
# See README.md, "Conductor".
set -euo pipefail

REPO=""
HOME_DIR=""
INTERVAL=300
UNINSTALL=0
WAKE_MODE=preserve

while [ $# -gt 0 ]; do
  case "$1" in
    --repo|--home|--interval)
      [ $# -ge 2 ] || { echo "install.sh: $1 needs a value" >&2; exit 1; }
      case "$1" in --repo) REPO="$2" ;; --home) HOME_DIR="$2" ;; *) INTERVAL="$2" ;; esac
      shift 2 ;;
    --no-wake) WAKE_MODE=no-wake; shift ;;
    --wake) WAKE_MODE=wake; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 1 ;;
  esac
done

[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "install.sh: --repo owner/name is required" >&2; exit 1; }
case "/$REPO/" in
  */./*|*/../*) echo "install.sh: expected a repository owner and name" >&2; exit 1 ;;
esac
# '+' is outside the repo-name alphabet, so the separator is unambiguous.
REPO_SLUG="$(LC_ALL=C tr '[:upper:]' '[:lower:]' <<< "${REPO//\//+}")"
LEGACY_SLUG="${REPO//\//-}"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.coord.conductor.$LEGACY_SLUG.plist"
if [ -f "$LEGACY_PLIST" ]; then
  # Keep an installed job's name only when its own configuration names this repo.
  REPO_SLUG="$(python3 - "$LEGACY_PLIST" "$REPO" "$REPO_SLUG" <<'PYTHON'
import plistlib
import sys

path, repo, slug = sys.argv[1:]
with open(path, "rb") as handle:
    installed_repo = plistlib.load(handle).get("EnvironmentVariables", {}).get("COORD_REPO")
if isinstance(installed_repo, str) and installed_repo.lower() == repo.lower():
    slug = installed_repo.replace("/", "-")
print(slug)
PYTHON
)"
fi
LABEL="com.coord.conductor.$REPO_SLUG"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/coord-conductor-$REPO_SLUG.log"
[ -n "$HOME_DIR" ] || HOME_DIR="$HOME/conductor/$REPO"
COORD="$(cd "$(dirname "$0")/.." && pwd)/coord"

DOMAIN="gui/$(id -u)"
if [ "$UNINSTALL" = 1 ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (the checkout at $HOME_DIR and the conductor's state directory are kept)"
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
gh auth status >/dev/null 2>&1 || { echo "install.sh: gh is not authenticated" >&2; exit 1; }

[ -x "$COORD" ] || [ -f "$COORD" ] || { echo "install.sh: cannot find coord next to this script ($COORD)" >&2; exit 1; }
NEW_CHECKOUT=0
if [ ! -d "$HOME_DIR/.git" ] && [ ! -f "$HOME_DIR/.git" ]; then
  mkdir -p "$(dirname "$HOME_DIR")"
  gh repo clone "$REPO" "$HOME_DIR" -- -q
  NEW_CHECKOUT=1
fi
"$PY" -I "$(dirname "$COORD")/conductor/check-origin.py" "$HOME_DIR" "$REPO"
if [ "$NEW_CHECKOUT" = 1 ]; then
  git -C "$HOME_DIR" checkout -q --detach HEAD
  echo "made the conductor's checkout: $HOME_DIR (detached at the default branch)"
fi

HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
AGENT="conductor@$HOST:conductor"
# launchd starts jobs with a bare PATH; retain every required tool's directory.
# Git may live separately from gh/Python and is needed for the watcher's self-update.
JOB_PATH="$(dirname "$GIT"):$(dirname "$(command -v gh)"):$(dirname "$PY"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Check the code that will run: bring a clean, detached checkout to the remote default branch first.
if [ -n "$(git -C "$HOME_DIR" status --porcelain)" ]; then
  echo "install.sh: WARNING: $HOME_DIR has local changes; checking it as it is" >&2
elif git -C "$HOME_DIR" symbolic-ref -q HEAD >/dev/null; then
  echo "install.sh: WARNING: $HOME_DIR is on a branch; the watcher expects it detached at the default branch" >&2
elif ! { git -C "$HOME_DIR" fetch -q origin HEAD && git -C "$HOME_DIR" checkout -q --detach FETCH_HEAD; }; then
  echo "install.sh: WARNING: could not update from the default branch; checking the checkout as it is" >&2
fi
echo "checking a dry-run tick as $AGENT at $(git -C "$HOME_DIR" rev-parse --short HEAD) ..."
# Build and check the candidate before stopping the installed job. plistlib keeps
# paths and environment values literal, including XML and shell metacharacters.
mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
TMP_PLIST="$(mktemp "$PLIST.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
"$PY" - "$PLIST" "$TMP_PLIST" "$LABEL" "$PY" "$HOME_DIR" "$JOB_PATH" "$HOME" "$AGENT" "$INTERVAL" "$LOG" "$WAKE_MODE" "$COORD" "$REPO" <<'PY'
import os
import pathlib
import plistlib
import shutil
import sys

old_path, new_path, label, python, checkout, path, home, agent, interval, log, mode, coord, repo = sys.argv[1:]
old = pathlib.Path(old_path)
installed = plistlib.loads(old.read_bytes()) if old.exists() else {}
# Keep operator settings (including CONDUCTOR_WAKE/CONDUCTOR_TRAIN) on refresh.
environment = dict(installed.get("EnvironmentVariables", {}))
environment.update(PATH=path, HOME=home, COORD_AGENT=agent, CONDUCTOR_HOME=checkout, COORD_REPO=repo)
if mode == "wake":
    # Validate the command selected by the candidate's preserved configuration,
    # without running it. Resolve relative paths from the job's working directory.
    configured = environment.get("CONDUCTOR_CLAUDE")
    os.chdir(checkout)
    executable = shutil.which(configured or "claude")
    if executable is None:
        if configured:
            sys.exit(f"install.sh: CONDUCTOR_CLAUDE is not executable: {configured}")
        sys.exit("install.sh: claude is not on PATH (use --no-wake for model-free ticks)")
    # A configured command name may live outside the other tools' directories.
    # Persist its resolved directory so launchd selects the same executable.
    environment["PATH"] = os.path.dirname(os.path.abspath(executable)) + os.pathsep + path
arguments = [python, coord, "conductor", "tick"]
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
echo "status: coord conductor status   stop: $0 --uninstall"

#!/bin/bash
# Install (or remove) the conductor's watcher on this Mac: a launchd agent that runs
# `coord conductor tick` every few minutes from a checkout kept at origin/main.
#
#   Tools/coord/conductor/install.sh              install or refresh
#   Tools/coord/conductor/install.sh --uninstall  stop and remove the launchd agent
#
# Options: --home DIR (the conductor's checkout; default ~/conductor/INISA, made as a
# detached worktree of this repository), --interval SECONDS (default 300).
# Every CI box runs one; the lease in coord makes exactly one of them the conductor.
# See Docs/agent-coordination.md, "The conductor".
set -euo pipefail

LABEL="com.inisa.conductor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/inisa-conductor.log"
HOME_DIR="$HOME/conductor/INISA"
INTERVAL=300
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_DIR="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 1 ;;
  esac
done

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "$UNINSTALL" = 1 ]; then
  rm -f "$PLIST"
  echo "removed $LABEL (the checkout at $HOME_DIR and the state in ~/.local/state/inisa-conductor are kept)"
  exit 0
fi

for tool in git python3 gh claude; do
  command -v "$tool" >/dev/null || { echo "install.sh: $tool is not on PATH" >&2; exit 1; }
done
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
PY="$(command -v python3)"
# launchd starts jobs with a bare PATH; give the watcher the one gh and claude run with here.
JOB_PATH="$(dirname "$(command -v claude)"):$(dirname "$(command -v gh)"):$(dirname "$PY"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HOME_DIR/Tools/coord/coord</string>
    <string>conductor</string>
    <string>tick</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$JOB_PATH</string>
    <key>HOME</key><string>$HOME</string>
    <key>COORD_AGENT</key><string>$AGENT</string>
    <key>CONDUCTOR_HOME</key><string>$HOME_DIR</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

echo "checking a dry-run tick as $AGENT ..."
(cd "$HOME_DIR" && env PATH="$JOB_PATH" COORD_AGENT="$AGENT" CONDUCTOR_HOME="$HOME_DIR" \
  "$PY" Tools/coord/coord conductor tick --dry-run)

launchctl bootstrap "$DOMAIN" "$PLIST"
echo "installed $LABEL: every ${INTERVAL}s, log $LOG"
echo "status: Tools/coord/coord conductor status   stop: $0 --uninstall"

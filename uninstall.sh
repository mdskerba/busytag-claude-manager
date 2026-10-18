#!/bin/sh
# Remove the busytag-claude hooks and launchd agent. Config and logs are kept.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.busytag.claude"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
cd "$ROOT"
"$ROOT/.venv/bin/python" -m busytag_claude.hooks_config uninstall
echo "Removed hooks and launchd agent. You can reopen the Busy Tag app."

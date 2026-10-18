#!/bin/sh
# Install busytag-claude: venv, default config, Claude Code hooks, launchd agent.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
LABEL="com.busytag.claude"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONF_DIR="$HOME/.config/busytag-claude"
LOG_DIR="$HOME/Library/Logs/busytag-claude"

echo "==> Python environment"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install -q --disable-pip-version-check -r "$ROOT/requirements.txt"

echo "==> Config ($CONF_DIR/config.json)"
mkdir -p "$CONF_DIR" "$LOG_DIR"
[ -f "$CONF_DIR/config.json" ] || cp "$ROOT/config.example.json" "$CONF_DIR/config.json"

echo "==> Claude Code hooks (~/.claude/settings.json, backup written)"
cd "$ROOT"
"$PY" -m busytag_claude.hooks_config install "'$PY' '$ROOT/busytag_claude/hook.py'"

echo "==> launchd agent ($PLIST)"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>busytag_claude.daemon</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/daemon.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/daemon.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "==> Settings app (~/Applications/Busy Tag Settings.app)"
"$ROOT/make_settings_app.sh"

echo "==> Busy Tag app plugin (dist/ClaudeSessions.busytagplugin)"
"$ROOT/make_plugin.sh" >/dev/null
TRANSPORT=$("$PY" -c 'from busytag_claude import config; print(config.load()["transport"])')
if [ "$TRANSPORT" = app ]; then
  open -a BusyTag "$ROOT/dist/ClaudeSessions.busytagplugin"
  echo "Done. In the Busy Tag app, enable the Claude Sessions plugin."
else
  echo "Done. Quit the Busy Tag app so the daemon can take the device."
fi
echo "Status: curl -s http://127.0.0.1:47621/status   Log: $LOG_DIR/daemon.log"

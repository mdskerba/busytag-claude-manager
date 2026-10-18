#!/bin/sh
# Build "Busy Tag Settings.app" in ~/Applications: double-click it to open the LED settings page.
# It starts the daemon first if it isn't running, so it works with Claude closed.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Busy Tag Settings.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Busy Tag Settings</string>
  <key>CFBundleIdentifier</key><string>com.busytag.claude.settings</string>
  <key>CFBundleExecutable</key><string>busytag-settings</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF
cat > "$APP/Contents/MacOS/busytag-settings" <<EOF
#!/bin/sh
cd "$ROOT"
PORT=\$("$ROOT/.venv/bin/python" -c 'from busytag_claude import config; print(config.load()["http_port"])')
URL="http://127.0.0.1:\$PORT/"
LABEL="com.busytag.claude"
if ! curl -s -m 1 "\${URL}settings" >/dev/null; then
  launchctl kickstart "gui/\$(id -u)/\$LABEL" 2>/dev/null \\
    || launchctl bootstrap "gui/\$(id -u)" "\$HOME/Library/LaunchAgents/\$LABEL.plist" 2>/dev/null || true
  i=0
  while [ \$i -lt 40 ] && ! curl -s -m 1 "\${URL}settings" >/dev/null; do sleep 0.25; i=\$((i + 1)); done
fi
open "\$URL"
EOF
chmod +x "$APP/Contents/MacOS/busytag-settings"
echo "Built $APP"

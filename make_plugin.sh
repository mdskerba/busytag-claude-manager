#!/bin/sh
# Package the "Claude Sessions" plugin for the Busy Tag app as dist/ClaudeSessions.busytagplugin.
# Open that file to install it in the app (used with "transport": "app").
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/busytag-plugin/ClaudeSessions"
OUT="$ROOT/dist/ClaudeSessions.busytagplugin"

[ -f "$SRC/icon.png" ] || "$ROOT/.venv/bin/python" -c '
import sys
from PIL import Image, ImageDraw
from busytag_claude import render
im = Image.new("RGB", (640 // render.S * render.S, 640 // render.S * render.S), render.BG)
render._spark(ImageDraw.Draw(im), 640 // render.S / 2, 640 // render.S / 2, 640 // render.S * 0.36, render.CLAUDE)
im.save(sys.argv[1])
' "$SRC/icon.png"

mkdir -p "$ROOT/dist"
rm -f "$OUT"
(cd "$SRC" && zip -qr -X "$OUT" manifest.json main.html config.html script.js icon.png)
echo "$OUT"

#!/usr/bin/env python3
"""Claude Code status line: forwards the session limit to the busytag-claude daemon and prints it.

Claude Code passes rate_limits (from the headers of its latest API response) to the status line
command, so this is the only live source for the limit bar that costs no extra requests.
Stdlib only, and any failure just prints less.
"""
import json
import os
import sys
import time
import urllib.request


def main():
    try:
        limits = (json.load(sys.stdin).get("rate_limits") or {})
    except ValueError:
        return 0
    window = limits.get("five_hour")
    if not window:
        return 0
    try:
        port = int(os.environ.get("BUSYTAG_CLAUDE_PORT", "47621"))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/usage",
            data=json.dumps({"five_hour": window}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass
    line = f"5h {round(window.get('used_percentage') or 0)}%"
    if window.get("resets_at"):
        line += time.strftime(" · resets %H:%M", time.localtime(window["resets_at"]))
    week = limits.get("seven_day")
    if week:
        line += f" · 7d {round(week.get('used_percentage') or 0)}%"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

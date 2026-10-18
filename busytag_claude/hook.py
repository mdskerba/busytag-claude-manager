#!/usr/bin/env python3
"""Claude Code hook: forwards the event to the busytag-claude daemon and exits.

Stdlib only, never prints, always exits 0, so it cannot slow down or change a Claude session.
"""
import json
import os
import sys
import urllib.request

FIELDS = ("session_id", "hook_event_name", "cwd", "transcript_path", "notification_type", "tool_name", "error")


def main():
    try:
        payload = json.load(sys.stdin)
        event = {k: payload[k] for k in FIELDS if payload.get(k) is not None}
        port = int(os.environ.get("BUSYTAG_CLAUDE_PORT", "47621"))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/event",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

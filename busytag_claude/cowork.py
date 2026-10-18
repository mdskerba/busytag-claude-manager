"""Cowork sessions: tails each session's audit log and turns it into hook-style events.

Cowork runs Claude Code with a private config folder per session, so the hooks in
~/.claude/settings.json never fire there. The Claude app logs every stream message of a
Cowork session to <session>/audit.jsonl on this Mac; this reads the lines added since the
last poll and maps them to the hook events SessionStore already understands.
"""
import glob
import json
import logging
import os
import time

log = logging.getLogger(__name__)

COWORK_ROOT = os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions")
POLL_S = 1.0
META_REFRESH_S = 30
READ_LIMIT = 4 * 1024 * 1024  # bytes per file per poll; the rest is read next time
ID_PREFIX = "cowork:"


def hook_event_name(rec):
    """Hook event equivalent of one audit record, or None to ignore it."""
    t, sub = rec.get("type"), rec.get("subtype")
    if t == "user":
        return "UserPromptSubmit"
    if t == "assistant":
        return "PreToolUse"
    if t == "system":
        if sub == "permission_request":  # tool approval, and AskUserQuestion
            return "PermissionRequest"
        if sub in ("permission_response", "permission_denied", "permission_auto_approved"):
            return "PostToolUse"
        return None
    if t == "result":
        # A stopped turn ends with error_during_execution; that's not a failure to show.
        if rec.get("is_error") and sub != "error_during_execution":
            return "StopFailure"
        return "Stop"
    return None


def read_meta(path):
    """(title, folder, archived) from a session's local_<id>.json; Nones if unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        folders = data.get("userSelectedFolders") or []
        return data.get("title"), (folders[0] if folders else None), bool(data.get("isArchived"))
    except (OSError, ValueError, AttributeError):
        return None, None, False


class _Session:
    def __init__(self, offset):
        self.offset = offset
        self.active = False  # has sent events, so it may need a SessionEnd
        self.title, self.cwd, self.archived = None, None, False
        self.meta_read = -1e9


class CoworkWatcher:
    """Call poll() periodically; it calls emit(event) for each new status-relevant line.

    Logs that exist on the first poll are read from their end, so history isn't replayed;
    logs that appear later (new sessions) are read from the start.
    """

    def __init__(self, emit, root=COWORK_ROOT, clock=time.monotonic):
        self.emit, self.root, self.clock = emit, root, clock
        self.sessions = {}  # audit.jsonl path -> _Session
        self.first_poll = True

    def poll(self):
        now = self.clock()
        for path in glob.glob(os.path.join(glob.escape(self.root), "*", "*", "local_*", "audit.jsonl")):
            s = self.sessions.get(path)
            if s is None:
                s = self.sessions[path] = _Session(self._size(path) if self.first_poll else 0)
            for rec in self._read_new(path, s):
                name = hook_event_name(rec)
                if name:
                    self._send(path, s, name, rec, now)
        self.first_poll = False
        for path, s in list(self.sessions.items()):
            if s.active and now - s.meta_read >= META_REFRESH_S:
                self._refresh_meta(path, s, now)
                if s.archived or not os.path.exists(path):
                    self.emit({"session_id": self._sid(path), "hook_event_name": "SessionEnd"})
                    s.active = False

    def run(self, stop, interval=POLL_S):
        while not stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("cowork poll failed")
            stop.wait(interval)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _sid(path):
        return ID_PREFIX + os.path.basename(os.path.dirname(path))

    @staticmethod
    def _size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _read_new(self, path, s):
        size = self._size(path)
        if size < s.offset:  # truncated or replaced
            s.offset = 0
        if size == s.offset:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(s.offset)
                data = f.read(READ_LIMIT)
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []  # a line still being written
        s.offset += end + 1
        out = []
        for line in data[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _refresh_meta(self, path, s, now):
        s.meta_read = now
        title, folder, s.archived = read_meta(os.path.dirname(path) + ".json")
        s.title = title or s.title
        s.cwd = folder or os.path.dirname(path)

    def _send(self, path, s, name, rec, now):
        if not s.active or now - s.meta_read >= META_REFRESH_S:
            self._refresh_meta(path, s, now)
        s.active = True
        event = {"session_id": self._sid(path), "hook_event_name": name, "cwd": s.cwd}
        if s.title:
            event["title"] = s.title
        if rec.get("tool_name"):
            event["tool_name"] = rec["tool_name"]
        if name == "StopFailure":
            event["error"] = "cowork_result_error"
        self.emit(event)

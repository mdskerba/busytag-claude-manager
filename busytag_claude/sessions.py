"""Claude Code session store: maps hook events to session statuses."""
import json
import os
import time
from dataclasses import asdict, dataclass, field

# Lower number = more urgent. Drives the aggregate LED color.
PRIORITY = {"needs_input": 0, "error": 1, "working": 2, "done": 3, "idle": 4}

TITLE_TAIL_BYTES = 512 * 1024
TITLE_REFRESH_S = 30
PRESETS_PATH = os.path.expanduser("~/Library/Application Support/Claude/ccd-session-presets.json")
PRESETS_REFRESH_S = 30
# Folders the Claude app makes for sessions started without a project folder ("No folder" in the app).
APP_FOLDERS = tuple(os.path.expanduser(f"~/Library/Application Support/Claude/{d}/")
                    for d in ("scratch-workspaces", "side-session-notes"))
NO_FOLDER = "No folder"
# Cowork sessions started without a folder run in a folder under here.
COWORK_DIR = os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions/")
COWORK = "Cowork"


def read_custom_title(transcript_path):
    """Return the last `custom-title` from a transcript (desktop sessions set it), or None."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TITLE_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if b"custom-title" not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            return entry["customTitle"].strip()
    return None


def project_root(cwd):
    """Project folder for a cwd; a worktree under <project>/.claude/worktrees/ counts as its project."""
    return (cwd or "").rstrip("/").split("/.claude/worktrees/")[0]


def project_name(cwd):
    root = project_root(cwd)
    if (root + "/").startswith(APP_FOLDERS):
        return NO_FOLDER
    if (root + "/").startswith(COWORK_DIR):
        return COWORK
    return os.path.basename(root)


def read_preset_names(path=PRESETS_PATH):
    """{folder: name} from the Claude desktop app's session presets; {} if unavailable."""
    try:
        with open(path) as f:
            data = json.load(f)
        out = {}
        for account in data.get("byAccount", {}).values():
            for p in account.get("presets", []):
                cwd = (p.get("target") or {}).get("cwd")
                if p.get("name") and cwd:
                    out[cwd.rstrip("/")] = p["name"]
        return out
    except (OSError, ValueError, AttributeError):
        return {}


def _under(root, folder):
    return root == folder or root.startswith(folder + "/")


class ProjectNamer:
    """Display name for a session's project, first match wins:
    1. `aliases` from config: a folder path (it and its subfolders) or a bare folder name;
    2. the desktop app's preset name for that folder (or a parent folder);
    3. the folder name.
    """

    def __init__(self, aliases=None, presets_reader=read_preset_names, clock=time.monotonic):
        self.paths, self.names = {}, {}
        for key, name in (aliases or {}).items():
            if "/" in key or key.startswith("~"):
                self.paths[os.path.expanduser(key).rstrip("/")] = name
            else:
                self.names[key] = name
        self.presets_reader, self.clock = presets_reader, clock
        self.presets, self.presets_read = {}, -1e9

    def _deepest(self, root, mapping):
        hits = [f for f in mapping if _under(root, f)]
        return mapping[max(hits, key=len)] if hits else None

    def __call__(self, cwd):
        root = project_root(cwd)
        base = project_name(cwd)
        if self.clock() - self.presets_read >= PRESETS_REFRESH_S:
            self.presets, self.presets_read = self.presets_reader(), self.clock()
        return self._deepest(root, self.paths) or self.names.get(base) or self._deepest(root, self.presets) or base


@dataclass
class Session:
    id: str
    cwd: str = ""
    transcript_path: str = ""
    status: str = "idle"
    first_seen: float = 0.0
    last_event: float = 0.0
    status_since: float = 0.0
    pending_permission: bool = False
    seen_permission_request: bool = False
    title: str | None = None
    title_checked: float = field(default=-1e9)


TIMES = ("first_seen", "last_event", "status_since")


class SessionStore:
    """Sessions survive daemon restarts through `state_path`: a session waiting for input sends
    no events, so one dropped on restart would never come back."""

    def __init__(self, title_reader=read_custom_title, project_namer=project_name, state_path=None,
                 wall_clock=time.time):
        self.sessions: dict[str, Session] = {}
        self.title_reader = title_reader
        self.project = lambda s: project_namer(s.cwd)
        self.state_path = state_path
        self.wall_clock = wall_clock

    def save(self, now):
        """Write sessions out; times are stored as wall-clock, since `now` is monotonic."""
        if not self.state_path:
            return
        offset = self.wall_clock() - now
        rows = []
        for s in self.sessions.values():
            d = asdict(s)
            for k in TIMES:
                d[k] += offset
            d.pop("title_checked")
            rows.append(d)
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(rows, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def load(self, now):
        if not self.state_path:
            return
        offset = now - self.wall_clock()
        try:
            with open(self.state_path) as f:
                rows = json.load(f)
            for d in rows:
                for k in TIMES:
                    d[k] += offset
                s = Session(**d)
                self.sessions[s.id] = s
        except (OSError, ValueError, TypeError, KeyError):
            self.sessions = {}

    def apply(self, event, now):
        """Apply one hook payload. Returns (session_id, old_status, new_status) or None."""
        sid = event.get("session_id")
        name = event.get("hook_event_name")
        if not sid or not name:
            return None
        if name == "SessionEnd":
            s = self.sessions.pop(sid, None)
            return (sid, s.status, None) if s else None

        s = self.sessions.get(sid)
        if s is None:
            s = Session(id=sid, first_seen=now, status_since=now)
            self.sessions[sid] = s
        s.cwd = event.get("cwd") or s.cwd
        s.transcript_path = event.get("transcript_path") or s.transcript_path
        s.title = event.get("title") or s.title  # Cowork sends its title; hooks don't
        s.last_event = now

        new = self._next_status(s, name, event)
        if new is None or new == s.status:
            return None
        old, s.status, s.status_since = s.status, new, now
        return (sid, old, new)

    @staticmethod
    def _next_status(s, name, event):
        if name == "SessionStart":
            return None if s.status != "idle" else "idle"
        if name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
            s.pending_permission = False
            return "working"
        if name == "PermissionRequest":
            s.pending_permission = True
            s.seen_permission_request = True
            return "needs_input"
        if name == "Notification":
            if event.get("notification_type") != "permission_prompt":
                return None  # idle_prompt only means the turn ended a while ago
            # The notification lags PermissionRequest by a few seconds; ignore it if
            # the prompt was already answered.
            if s.seen_permission_request and not s.pending_permission:
                return None
            return "needs_input"
        if name == "Stop":
            s.pending_permission = False
            return "done"
        if name == "StopFailure":
            s.pending_permission = False
            return "error"
        return None

    def prune(self, now, idle_timeout_s, busy_timeout_s):
        """Drop sessions with no events for a while (busy ones get longer)."""
        dropped = []
        for sid, s in list(self.sessions.items()):
            limit = busy_timeout_s if s.status in ("working", "needs_input") else idle_timeout_s
            if now - s.last_event > limit:
                dropped.append(self.sessions.pop(sid))
        return dropped

    def refresh_titles(self, now):
        for s in self.sessions.values():
            if now - s.title_checked >= TITLE_REFRESH_S:
                s.title_checked = now
                s.title = self.title_reader(s.transcript_path) or s.title

    def ordered(self):
        return sorted(self.sessions.values(), key=lambda s: (s.first_seen, s.id))

    def names(self):
        """Short display names; sessions sharing a name get ' #n' in first-seen order."""
        base = {}
        for s in self.ordered():
            base[s.id] = s.title or self.project(s) or s.id[:8]
        counts = {}
        for n in base.values():
            counts[n] = counts.get(n, 0) + 1
        seen, out = {}, {}
        for s in self.ordered():
            n = base[s.id]
            if counts[n] > 1:
                seen[n] = seen.get(n, 0) + 1
                n = f"{n} #{seen[n]}"
            out[s.id] = n
        return out

    def aggregate_status(self):
        """Most urgent status among sessions that have started a turn, or None."""
        statuses = [s.status for s in self.sessions.values() if s.status != "idle"]
        return min(statuses, key=PRIORITY.__getitem__) if statuses else None

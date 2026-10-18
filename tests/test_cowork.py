import json

from busytag_claude.cowork import CoworkWatcher, hook_event_name
from busytag_claude.sessions import COWORK, SessionStore, project_name


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_session(root, sid="local_abc", title="Sponsor research", folders=(), archived=False):
    space = root / "acct" / "space"
    d = space / sid
    d.mkdir(parents=True, exist_ok=True)
    (space / f"{sid}.json").write_text(json.dumps(
        {"title": title, "userSelectedFolders": list(folders), "isArchived": archived}))
    audit = d / "audit.jsonl"
    audit.touch()
    return audit


def append(audit, *records):
    with open(audit, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def watcher(root):
    events, clock = [], Clock()
    return CoworkWatcher(events.append, root=str(root), clock=clock), events, clock


def names(events):
    return [e["hook_event_name"] for e in events]


def test_record_mapping():
    assert hook_event_name({"type": "user"}) == "UserPromptSubmit"
    assert hook_event_name({"type": "assistant"}) == "PreToolUse"
    assert hook_event_name({"type": "system", "subtype": "permission_request"}) == "PermissionRequest"
    assert hook_event_name({"type": "system", "subtype": "permission_response"}) == "PostToolUse"
    assert hook_event_name({"type": "result", "subtype": "success", "is_error": False}) == "Stop"
    assert hook_event_name({"type": "result", "subtype": "success", "is_error": True}) == "StopFailure"
    assert hook_event_name({"type": "result", "subtype": "error_during_execution", "is_error": True}) == "Stop"
    assert hook_event_name({"type": "system", "subtype": "thinking_tokens"}) is None
    assert hook_event_name({"type": "rate_limit_event"}) is None


def test_existing_history_not_replayed(tmp_path):
    audit = make_session(tmp_path)
    append(audit, {"type": "user"}, {"type": "result", "is_error": False})
    w, events, _ = watcher(tmp_path)
    w.poll()
    assert events == []
    append(audit, {"type": "user"})
    w.poll()
    assert names(events) == ["UserPromptSubmit"]


def test_new_session_read_from_start_with_title_and_folder(tmp_path):
    w, events, _ = watcher(tmp_path)
    w.poll()
    audit = make_session(tmp_path, folders=["/Users/u/acme-web"])
    append(audit, {"type": "user"}, {"type": "system", "subtype": "permission_request", "tool_name": "AskUserQuestion"})
    w.poll()
    assert names(events) == ["UserPromptSubmit", "PermissionRequest"]
    e = events[1]
    assert e["session_id"] == "cowork:local_abc"
    assert e["title"] == "Sponsor research"
    assert e["cwd"] == "/Users/u/acme-web"
    assert e["tool_name"] == "AskUserQuestion"


def test_partial_line_waits_for_newline(tmp_path):
    w, events, _ = watcher(tmp_path)
    w.poll()
    audit = make_session(tmp_path)
    with open(audit, "a") as f:
        f.write('{"type": "us')
    w.poll()
    assert events == []
    with open(audit, "a") as f:
        f.write('er"}\n')
    w.poll()
    assert names(events) == ["UserPromptSubmit"]


def test_archived_session_ends(tmp_path):
    w, events, clock = watcher(tmp_path)
    w.poll()
    audit = make_session(tmp_path)
    append(audit, {"type": "user"}, {"type": "result", "is_error": False})
    w.poll()
    make_session(tmp_path, archived=True)
    clock.t = 31
    w.poll()
    assert names(events) == ["UserPromptSubmit", "Stop", "SessionEnd"]
    clock.t = 62
    w.poll()
    assert names(events)[-1] == "SessionEnd" and len(events) == 3  # ended once


def test_quiet_session_without_events_never_ends(tmp_path):
    w, events, clock = watcher(tmp_path)
    make_session(tmp_path, archived=True)
    w.poll()
    clock.t = 100
    w.poll()
    assert events == []


def test_events_drive_session_store(tmp_path):
    store = SessionStore(title_reader=lambda p: None)
    w, events, _ = watcher(tmp_path)
    w.emit = lambda e: store.apply(e, 0)
    w.poll()
    audit = make_session(tmp_path)
    append(audit, {"type": "user"}, {"type": "system", "subtype": "permission_request"})
    w.poll()
    s = store.sessions["cowork:local_abc"]
    assert s.status == "needs_input"
    assert store.names()["cowork:local_abc"] == "Sponsor research"
    append(audit, {"type": "system", "subtype": "permission_response"}, {"type": "result", "is_error": True})
    w.poll()
    assert s.status == "error"


def test_folderless_cowork_project_name(tmp_path):
    from busytag_claude import sessions
    assert project_name(sessions.COWORK_DIR + "acct/space/local_abc") == COWORK

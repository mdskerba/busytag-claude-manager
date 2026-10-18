import os
import json

from busytag_claude.sessions import ProjectNamer, SessionStore, project_name, read_custom_title, read_preset_names


def ev(name, sid="s1", **kw):
    return {"session_id": sid, "hook_event_name": name, "cwd": "/Users/u/acme-web", **kw}


def make():
    return SessionStore(title_reader=lambda p: None)


def test_full_turn_lifecycle():
    st = make()
    assert st.apply(ev("SessionStart"), 0) == ("s1", "idle", "idle") or st.sessions["s1"].status == "idle"
    assert st.apply(ev("UserPromptSubmit"), 1) == ("s1", "idle", "working")
    assert st.apply(ev("PreToolUse", tool_name="Write"), 2) is None  # still working
    assert st.apply(ev("PermissionRequest", tool_name="Write"), 3) == ("s1", "working", "needs_input")
    assert st.apply(ev("PostToolUse"), 4) == ("s1", "needs_input", "working")
    assert st.apply(ev("Stop"), 5) == ("s1", "working", "done")
    assert st.apply(ev("SessionEnd"), 6) == ("s1", "done", None)
    assert st.sessions == {}


def test_late_permission_notification_ignored_after_answer():
    st = make()
    st.apply(ev("PermissionRequest"), 1)
    st.apply(ev("PostToolUse"), 2)  # user answered quickly
    assert st.apply(ev("Notification", notification_type="permission_prompt"), 5) is None
    assert st.sessions["s1"].status == "working"


def test_permission_notification_alone_means_needs_input():
    st = make()
    st.apply(ev("UserPromptSubmit"), 1)
    assert st.apply(ev("Notification", notification_type="permission_prompt"), 2)[2] == "needs_input"


def test_idle_prompt_does_not_change_status():
    st = make()
    st.apply(ev("Stop"), 1)
    assert st.apply(ev("Notification", notification_type="idle_prompt"), 61) is None
    assert st.sessions["s1"].status == "done"


def test_stop_failure_is_error():
    st = make()
    st.apply(ev("UserPromptSubmit"), 1)
    assert st.apply(ev("StopFailure", error="model_not_found"), 2)[2] == "error"


def test_aggregate_priority():
    st = make()
    st.apply(ev("Stop", "a"), 1)
    st.apply(ev("UserPromptSubmit", "b"), 1)
    assert st.aggregate_status() == "working"
    st.apply(ev("StopFailure", "c"), 1)
    assert st.aggregate_status() == "error"
    st.apply(ev("PermissionRequest", "d"), 1)
    assert st.aggregate_status() == "needs_input"


def test_names_use_folder_and_disambiguate():
    st = make()
    st.apply(ev("SessionStart", "a"), 1)
    st.apply(ev("SessionStart", "b"), 2)
    st.apply({**ev("SessionStart", "c"), "cwd": "/x/Other/"}, 3)
    assert st.names() == {"a": "acme-web #1", "b": "acme-web #2", "c": "Other"}


def test_title_preferred_over_folder(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": "hi"},
        {"type": "custom-title", "customTitle": "Old title"},
        {"type": "custom-title", "customTitle": "Busy tag device integration"},
        {"type": "assistant"},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert read_custom_title(str(t)) == "Busy tag device integration"
    st = SessionStore()
    st.apply(ev("SessionStart", transcript_path=str(t)), 0)
    st.refresh_titles(0)
    assert st.names()["s1"] == "Busy tag device integration"


def test_missing_transcript_falls_back():
    assert read_custom_title("/nonexistent/x.jsonl") is None
    assert read_custom_title(None) is None


def test_prune_uses_longer_timeout_for_busy_sessions():
    st = make()
    st.apply(ev("Stop", "done"), 0)
    st.apply(ev("PermissionRequest", "waiting"), 0)
    st.prune(31 * 60, idle_timeout_s=30 * 60, busy_timeout_s=120 * 60)
    assert list(st.sessions) == ["waiting"]


def test_ignores_malformed_events():
    st = make()
    assert st.apply({}, 0) is None
    assert st.apply({"session_id": "x"}, 0) is None
    assert st.sessions == {}


def test_project_name_maps_worktrees_to_their_project():
    assert project_name("/Users/u/acme-web/") == "acme-web"
    assert project_name("/Users/u/acme-web/.claude/worktrees/fix-auth") == "acme-web"
    assert project_name("/Users/u/acme-web/.claude/worktrees/fix-auth/web") == "acme-web"
    assert project_name("") == ""


def test_app_made_folders_show_as_no_folder():
    app = os.path.expanduser("~/Library/Application Support/Claude")
    assert project_name(f"{app}/scratch-workspaces/a/b/scratch-2026-10-16-ad0c98") == "No folder"
    assert project_name(f"{app}/side-session-notes/local_8fb9fedf") == "No folder"
    assert ProjectNamer(presets_reader=dict, clock=lambda: 0)(f"{app}/side-session-notes/local_x") == "No folder"


def test_project_namer_precedence():
    presets = {"/Users/u/acme-web": "Acme Web", "/Users/u/demo-api": "Demo API"}
    namer = ProjectNamer(
        {"/Users/u/side-project": "Side Project", "demo-api": "API alias"},
        presets_reader=lambda: presets,
        clock=lambda: 0,
    )
    assert namer("/Users/u/side-project/busytag-claude") == "Side Project"  # path covers subfolders
    assert namer("/Users/u/demo-api") == "API alias"  # config beats app preset
    assert namer("/Users/u/acme-web/.claude/worktrees/x/web") == "Acme Web"  # app preset
    assert namer("/Users/u/Other") == "Other"  # folder fallback
    assert namer("/Users/u/side-project-old") == "side-project-old"  # no partial-name match


def test_untitled_session_uses_project_name():
    st = SessionStore(title_reader=lambda p: None, project_namer=lambda cwd: "Acme Web")
    st.apply(ev("SessionStart"), 1)
    assert st.names() == {"s1": "Acme Web"}


def test_read_preset_names(tmp_path):
    p = tmp_path / "presets.json"
    p.write_text(json.dumps({"byAccount": {"a": {"presets": [
        {"name": "Side Project", "target": {"kind": "local", "cwd": ""}},
        {"name": "acme-web", "target": {"kind": "local", "cwd": "/Users/u/acme-web/"}},
    ]}}}))
    assert read_preset_names(str(p)) == {"/Users/u/acme-web": "acme-web"}
    assert read_preset_names(str(tmp_path / "missing.json")) == {}


def test_waiting_session_survives_restart(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = SessionStore(title_reader=lambda p: None, state_path=path, wall_clock=lambda: 5000.0)
    store.apply({"session_id": "a", "hook_event_name": "UserPromptSubmit", "cwd": "/x/proj"}, 100.0)
    store.apply({"session_id": "a", "hook_event_name": "PermissionRequest"}, 110.0)
    store.save(110.0)

    # restarted: monotonic clock starts over, 30 s of wall time later
    restarted = SessionStore(title_reader=lambda p: None, state_path=path, wall_clock=lambda: 5030.0)
    restarted.load(7.0)
    s = restarted.sessions["a"]
    assert (s.status, s.cwd, s.pending_permission) == ("needs_input", "/x/proj", True)
    assert s.status_since == 7.0 - 30


def test_unreadable_state_starts_empty(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("not json")
    store = SessionStore(state_path=str(path))
    store.load(0.0)
    assert store.sessions == {}

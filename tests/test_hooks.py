import json
import os
import subprocess
import sys
import time

from busytag_claude import hooks_config

HOOK = os.path.join(os.path.dirname(__file__), "..", "busytag_claude", "hook.py")


def test_install_keeps_other_hooks_and_uninstall_restores(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    original = {
        "model": "haiku",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
    }
    settings.write_text(json.dumps(original))
    monkeypatch.setattr(hooks_config, "SETTINGS", str(settings))

    cmd = "'/v/python' '/x/busytag_claude/hook.py'"
    hooks_config.install(cmd)
    hooks_config.install(cmd)  # idempotent
    cfg = json.loads(settings.read_text())
    assert cfg["model"] == "haiku"
    assert set(hooks_config.EVENTS) <= set(cfg["hooks"])
    stop_cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds == ["say done", cmd]
    assert cfg["hooks"]["PermissionRequest"][0]["matcher"] == "*"

    hooks_config.uninstall()
    assert json.loads(settings.read_text()) == original
    assert list(tmp_path.glob("settings.json.bak-busytag-*"))


def test_hook_is_silent_fast_and_exits_zero_without_daemon():
    payload = json.dumps({"session_id": "x", "hook_event_name": "Stop", "prompt": "secret"})
    env = dict(os.environ, BUSYTAG_CLAUDE_PORT="1")  # nothing listens there
    t = time.monotonic()
    r = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True, env=env)
    elapsed = time.monotonic() - t
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""
    assert elapsed < 0.5, elapsed


def test_hook_survives_garbage_input():
    r = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""

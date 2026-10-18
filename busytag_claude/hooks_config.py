"""Add or remove the busytag-claude hook (and status line) in ~/.claude/settings.json.

Usage: python -m busytag_claude.hooks_config install|uninstall <hook command>
Other hooks in the file are left untouched, and so is a status line of your own (the limit bar
then falls back to polling); a timestamped backup is written first.
"""
import json
import os
import shutil
import sys
import time

SETTINGS = os.path.expanduser("~/.claude/settings.json")
MARKER = "busytag_claude/hook.py"
STATUSLINE_MARKER = "busytag_claude/statusline.py"
EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "Notification", "Stop", "StopFailure", "SessionEnd",
]
MATCHER_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"}


def _load():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(cfg):
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, f"{SETTINGS}.bak-busytag-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)


def _strip(hooks):
    """Remove our entries; drop groups and events left empty."""
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            g = dict(g, hooks=[h for h in g.get("hooks", []) if MARKER not in h.get("command", "")])
            if g["hooks"]:
                groups.append(g)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def install(command):
    cfg = _load()
    hooks = cfg.setdefault("hooks", {})
    _strip(hooks)
    entry = {"type": "command", "command": command, "timeout": 5}
    for event in EVENTS:
        group = {"hooks": [entry]}
        if event in MATCHER_EVENTS:
            group = {"matcher": "*", **group}
        hooks.setdefault(event, []).append(group)
    if not cfg.get("statusLine") or _ours(cfg["statusLine"]):
        cfg["statusLine"] = {"type": "command", "command": command.replace("hook.py", "statusline.py")}
    _save(cfg)


def _ours(statusline):
    return STATUSLINE_MARKER in statusline.get("command", "")


def uninstall():
    cfg = _load()
    hooks = cfg.get("hooks", {})
    _strip(hooks)
    if not hooks:
        cfg.pop("hooks", None)
    if cfg.get("statusLine") and _ours(cfg["statusLine"]):
        del cfg["statusLine"]
    _save(cfg)


if __name__ == "__main__":
    if sys.argv[1:2] == ["install"] and len(sys.argv) == 3:
        install(sys.argv[2])
    elif sys.argv[1:2] == ["uninstall"]:
        uninstall()
    else:
        sys.exit(__doc__)

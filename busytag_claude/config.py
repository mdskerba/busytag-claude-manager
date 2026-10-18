"""User config: ~/.config/busytag-claude/config.json merged over defaults."""
import json
import logging
import os

log = logging.getLogger(__name__)

CONFIG_PATH = os.path.expanduser("~/.config/busytag-claude/config.json")
SESSIONS_PATH = os.path.expanduser("~/.config/busytag-claude/sessions.json")
USAGE_CACHE_PATH = os.path.expanduser("~/.config/busytag-claude/usage-cache.json")
LOG_DIR = os.path.expanduser("~/Library/Logs/busytag-claude")
DEFAULT_HTTP_PORT = 47621

# Keys older configs may still carry; accepted and ignored.
RETIRED = {"rotation_seconds"}

DEFAULTS = {
    "http_port": DEFAULT_HTTP_PORT,
    # "usb": the daemon owns the USB port. "app": the Busy Tag app keeps it and shows our
    # screen through its "Claude Sessions" plugin, so its other integrations keep running.
    "transport": "usb",
    "device_port": None,  # e.g. "/dev/cu.usbmodemXXXX"; None = auto-detect by VID:PID
    "flash_on_input": True,
    "flash_color": "FF5500",
    "flash_times": 2,
    "flash_fade": 40,  # device fade time per step (larger = softer)
    "flash_on_ms": 250,  # hold at full / dark between fades
    "working_spinner": True,  # dim blue dot circling the unlit LEDs while working
    "spinner_color": "000C38",
    "spinner_fade": 360,  # ~7.6 s per lap with the colour above
    # While one session waits for input and another works, this LED (0-6) stays the
    # needs_input colour and the working dot circles the others.
    "input_led": 0,
    "brightness": None,  # 0-100; None = leave device setting alone
    "colors": {
        "needs_input": "FF5500",
        "error": "300000",  # dim red; entering it flashes bright red first
        "working": "0040FF",
        "done": "063000",
        "idle": "000000",
        "none": "000000",
    },
    "idle_timeout_minutes": 30,
    "busy_timeout_minutes": 120,
    "reconnect_seconds": 2,
    "port_busy_retry_seconds": 10,
    # Share the device with the Busy Tag app: quit it while a session is working or
    # needs input, reopen it after this many quiet minutes (null = never hand back).
    "manage_busytag_app": True,
    "busytag_app_name": "BusyTag",
    "handback_after_minutes": 3,
    # Project label on screen, e.g. {"~/side-project": "Side Project"} or {"acme-web": "Acme Web"}.
    # A path also covers its subfolders. Unlisted projects use the Claude app's preset name, else the folder name.
    "project_names": {},
    # Bar under the session count: 5-hour session limit used, and time until it resets.
    "usage_bar": True,
    "usage_poll_seconds": 120,
    # Also show Cowork sessions (read from their logs; Cowork doesn't run Claude Code hooks).
    "cowork": True,
}


# What the settings page may change, and which LED states it shows.
LED_STATES = ("working", "needs_input", "done", "error")
SETTINGS = ("colors", "spinner_color", "flash_color", "flash_on_input", "working_spinner")


def settings(cfg):
    """The LED settings the settings page edits."""
    return {"colors": {s: cfg["colors"][s] for s in LED_STATES}, **{k: cfg[k] for k in SETTINGS[1:]}}


def check_settings(changes):
    """Validated copy of a settings-page change; raises ValueError on anything else."""
    def color(v):
        if not (isinstance(v, str) and len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v)):
            raise ValueError(f"not an RRGGBB colour: {v!r}")
        return v.upper()

    out = {}
    for k, v in changes.items():
        if k == "colors":
            if not isinstance(v, dict) or not set(v) <= set(LED_STATES):
                raise ValueError("colors must map LED states to colours")
            out[k] = {s: color(c) for s, c in v.items()}
        elif k in ("spinner_color", "flash_color"):
            out[k] = color(v)
        elif k in ("flash_on_input", "working_spinner"):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
            out[k] = v
        else:
            raise ValueError(f"unknown setting {k!r}")
    return out


def save(changes, path=CONFIG_PATH):
    """Merge `changes` into the user's config file, keeping everything else in it."""
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    for k, v in changes.items():
        if k == "colors":
            user.setdefault("colors", {}).update(v)
        else:
            user[k] = v
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(user, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load(path=CONFIG_PATH):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable config %s: %s", path, e)
        return cfg
    for k, v in user.items():
        if k == "colors" and isinstance(v, dict):
            cfg["colors"].update(v)
        elif k in cfg:
            cfg[k] = v
        elif k in RETIRED:
            pass
        else:
            log.warning("unknown config key %r", k)
    return cfg

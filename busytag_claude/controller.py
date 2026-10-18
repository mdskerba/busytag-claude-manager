"""Decides what the Busy Tag shows and keeps the device in sync with the session store."""
import logging
import threading

from . import render
from .device import DeviceError, PortBusy
from .sessions import PRIORITY

log = logging.getLogger(__name__)

FILES = ("cc_status_a.png", "cc_status_b.png")
# Sessions in these states make us take the device from the Busy Tag app.
ACTIVE = ("working", "needs_input")
APP_QUIT_RETRY_S = 1.5
APP_QUIT_COOLDOWN_S = 15
LED_COUNT = 7
PREVIEW_S = 4  # a colour previewed from the settings page stays on the ring this long


ONLY_THIS_LED = 128  # mask bit: light only the given LED, others off (as in busytag-lib's "running")


def spinner_steps(color, fade):
    """One dim LED gliding around the ring; the device fades between steps."""
    return [(ONLY_THIS_LED | (1 << i), color, fade, 0) for i in range(LED_COUNT)]


def waiting_spinner_steps(color, fade, input_color, input_led):
    """The working dot circling every LED but `input_led`, which holds `input_color`: one
    session waits for input while another works. Each step sets only its own LEDs."""
    ring = [i for i in range(LED_COUNT) if i != input_led]
    steps = [(1 << input_led, input_color, 0, 0)]
    for n, i in enumerate(ring):
        steps += [(1 << i, color, fade // 2, 0), (1 << ring[n - 1], "000000", fade // 2, 0)]
    return steps


FLASHES = ("needs_input", "error")  # states the ring flashes on entering
WAITING_WORKING = "needs_input+working"  # LED mode: a session waits while another works


def full_brightness(color):
    """The same hue with its brightest channel at FF: "300000" -> "FF0000"."""
    rgb = [int(color[i:i + 2], 16) for i in (0, 2, 4)]
    m = max(rgb)
    return "".join(f"{round(v * 255 / m):02X}" for v in rgb) if m else "FFFFFF"


def flash_steps(color, fade, hold_ms):
    return [(127, color, fade, hold_ms), (127, "000000", fade, hold_ms)]


class Controller:
    def __init__(self, store, device, cfg, renderer=render, app=None, usage=None):
        self.store = store
        self.usage = usage  # UsagePoller, or None to leave out the session limit bar
        self.device = device
        self.cfg = cfg
        self.render = renderer
        self.app = app  # BusyTagApp, or None to never touch the Busy Tag app
        self.lock = threading.Lock()
        self.pending_flash = None  # "needs_input" or "error": the ring flashes once on entering it
        self.preview = None  # (colour, until) from the settings page
        # sharing the device with the Busy Tag app
        self.handed_back = False
        self.quiet_since = None
        self.app_quit_at = -1e9
        # device state cache (reset on every connect)
        self._reset_device_cache()
        self.next_connect_at = 0.0
        self.busy_logged = False
        self.missing_logged = False

    def _reset_device_cache(self):
        self.shown_key = None
        self.displayed_file = None
        self.led_mode = None
        self.pattern_loaded = None
        self.flash_until = 0.0

    # -- input from the HTTP thread --------------------------------------

    def on_event(self, event, now):
        with self.lock:
            change = self.store.apply(event, now)
            if change:
                self.store.save(now)
            if change and change[2] in FLASHES:
                self.pending_flash = change[2]
            return change

    # -- input from the settings page --------------------------------------

    def preview_led(self, color, now):
        """Show `color` on the whole ring for a few seconds, then go back to the session's status."""
        with self.lock:
            self.preview = (color, now + PREVIEW_S)

    def apply_settings(self, changes):
        """Take new LED settings now: the next tick redraws the ring and reloads its pattern."""
        with self.lock:
            for k, v in changes.items():
                if k == "colors":
                    self.cfg["colors"].update(v)
                else:
                    self.cfg[k] = v
            self.led_mode = None
            self.pattern_loaded = None

    # -- view -------------------------------------------------------------

    def compute_view(self, now):
        """Return (screen_key, led_status). screen_key is ('idle',) or a session tuple;
        led_status is the shown session's status.

        The screen only moves on a send or a response: it shows the session whose status
        changed last (the one waiting longest is pinned while any need input). Sessions
        that were opened but never sent a prompt stay off screen, and a title that arrives
        mid-turn is picked up at the next status change, not redrawn on its own.

        With sessions in 2+ projects the key ends with an overview: the shown session's
        project first, then the others by urgency, as (project, most urgent status,
        ((name, status, is_current), ...)). Otherwise it ends with None. After that comes the
        session limit as (percent used, minutes until reset), or None when unknown.
        """
        sessions = [s for s in self.store.ordered() if s.status != "idle"]
        if not sessions:
            return ("idle",), None
        needs = sorted((s for s in sessions if s.status == "needs_input"), key=lambda s: s.status_since)
        current = needs[0] if needs else max(sessions, key=lambda s: s.status_since)
        shown = self.shown_key
        if shown and shown[0] == "session" and shown[1] == current.id and shown[3] == current.status:
            name = shown[2]
        else:
            name = self.store.names()[current.id]
        project = self.store.project(current)
        group = sum(1 for s in sessions if self.store.project(s) == project)
        key = ("session", current.id, name, current.status, len(sessions) - 1, len(needs), project, group,
               self._overview(sessions, current, name), self.usage.limit() if self.usage else None)
        # The LED ring follows the session on screen (a waiting session is pinned there), but
        # keeps showing other sessions' work with the input LED held orange.
        if needs and any(s.status == "working" for s in sessions):
            return key, WAITING_WORKING
        return key, current.status

    def _overview(self, sessions, current, current_name):
        by_project = {}
        for s in sessions:
            by_project.setdefault(self.store.project(s), []).append(s)
        if len(by_project) < 2:
            return None
        names = self.store.names()
        groups = []
        for project, members in by_project.items():
            top = min((s.status for s in members), key=PRIORITY.__getitem__)
            rows = tuple(
                (current_name if s is current else names[s.id], s.status, s is current) for s in members
            )
            highlighted = current in members
            latest = max(s.status_since for s in members)
            groups.append(((not highlighted, PRIORITY[top], -latest), (project, top, rows)))
        return tuple(g for _, g in sorted(groups, key=lambda g: g[0]))

    # -- main loop step ---------------------------------------------------

    def tick(self, now):
        with self.lock:
            if self.store.prune(now, self.cfg["idle_timeout_minutes"] * 60, self.cfg["busy_timeout_minutes"] * 60):
                self.store.save(now)
            self.store.refresh_titles(now)
            key, led_status = self.compute_view(now)
            # Only flash while the shown session is still in that state (it may sit pending
            # during a takeover).
            flash_status = "needs_input" if led_status == WAITING_WORKING else led_status
            flash = self.pending_flash if self.pending_flash == flash_status else None
            if not flash:
                self.pending_flash = None
            active = any(s.status in ACTIVE for s in self.store.ordered())

        if active:
            self.quiet_since = None
            self.handed_back = False
        elif self.quiet_since is None:
            self.quiet_since = now
        if self.handed_back:
            return
        if self._should_hand_back(now):
            self._hand_back()
            return

        if not self._ensure_connected(now, active):
            return
        try:
            self._sync(key, led_status, flash, now)
        except DeviceError as e:
            log.warning("device error, reconnecting: %s", e)
            self.device.close()
            self.next_connect_at = now + self.cfg["reconnect_seconds"]
            return
        if flash:
            with self.lock:
                self.pending_flash = None

    # -- sharing with the Busy Tag app -----------------------------------

    def _should_hand_back(self, now):
        minutes = self.cfg.get("handback_after_minutes")
        return (
            self.app is not None
            and minutes is not None
            and self.device.is_open
            and self.quiet_since is not None
            and now - self.quiet_since >= minutes * 60
        )

    def _hand_back(self):
        log.info("no active Claude sessions; handing the device back to the Busy Tag app")
        try:
            self.device.set_color("000000")  # don't leave the ring lit with nobody driving it
        except DeviceError:
            pass
        self.device.close()
        self.handed_back = True
        self.app.launch()

    def _ensure_connected(self, now, active=True):
        if self.device.is_open:
            return True
        if now < self.next_connect_at:
            return False
        try:
            self.device.open()
        except PortBusy:
            if self.app is not None and not active:
                # Nothing to show; the Busy Tag app can keep the device.
                self.handed_back = True
                return False
            if self.app is not None and now - self.app_quit_at >= APP_QUIT_COOLDOWN_S:
                log.info("Claude session active; quitting the Busy Tag app to take the device")
                self.app.quit()
                self.app_quit_at = now
                self.next_connect_at = now + APP_QUIT_RETRY_S
                return False
            if self.app is not None and now - self.app_quit_at < APP_QUIT_COOLDOWN_S:
                self.next_connect_at = now + APP_QUIT_RETRY_S
                return False
            if not self.busy_logged:
                log.warning("Busy Tag port is busy. Quit the Busy Tag app to enable Claude status.")
                self.busy_logged = True
            self.next_connect_at = now + self.cfg["port_busy_retry_seconds"]
            return False
        except DeviceError as e:
            if not self.missing_logged:
                log.warning("Busy Tag not available (%s); retrying until it's plugged in", e)
                self.missing_logged = True
            self.next_connect_at = now + self.cfg["reconnect_seconds"]
            return False
        self.busy_logged = False
        self.missing_logged = False
        self._reset_device_cache()
        self.displayed_file = self.device.current_picture()
        if self.cfg.get("brightness") is not None:
            self.device.set_brightness(self.cfg["brightness"])
        return True

    def _sync(self, key, led_status, flash, now):
        if key != self.shown_key:
            if key[0] == "idle":
                png = self.render.render_idle()
            else:
                _, _, name, status, others, waiting, project, group, overview, limit = key
                png = self.render.render_session(name, status, others, waiting, project, group, overview, limit)
            target = FILES[1] if self.displayed_file == FILES[0] else FILES[0]
            self.device.upload(target, png)
            self.device.show(target)
            self.displayed_file = target
            self.shown_key = key
            log.info("screen -> %s", key)

        if flash == "error" or (flash == "needs_input" and self.cfg["flash_on_input"]):
            self._load_pattern(flash)
            self.device.events.clear()
            self.device.play_pattern(self.cfg["flash_times"])
            # Any AT+SC stops the pattern, so hold LED updates until the device reports
            # the pattern finished (+evn:PP,0), with a time cap in case the event is missed.
            self.flash_until = now + self.cfg["flash_times"] * 2 + 2
            self.led_mode = None
            log.info("flash x%d", self.cfg["flash_times"])

        if self.flash_until:
            if now < self.flash_until and "+evn:PP,0" not in self.device.poll_events():
                return
            self.flash_until = 0.0
        if self.preview:
            color, until = self.preview
            if now < until:
                if self.led_mode != ("preview", color):
                    self.device.set_color(color)  # also stops the spinner
                    self.led_mode = ("preview", color)
                return
            self.preview = None
            self.led_mode = None
        mode = led_status or "none"
        if mode == self.led_mode:
            return
        if mode == WAITING_WORKING:
            led = 1 << self.cfg["input_led"]
            if self.cfg["working_spinner"]:
                self.device.set_color("000000")
                self._load_pattern(WAITING_WORKING)
                self.device.play_pattern(255)
            else:
                self.device.set_color(self.cfg["colors"]["working"], 127 & ~led)
                self.device.set_color(self.cfg["colors"]["needs_input"], led)
        elif mode == "working" and self.cfg["working_spinner"]:
            self.device.set_color("000000")  # unlit ring under the dot
            self._load_pattern("spinner")
            self.device.play_pattern(255)  # 255 = loop forever
        else:
            self.device.set_color(self.cfg["colors"][mode])  # also stops any pattern
        self.led_mode = mode

    def _load_pattern(self, name):
        """The device holds one custom pattern; reload it only when switching."""
        if self.pattern_loaded == name:
            return
        if name in FLASHES:
            # An error (e.g. the usage limit) flashes its own colour at full brightness, then
            # the ring holds the dimmer error colour.
            color = self.cfg["flash_color"] if name == "needs_input" else full_brightness(self.cfg["colors"]["error"])
            steps = flash_steps(color, self.cfg["flash_fade"], self.cfg["flash_on_ms"])
        elif name == WAITING_WORKING:
            steps = waiting_spinner_steps(self.cfg["spinner_color"], self.cfg["spinner_fade"],
                                          self.cfg["colors"]["needs_input"], self.cfg["input_led"])
        else:
            steps = spinner_steps(self.cfg["spinner_color"], self.cfg["spinner_fade"])
        self.device.set_pattern(steps)
        self.pattern_loaded = name

    def snapshot(self):
        with self.lock:
            names = self.store.names()
            return {
                "connected": self.device.is_open,
                "handed_back": self.handed_back,
                "port": getattr(self.device, "port", None),
                "shown": list(self.shown_key) if self.shown_key else None,
                "sessions": [
                    {"id": s.id, "name": names[s.id], "status": s.status, "cwd": s.cwd}
                    for s in self.store.ordered()
                ],
            }

    def shutdown(self):
        """Leave the device dark with the idle screen and release the port."""
        if not self.device.is_open:
            return
        try:
            png = self.render.render_idle()
            target = FILES[1] if self.displayed_file == FILES[0] else FILES[0]
            self.device.upload(target, png)
            self.device.show(target)
            self.device.set_color("000000")
        except DeviceError:
            pass
        self.device.close()

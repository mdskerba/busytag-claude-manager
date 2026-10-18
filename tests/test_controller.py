import json

import pytest

from busytag_claude import config
from busytag_claude.controller import FILES, WAITING_WORKING, Controller, flash_steps, spinner_steps, waiting_spinner_steps
from busytag_claude.device import DeviceError, PortBusy
from busytag_claude.sessions import SessionStore


class FakeRender:
    @staticmethod
    def render_session(name, status, others, waiting, project=None, group=1, overview=None, limit=None):
        return json.dumps([name, status, others, waiting]).encode()

    @staticmethod
    def render_idle():
        return b"idle"


class FakeDevice:
    def __init__(self, busy=False):
        self.is_open = False
        self.busy = busy
        self.calls = []
        self.files = {}
        self.showing = "app_image.png"
        self.fail_next = False
        self.events = []

    def poll_events(self):
        return self.events

    def finish_pattern(self):
        self.events.append("+evn:PP,0")

    def open(self):
        if self.busy:
            raise PortBusy("busy")
        self.is_open = True

    def close(self):
        self.is_open = False

    def _call(self, *c):
        if self.fail_next:
            self.fail_next = False
            raise DeviceError("unplugged")
        self.calls.append(c)

    def current_picture(self):
        return self.showing

    def upload(self, name, data):
        assert name != self.showing, "must never overwrite the picture on screen"
        self._call("upload", name)
        self.files[name] = data

    def show(self, name):
        self._call("show", name)
        self.showing = name

    def set_color(self, rgb, mask=127):
        self._call(*(("color", rgb) if mask == 127 else ("color", rgb, mask)))

    def set_brightness(self, p):
        self._call("brightness", p)

    def set_pattern(self, steps):
        self._call("pattern", tuple(steps))

    def play_pattern(self, repeat):
        self._call("play", repeat)

    def screen(self):
        data = self.files[self.showing]
        return data if data == b"idle" else tuple(json.loads(data))

    def kinds(self):
        return [c[0] for c in self.calls]


def ev(name, sid="s1", **kw):
    return {"session_id": sid, "hook_event_name": name, "cwd": f"/p/{sid}", **kw}


@pytest.fixture
def ctl():
    cfg = config.load("/nonexistent")
    dev = FakeDevice()
    return Controller(SessionStore(title_reader=lambda p: None), dev, cfg, renderer=FakeRender), dev


def test_idle_screen_when_no_sessions(ctl):
    c, dev = ctl
    c.tick(0)
    assert dev.screen() == b"idle"
    assert ("color", "000000") in dev.calls


def test_status_change_updates_screen_and_leds(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    assert dev.screen() == ("s1", "working", 0, 0)
    assert dev.calls[-3:] == [("color", "000000"), ("pattern", tuple(spinner_steps("000C38", 360))), ("play", 255)]
    c.on_event(ev("Stop"), 1)
    c.tick(1)
    assert dev.screen() == ("s1", "done", 0, 0)
    assert dev.calls[-1] == ("color", "063000")


def test_no_redraw_when_nothing_changes(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    n = len(dev.calls)
    for t in range(1, 4):
        c.on_event(ev("PostToolUse"), t)
        c.tick(t)
    assert len(dev.calls) == n


def test_alternates_files_and_never_overwrites_shown(ctl):
    c, dev = ctl
    for i, name in enumerate(["UserPromptSubmit", "Stop", "UserPromptSubmit", "StopFailure"]):
        c.on_event(ev(name), i)
        c.tick(i)
    uploads = [x[1] for x in dev.calls if x[0] == "upload"]
    assert uploads == [FILES[0], FILES[1], FILES[0], FILES[1]]


def test_flash_twice_once_per_transition(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.on_event(ev("PermissionRequest"), 1)
    c.on_event(ev("Notification", notification_type="permission_prompt"), 1.5)  # same transition
    c.tick(1.5)
    assert dev.calls.count(("play", 2)) == 1
    assert dev.screen()[1] == "needs_input"
    # LEDs are held while the pattern plays, until the device reports it finished
    before = len(dev.calls)
    c.tick(2.0)
    c.tick(3.0)
    assert len(dev.calls) == before
    dev.finish_pattern()
    c.tick(3.25)
    assert dev.calls[-1] == ("color", "FF5500")
    # further ticks never re-flash
    for t in range(4, 20):
        c.tick(t)
    assert dev.calls.count(("play", 2)) == 1


def test_flash_hold_has_time_cap_if_event_is_missed(ctl):
    c, dev = ctl
    c.on_event(ev("PermissionRequest"), 0)
    c.tick(0)
    c.tick(5.9)
    assert dev.calls[-1] == ("play", 2)
    c.tick(6.0)  # 2 flashes * 2 s + 2 s cap
    assert dev.calls[-1] == ("color", "FF5500")


def test_flash_pattern_is_two_soft_orange_flashes():
    assert flash_steps("FF5500", 40, 250) == [(127, "FF5500", 40, 250), (127, "000000", 40, 250)]

def test_pattern_reloaded_only_when_switching(ctl):
    c, dev = ctl
    for i, sid in enumerate("abc"):  # three sessions start waiting, one after another
        c.on_event(ev("PermissionRequest", sid), i * 10)
        c.tick(i * 10)
    assert dev.kinds().count("pattern") == 1
    assert dev.calls.count(("play", 2)) == 3
    for sid in "abc":
        c.on_event(ev("PostToolUse", sid), 40)
    c.tick(40)  # all working: spinner replaces the flash pattern
    c.on_event(ev("PostToolUse", "a"), 41)
    c.tick(41)
    assert dev.kinds().count("pattern") == 2
    assert dev.calls.count(("play", 255)) == 1


def test_spinner_walks_one_led_around_the_ring():
    steps = spinner_steps("000C38", 360)
    assert [mask for mask, _, _, _ in steps] == [129, 130, 132, 136, 144, 160, 192]  # 128 = only this LED
    assert {(c, f) for _, c, f, _ in steps} == {("000C38", 360)}
    assert len(steps) <= 40  # device pattern limit


def test_working_to_done_stops_spinner_with_solid_green(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.on_event(ev("Stop"), 1)
    c.tick(1)
    assert dev.calls[-1] == ("color", "063000")


def test_spinner_disabled_uses_solid_blue(ctl):
    c, dev = ctl
    c.cfg["working_spinner"] = False
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    assert dev.calls[-1] == ("color", "0040FF") and "play" not in dev.kinds()


def test_flash_disabled_goes_straight_to_amber(ctl):
    c, dev = ctl
    c.cfg["flash_on_input"] = False
    c.on_event(ev("PermissionRequest"), 0)
    c.tick(0)
    assert "play" not in dev.kinds()
    assert dev.calls[-1] == ("color", "FF5500")


def test_needs_input_is_pinned(ctl):
    c, dev = ctl
    for i, sid in enumerate(("a", "b", "c")):
        c.on_event(ev("UserPromptSubmit", sid), i)
    c.tick(2)
    assert dev.screen() == ("c", "working", 2, 0)
    c.on_event(ev("PermissionRequest", "a"), 3)
    c.tick(3)
    assert dev.screen() == ("a", "needs_input", 2, 1)
    c.on_event(ev("Stop", "b"), 4)  # a response elsewhere doesn't steal the screen
    for t in range(4, 30):
        c.tick(t)
        assert dev.screen()[0] == "a"


def test_longest_waiting_shown_first(ctl):
    c, dev = ctl
    for sid in ("a", "b", "c"):
        c.on_event(ev("UserPromptSubmit", sid), 0)
    c.on_event(ev("PermissionRequest", "c"), 1)
    c.on_event(ev("PermissionRequest", "b"), 2)
    c.tick(2)
    assert dev.screen() == ("c", "needs_input", 2, 2)
    c.on_event(ev("PostToolUse", "c"), 3)
    c.tick(3)
    assert dev.screen() == ("b", "needs_input", 2, 1)


def test_no_timed_rotation(ctl):
    c, dev = ctl
    for sid in ("a", "b"):
        c.on_event(ev("UserPromptSubmit", sid), 0)
    c.tick(0)
    uploads = dev.kinds().count("upload")
    for t in range(1, 120):
        c.tick(t)
    assert dev.kinds().count("upload") == uploads


def test_shows_session_that_changed_last(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit", "a"), 0)
    c.on_event(ev("UserPromptSubmit", "b"), 1)
    c.tick(1)
    assert dev.screen()[:2] == ("b", "working")
    c.on_event(ev("Stop", "a"), 2)  # a answered
    c.tick(2)
    assert dev.screen() == ("a", "done", 1, 0)
    c.on_event(ev("PostToolUse", "b"), 3)  # tool calls are not sends or responses
    c.tick(3)
    assert dev.screen()[0] == "a"


def test_opened_chat_without_prompt_stays_off_screen(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit", "a"), 0)
    c.on_event(ev("Stop", "a"), 1)
    c.tick(1)
    n = len(dev.calls)
    c.on_event(ev("SessionStart", "b"), 2)
    for t in range(2, 20):
        c.tick(t)
    assert len(dev.calls) == n and dev.screen() == ("a", "done", 0, 0)
    c.on_event(ev("UserPromptSubmit", "b"), 20)
    c.tick(20)
    assert dev.screen() == ("b", "working", 1, 0)


def test_only_new_session_shows_idle_screen(ctl):
    c, dev = ctl
    c.on_event(ev("SessionStart"), 0)
    c.tick(0)
    assert dev.screen() == b"idle"


def test_title_change_waits_for_next_status_change():
    titles = {"t": None}
    cfg = config.load("/nonexistent")
    dev = FakeDevice()
    c = Controller(SessionStore(title_reader=lambda p: titles["t"]), dev, cfg, renderer=FakeRender)
    c.on_event(ev("UserPromptSubmit", transcript_path="/x"), 0)
    c.tick(0)
    assert dev.screen()[0] == "s1"
    titles["t"] = "Nice title"
    c.tick(40)  # title refresh happens, but the turn is still running
    assert dev.screen()[0] == "s1"
    c.on_event(ev("Stop"), 41)
    c.tick(41)
    assert dev.screen() == ("Nice title", "done", 0, 0)


def test_port_busy_retries_without_crashing(ctl):
    c, dev = ctl
    dev.busy = True
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    assert dev.calls == []
    dev.busy = False
    c.tick(5)  # retry is 10 s
    assert dev.calls == []
    c.tick(10)
    assert dev.screen()[1] == "working"


def test_reconnect_redraws_after_unplug(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.on_event(ev("Stop"), 1)
    dev.fail_next = True
    c.tick(1)
    assert not dev.is_open
    c.tick(2)
    assert not dev.is_open  # waits reconnect_seconds
    c.tick(3)  # reconnect, redraw everything
    assert dev.is_open and dev.screen()[1] == "done"
    assert dev.calls[-1] == ("color", "063000")


# -- sharing the device with the Busy Tag app --------------------------------


class FakeApp:
    """Owns the port while running, like the real Busy Tag app."""

    def __init__(self, dev, running=True):
        self.dev = dev
        self.calls = []
        self.set_running(running)

    def set_running(self, running):
        self.running = running
        self.dev.busy = running

    def quit(self):
        self.calls.append("quit")
        self.set_running(False)

    def launch(self):
        self.calls.append("launch")
        self.set_running(True)


@pytest.fixture
def shared():
    cfg = config.load("/nonexistent")
    dev = FakeDevice()
    app = FakeApp(dev)
    c = Controller(SessionStore(title_reader=lambda p: None), dev, cfg, renderer=FakeRender, app=app)
    return c, dev, app


def test_app_keeps_device_while_nothing_is_active(shared):
    c, dev, app = shared
    c.tick(0)
    c.on_event(ev("Stop"), 1)
    for t in range(1, 30):
        c.tick(t)
    assert app.calls == [] and not dev.is_open


def test_takes_device_from_app_when_session_starts_working(shared):
    c, dev, app = shared
    c.tick(0)
    c.on_event(ev("UserPromptSubmit"), 1)
    c.tick(1)
    assert app.calls == ["quit"]
    c.tick(2.5)  # retry 1.5 s after the app was asked to quit
    assert dev.is_open and dev.screen()[1] == "working"


def test_flash_survives_takeover(shared):
    c, dev, app = shared
    c.tick(0)
    c.on_event(ev("PermissionRequest"), 1)
    c.tick(1)  # quits the app, not connected yet
    assert "play" not in dev.kinds()
    c.tick(2.6)
    assert dev.screen()[1] == "needs_input"
    assert ("play", 2) in dev.calls


def test_no_stale_flash_if_answered_before_takeover(shared):
    c, dev, app = shared
    c.tick(0)
    c.on_event(ev("PermissionRequest"), 1)
    c.tick(1)
    c.on_event(ev("PostToolUse"), 1.5)
    c.tick(2.6)
    assert dev.screen()[1] == "working" and ("play", 2) not in dev.calls


def test_hands_back_after_quiet_minutes_and_takes_over_again(shared):
    c, dev, app = shared
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.tick(1.5)
    assert dev.is_open
    c.on_event(ev("Stop"), 10)
    c.tick(10)
    c.tick(10 + 179)
    assert dev.is_open  # 3 minutes not reached
    c.tick(10 + 180)
    assert not dev.is_open and app.calls == ["quit", "launch"]
    c.tick(10 + 200)
    assert app.calls == ["quit", "launch"]  # stays with the app
    c.on_event(ev("UserPromptSubmit"), 300)
    c.tick(300)
    c.tick(301.5)
    assert app.calls == ["quit", "launch", "quit"] and dev.is_open


def test_working_session_is_never_handed_back(shared):
    c, dev, app = shared
    c.on_event(ev("UserPromptSubmit"), 0)
    for t in range(0, 3600, 30):
        c.on_event(ev("PostToolUse"), t)
        c.tick(t)
    assert app.calls == ["quit"] and dev.is_open


def test_handback_disabled(shared):
    c, dev, app = shared
    c.cfg["handback_after_minutes"] = None
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.tick(1.5)
    c.on_event(ev("Stop"), 2)
    for t in range(2, 3600, 60):
        c.tick(t)
    assert dev.is_open and app.calls == ["quit"]


def test_view_carries_project_and_group_size():
    store = SessionStore(title_reader=lambda p: None)
    ctl = Controller(store, FakeDevice(), config.DEFAULTS)
    for sid, cwd, t in (("a", "/u/acme-web", 1), ("b", "/u/acme-web/.claude/worktrees/x", 2), ("c", "/u/Other", 3)):
        store.apply({"session_id": sid, "hook_event_name": "UserPromptSubmit", "cwd": cwd}, t)
    key, _ = ctl.compute_view(4)
    assert key[1] == "c" and key[6:8] == ("Other", 1)
    store.apply({"session_id": "b", "hook_event_name": "Stop", "cwd": "/u/acme-web/.claude/worktrees/x"}, 5)
    key, _ = ctl.compute_view(6)
    assert key[1] == "b" and key[6:8] == ("acme-web", 2)


def test_overview_lists_highlighted_project_first_then_by_urgency():
    store = SessionStore(title_reader=lambda p: None)
    ctl = Controller(store, FakeDevice(), config.DEFAULTS)

    def send(sid, cwd, event, t):
        store.apply({"session_id": sid, "hook_event_name": event, "cwd": cwd}, t)

    send("a", "/u/Alpha", "UserPromptSubmit", 1)
    key, _ = ctl.compute_view(2)
    assert key[8] is None  # one project: single-session layout
    send("b", "/u/Beta", "UserPromptSubmit", 3)
    send("c", "/u/Gamma", "UserPromptSubmit", 4)
    send("c", "/u/Gamma", "PermissionRequest", 5)
    send("d", "/u/Beta", "UserPromptSubmit", 6)
    key, _ = ctl.compute_view(7)
    assert key[1] == "c"  # needs input is pinned
    assert [g[0] for g in key[8]] == ["Gamma", "Beta", "Alpha"]  # highlighted, then most recent
    assert key[8][0] == ("Gamma", "needs_input", (("Gamma", "needs_input", True),))
    assert key[8][1][2] == (("Beta #1", "working", False), ("Beta #2", "working", False))


def test_led_follows_the_shown_session():
    store = SessionStore(title_reader=lambda p: None)
    ctl = Controller(store, FakeDevice(), config.DEFAULTS)

    def send(sid, event, t):
        store.apply({"session_id": sid, "hook_event_name": event, "cwd": f"/u/{sid}"}, t)

    send("a", "UserPromptSubmit", 1)
    send("b", "UserPromptSubmit", 2)
    send("b", "Stop", 3)
    key, led = ctl.compute_view(4)
    assert key[1] == "b" and led == "done"  # not "working" from session a
    send("a", "PermissionRequest", 5)
    key, led = ctl.compute_view(6)
    assert key[1] == "a" and led == "needs_input"  # waiting session is pinned, so it still flashes


def test_error_flashes_bright_red_then_holds_dim_red(ctl):
    # e.g. StopFailure "rate_limit" when the usage limit is reached
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.on_event(ev("StopFailure", error="rate_limit"), 1)
    c.tick(1)
    assert ("pattern", tuple(flash_steps("FF0000", 40, 250))) in dev.calls
    assert dev.calls[-1] == ("play", 2)
    dev.finish_pattern()
    c.tick(2)
    assert dev.calls[-1] == ("color", "300000")  # dim red, not full brightness
    n = len(dev.calls)
    for t in range(3, 60):
        c.tick(t)
    assert len(dev.calls) == n  # flashes once per transition


def test_needs_input_still_flashes_orange_after_an_error(ctl):
    c, dev = ctl
    c.on_event(ev("StopFailure"), 0)
    c.tick(0)
    dev.finish_pattern()
    c.tick(1)
    c.on_event(ev("PermissionRequest"), 2)
    c.tick(2)
    assert dev.calls[-2:] == [("pattern", tuple(flash_steps("FF5500", 40, 250))), ("play", 2)]


def test_hand_back_turns_the_ring_off(shared):
    c, dev, app = shared
    c.on_event(ev("UserPromptSubmit"), 0)
    c.tick(0)
    c.tick(1.5)
    c.on_event(ev("StopFailure", error="rate_limit"), 10)
    c.tick(10)
    dev.finish_pattern()
    c.tick(11)
    c.tick(11 + 180)
    assert not dev.is_open and dev.calls[-1] == ("color", "000000")


def test_waiting_session_holds_input_led_while_another_works(ctl):
    c, dev = ctl
    c.on_event(ev("UserPromptSubmit", "a"), 0)
    c.on_event(ev("UserPromptSubmit", "b"), 0)
    c.on_event(ev("PermissionRequest", "a"), 1)
    c.tick(1)
    assert dev.calls[-1] == ("play", 2)  # flashes first
    dev.finish_pattern()
    c.tick(2)
    assert dev.screen()[:2] == ("a", "needs_input")  # screen stays on the waiting session
    steps = waiting_spinner_steps("000C38", 360, "FF5500", 0)
    assert dev.calls[-3:] == [("color", "000000"), ("pattern", tuple(steps)), ("play", 255)]
    for t in range(3, 60):  # and the ring keeps it until answered
        c.tick(t)
    assert dev.calls[-1] == ("play", 255)
    c.on_event(ev("PostToolUse", "a"), 60)
    c.tick(60)
    assert dev.calls[-1] == ("play", 255) and ("pattern", tuple(spinner_steps("000C38", 360))) in dev.calls


def test_waiting_led_without_spinner(ctl):
    c, dev = ctl
    c.cfg = dict(c.cfg, working_spinner=False, flash_on_input=False, input_led=3)
    c.on_event(ev("UserPromptSubmit", "b"), 0)
    c.on_event(ev("PermissionRequest", "a"), 1)
    c.tick(1)
    assert dev.calls[-2:] == [("color", "0040FF", 127 & ~8), ("color", "FF5500", 8)]


def test_waiting_spinner_skips_input_led():
    steps = waiting_spinner_steps("000C38", 360, "FF5500", 2)
    assert steps[0] == (4, "FF5500", 0, 0)
    assert all(mask != 4 for mask, *_ in steps[1:])
    lit = [mask for mask, color, *_ in steps[1:] if color != "000000"]
    assert lit == [1, 2, 8, 16, 32, 64]


def test_led_mode_mixes_waiting_and_working():
    store = SessionStore(title_reader=lambda p: None)
    ctl = Controller(store, FakeDevice(), config.DEFAULTS)
    store.apply({"session_id": "a", "hook_event_name": "PermissionRequest", "cwd": "/u/a"}, 1)
    assert ctl.compute_view(2)[1] == "needs_input"
    store.apply({"session_id": "b", "hook_event_name": "UserPromptSubmit", "cwd": "/u/b"}, 3)
    assert ctl.compute_view(4)[1] == WAITING_WORKING

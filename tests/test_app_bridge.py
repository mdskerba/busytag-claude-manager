import base64
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from busytag_claude import config
from busytag_claude.app_bridge import STALE_S, AppBridge, apply_mask
from busytag_claude.controller import Controller
from busytag_claude.daemon import EventLog, make_handler
from busytag_claude.device import DeviceError
from busytag_claude.sessions import SessionStore
from test_controller import FakeRender, ev


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def app():
    clock = Clock()
    bridge = AppBridge(clock=clock)
    cfg = config.load("/nonexistent")
    c = Controller(SessionStore(title_reader=lambda p: None), bridge, cfg, renderer=FakeRender)
    return c, bridge, clock


def step(c, bridge, clock, seconds=1, **poll):
    """The plugin polls, then the daemon ticks, `seconds` later."""
    clock.t += seconds
    frame = bridge.frame(**poll)
    c.tick(clock.t)
    return frame


def screen(frame):
    data = base64.b64decode(frame["image"])
    return data if data == b"idle" else tuple(json.loads(data))


def test_not_connected_until_the_plugin_polls(app):
    c, bridge, clock = app
    c.tick(0)
    assert not bridge.is_open
    with pytest.raises(DeviceError):
        bridge.open()
    assert "image" not in bridge.frame()


def test_plugin_gets_the_screen_and_ring(app):
    c, bridge, clock = app
    step(c, bridge, clock)
    c.on_event(ev("UserPromptSubmit"), clock.t)
    c.cfg["working_spinner"] = False
    step(c, bridge, clock)
    frame = bridge.frame()
    assert frame["connected"]
    assert screen(frame) == ("s1", "working", 0, 0)
    assert frame["led"] == {"ring": [c.cfg["colors"]["working"]] * 7}


def test_image_left_out_when_the_plugin_has_it(app):
    c, bridge, clock = app
    step(c, bridge, clock)
    step(c, bridge, clock)
    frame = bridge.frame()
    assert "image" not in bridge.frame(have_image=frame["image_seq"])
    assert "image" in bridge.frame(have_image=frame["image_seq"] - 1)


def test_needs_input_flash_ends_when_the_plugin_reports_it(app):
    c, bridge, clock = app
    step(c, bridge, clock)
    c.on_event(ev("PermissionRequest"), clock.t)
    step(c, bridge, clock)
    frame = bridge.frame()
    assert frame["led"]["repeat"] == c.cfg["flash_times"]
    assert frame["led"]["steps"][0][1] == c.cfg["flash_color"]
    step(c, bridge, clock, 0.5)
    assert "steps" in bridge.frame()["led"]  # still holding for the pattern
    step(c, bridge, clock, 0.5, done=frame["led_seq"])
    assert bridge.frame()["led"] == {"ring": [c.cfg["colors"]["needs_input"]] * 7}


def test_stale_done_report_is_ignored(app):
    c, bridge, clock = app
    step(c, bridge, clock)
    c.on_event(ev("PermissionRequest"), clock.t)
    step(c, bridge, clock)
    seq = bridge.frame()["led_seq"]
    bridge.frame(done=seq - 1)
    assert bridge.events == []


def test_plugin_off_screen_disconnects_and_redraws_on_return(app):
    c, bridge, clock = app
    step(c, bridge, clock)
    clock.t += STALE_S + 1
    c.tick(clock.t)
    assert not bridge.is_open
    seq = bridge.frame()["image_seq"]
    step(c, bridge, clock, c.cfg["reconnect_seconds"])
    assert bridge.frame()["image_seq"] == seq + 1  # everything re-sent after reconnecting


def test_masked_colours_keep_the_other_leds():
    ring = apply_mask(["000000"] * 7, "0040FF", 127 & ~1)
    assert apply_mask(ring, "FF5500", 1) == ["FF5500"] + ["0040FF"] * 6
    assert apply_mask(ring, "FF5500", 128 | 2) == ["000000", "FF5500"] + ["000000"] * 5


def test_split_ring_reaches_the_plugin(app):
    c, bridge, clock = app
    bridge.frame()
    bridge.open()
    bridge.set_color("0040FF", 127 & ~1)
    bridge.set_color("FF5500", 1)
    assert bridge.frame()["led"] == {"ring": ["FF5500"] + ["0040FF"] * 6}


def test_frame_endpoint(app):
    c, bridge, clock = app
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, EventLog("/dev/null")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/frame"
        json.load(urlopen(url))
        c.tick(clock.t)
        frame = json.load(urlopen(url))
        assert screen(frame) == b"idle"
        assert "image" not in json.load(urlopen(f"{url}?have={frame['image_seq']}&done=x"))
    finally:
        server.shutdown()

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from busytag_claude import config
from busytag_claude.controller import PREVIEW_S, Controller
from busytag_claude.daemon import make_handler
from busytag_claude.sessions import SessionStore

from test_controller import FakeDevice, FakeRender, ev


@pytest.fixture
def ctl():
    cfg = config.load("/nonexistent")
    dev = FakeDevice()
    return Controller(SessionStore(title_reader=lambda p: None), dev, cfg, renderer=FakeRender), dev


def test_check_settings_validates():
    assert config.check_settings({"colors": {"done": "0a3000"}, "working_spinner": False}) == {
        "colors": {"done": "0A3000"},
        "working_spinner": False,
    }
    for bad in ({"colors": {"idle": "000000"}}, {"flash_color": "red"}, {"flash_on_input": "yes"}, {"http_port": 1}):
        with pytest.raises(ValueError):
            config.check_settings(bad)


def test_save_merges_into_user_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"project_names": {"a": "A"}, "colors": {"error": "FF0000"}}))
    config.save({"colors": {"done": "001100"}, "flash_on_input": False}, str(path))
    saved = json.loads(path.read_text())
    assert saved == {"project_names": {"a": "A"}, "colors": {"error": "FF0000", "done": "001100"}, "flash_on_input": False}
    cfg = config.load(str(path))
    assert cfg["colors"]["done"] == "001100" and cfg["colors"]["working"] == "0040FF"


def test_preview_shows_then_returns_to_status(ctl):
    c, dev = ctl
    c.on_event(ev("Stop"), 0)
    c.tick(0)
    assert dev.calls[-1] == ("color", "063000")
    c.preview_led("FFFFFF", 1)
    c.tick(1)
    assert dev.calls[-1] == ("color", "FFFFFF")
    c.tick(2)
    assert dev.calls[-1] == ("color", "FFFFFF")  # not resent every tick
    n = len(dev.calls)
    c.tick(1 + PREVIEW_S)
    assert dev.calls[n:] == [("color", "063000")]


def test_new_settings_apply_at_once(ctl):
    c, dev = ctl
    c.on_event(ev("Stop"), 0)
    c.tick(0)
    c.apply_settings({"colors": {"done": "002200"}})
    c.tick(1)
    assert dev.calls[-1] == ("color", "002200")


@pytest.fixture
def server(ctl, tmp_path):
    c, dev = ctl
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(c, None, str(tmp_path / "config.json")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", c, tmp_path / "config.json"
    srv.shutdown()


def _post(url, body, **headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_settings_page_and_round_trip(server):
    url, c, path = server
    with urllib.request.urlopen(url + "/") as r:
        assert b"Busy Tag LEDs" in r.read()
    with urllib.request.urlopen(url + "/settings") as r:
        got = json.loads(r.read())
    assert got["colors"]["done"] == "063000" and got["spinner_color"] == "000C38"
    assert _post(url + "/settings", {"colors": {"done": "00ff00"}}) == (200, {"ok": True})
    assert json.loads(path.read_text())["colors"]["done"] == "00FF00"
    assert c.cfg["colors"]["done"] == "00FF00"


def test_settings_refuses_other_origins_and_bad_input(server):
    url, c, path = server
    assert _post(url + "/settings", {"flash_on_input": False}, Origin="https://evil.example")[0] == 403
    assert _post(url + "/preview", {"color": "FFFFFF"}, Origin="https://evil.example")[0] == 403
    assert _post(url + "/settings", {"brightness": 5})[0] == 400
    assert _post(url + "/preview", {"color": "nope"})[0] == 400
    req = urllib.request.Request(url + "/settings", b"flash_on_input=false", {"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 415
    assert not path.exists() and c.preview is None

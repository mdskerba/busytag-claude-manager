"""busytag-claude daemon: receives hook events over localhost HTTP and drives the Busy Tag.

Run: python -m busytag_claude.daemon
"""
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config
from .app_bridge import AppBridge
from .app_control import BusyTagApp
from .controller import Controller
from .cowork import CoworkWatcher
from .device import BusyTagDevice
from .sessions import ProjectNamer, SessionStore
from . import usage as usage_mod
from .usage import UsagePoller

log = logging.getLogger("busytag_claude")

TICK_S = 0.25
# Only these fields are logged; prompts, tool inputs and outputs never leave the hook.
EVENT_LOG_FIELDS = ("session_id", "hook_event_name", "notification_type", "tool_name", "error")


class EventLog:
    """Append-only JSONL of status events, for the response-time success metric."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event, change):
        rec = {"ts": round(time.time(), 3), **{k: event[k] for k in EVENT_LOG_FIELDS if event.get(k)}}
        if change:
            rec["from"], rec["to"] = change[1], change[2]
        with self.lock, open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")


SETTINGS_PAGE = os.path.join(os.path.dirname(__file__), "settings.html")


def make_handler(controller, event_log, config_path=config.CONFIG_PATH):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path in ("/settings", "/preview"):
                return self._settings_post()
            if self.path == "/usage":
                return self._usage_post()
            if self.path != "/event":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                event = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            change = controller.on_event(event, time.monotonic())
            try:
                event_log.write(event, change)
            except OSError:
                pass
            self._send(200, {"ok": True})

        def do_GET(self):
            if self.path == "/":
                with open(SETTINGS_PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/settings":
                snap = controller.snapshot()
                current = next((s["status"] for s in snap["sessions"] if snap["shown"] and s["id"] == snap["shown"][1]), None)
                return self._send(200, {
                    **config.settings(controller.cfg),
                    "connected": snap["connected"],
                    "handed_back": snap["handed_back"],
                    "current": current,
                })
            path, _, query = self.path.partition("?")
            if path == "/frame" and hasattr(controller.device, "frame"):
                q = parse_qs(query)
                num = lambda k: int(q[k][0]) if q.get(k, [""])[0].isdigit() else None
                return self._send(200, controller.device.frame(num("have"), num("done")))
            if self.path != "/status":
                return self._send(404, {"error": "not found"})
            self._send(200, controller.snapshot())

        def _usage_post(self):
            # From statusline.py: Claude Code's rate_limits, live after every API response.
            try:
                length = int(self.headers.get("Content-Length", 0))
                window = json.loads(self.rfile.read(length) or b"{}")["five_hour"]
                value = float(window["used_percentage"]), float(window["resets_at"])
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": "expected five_hour.used_percentage and resets_at"})
            if controller.usage:
                controller.usage.push(value)
            self._send(200, {"ok": True})

        def _settings_post(self):
            # Only the settings page itself may change things: a JSON body (other sites can't send
            # one without a CORS preflight, which this server never grants) from our own origin.
            origin = self.headers.get("Origin")
            port = self.server.server_address[1]
            if origin not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self._send(403, {"error": "forbidden"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(415, {"error": "send JSON"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/preview":
                    color = config.check_settings({"flash_color": body.get("color")})["flash_color"]
                    controller.preview_led(color, time.monotonic())
                    return self._send(200, {"ok": True, "connected": controller.device.is_open})
                changes = config.check_settings(body)
            except (ValueError, AttributeError) as e:
                return self._send(400, {"error": str(e)})
            try:
                config.save(changes, config_path)
            except OSError as e:
                return self._send(500, {"error": f"could not write config: {e}"})
            controller.apply_settings(changes)
            log.info("settings changed: %s", changes)
            self._send(200, {"ok": True})

        def _send(self, code, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

    return Handler


def main():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("BUSYTAG_CLAUDE_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = config.load()
    if cfg["transport"] == "app":
        device, app = AppBridge(), None  # the app keeps the port; never quit it
    else:
        device = BusyTagDevice(port=cfg["device_port"])
        app = BusyTagApp(cfg["busytag_app_name"]) if cfg["manage_busytag_app"] else None
    store = SessionStore(project_namer=ProjectNamer(cfg["project_names"]), state_path=config.SESSIONS_PATH)
    store.load(time.monotonic())
    usage = None
    if cfg["usage_bar"]:
        usage = UsagePoller(cfg["usage_poll_seconds"], cache_path=config.USAGE_CACHE_PATH,
                            expires_at=usage_mod.token_expires_at, refresher=usage_mod.refresh_token)
        usage.start()
    controller = Controller(store, device, cfg, app=app, usage=usage)
    event_log = EventLog(os.path.join(config.LOG_DIR, "events.jsonl"))

    server = ThreadingHTTPServer(("127.0.0.1", cfg["http_port"]), make_handler(controller, event_log))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("listening on http://127.0.0.1:%d", cfg["http_port"])
    if cfg["transport"] == "app":
        log.info("showing status through the Busy Tag app's Claude Sessions plugin")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    if cfg["cowork"]:
        def on_cowork_event(event):
            change = controller.on_event(event, time.monotonic())
            try:
                event_log.write(event, change)
            except OSError:
                pass

        threading.Thread(target=CoworkWatcher(on_cowork_event).run, args=(stop,), daemon=True).start()
        log.info("watching Cowork sessions")

    while not stop.is_set():
        try:
            controller.tick(time.monotonic())
        except Exception:
            log.exception("tick failed")
        stop.wait(TICK_S)

    log.info("shutting down")
    server.shutdown()
    controller.shutdown()


if __name__ == "__main__":
    main()

"""Drive the Busy Tag through the Busy Tag app instead of USB (`"transport": "app"`).

The app keeps the USB port and runs our "Claude Sessions" plugin (busytag-plugin/). The plugin
polls GET /frame on the daemon and replays what it gets with the app's plugin API (setImage,
setLedColor). AppBridge stands in for BusyTagDevice so the Controller works unchanged: it
records the picture and LED state the Controller asks for, and counts as connected while the
plugin is polling, i.e. while the app has it on screen.
"""
import base64
import threading
import time

from .device import DeviceError

STALE_S = 5  # the plugin polls every second; no poll for this long = it's off screen
LED_COUNT = 7
ONLY_THIS_LED = 128  # mask bit: the LEDs not in the mask go off


def apply_mask(ring, color, mask):
    """The ring after AT+SC=<mask>,<color>: a copy of `ring` (one colour per LED)."""
    return [color if mask & (1 << i) else ("000000" if mask & ONLY_THIS_LED else c) for i, c in enumerate(ring)]


class AppBridge:
    port = "busytag-app"

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.last_poll = -1e9
        self.connected = False
        self.events = []
        self.files = {}
        self.image = None  # PNG bytes on screen
        self.image_seq = 0
        # ("ring", [colour per LED]) or ("pattern", steps, repeat); led_seq bumps on every change
        self.ring = ["000000"] * LED_COUNT
        self.led = ("ring", self.ring)
        self.led_seq = 0
        self.steps = []
        self.pattern_seq = None  # led_seq of the pattern playing, until the plugin reports it done

    # -- plugin side (HTTP thread) ------------------------------------------

    def frame(self, have_image=None, done=None):
        """What the plugin should show. Leaves out the image if it already has `have_image`."""
        with self.lock:
            self.last_poll = self.clock()
            if done is not None and done == self.pattern_seq:
                self.events.append("+evn:PP,0")
                self.pattern_seq = None
            out = {"connected": self.connected, "image_seq": self.image_seq, "led_seq": self.led_seq}
            if self.image is not None and have_image != self.image_seq:
                out["image"] = base64.b64encode(self.image).decode()
            if self.led[0] == "ring":
                out["led"] = {"ring": list(self.led[1])}
            else:
                out["led"] = {"steps": [list(s) for s in self.led[1]], "repeat": self.led[2]}
            return out

    # -- device interface (Controller thread) -------------------------------

    @property
    def is_open(self):
        with self.lock:
            if self.connected and self.clock() - self.last_poll > STALE_S:
                self.connected = False
            return self.connected

    def open(self):
        with self.lock:
            if self.clock() - self.last_poll > STALE_S:
                raise DeviceError("Claude Sessions plugin isn't running in the Busy Tag app")
            self.connected = True
            self.events = []

    def close(self):
        with self.lock:
            self.connected = False

    def poll_events(self):
        if not self.is_open:
            raise DeviceError("plugin went away")
        return self.events

    def current_picture(self):
        return None

    def upload(self, name, data):
        self.files[name] = data

    def show(self, name):
        with self.lock:
            self.image = self.files[name]
            self.image_seq += 1

    def set_color(self, rgb, mask=127):
        # Like the device, a colour stops any pattern and leaves LEDs outside the mask as they were.
        self.ring = apply_mask(self.ring, rgb.upper(), mask)
        self._set_led(("ring", self.ring))

    def set_brightness(self, percent):
        pass  # the app owns display brightness

    def set_pattern(self, steps):
        self.steps = [tuple(s) for s in steps]

    def play_pattern(self, repeat):
        self._set_led(("pattern", self.steps, int(repeat)))
        with self.lock:
            self.pattern_seq = self.led_seq

    def _set_led(self, led):
        with self.lock:
            self.led = led
            self.led_seq += 1
            self.pattern_seq = None

"""Busy Tag USB CDC driver (AT commands, firmware 2.0).

Protocol notes verified in the Phase 0 spike (see spike/FINDINGS.md):
- Query replies (+XX:...) are NOT followed by OK, so reads end on OK/ERROR/'>' or a quiet period.
- `+evn:` lines arrive asynchronously, interleaved with replies; they are routed to `events`.
- AT+CP steps must be written back-to-back, or the parser hangs waiting for the rest.
- AT+PP=<play 1|0>,<repeat>.
- The picture on screen is locked; never AT+UF over it.
"""
import logging
import time

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

VID, PID = 0x303A, 0x81DF
BAUD = 460800


class DeviceError(Exception):
    pass


class PortBusy(DeviceError):
    """Another program (usually the Busy Tag app) holds the port."""


def find_port():
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
    return None


class BusyTagDevice:
    def __init__(self, port=None, baud=BAUD):
        self.port_override = port
        self.baud = baud
        self.ser = None
        self.port = None
        self.events = []

    # -- connection -------------------------------------------------------

    def open(self):
        port = self.port_override or find_port()
        if not port:
            raise DeviceError("Busy Tag not found")
        try:
            self.ser = serial.Serial(port, self.baud, timeout=0.05, exclusive=True)
        except (serial.SerialException, OSError) as e:
            # 16 = EBUSY (held by the Busy Tag app); 35 = EAGAIN on flock (held by another
            # exclusive opener, e.g. a previous daemon still shutting down).
            if getattr(e, "errno", None) in (16, 35) or "busy" in str(e).lower() or "lock" in str(e).lower():
                raise PortBusy(f"{port} is busy") from e
            raise DeviceError(str(e)) from e
        self.port = port
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        if self.cmd("AT") != ["OK"]:
            self.close()
            raise DeviceError("device did not answer AT")
        log.info("connected to Busy Tag on %s", port)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    @property
    def is_open(self):
        return self.ser is not None

    # -- low level --------------------------------------------------------

    def _write(self, data):
        try:
            self.ser.write(data)
        except (serial.SerialException, OSError) as e:
            self.close()
            raise DeviceError(str(e)) from e

    def _read_reply(self, timeout=2.0, idle=0.25):
        """Collect reply lines until OK / ERROR / '>' / quiet period / timeout."""
        lines, buf = [], b""
        end, last = time.monotonic() + timeout, None
        while time.monotonic() < end:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                self.close()
                raise DeviceError(str(e)) from e
            now = time.monotonic()
            if not chunk:
                if lines and not buf and last and now - last > idle:
                    return lines
                continue
            last = now
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                if line.startswith("+evn:"):
                    self.events.append(line)
                    continue
                lines.append(line)
                if line == "OK" or line.startswith("ERROR"):
                    return lines
            if buf.strip() == b">":  # payload prompt has no line ending
                lines.append(">")
                return lines
        return lines

    def poll_events(self):
        """Read whatever the device sent unprompted; return all `+evn:` lines since last clear."""
        if not self.ser:
            raise DeviceError("not connected")
        try:
            waiting = self.ser.in_waiting
            data = self.ser.read(waiting) if waiting else b""
        except (serial.SerialException, OSError) as e:
            self.close()
            raise DeviceError(str(e)) from e
        for raw in data.splitlines():
            line = raw.decode(errors="replace").strip()
            if line.startswith("+evn:"):
                self.events.append(line)
        return self.events

    def cmd(self, command, timeout=2.0):
        if not self.ser:
            raise DeviceError("not connected")
        self._write((command + "\r\n").encode())
        return self._read_reply(timeout)

    def _expect_ok(self, command, timeout=2.0):
        reply = self.cmd(command, timeout)
        if not reply or reply[-1] != "OK":
            raise DeviceError(f"{command} -> {reply}")

    # -- commands ---------------------------------------------------------

    def current_picture(self):
        for line in self.cmd("AT+SP?"):
            if line.startswith("+SP:"):
                return line[4:].strip()
        return None

    def upload(self, name, data):
        reply = self.cmd(f"AT+UF={name},{len(data)}", timeout=3)
        if not reply or reply[-1] != ">":
            raise DeviceError(f"AT+UF {name} -> {reply}")
        self._write(data)
        reply = self._read_reply(timeout=15)
        if not reply or reply[-1] != "OK":
            raise DeviceError(f"upload {name} -> {reply}")

    def show(self, name):
        self._expect_ok(f"AT+SP={name}", timeout=3)

    def set_color(self, rgb, mask=127):
        self._expect_ok(f"AT+SC={mask},{rgb.upper()}")

    def set_brightness(self, percent):
        self._expect_ok(f"AT+DB={int(percent)}")

    def set_pattern(self, steps):
        """steps: list of (led_mask, 'RRGGBB', speed, delay_ms)."""
        reply = self.cmd(f"AT+CP={len(steps)}", timeout=1)
        if not reply or reply[-1] != ">":
            raise DeviceError(f"AT+CP -> {reply}")
        for i, (mask, color, speed, delay) in enumerate(steps):
            self._write(f"+CP:{mask},{color.upper()},{speed},{delay}\r\n".encode())
            if i < len(steps) - 1:
                time.sleep(0.01)
        reply = self._read_reply(timeout=1.5)
        if not reply or reply[-1] != "OK":
            raise DeviceError(f"pattern steps -> {reply}")

    def play_pattern(self, repeat):
        self._expect_ok(f"AT+PP=1,{int(repeat)}")

import errno

import pytest
import serial

from busytag_claude import device
from busytag_claude.device import BusyTagDevice, PortBusy


@pytest.mark.parametrize(
    "exc",
    [
        serial.SerialException(errno.EBUSY, "could not open port: Resource busy"),
        serial.SerialException(35, "Could not exclusively lock port: Resource temporarily unavailable"),
    ],
)
def test_held_port_is_reported_as_busy(monkeypatch, exc):
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(device.serial, "Serial", boom)
    with pytest.raises(PortBusy):
        BusyTagDevice(port="/dev/fake").open()


class FakeSerial:
    def __init__(self, data):
        self.data = data

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


def test_poll_events_collects_only_event_lines():
    d = BusyTagDevice(port="/dev/fake")
    d.ser = FakeSerial(b"+evn:PP,1\r\nOK\r\n+evn:PP,0\r\n")
    assert d.poll_events() == ["+evn:PP,1", "+evn:PP,0"]
    assert d.poll_events() == ["+evn:PP,1", "+evn:PP,0"]  # kept until cleared

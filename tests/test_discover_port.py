import json
import types

import serial as pyserial

import daemon


class FakeSerialPort:
    """Stands in for serial.Serial() as a context manager. `responses`
    maps device path -> the raw bytes it should hand back on read(),
    delivered once. A device path with no entry (or an explicit
    SerialException-raising factory) simulates "opened fine but never
    replies" / "failed to open" respectively.
    """

    def __init__(self, device, baud, timeout=None, responses=None):
        self.device = device
        self.sent = b""
        self._reply = (responses or {}).get(device)
        self._delivered = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.sent += data

    def read(self, n):
        if self._reply and not self._delivered:
            self._delivered = True
            return self._reply
        return b""


def make_port(device, vid=daemon.ADAFRUIT_USB_VID):
    return types.SimpleNamespace(device=device, vid=vid)


def hello_bytes(device_id):
    return (json.dumps({"t": "hello", "device": device_id}) + "\n").encode("utf-8")


def test_discover_port_returns_none_with_no_candidates(monkeypatch):
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: [])
    assert daemon.discover_port(handshake_timeout=0.05) is None


def test_discover_port_ignores_non_adafruit_vid(monkeypatch):
    ports = [make_port("/dev/cu.other", vid=0x1234)]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(
        daemon.serial, "Serial",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should never probe this port")),
    )
    assert daemon.discover_port(handshake_timeout=0.05) is None


def test_discover_port_picks_the_port_that_replies_hello(monkeypatch):
    # Simulates the real two-port situation: the console (REPL) endpoint
    # never answers, the data endpoint does.
    ports = [make_port("/dev/cu.usbmodem1"), make_port("/dev/cu.usbmodem3")]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)

    responses = {"/dev/cu.usbmodem3": hello_bytes(daemon.PAD_DEVICE_ID)}
    monkeypatch.setattr(
        daemon.serial, "Serial",
        lambda device, baud, timeout=None: FakeSerialPort(device, baud, timeout, responses),
    )

    assert daemon.discover_port(handshake_timeout=0.05) == "/dev/cu.usbmodem3"


def test_discover_port_rejects_mismatched_device_id(monkeypatch):
    ports = [make_port("/dev/cu.usbmodem1")]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)

    responses = {"/dev/cu.usbmodem1": hello_bytes("some-other-board")}
    monkeypatch.setattr(
        daemon.serial, "Serial",
        lambda device, baud, timeout=None: FakeSerialPort(device, baud, timeout, responses),
    )

    assert daemon.discover_port(handshake_timeout=0.05) is None


def test_discover_port_skips_a_port_that_fails_to_open(monkeypatch):
    ports = [make_port("/dev/cu.busy"), make_port("/dev/cu.usbmodem3")]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)

    responses = {"/dev/cu.usbmodem3": hello_bytes(daemon.PAD_DEVICE_ID)}

    def fake_serial(device, baud, timeout=None):
        if device == "/dev/cu.busy":
            raise pyserial.SerialException("port busy")
        return FakeSerialPort(device, baud, timeout, responses)

    monkeypatch.setattr(daemon.serial, "Serial", fake_serial)

    assert daemon.discover_port(handshake_timeout=0.05) == "/dev/cu.usbmodem3"


def test_discover_port_picks_first_when_multiple_reply(monkeypatch):
    ports = [make_port("/dev/cu.usbmodemA"), make_port("/dev/cu.usbmodemB")]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)

    responses = {
        "/dev/cu.usbmodemA": hello_bytes(daemon.PAD_DEVICE_ID),
        "/dev/cu.usbmodemB": hello_bytes(daemon.PAD_DEVICE_ID),
    }
    monkeypatch.setattr(
        daemon.serial, "Serial",
        lambda device, baud, timeout=None: FakeSerialPort(device, baud, timeout, responses),
    )

    assert daemon.discover_port(handshake_timeout=0.05) == "/dev/cu.usbmodemA"

import json
import time
import types

import daemon
import hid_protocol

from test_hid_pad_link import make_candidate, make_fake_hid


class FakeSerialConn:
    """Stands in for an already-open serial.Serial() connection — not
    test_discover_port.py's context-manager FakeSerialPort, since
    SerialPadLink.open() keeps a persistent connection open directly
    rather than using `with`. `reply` is delivered once, but only
    *after* a {"t": "ping"} has actually been written — otherwise the
    background reader thread (started by open(), running concurrently
    with a later, explicit handshake() call in these tests) could
    consume the queued reply before handshake() ever sends its own
    ping. A brief sleep on the "nothing to read" path mimics
    pyserial's blocking-with-timeout read() instead of busy-looping
    the thread.
    """

    def __init__(self, reply=None):
        self.written = b""
        self._reply = reply
        self._delivered = False
        self._pinged = False

    def write(self, data):
        self.written += data
        if b'"t": "ping"' in data or b'"t":"ping"' in data:
            self._pinged = True
        return len(data)

    def read(self, n):
        if self._pinged and self._reply and not self._delivered:
            self._delivered = True
            return self._reply
        time.sleep(0.01)
        return b""

    def close(self):
        pass


def hello_bytes(slots, device_id=daemon.PAD_DEVICE_ID):
    return (json.dumps({"t": "hello", "device": device_id, "slots": slots}) + "\n").encode("utf-8")


def hello_report(slots, device_id=hid_protocol.DEVICE_ID_AIR75_V2):
    return bytes([hid_protocol.MSG_HELLO, device_id, slots]) + bytes(hid_protocol.REPORT_SIZE - 3)


# --- SerialPadLink.handshake() -------------------------------------------

def test_serialpadlink_handshake_returns_slots(monkeypatch):
    conn = FakeSerialConn(reply=hello_bytes(7))
    monkeypatch.setattr(daemon.serial, "Serial", lambda *a, **k: conn)

    link = daemon.SerialPadLink("/dev/cu.fake", 115200, lambda m: None)
    link.open()
    try:
        assert link.handshake(timeout=1.0) == {"slots": 7}
    finally:
        link.close()


def test_serialpadlink_handshake_times_out_with_no_reply(monkeypatch):
    conn = FakeSerialConn(reply=None)
    monkeypatch.setattr(daemon.serial, "Serial", lambda *a, **k: conn)

    link = daemon.SerialPadLink("/dev/cu.fake", 115200, lambda m: None)
    link.open()
    try:
        assert link.handshake(timeout=0.05) is None
    finally:
        link.close()


def test_serialpadlink_handshake_headless_returns_none_immediately():
    link = daemon.SerialPadLink(None, 115200, lambda m: None)
    # open() never called — attached stays False, matching a headless
    # daemon run (see recording_daemon in tests/conftest.py).
    started = time.monotonic()
    assert link.handshake(timeout=5.0) is None
    assert time.monotonic() - started < 1.0  # must not actually wait out the timeout


# --- HidPadLink.handshake() ------------------------------------------------

def test_hidpadlink_handshake_returns_slots(monkeypatch):
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report(9)}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.HidPadLink(lambda m: None)
    link.open()
    try:
        assert link.attached is True
        assert link.handshake(timeout=1.0) == {"slots": 9}
    finally:
        link.close()


def test_hidpadlink_handshake_times_out_after_discovery_stops_answering(monkeypatch):
    """open() can only attach if discovery got a hello reply (unlike
    SerialPadLink, HidPadLink has no "forced path, skip discovery"
    mode) — so the realistic timeout case isn't "never answered
    anything," it's "answered the discovery ping, then went quiet."
    Simulated with a reply consumed at most once, shared across every
    hid.Device() instance opened against this path (discovery's probe
    and open()'s persistent connection are separate instances).
    """
    path = b"iface0"
    reply = [hello_report(9)]  # mutable + shared: delivered at most once, ever

    class OnceThenSilentDevice:
        def __init__(self, path=None, vid=None, pid=None, serial=None):
            self.written = b""
            self._pinged = False

        def write(self, data):
            data = bytes(data)
            self.written += data
            if data[1:2] == bytes([hid_protocol.MSG_PING]):
                self._pinged = True
            return len(data)

        def read(self, size, timeout=None):
            if self._pinged and reply:
                return reply.pop(0)
            time.sleep(0.01)
            return b""

        def close(self):
            pass

    fake_hid = types.SimpleNamespace(
        enumerate=lambda vid, pid: [make_candidate(path)],
        Device=OnceThenSilentDevice,
        HIDException=Exception,
    )
    monkeypatch.setattr(daemon, "hid", fake_hid)

    link = daemon.HidPadLink(lambda m: None)
    link.open()
    try:
        assert link.attached is True  # discovery's own probe got the one reply
        assert link.handshake(timeout=0.05) is None  # nothing left for this ping
    finally:
        link.close()


def test_hidpadlink_handshake_headless_returns_none_immediately(monkeypatch):
    monkeypatch.setattr(daemon, "hid", None)
    link = daemon.HidPadLink(lambda m: None)
    link.open()
    started = time.monotonic()
    assert link.handshake(timeout=5.0) is None
    assert time.monotonic() - started < 1.0


# --- AutoPadLink.handshake() -----------------------------------------------

def test_autopadlink_handshake_delegates_to_active_transport(monkeypatch):
    monkeypatch.setattr(daemon, "discover_port", lambda baud: None)
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report(4)}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.AutoPadLink(lambda m: None)
    link.open()
    try:
        assert isinstance(link._active, daemon.HidPadLink)
        assert link.handshake(timeout=1.0) == {"slots": 4}
    finally:
        link.close()


def test_autopadlink_handshake_none_before_open():
    link = daemon.AutoPadLink(lambda m: None)
    assert link.handshake(timeout=5.0) is None


# --- Daemon.apply_handshake() -----------------------------------------

def test_apply_handshake_resizes_slots():
    d = daemon.Daemon()
    assert d.slots.num_slots == daemon.NUM_SLOTS

    d.apply_handshake({"slots": 3})
    assert d.slots.num_slots == 3


def test_apply_handshake_keeps_default_when_none():
    d = daemon.Daemon()
    d.apply_handshake(None)
    assert d.slots.num_slots == daemon.NUM_SLOTS


def test_apply_handshake_keeps_default_when_slots_missing():
    d = daemon.Daemon()
    d.apply_handshake({})
    assert d.slots.num_slots == daemon.NUM_SLOTS


def test_apply_handshake_resized_manager_starts_empty():
    # Resizing must produce a fresh SlotManager, not resize in place —
    # any pre-handshake allocations (none should exist in practice,
    # since apply_handshake() runs before start_unix_server in
    # serve(), but worth locking in the discard behavior explicitly).
    d = daemon.Daemon()
    d.slots.allocate("stale-session")
    d.apply_handshake({"slots": 2})
    assert d.slots.slot_for("stale-session") is None
    assert d.slots.num_slots == 2

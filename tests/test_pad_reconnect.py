import threading
import time
import types

import daemon
import hid_protocol
import serial as pyserial

from test_hid_pad_link import (
    FakeHidDevice, FakeHidException, hello_report, make_candidate, make_fake_hid,
)
from test_discover_port import FakeSerialPort, hello_bytes, make_port


# --- Daemon: pad_state cache + resync_pad -----------------------------------

def test_send_pad_caches_slot_message(recording_daemon):
    d, sent = recording_daemon
    d._send_pad({"t": "slot", "i": 2, "state": "working", "label": "Read"})
    assert d.pad_state[2] == {"t": "slot", "i": 2, "state": "working", "label": "Read"}
    assert sent == [{"t": "slot", "i": 2, "state": "working", "label": "Read"}]


def test_send_pad_merges_state_only_update_keeping_label(recording_daemon):
    # A PreToolUse message sets a label; a later state-only update (e.g.
    # PostToolUse) has no "label" key at all — the cache must keep the
    # label rather than a plain dict-overwrite dropping it, or a resync
    # after reconnect would come back unlabeled.
    d, sent = recording_daemon
    d._send_pad({"t": "slot", "i": 0, "state": "working", "label": "Read"})
    d._send_pad({"t": "slot", "i": 0, "state": "idle"})
    assert d.pad_state[0] == {"t": "slot", "i": 0, "state": "idle", "label": "Read"}


def test_send_pad_clear_pops_slot_from_cache(recording_daemon):
    d, sent = recording_daemon
    d._send_pad({"t": "slot", "i": 1, "state": "idle", "label": "proj"})
    d._send_pad({"t": "clear", "i": 1})
    assert 1 not in d.pad_state


def test_resync_pad_replays_every_cached_slot(recording_daemon):
    d, sent = recording_daemon
    d._send_pad({"t": "slot", "i": 0, "state": "idle", "label": "a"})
    d._send_pad({"t": "slot", "i": 1, "state": "working", "label": "b"})
    sent.clear()

    d.resync_pad()

    assert len(sent) == 2
    assert {"t": "slot", "i": 0, "state": "idle", "label": "a"} in sent
    assert {"t": "slot", "i": 1, "state": "working", "label": "b"} in sent


def test_resync_pad_skips_freed_slots(recording_daemon):
    d, sent = recording_daemon
    d._send_pad({"t": "slot", "i": 0, "state": "idle", "label": "a"})
    d._send_pad({"t": "clear", "i": 0})
    sent.clear()

    d.resync_pad()

    assert sent == []


def test_daemon_wires_resync_pad_as_transport_on_reattach():
    d = daemon.Daemon()
    assert d.pad.on_reattach == d.resync_pad


# --- HidPadLink: reconnect on HIDException ----------------------------------

def test_hidpadlink_reconnect_rediscovers_and_reopens(monkeypatch):
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report()}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    reattached = []
    link = daemon.HidPadLink(lambda m: None, on_reattach=lambda: reattached.append(True))
    link.open()
    try:
        assert link.attached is True
        old_dev = link._dev

        # Simulate the OS-level handle dying — e.g. the keyboard's
        # wireless link dropping while it slept — the same thing
        # _read_loop reacts to on a real HIDException.
        link.attached = False
        link._reconnect()

        assert link.attached is True
        assert link._dev is not old_dev
        assert old_dev.closed is True
        assert reattached == [True]
    finally:
        link.close()


def test_hidpadlink_reconnect_waits_until_pad_reappears(monkeypatch):
    # Nothing answers discovery at first; the reconnect loop must keep
    # retrying (not give up) until a candidate shows up.
    state = {"devices": []}

    def enumerate(vid, pid):
        return state["devices"]

    fake_hid = types.SimpleNamespace(
        enumerate=enumerate,
        Device=lambda path=None, vid=None, pid=None, serial=None: FakeHidDevice(
            path, {b"iface0": hello_report()}
        ),
        HIDException=FakeHidException,
    )
    monkeypatch.setattr(daemon, "hid", fake_hid)

    link = daemon.HidPadLink(lambda m: None)
    link.RECONNECT_POLL_SECONDS = 0.02

    def make_it_appear():
        time.sleep(0.05)
        state["devices"] = [make_candidate(b"iface0")]

    t = threading.Thread(target=make_it_appear)
    t.start()
    try:
        link._reconnect()
        assert link.attached is True
    finally:
        t.join()
        link.close()


def test_hidpadlink_read_loop_reconnects_after_hid_exception(monkeypatch):
    """End-to-end: a HIDException from the background reader thread (the
    realistic NuPhy-asleep case) must not just retry read() forever on
    the same handle — see the daemon.py comment in _read_loop for why a
    plain read timeout (empty bytes, no exception) is fine as-is but this
    isn't. It should rediscover, reopen, and resume."""
    path = b"iface0"
    candidates = [make_candidate(path)]
    responses = {path: hello_report()}
    open_count = [0]

    class FailsOnceThenNormal(FakeHidDevice):
        def read(self, size, timeout=None):
            if not getattr(self, "_failed", False):
                self._failed = True
                raise FakeHidException("link dropped")
            return super().read(size, timeout)

    def Device(path=None, vid=None, pid=None, serial=None):
        open_count[0] += 1
        # 1st open: discover_hid_device()'s own probe inside open().
        # 2nd open: open()'s persistent connection — this is the one
        # whose *first* read() should look like the pad going quiet.
        # 3rd+: the reconnect's own probe + persistent connection —
        # must behave normally so recovery actually completes.
        if open_count[0] == 2:
            return FailsOnceThenNormal(path, responses)
        return FakeHidDevice(path, responses)

    fake_hid = types.SimpleNamespace(
        enumerate=lambda vid, pid: candidates, Device=Device, HIDException=FakeHidException,
    )
    monkeypatch.setattr(daemon, "hid", fake_hid)

    reattached = threading.Event()
    link = daemon.HidPadLink(lambda m: None, on_reattach=reattached.set)
    link.open()
    try:
        assert link.attached is True
        assert reattached.wait(timeout=2.0), "on_reattach never fired"
        assert link.attached is True
    finally:
        link.close()


# --- SerialPadLink: reconnect on SerialException ----------------------------

class FlakySerialConn:
    """Like test_pad_handshake.py's FakeSerialConn, but read() can be
    made to raise pyserial.SerialException on demand — standing in for
    the port dying under a real disconnect."""

    def __init__(self, device, reply=None, fail_reads=0):
        self.device = device
        self.written = b""
        self._reply = reply
        self._delivered = False
        self._fail_reads = fail_reads

    def write(self, data):
        self.written += data
        return len(data)

    def read(self, n):
        if self._fail_reads > 0:
            self._fail_reads -= 1
            raise pyserial.SerialException("device disconnected")
        if self._reply and not self._delivered:
            self._delivered = True
            return self._reply
        time.sleep(0.01)
        return b""

    def close(self):
        pass


def test_serialpadlink_reconnect_retries_forced_port(monkeypatch):
    # MACROPAD_SERIAL_PORT pins an exact port — reconnect must keep
    # retrying that same path rather than re-probing (which could
    # attach to a different board if more than one is plugged in).
    calls = []

    def fake_serial(device, baud, timeout=None):
        calls.append(device)
        return FlakySerialConn(device)

    monkeypatch.setattr(daemon.serial, "Serial", fake_serial)
    monkeypatch.setattr(
        daemon, "discover_port",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("forced port must not re-probe")),
    )

    reattached = []
    link = daemon.SerialPadLink(
        "/dev/cu.fake", 115200, lambda m: None, on_reattach=lambda: reattached.append(True)
    )
    link.open()
    try:
        assert link.attached is True
        link.attached = False
        link._reconnect()
        assert link.attached is True
        assert calls == ["/dev/cu.fake", "/dev/cu.fake"]
        assert reattached == [True]
    finally:
        link.close()


class ClosableFakeSerialPort(FakeSerialPort):
    """FakeSerialPort (test_discover_port.py) is only ever used as a
    `with serial.Serial(...) as ser:` context manager inside
    discover_port() itself, so it has no close(). SerialPadLink keeps
    a persistent connection and calls .close() on it directly (both in
    Daemon.close() and at the top of _reconnect()) — needs one here."""

    def close(self):
        pass


def test_serialpadlink_reconnect_reprobes_when_not_forced(monkeypatch):
    # No MACROPAD_SERIAL_PORT — the original path came from discover_port()
    # and isn't stable across replugs, so reconnect must re-probe rather
    # than retry a path that may no longer be valid.
    ports = [make_port("/dev/cu.usbmodem1")]
    monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports)
    responses = {"/dev/cu.usbmodem1": hello_bytes(daemon.PAD_DEVICE_ID)}
    monkeypatch.setattr(
        daemon.serial, "Serial",
        lambda device, baud, timeout=None: ClosableFakeSerialPort(device, baud, timeout, responses),
    )

    link = daemon.SerialPadLink(None, 115200, lambda m: None)
    link.open()
    try:
        assert link.attached is True
        assert link.port == "/dev/cu.usbmodem1"

        # Now simulate reconnecting after the port disappeared and came
        # back under a new device node — must not call close() first,
        # since that would set link._stop and short-circuit _reconnect()'s
        # own wait loop.
        ports2 = [make_port("/dev/cu.usbmodem7")]
        monkeypatch.setattr(daemon.list_ports, "comports", lambda: ports2)
        responses2 = {"/dev/cu.usbmodem7": hello_bytes(daemon.PAD_DEVICE_ID)}
        monkeypatch.setattr(
            daemon.serial, "Serial",
            lambda device, baud, timeout=None: ClosableFakeSerialPort(device, baud, timeout, responses2),
        )
        link.attached = False
        link._reconnect()

        assert link.attached is True
        assert link.port == "/dev/cu.usbmodem7"
    finally:
        link.close()


def test_serialpadlink_read_loop_reconnects_after_serial_exception(monkeypatch):
    open_count = [0]

    def fake_serial(device, baud, timeout=None):
        open_count[0] += 1
        # 1st open() call is this test's initial persistent connection —
        # its first read() should look like the pad disconnecting.
        if open_count[0] == 1:
            return FlakySerialConn(device, fail_reads=1)
        return FlakySerialConn(device)

    monkeypatch.setattr(daemon.serial, "Serial", fake_serial)

    reattached = threading.Event()
    link = daemon.SerialPadLink("/dev/cu.fake", 115200, lambda m: None, on_reattach=reattached.set)
    link.open()
    try:
        assert link.attached is True
        assert reattached.wait(timeout=2.0), "on_reattach never fired"
        assert link.attached is True
    finally:
        link.close()

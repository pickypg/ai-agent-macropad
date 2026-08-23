import logging
import types
import time

import daemon
import hid_protocol
import pad_link

from test_hid_pad_link import hello_report, make_candidate, make_fake_hid


# --- HidPadLink.handshake() ------------------------------------------------

def test_hidpadlink_handshake_returns_slots(monkeypatch):
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report(slots=9)}
    monkeypatch.setattr(pad_link, "hid", make_fake_hid(devices, responses))

    link = pad_link.HidPadLink(lambda m: None)
    link.open()
    try:
        assert link.attached is True
        assert link.handshake(timeout=1.0) == {
            "slots": 9,
            "protocol": hid_protocol.PROTOCOL_VERSION,
        }
    finally:
        link.close()


def test_hidpadlink_handshake_times_out_after_discovery_stops_answering(monkeypatch):
    """open() can only attach if discovery got a hello reply — so the
    realistic timeout case isn't "never answered anything," it's
    "answered the discovery ping, then went quiet." Simulated with a
    reply consumed at most once, shared across every hid.Device()
    instance opened against this path (discovery's probe and open()'s
    persistent connection are separate instances).
    """
    path = b"iface0"
    reply = [hello_report(slots=9)]  # mutable + shared: delivered at most once, ever

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
    monkeypatch.setattr(pad_link, "hid", fake_hid)

    link = pad_link.HidPadLink(lambda m: None)
    link.open()
    try:
        assert link.attached is True  # discovery's own probe got the one reply
        assert link.handshake(timeout=0.05) is None  # nothing left for this ping
    finally:
        link.close()


def test_hidpadlink_handshake_headless_returns_none_immediately(monkeypatch):
    monkeypatch.setattr(pad_link, "hid", None)
    link = pad_link.HidPadLink(lambda m: None)
    link.open()
    started = time.monotonic()
    assert link.handshake(timeout=5.0) is None
    assert time.monotonic() - started < 1.0


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


def test_apply_handshake_warns_when_pad_protocol_is_newer(caplog):
    d = daemon.Daemon()
    with caplog.at_level(logging.WARNING, logger="macropad-daemon"):
        d.apply_handshake({
            "slots": 4,
            "protocol": hid_protocol.PROTOCOL_VERSION + 1,
        })
    assert d.slots.num_slots == 4
    assert "newer than daemon's" in caplog.text
    assert "update the daemon" in caplog.text


def test_apply_handshake_warns_when_pad_protocol_is_older(caplog):
    d = daemon.Daemon()
    with caplog.at_level(logging.WARNING, logger="macropad-daemon"):
        d.apply_handshake({
            "slots": 4,
            "protocol": hid_protocol.PROTOCOL_VERSION - 1,
        })
    assert d.slots.num_slots == 4
    assert "older than daemon's" in caplog.text
    assert "may lack functionality" in caplog.text


def test_apply_handshake_matching_protocol_does_not_warn(caplog):
    d = daemon.Daemon()
    with caplog.at_level(logging.WARNING, logger="macropad-daemon"):
        d.apply_handshake({
            "slots": 4,
            "protocol": hid_protocol.PROTOCOL_VERSION,
        })
    assert d.slots.num_slots == 4
    assert caplog.text == ""

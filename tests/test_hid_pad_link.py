import time
import types

import daemon
import hid_protocol


class FakeHidException(Exception):
    pass


class FakeHidDevice:
    """Stands in for hid.Device(path=...). `responses` maps device
    path -> the raw report bytes it should hand back on read(), once —
    same one-shot-reply shape as test_discover_port.py's
    FakeSerialPort — but only *after* an actual MSG_PING has been
    written to it, not unconditionally on the first read(). This
    matters once a background reader thread is involved (handshake()
    tests, not just discover_hid_device()'s own synchronous probing):
    without gating on a real ping, a queued reply could get delivered
    to whichever read() happens to run first — the persistent
    connection's reader thread racing a later explicit handshake()
    call — rather than only in response to that call's own ping. A
    brief sleep on the "nothing to read" path mimics hidapi's
    blocking-with-timeout read() instead of busy-looping the thread.
    """

    def __init__(self, path, responses=None):
        self.path = path
        self.written = b""
        self.closed = False
        self._reply = (responses or {}).get(path)
        self._delivered = False
        self._pinged = False

    def write(self, data):
        data = bytes(data)
        self.written += data
        if data[1:2] == bytes([hid_protocol.MSG_PING]):
            self._pinged = True
        return len(data)

    def read(self, size, timeout=None):
        if self._pinged and self._reply and not self._delivered:
            self._delivered = True
            return self._reply
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


def make_fake_hid(devices, responses=None, open_failures=None):
    """Builds a fake `hid` module. `devices` is what hid.enumerate()
    returns (list of dicts with vendor_id/product_id/usage_page/usage/
    path). `responses` maps path -> reply bytes. `open_failures` is a
    set of paths whose Device() should raise FakeHidException.
    """
    open_failures = open_failures or set()

    def enumerate(vid, pid):
        return [
            d for d in devices
            if (vid == 0 or d["vendor_id"] == vid) and (pid == 0 or d["product_id"] == pid)
        ]

    def Device(path=None, vid=None, pid=None, serial=None):
        if path in open_failures:
            raise FakeHidException(f"failed to open {path!r}")
        return FakeHidDevice(path, responses)

    return types.SimpleNamespace(enumerate=enumerate, Device=Device, HIDException=FakeHidException)


def make_candidate(path, vid=daemon.NUPHY_USB_VID, pid=daemon.NUPHY_USB_PID,
                    usage_page=daemon.RAW_USAGE_PAGE, usage=daemon.RAW_USAGE_ID):
    return {
        "path": path, "vendor_id": vid, "product_id": pid,
        "usage_page": usage_page, "usage": usage,
    }


def hello_report(device_id=hid_protocol.DEVICE_ID_AIR75_V2, slots=16):
    return bytes([hid_protocol.MSG_HELLO, device_id, slots]) + bytes(hid_protocol.REPORT_SIZE - 3)


# --- discover_hid_device ---------------------------------------------------

def test_discover_hid_device_returns_none_with_no_candidates(monkeypatch):
    fake = make_fake_hid([])
    monkeypatch.setattr(daemon, "hid", fake)
    assert daemon.discover_hid_device(daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID) is None


def test_discover_hid_device_ignores_wrong_usage_page(monkeypatch):
    # Same VID/PID as the raw HID interface, but this is the board's
    # normal boot-keyboard interface — must never be probed as the pad.
    devices = [make_candidate(b"iface0", usage_page=0x0001, usage=0x06)]
    fake = make_fake_hid(devices)
    monkeypatch.setattr(daemon, "hid", fake)
    assert daemon.discover_hid_device(daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID) is None


def test_discover_hid_device_picks_interface_that_replies_hello(monkeypatch):
    devices = [make_candidate(b"iface0"), make_candidate(b"iface1")]
    responses = {b"iface1": hello_report()}
    fake = make_fake_hid(devices, responses)
    monkeypatch.setattr(daemon, "hid", fake)

    assert daemon.discover_hid_device(
        daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID, handshake_timeout=0.05
    ) == b"iface1"


def test_discover_hid_device_skips_interface_that_fails_to_open(monkeypatch):
    devices = [make_candidate(b"busy"), make_candidate(b"iface1")]
    responses = {b"iface1": hello_report()}
    fake = make_fake_hid(devices, responses, open_failures={b"busy"})
    monkeypatch.setattr(daemon, "hid", fake)

    assert daemon.discover_hid_device(
        daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID, handshake_timeout=0.05
    ) == b"iface1"


def test_discover_hid_device_returns_none_when_nothing_replies(monkeypatch):
    devices = [make_candidate(b"iface0")]
    fake = make_fake_hid(devices)
    monkeypatch.setattr(daemon, "hid", fake)

    assert daemon.discover_hid_device(
        daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID, handshake_timeout=0.05
    ) is None


def test_discover_hid_device_returns_none_when_hid_unavailable(monkeypatch):
    monkeypatch.setattr(daemon, "hid", None)
    assert daemon.discover_hid_device(daemon.NUPHY_USB_VID, daemon.NUPHY_USB_PID) is None


# --- HidPadLink --------------------------------------------------------

def test_hidpadlink_open_headless_when_hid_unavailable(monkeypatch):
    monkeypatch.setattr(daemon, "hid", None)
    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    assert link.attached is False


def test_hidpadlink_open_headless_when_nothing_found(monkeypatch):
    monkeypatch.setattr(daemon, "hid", make_fake_hid([]))
    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    assert link.attached is False


def test_hidpadlink_open_attaches_and_closes_cleanly(monkeypatch):
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report()}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    try:
        assert link.attached is True
    finally:
        link.close()
    assert link._dev.closed is True


def test_hidpadlink_write_json_translates_slot(monkeypatch):
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report()}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    try:
        link.write_json({"t": "slot", "i": 3, "state": "working", "label": "Read"})
        written = link._dev.written
        assert written[0:1] == bytes([0])  # hidapi report-ID prefix
        report = written[1:]
        assert report[0] == hid_protocol.MSG_SLOT
        assert report[1] == 3
        assert report[2] == hid_protocol.STATE_WORKING
    finally:
        link.close()


def test_hidpadlink_write_json_translates_clear_to_off(monkeypatch):
    # Not STATE_IDLE — rp2040/code.py's handle_message() shows "clear"
    # is fully dark, distinct from "idle"'s dim glow (a session that's
    # merely quiet vs. no session mapped here at all).
    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report()}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    try:
        link.write_json({"t": "clear", "i": 5})
        report = link._dev.written[1:]
        assert report[0] == hid_protocol.MSG_SLOT
        assert report[1] == 5
        assert report[2] == hid_protocol.STATE_OFF
    finally:
        link.close()


def test_hidpadlink_write_json_headless_is_a_noop(monkeypatch):
    monkeypatch.setattr(daemon, "hid", None)
    link = daemon.HidPadLink(lambda msg: None)
    link.open()
    link.write_json({"t": "slot", "i": 0, "state": "idle"})  # must not raise


# --- transport selection -------------------------------------------------

def test_make_pad_link_serial(monkeypatch):
    monkeypatch.setattr(daemon, "TRANSPORT", "serial")
    assert isinstance(daemon.make_pad_link(lambda m: None), daemon.SerialPadLink)


def test_make_pad_link_hid(monkeypatch):
    monkeypatch.setattr(daemon, "TRANSPORT", "hid")
    assert isinstance(daemon.make_pad_link(lambda m: None), daemon.HidPadLink)


def test_make_pad_link_unset_is_auto(monkeypatch):
    monkeypatch.setattr(daemon, "TRANSPORT", None)
    assert isinstance(daemon.make_pad_link(lambda m: None), daemon.AutoPadLink)


def test_make_pad_link_unrecognized_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr(daemon, "TRANSPORT", "carrier-pigeon")
    assert isinstance(daemon.make_pad_link(lambda m: None), daemon.AutoPadLink)


def test_autopadlink_prefers_serial_when_both_answer(monkeypatch):
    # Serial discovery succeeds ...
    monkeypatch.setattr(daemon, "discover_port", lambda baud: "/dev/cu.usbmodem1")
    fake_serial_port = types.SimpleNamespace(
        read=lambda n: b"", write=lambda data: None, close=lambda: None,
    )
    monkeypatch.setattr(daemon.serial, "Serial", lambda *a, **k: fake_serial_port)

    # ... so HID discovery must never even be attempted.
    monkeypatch.setattr(
        daemon, "discover_hid_device",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HID should not be probed")),
    )

    link = daemon.AutoPadLink(lambda m: None)
    link.open()
    try:
        assert isinstance(link._active, daemon.SerialPadLink)
    finally:
        link.close()


def test_autopadlink_falls_back_to_hid_when_serial_finds_nothing(monkeypatch):
    monkeypatch.setattr(daemon, "discover_port", lambda baud: None)

    devices = [make_candidate(b"iface0")]
    responses = {b"iface0": hello_report()}
    monkeypatch.setattr(daemon, "hid", make_fake_hid(devices, responses))

    link = daemon.AutoPadLink(lambda m: None)
    link.open()
    try:
        assert isinstance(link._active, daemon.HidPadLink)
        assert link._active.attached is True
    finally:
        link.close()


def test_autopadlink_headless_when_neither_answers(monkeypatch):
    monkeypatch.setattr(daemon, "discover_port", lambda baud: None)
    monkeypatch.setattr(daemon, "hid", make_fake_hid([]))

    link = daemon.AutoPadLink(lambda m: None)
    link.open()
    link.write_json({"t": "slot", "i": 0, "state": "idle"})  # must not raise
    link.close()

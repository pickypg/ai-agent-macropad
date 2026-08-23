"""
HID connection to a QMK-based pad (e.g. NuPhy Air75 V2, Keychron K1 Pro).

Owns the raw-HID device handle's whole lifecycle: discovery (see
discover_hid_device()/discover_hid_pad() below), the open connection
itself, a background thread that translates inbound reports into the
dict shapes daemon.py's on_device_event() consumes, and reconnection
after the OS-level handle dies (e.g. a wireless pad's BT link dropping
while asleep).

HidPadLink.open()/close() are meant to be called repeatedly over the
life of a daemon process, not just once at startup/shutdown — see
daemon.py's Daemon._reconcile_pad(), which releases the handle
whenever no Claude Code sessions are active (so the VIA app, which
needs exclusive access to this same raw HID interface, can use it)
and reacquires it lazily on the next session. Both open() and close()
are written to be safe across multiple cycles on the same instance —
see the comments inline on the specific state each one resets.
"""
import logging
import threading
import time

try:
    import hid  # optional — only needed to actually attach to a pad
except ImportError:
    hid = None

import hid_protocol

log = logging.getLogger("macropad-daemon")

# QMK's raw HID usage page/usage (RAW_USAGE_PAGE/RAW_USAGE_ID defaults
# in tmk_core/protocol/usb_descriptor_common.h) — narrows HID discovery
# to the raw HID interface specifically, not the same board's normal
# boot-keyboard/NKRO HID interfaces, which share the same VID/PID.
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE_ID = 0x61


def discover_hid_device(vid, pid, handshake_timeout=1.5):
    """Find a QMK pad's raw-HID interface path without assuming it's
    the only HID interface the board exposes. A QMK keyboard enumerates
    several interfaces under one VID/PID (boot keyboard, NKRO, and the
    raw HID/VIA interface) — usage_page/usage narrows candidates to
    just the raw HID one (RAW_USAGE_PAGE/RAW_USAGE_ID). Each candidate
    then gets a MSG_PING and only counts as a match if it answers
    MSG_HELLO within handshake_timeout — raw HID is call-and-response
    (see hid_protocol.py), so nothing arrives unprompted.

    Returns the interface's opaque hidapi `path`, or None if nothing
    answered.
    """
    if hid is None:
        return None
    candidates = [
        d for d in hid.enumerate(vid, pid)
        if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE_ID
    ]
    log.info(
        "HID discovery: %d raw-HID interface(s) matching vid=0x%04x pid=0x%04x",
        len(candidates), vid, pid,
    )
    if not candidates:
        return None

    for candidate in candidates:
        path = candidate["path"]
        try:
            dev = hid.Device(path=path)
        except hid.HIDException as e:
            log.info("HID discovery: %r failed to open: %s", path, e)
            continue
        try:
            dev.write(bytes([0]) + hid_protocol.pack_ping())
            deadline = time.monotonic() + handshake_timeout
            while time.monotonic() < deadline:
                data = dev.read(hid_protocol.REPORT_SIZE, timeout=100)
                if not data:
                    continue
                msg = hid_protocol.parse_report(bytes(data))
                if msg and msg.get("t") == "hello":
                    log.info("HID discovery: %r replied hello — match", path)
                    return path
        except hid.HIDException as e:
            log.info("HID discovery: %r failed during handshake: %s", path, e)
        finally:
            dev.close()

    log.info("HID discovery: no interface answered hello within %ss", handshake_timeout)
    return None


def discover_hid_pad(candidates=hid_protocol.KNOWN_HID_PADS, handshake_timeout=1.5):
    """Tries each hid_protocol.KnownPad in `candidates` in turn via
    discover_hid_device(), returning (path, pad) for whichever one
    answers hello first, or None if none do. Lets HidPadLink
    auto-detect which known QMK pad (if any) is plugged in.
    """
    if hid is None:
        return None
    for pad in candidates:
        path = discover_hid_device(pad.vid, pad.pid, handshake_timeout=handshake_timeout)
        if path:
            return path, pad
    return None


def _pack_for_hid(obj):
    """Translate a write_json() dict into a Phase 1 binary report, or
    None if this message type has no HID encoding. Only "slot" and
    "clear" ever reach write_json() today — a cleared slot has no
    label to clear on an RGB-only pad, so it's just MSG_SLOT with
    state "off" (fully dark — distinct from "idle"'s dim glow).
    """
    t = obj.get("t")
    if t == "slot":
        return hid_protocol.pack_slot(obj["i"], obj["state"])
    if t == "clear":
        return hid_protocol.pack_slot(obj["i"], "off")
    return None


class HidPadLink:
    """Owns the HID connection to a QMK-based pad. write_json(obj) is
    host->device; a background thread handles device->host reads so
    the daemon's socket server never blocks on HID I/O.

    open()/close() are safe to call repeatedly on the same instance —
    see the comments in each about what state they reset, needed
    because Daemon._reconcile_pad() cycles this connection open and
    closed over the daemon's lifetime rather than opening it once at
    startup and closing it once at shutdown.
    """

    # Seconds between reconnect attempts once the handle has died — a
    # class attribute (not a literal in _reconnect()) so tests can
    # shrink it instead of eating this delay for real.
    RECONNECT_POLL_SECONDS = 2

    def __init__(self, on_device_event, vid=None, pid=None, on_reattach=None):
        self.on_device_event = on_device_event
        self.attached = False
        # Fired after a read loop that lost the connection (real
        # HIDException, not a plain read timeout — see _reconnect())
        # re-establishes it. Lets Daemon replay per-slot state the pad
        # missed while it was asleep/disconnected.
        self.on_reattach = on_reattach or (lambda: None)
        # Correlates a pending handshake() call with the "hello" reply
        # its ping eventually produces, without racing the background
        # reader thread for the same bytes — see _dispatch()/
        # handshake() below.
        self._hello_event = threading.Event()
        self._last_hello = None

        # An explicit vid/pid pins discovery to just that one (unnamed)
        # board; otherwise try every board this module knows about (see
        # hid_protocol.KNOWN_HID_PADS) and attach to whichever one
        # actually answers. self.vid/self.pid start as whatever was
        # passed in (possibly None) and get set to the pad that
        # actually answered once open()/_reconnect() succeeds.
        if vid is not None and pid is not None:
            self.candidates = (hid_protocol.KnownPad(None, vid, pid, None),)
        else:
            self.candidates = hid_protocol.KNOWN_HID_PADS
        self.vid = vid
        self.pid = pid
        self._dev = None
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        # Undo close()'s permanent-looking stop signal from a prior
        # cycle on this same instance — without this, a reader thread
        # started below would see _stop already set and exit
        # immediately, leaving attached=True but nothing actually
        # reading device->host reports.
        self._stop.clear()

        if hid is None:
            log.warning(
                "the 'hid' package isn't installed — HID transport unavailable "
                "(pip install hid, plus the native hidapi library)"
            )
            return
        found = discover_hid_pad(self.candidates)
        if not found:
            log.warning(
                "no known HID pad found (tried: %s) — running headless "
                "(plug in the pad, or check back once a session starts)",
                ", ".join("%s (0x%04x/0x%04x)" % (p.name or "unnamed", p.vid, p.pid) for p in self.candidates),
            )
            return
        path, pad = found
        self.vid, self.pid = pad.vid, pad.pid
        try:
            self._dev = hid.Device(path=path)
        except hid.HIDException:
            log.exception("failed to open HID device at %r", path)
            return
        self.attached = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log.info("attached to pad over HID (path=%r)", path)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._dev:
            self._dev.close()
        # Reset every bit of state open() depends on being fresh — a
        # caller that checks .attached right after close() (e.g.
        # Daemon._reconcile_pad()) needs an accurate answer, and a
        # later open() call needs a clean slate rather than a stale
        # handle it never reopens.
        self.attached = False
        self._dev = None
        self._thread = None

    def write_json(self, obj):
        report = _pack_for_hid(obj)
        if report is None:
            log.debug("HID transport: no encoding for %s, dropping", obj)
            return
        if self._dev is None:
            log.debug("(headless) would send: %s", obj)
            return
        try:
            # hidapi expects a leading report-ID byte on writes even
            # though QMK's raw HID interface doesn't use numbered
            # report IDs (id 0, implicit) — same convention on the
            # read side in _read_loop below.
            self._dev.write(bytes([0]) + report)
        except hid.HIDException:
            log.exception("HID write failed")

    def _send_ping(self):
        if self._dev is None:
            return
        try:
            self._dev.write(bytes([0]) + hid_protocol.pack_ping())
        except hid.HIDException:
            log.exception("HID write failed (handshake ping)")

    def _dispatch(self, msg):
        """The read loop calls this instead of on_device_event
        directly. A "hello" reply is forwarded normally *and* wakes up
        any handshake() call waiting on one — the same background
        thread does both jobs, so there's no separate blocking read to
        race against it.
        """
        if msg.get("t") == "hello":
            self._last_hello = msg
            self._hello_event.set()
        self.on_device_event(msg)

    def handshake(self, timeout=1.5):
        """Ask the device how many slots it has: pings and waits up to
        `timeout` seconds for a "hello" reply correlated via
        _dispatch() above. Returns {"slots": N, "protocol": P}, or
        None if open() found no pad (nothing to ask) or nothing valid
        replied in time — callers should fall back to a sane default
        rather than treat that as fatal, same posture as a headless
        pad generally. `protocol` is the pad's PROTOCOL_VERSION; the
        daemon warns (but still attaches) if it doesn't match ours.
        """
        if not self.attached:
            return None
        self._hello_event.clear()
        self._send_ping()
        if self._hello_event.wait(timeout):
            return {
                "slots": self._last_hello.get("slots"),
                "protocol": self._last_hello.get("protocol"),
            }
        log.warning("pad didn't answer a slots handshake within %ss", timeout)
        return None

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self._dev.read(hid_protocol.REPORT_SIZE, timeout=200)
            except hid.HIDException:
                # Unlike a plain read timeout (which just returns b""
                # and falls through below), HIDException means the
                # OS-level handle itself died — the realistic cause is
                # a wireless keyboard's BT link dropping while asleep,
                # not mere firmware quiet. Retrying read() on the same
                # self._dev can't recover from that; only rediscovering
                # and reopening can.
                log.warning("HID read failed — pad asleep/disconnected, reconnecting")
                self.attached = False
                self._reconnect()
                continue
            if not data:
                continue
            msg = hid_protocol.parse_report(bytes(data))
            if msg is None:
                log.warning("unrecognized HID report: %r", bytes(data))
                continue
            self._dispatch(msg)

    def _reconnect(self):
        """Block (without spinning) until the pad reappears over HID
        or close() is called, then swap in a fresh device handle.
        Re-runs discover_hid_device() each attempt rather than
        reopening the old path directly, since it may no longer be
        valid after the underlying handle died.
        """
        if self._dev:
            try:
                self._dev.close()
            except hid.HIDException:
                pass
            self._dev = None  # write_json()/_send_ping() already no-op on None
        while not self._stop.is_set():
            found = discover_hid_pad(self.candidates)
            if found:
                path, pad = found
                self.vid, self.pid = pad.vid, pad.pid
                try:
                    self._dev = hid.Device(path=path)
                    self.attached = True
                    log.info("pad reattached over HID (path=%r)", path)
                    self.on_reattach()
                    return
                except hid.HIDException:
                    pass
            self._stop.wait(self.RECONNECT_POLL_SECONDS)

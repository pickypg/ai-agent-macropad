#!/usr/bin/env python3
"""
Claude Code Macropad — Phase 3 daemon.

Listens for hook events on a Unix socket, maintains the
session_id -> pad slot mapping, and mirrors state to the pad over
either a serial MacroPad RP2040 or a QMK-based pad over HID (e.g. a
NuPhy Air75 V2) — see PadTransport below. Also reads key/encoder
events back from the pad — routing those to tmux panes is Phase 5, so
for now they're just logged to confirm the round trip works.

Usage:
    python3 daemon.py

By default the daemon auto-detects the pad (AutoPadLink): it tries
serial discovery first — probing every USB serial device advertising
Adafruit's vendor ID with a ping/hello handshake (see discover_port()
below and the "ping" handler in rp2040/code.py) — then HID discovery
(see discover_hid_device() below), attaching to whichever answers
first. Set MACROPAD_TRANSPORT=serial or MACROPAD_TRANSPORT=hid to
force one transport instead of auto-detecting, and
MACROPAD_SERIAL_PORT to skip serial discovery and force a specific
port:

    MACROPAD_TRANSPORT=hid python3 daemon.py
    MACROPAD_SERIAL_PORT=/dev/cu.usbmodem14201 python3 daemon.py

Runs headless (no crash, just logs what it *would* send) if no pad
answers either transport, so you can develop the socket/slot-mapping
logic before any hardware is plugged in.

Note (macOS): use the /dev/cu.* device, not /dev/tty.*. The tty
node waits on DCD/carrier-detect before some opens complete, which
can make pyserial's Serial() hang until the board is unplugged.
cu.* opens immediately and is the right node for a program writing
to the port (as opposed to an interactive `screen` session).

Requires: pip install pyserial. HID transport additionally requires
pip install hid, plus the native hidapi library (e.g. brew install
hidapi on macOS) — optional: daemon.py runs fine without either
installed, it just can't attach over HID.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial  # pyserial
import serial.tools.list_ports as list_ports

try:
    import hid  # optional — only needed for MACROPAD_TRANSPORT=hid/auto against a QMK pad
except ImportError:
    hid = None

import hid_protocol

# --- Config ------------------------------------------------------------

# Shared with hook.sh (which also drops itself here, see README's "Wire
# up real Claude Code hooks" step) so everything the daemon owns on
# disk lives under one directory instead of scattered directly in the
# home directory. Created eagerly since a standalone daemon run (e.g.
# via fake_hooks.py, before hook.sh has ever been copied anywhere) is
# the first thing to need it, both for the socket bind below and for
# the events-log handler created at import time further down.
CONFIG_DIR = Path(os.path.expanduser("~/.claude-macropad"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SOCKET_PATH = str(CONFIG_DIR / "daemon.sock")
SERIAL_PORT = os.environ.get("MACROPAD_SERIAL_PORT")  # e.g. /dev/tty.usbmodem14201
SERIAL_BAUD = 115200
NUM_SLOTS = 12
EVENTS_LOG_PATH = str(CONFIG_DIR / "events.log")

# USB vendor ID shared by every CircuitPython board (Adafruit's), used
# to narrow the auto-discovery scan below before it probes anything.
ADAFRUIT_USB_VID = 0x239A

# Must match DEVICE_ID in rp2040/code.py's "hello" response.
PAD_DEVICE_ID = "claude-macropad-v1"

# USB VID/PID for the NuPhy Air75 V2 (ANSI), confirmed against NuPhy's
# own keyboard.json during Phase 0. Unlike ADAFRUIT_USB_VID above,
# this alone disambiguates the board — no shared-vendor-ID ambiguity
# to resolve the way serial discovery has to. A second QMK board would
# need its own VID/PID here; out of scope until one is in hand (see
# the plan's "explicitly out of scope" section).
NUPHY_USB_VID = 0x19F5
NUPHY_USB_PID = 0x3246

# QMK's raw HID usage page/usage (RAW_USAGE_PAGE/RAW_USAGE_ID defaults
# in tmk_core/protocol/usb_descriptor_common.h) — narrows HID discovery
# to the raw HID interface specifically, not the same board's normal
# boot-keyboard/NKRO HID interfaces, which share the same VID/PID.
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE_ID = 0x61

# "serial" | "hid" | unset (auto: try serial discovery, then HID, then
# run headless if neither answers — see AutoPadLink).
TRANSPORT = os.environ.get("MACROPAD_TRANSPORT")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("macropad-daemon")

# Separate logger + rotating file, deliberately independent of the
# console logger above. Purpose: capture the raw bytes of every line
# that hits the socket — valid JSON or not — so a framing/parsing bug
# like the jq-pretty-print issue can be diagnosed from disk after the
# fact instead of requiring you to reproduce it live with the daemon
# open in front of you. propagate=False keeps this out of the console
# log so the two don't duplicate each other.
events_log = logging.getLogger("macropad-events")
events_log.propagate = False
events_log.setLevel(logging.INFO)
_events_handler = logging.handlers.RotatingFileHandler(
    EVENTS_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3
)
_events_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
events_log.addHandler(_events_handler)


# --- Hook event -> pad state mapping (build brief §5) --------------------

# Tools where Claude is blocked waiting on the user, but nothing has
# gone wrong — distinct from an actual error, and distinct from a
# generic "working" tool call. Notification's "waiting" (permission
# prompts) stays separate too: those are lower-urgency than these.
ATTENTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


def hook_to_state(event_name, tool_name=None, notification_type=None):
    """Pad state a given hook event maps to, or None if this hook
    doesn't change display state by itself (e.g. SubagentStop).

    AskUserQuestion and ExitPlanMode are special-cased: both are
    PreToolUse events, but both mean Claude is blocked on the user
    making a choice, not just "running a tool" — worth a visually
    distinct (and blinking, on the pad side) state.

    Notification is also special-cased by subtype: agent_needs_input
    means Claude is fully stalled until you answer, same urgency as
    AskUserQuestion, so it maps to "question" too. idle_prompt
    (Claude's been idle 60s+) is lower-stakes and stays "waiting".
    permission_prompt is deliberately NOT handled here — confirmed
    unreliable in practice (never fired for a real "Allow this
    command?" prompt, even on a current Claude Code version).
    PermissionRequest, handled separately below, is the working
    replacement for that specific case.
    """
    if event_name == "PreToolUse" and tool_name in ATTENTION_TOOLS:
        return "question"

    if event_name == "PermissionRequest":
        # More literal match for "the interactive approval dialog is
        # showing" than Notification:permission_prompt — worth testing
        # as the primary signal, since Notification's reliability is
        # unclear even on a current Claude Code version.
        return "question"

    if event_name == "Notification":
        if notification_type == "agent_needs_input":
            return "question"
        if notification_type == "idle_prompt":
            return "waiting"
        return None  # other subtypes (auth_success, elicitation_*, ...) — no state change

    return {
        "SessionStart": "idle",
        "UserPromptSubmit": "working",
        "PreToolUse": "working",
        "PostToolUse": "working",
        "PostToolUseFailure": "error",
        "Stop": "done",
    }.get(event_name)


# --- Slot allocation -------------------------------------------------------

class SlotManager:
    """Maps Claude Code session_id -> pad slot index (0..num_slots-1).

    Deliberately naive for Phase 3: first-fit allocation, no eviction.
    §9's open question (LRU eviction vs. OLED paging past num_slots
    concurrent sessions) is intentionally left unanswered here.
    """

    def __init__(self, num_slots):
        self.num_slots = num_slots
        self.session_to_slot = {}
        self.slot_to_session = [None] * num_slots
        self.lock = threading.Lock()

    def allocate(self, session_id):
        with self.lock:
            if session_id in self.session_to_slot:
                return self.session_to_slot[session_id]
            for i in range(self.num_slots):
                if self.slot_to_session[i] is None:
                    self.slot_to_session[i] = session_id
                    self.session_to_slot[session_id] = i
                    return i
            log.warning(
                "no free slots for session %s (>%d concurrent)",
                session_id, self.num_slots,
            )
            return None

    def free(self, session_id):
        with self.lock:
            i = self.session_to_slot.pop(session_id, None)
            if i is not None:
                self.slot_to_session[i] = None
            return i

    def slot_for(self, session_id):
        with self.lock:
            return self.session_to_slot.get(session_id)


# --- Port auto-discovery ------------------------------------------------

def discover_port(baud=SERIAL_BAUD, handshake_timeout=1.5):
    """Find the pad's serial device without a hardcoded port number.

    The /dev/cu.usbmodemNNNN suffix macOS assigns isn't stable across
    replugs or reboots, so MACROPAD_SERIAL_PORT would otherwise need
    updating by hand every time. Instead: narrow candidates to devices
    advertising Adafruit's USB vendor ID (shared by every CircuitPython
    board, so this alone isn't a reliable enough match if more than one
    such board is attached), then send each a {"t": "ping"} and only
    accept it as the pad if it answers {"t": "hello", "device":
    "claude-macropad-v1"} within handshake_timeout — see the "ping"
    handler in rp2040/code.py.

    Returns the device path, or None if nothing answered.
    """
    all_ports = list(list_ports.comports())
    candidates = [p for p in all_ports if p.vid == ADAFRUIT_USB_VID]
    log.info(
        "discovery: %d serial port(s) total, %d matching vid=0x%04x: %s",
        len(all_ports), len(candidates), ADAFRUIT_USB_VID,
        ", ".join(p.device for p in candidates) or "<none>",
    )
    if not candidates:
        return None

    found = []
    for p in candidates:
        replied_ids = []
        try:
            with serial.Serial(p.device, baud, timeout=0.2) as ser:
                ser.reset_input_buffer()
                ser.write((json.dumps({"t": "ping"}) + "\n").encode("utf-8"))
                buf = b""
                deadline = time.monotonic() + handshake_timeout
                while time.monotonic() < deadline:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if msg.get("t") != "hello":
                            continue
                        replied_ids.append(msg.get("device"))
                        if msg.get("device") == PAD_DEVICE_ID:
                            found.append(p.device)
                            break
                    if found and found[-1] == p.device:
                        break
        except serial.SerialException as e:
            log.info("discovery: %s failed to open: %s", p.device, e)
            continue

        if found and found[-1] == p.device:
            log.info("discovery: %s replied hello device=%r — match", p.device, PAD_DEVICE_ID)
        elif replied_ids:
            log.info(
                "discovery: %s replied hello device=%r — expected %r, skipping",
                p.device, replied_ids[-1], PAD_DEVICE_ID,
            )
        else:
            log.info("discovery: %s — no hello reply within %ss", p.device, handshake_timeout)

    if not found:
        return None
    if len(found) > 1:
        log.warning(
            "multiple pads responded to discovery (%s) — using %s",
            ", ".join(found), found[0],
        )
    return found[0]


# --- HID discovery -------------------------------------------------------

def discover_hid_device(vid, pid, handshake_timeout=1.5):
    """Find a QMK pad's raw-HID interface path without assuming it's
    the only HID interface the board exposes. A QMK keyboard enumerates
    several interfaces under one VID/PID (boot keyboard, NKRO, and the
    raw HID/VIA interface) — usage_page/usage narrows candidates to
    just the raw HID one (RAW_USAGE_PAGE/RAW_USAGE_ID), the same way
    discover_port() narrows serial candidates by vendor ID before
    probing. Each candidate then gets a MSG_PING and only counts as a
    match if it answers MSG_HELLO within handshake_timeout — raw HID
    is call-and-response (see hid_protocol.py), so nothing arrives
    unprompted the way discover_port() can just listen after a plain
    ping.

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


# --- Pad transports --------------------------------------------------------

class PadTransport:
    """Common interface every pad link implements: open()/close() own
    the connection lifecycle, write_json(obj) is host -> device, and
    on_device_event(dict) (passed in here, called from each subclass's
    own background reader thread) is device -> host. Everything
    downstream — SlotManager, hook_to_state, Daemon.handle_hook_event,
    the stall watcher — only ever touches these, so it needs zero
    changes regardless of which transport (or AutoPadLink's choice
    between them) is actually active.
    """

    def __init__(self, on_device_event):
        self.on_device_event = on_device_event
        self.attached = False
        # Correlates a pending handshake() call with the "hello" reply
        # its ping eventually produces, without a second connection or
        # racing the background reader thread for the same bytes —
        # see _dispatch()/handshake() below.
        self._hello_event = threading.Event()
        self._last_hello = None

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def write_json(self, obj):
        raise NotImplementedError

    def _send_ping(self):
        raise NotImplementedError

    def _dispatch(self, msg):
        """Every subclass's read loop calls this instead of
        on_device_event directly. A "hello" reply is forwarded
        normally *and* wakes up any handshake() call waiting on one —
        the same background thread does both jobs, so there's no
        separate blocking read to race against it.
        """
        if msg.get("t") == "hello":
            self._last_hello = msg
            self._hello_event.set()
        self.on_device_event(msg)

    def handshake(self, timeout=1.5):
        """Ask the device how many slots it has (Phase 3): pings and
        waits up to `timeout` seconds for a "hello" reply correlated
        via _dispatch() above. Returns {"slots": N}, or None if
        open() found no pad (nothing to ask) or nothing valid replied
        in time — callers should fall back to a sane default rather
        than treat that as fatal, same posture as a headless pad
        generally.
        """
        if not self.attached:
            return None
        self._hello_event.clear()
        self._send_ping()
        if self._hello_event.wait(timeout):
            return {"slots": self._last_hello.get("slots")}
        log.warning("pad didn't answer a slots handshake within %ss", timeout)
        return None


class SerialPadLink(PadTransport):
    """Owns the serial connection to a MacroPad RP2040. write_json() is
    host->device; a background thread handles device->host reads so
    the socket server never blocks on serial I/O."""

    def __init__(self, port, baud, on_device_event):
        super().__init__(on_device_event)
        self.port = port
        self.baud = baud
        self._ser = None
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        if not self.port:
            log.info("no MACROPAD_SERIAL_PORT set — probing for the pad")
            self.port = discover_port(self.baud)
            if not self.port:
                log.warning(
                    "no pad found via serial auto-discovery — running headless "
                    "(plug in the pad, or set MACROPAD_SERIAL_PORT to force a port)"
                )
                return
        self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
        self.attached = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log.info("attached to pad on %s", self.port)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._ser:
            self._ser.close()

    def write_json(self, obj):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        if self._ser is None:
            log.debug("(headless) would send: %s", line)
            return
        try:
            self._ser.write(line)
        except serial.SerialException:
            log.exception("serial write failed")

    def _send_ping(self):
        if self._ser is None:
            return
        try:
            self._ser.write((json.dumps({"t": "ping"}) + "\n").encode("utf-8"))
        except serial.SerialException:
            log.exception("serial write failed (handshake ping)")

    def _read_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except serial.SerialException:
                log.exception("serial read failed — retrying in 1s")
                time.sleep(1)
                continue
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        log.warning("bad json from pad: %r", line)
                        continue
                    self._dispatch(msg)


def _pack_for_hid(obj):
    """Translate a write_json() dict into a Phase 1 binary report, or
    None if this message type has no HID encoding. Only "slot" and
    "clear" ever reach write_json() today (see handle_hook_event and
    on_device_event below) — a cleared slot has no label to clear on
    an RGB-only pad, so it's just MSG_SLOT with state "off" (fully
    dark — distinct from "idle"'s dim glow, matching rp2040/code.py's
    handle_message(); see hid_protocol.py's module docstring).
    """
    t = obj.get("t")
    if t == "slot":
        return hid_protocol.pack_slot(obj["i"], obj["state"])
    if t == "clear":
        return hid_protocol.pack_slot(obj["i"], "off")
    return None


class HidPadLink(PadTransport):
    """Owns the HID connection to a QMK-based pad (e.g. NuPhy Air75
    V2). Same open()/close()/write_json() shape as SerialPadLink, but
    reports are hid_protocol.py's fixed-size binary reports instead of
    JSON lines, and raw HID is call-and-response — the device never
    pushes MSG_HELLO unprompted, so discovery always pings first (see
    discover_hid_device() above).
    """

    def __init__(self, on_device_event, vid=NUPHY_USB_VID, pid=NUPHY_USB_PID):
        super().__init__(on_device_event)
        self.vid = vid
        self.pid = pid
        self._dev = None
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        if hid is None:
            log.warning(
                "the 'hid' package isn't installed — HID transport unavailable "
                "(pip install hid, plus the native hidapi library)"
            )
            return
        path = discover_hid_device(self.vid, self.pid)
        if not path:
            log.warning(
                "no HID pad found (vid=0x%04x pid=0x%04x) — running headless "
                "(plug in the pad, or check MACROPAD_TRANSPORT)",
                self.vid, self.pid,
            )
            return
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
            # report IDs (id 0, implicit) — confirm this convention
            # (and the equivalent on the read side, in _read_loop
            # below) holds during Phase 6's real-hardware bring-up.
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

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self._dev.read(hid_protocol.REPORT_SIZE, timeout=200)
            except hid.HIDException:
                log.exception("HID read failed — retrying in 1s")
                time.sleep(1)
                continue
            if not data:
                continue
            msg = hid_protocol.parse_report(bytes(data))
            if msg is None:
                log.warning("unrecognized HID report: %r", bytes(data))
                continue
            self._dispatch(msg)


class AutoPadLink(PadTransport):
    """Default when MACROPAD_TRANSPORT isn't set: tries serial
    discovery first (today's more mature, more-tested path), then
    falls back to HID discovery, and finally runs headless if neither
    pad answers. A session only ever has one physical pad attached, so
    this is discovery order, not fan-out to both at once — whichever
    transport actually attaches owns write_json/close from then on.
    """

    def __init__(self, on_device_event):
        super().__init__(on_device_event)
        self._active = None

    def open(self):
        serial_link = SerialPadLink(SERIAL_PORT, SERIAL_BAUD, self.on_device_event)
        serial_link.open()
        if serial_link.attached:
            self._active = serial_link
            return

        hid_link = HidPadLink(self.on_device_event)
        hid_link.open()
        if hid_link.attached:
            self._active = hid_link
            return

        log.warning("no pad found via serial or HID auto-discovery — running headless")
        self._active = serial_link  # headless SerialPadLink already logs "(headless) would send"

    def write_json(self, obj):
        if self._active is None:
            log.debug("(headless, not yet open) would send: %s", obj)
            return
        self._active.write_json(obj)

    def handshake(self, timeout=1.5):
        # Delegates rather than using PadTransport's own implementation
        # — AutoPadLink has no connection or reader thread of its own,
        # just whichever concrete transport open() picked.
        if self._active is None:
            return None
        return self._active.handshake(timeout)

    def close(self):
        if self._active:
            self._active.close()


def make_pad_link(on_device_event):
    """Picks the pad transport per MACROPAD_TRANSPORT (see module
    docstring): "serial" or "hid" force one explicitly, anything else
    (including unset) gets AutoPadLink's try-serial-then-hid order.
    """
    if TRANSPORT == "serial":
        return SerialPadLink(SERIAL_PORT, SERIAL_BAUD, on_device_event)
    if TRANSPORT == "hid":
        return HidPadLink(on_device_event)
    if TRANSPORT is not None:
        log.warning(
            "unrecognized MACROPAD_TRANSPORT=%r (expected 'serial' or 'hid') "
            "— auto-detecting instead",
            TRANSPORT,
        )
    return AutoPadLink(on_device_event)


# --- Daemon: socket server + hook->state logic -------------------------

# Notification:permission_prompt is known to be unreliable — it can
# silently not fire at all (anthropics/claude-code#58909, a 6-second
# idle-gate bug that misses prompts appearing during active thinking).
# As a backstop that doesn't depend on that hook firing, a PreToolUse
# with no matching PostToolUse/PostToolUseFailure within this many
# seconds is treated as probably blocked on the user and escalated to
# "question".
#
# Tradeoff: this can't distinguish "waiting on you" from "just a slow
# command" — a long-running Bash call (npm install, a test suite) will
# false-positive into "question" until it finishes. 10s is a guess at
# a reasonable middle ground; raise it if slow-but-legitimate commands
# are triggering false blinks, lower it if real prompts are taking too
# long to show up. Set to None to disable the heuristic entirely.
STALL_THRESHOLD_SECONDS = 10


class Daemon:
    def __init__(self):
        # Default sizing — used as-is by tests and any headless run
        # (see recording_daemon in tests/conftest.py). serve() resizes
        # this after the pad's handshake reports its real slot count
        # (Phase 3), so a running daemon with real hardware attached
        # never actually uses NUM_SLOTS unless the handshake fails.
        self.slots = SlotManager(NUM_SLOTS)
        self.pad = make_pad_link(self.on_device_event)
        # session_id -> {"slot": i, "tool_name": ..., "since": monotonic
        # timestamp, "escalated": bool}. Populated on PreToolUse when the
        # initial state is "working" (not already "question"/etc.),
        # cleared on PostToolUse or PostToolUseFailure for that session.
        self.pending_calls = {}
        # session_id -> tmux pane id (e.g. "%37"), as reported by
        # hook.sh reading $TMUX_PANE from its own environment.
        # Populated at SessionStart, cleared at SessionEnd. Empty
        # string (not running inside tmux) is never stored.
        self.session_panes = {}
        # session_id -> project folder name (same string already used
        # for the pad's slot label), used to match a VS Code window by
        # title when no tmux pane is available. This is the primary
        # path when Claude Code runs in VS Code's integrated terminal
        # rather than inside tmux.
        self.session_projects = {}
        # session_id -> controlling tty device (e.g. "/dev/ttys003"),
        # captured by hook.sh at SessionStart via ps against its
        # parent process. Used to find and select the exact
        # Terminal.app tab this session is running in — an exact
        # match, unlike VS Code's fuzzy title substring match.
        self.session_ttys = {}

    # device -> host: keypress / encoder events from the pad.
    #
    # Phase 5 scope, deliberately minimal: a key press does exactly
    # one thing — bring that session's window to the front. Nothing
    # else (no approve/reject/esc/compact) is wired yet. Dispatch
    # mechanisms, tried in order:
    #   1. tmux select-window, if a pane was recorded for this session
    #   2. Terminal.app tab activation, matched by exact controlling
    #      tty — only ever populated at SessionStart (see hook.sh)
    #   3. VS Code window activation via AppleScript, matched by
    #      project folder name
    #   4. IntelliJ IDEA window activation, same project-name match —
    #      tried after VS Code since both are gated on the same
    #      `project` value and either/neither may actually be running
    #
    # Encoder events are still just logged; scrolling/paging is Phase
    # 6 territory since self.slots.num_slots currently matches the
    # pad's physical key count 1:1, so there's nothing to page through
    # yet.
    def on_device_event(self, msg):
        t = msg.get("t")
        if t == "key":
            i = msg.get("i")
            valid_index = i is not None and i < self.slots.num_slots
            session_id = self.slots.slot_to_session[i] if valid_index else None
            log.info("key %s -> session %s", i, session_id)
            if session_id is None:
                events_log.info("DISPATCH key=%s result=no_session", i)
                if valid_index:
                    # Slot has no session mapped (stale key press, race
                    # with SessionEnd, or the pad's own state drifted
                    # from ours) — clear its color/OLED rather than
                    # leaving it showing whatever it last displayed.
                    self.pad.write_json({"t": "clear", "i": i})
                return
            self.dispatch_bring_to_front(i, session_id)
        elif t == "enc":
            log.info("encoder delta %s", msg.get("d"))
        elif t == "enc_click":
            log.info("encoder click")
        else:
            log.info("unhandled device event: %s", msg)

    def dispatch_bring_to_front(self, slot, session_id):
        pane = self.session_panes.get(session_id)
        if pane and self._dispatch_tmux(slot, pane):
            return

        tty = self.session_ttys.get(session_id)
        if tty and self._dispatch_terminal(slot, tty):
            return

        project = self.session_projects.get(session_id)
        if project and self._dispatch_vscode(slot, project):
            return
        if project and self._dispatch_intellij(slot, project):
            return

        if not pane and not tty and not project:
            log.warning(
                "no tmux pane, tty, or project name known for slot %s (session=%s)",
                slot, session_id,
            )
            events_log.info(
                "DISPATCH slot=%s session=%s result=no_target_known", slot, session_id
            )
        # else: whichever dispatch(es) were attempted already logged
        # their own specific failure reason.

    def _dispatch_tmux(self, slot, pane):
        """Try tmux select-window. Returns True on success, False on
        any failure (falls through to VS Code dispatch if available).
        Runs on the pad transport's background reader thread (not the
        asyncio event loop), so a blocking subprocess call here is
        fine — it only stalls pad reads briefly, not hook processing.
        """
        try:
            result = subprocess.run(
                ["tmux", "select-window", "-t", pane],
                capture_output=True,
                timeout=2,
                text=True,
            )
        except FileNotFoundError:
            log.warning("tmux not found on PATH — can't dispatch keypress")
            events_log.info("DISPATCH slot=%s pane=%s result=tmux_not_found", slot, pane)
            return False
        except subprocess.TimeoutExpired:
            log.warning("tmux select-window timed out for pane %s", pane)
            events_log.info("DISPATCH slot=%s pane=%s result=timeout", slot, pane)
            return False

        if result.returncode != 0:
            # Most common cause: the pane no longer exists (session
            # ended outside of our SessionEnd hook catching it — e.g.
            # the tmux pane was killed directly) — not a crash, just
            # a stale mapping.
            log.warning(
                "tmux select-window failed for pane %s: %s", pane, result.stderr.strip()
            )
            events_log.info(
                "DISPATCH slot=%s pane=%s result=tmux_error stderr=%r",
                slot, pane, result.stderr.strip(),
            )
            return False

        log.info("selected tmux window for pane %s (slot %s)", pane, slot)
        events_log.info("DISPATCH slot=%s pane=%s result=ok", slot, pane)
        return True

    def _dispatch_terminal(self, slot, tty):
        """Try to select+raise the Terminal.app tab whose tty matches
        exactly. macOS only. Returns True on success, False on any
        failure (falls through to VS Code dispatch if available).

        Uses Terminal.app's own AppleScript dictionary (windows/tabs/
        tty are native properties) rather than System Events GUI
        scripting — only System Events is needed for the up-front
        process-existence check, to avoid accidentally launching
        Terminal.app via `tell application "Terminal"` if it isn't
        already running.

        This naturally never matches when the session is actually
        running inside tmux: the tty captured at SessionStart is
        whatever pty was current at that moment, which inside tmux is
        the pane's own inner pty, not the outer Terminal.app tab
        hosting the tmux client. No special-casing needed to keep the
        two paths from interfering — tmux is tried first anyway.

        Same permission requirement as VS Code dispatch: the terminal
        running this daemon needs Accessibility/Automation access
        granted for "System Events" and, separately, "Terminal" — a
        second permission prompt distinct from the first.
        """
        safe_tty = tty.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "Terminal") then
                return "no_process"
            end if
        end tell
        tell application "Terminal"
            set foundTab to missing value
            set foundWindow to missing value
            repeat with w in windows
                repeat with t in tabs of w
                    try
                        if (tty of t) is "{safe_tty}" then
                            set foundTab to t
                            set foundWindow to w
                            exit repeat
                        end if
                    end try
                end repeat
                if foundTab is not missing value then exit repeat
            end repeat
            if foundTab is missing value then
                return "no_match"
            end if
            set selected tab of foundWindow to foundTab
            set frontmost of foundWindow to true
            activate
            return "ok"
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — Terminal dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s tty=%s result=osascript_not_found", slot, tty
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating Terminal tab for %s", tty)
            events_log.info("DISPATCH slot=%s tty=%s result=timeout", slot, tty)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning("Terminal tab activation failed for tty %r: %s", tty, reason)
            events_log.info(
                "DISPATCH slot=%s tty=%s result=terminal_error reason=%r",
                slot, tty, reason,
            )
            return False

        log.info("activated Terminal tab for tty %r (slot %s)", tty, slot)
        events_log.info("DISPATCH slot=%s tty=%s result=ok", slot, tty)
        return True

    def _dispatch_vscode(self, slot, project):
        """Try to raise a VS Code window whose title contains the
        project folder name, via AppleScript/System Events. macOS
        only. Returns True on success, False on any failure.

        Known limitations (not fixable without a companion VS Code
        extension):
          - Window-level only, not tab-level. Two sessions running as
            two integrated-terminal tabs in ONE VS Code window can't
            be distinguished — both resolve to the same window.
          - Matches by substring on window title. A project named
            "app" will also match a window titled "my-app — Visual
            Studio Code". Ambiguous matches silently pick the first
            one System Events returns.
          - Requires the terminal running this daemon to have been
            granted Accessibility/Automation permission for "System
            Events" and "Visual Studio Code" in System Settings ->
            Privacy & Security. Without it, osascript fails with a
            permission error, logged like any other failure.
          - Depends on VS Code's default window-title format (folder
            name visible in the title). A customized "window.title"
            setting in VS Code can break the match.
        """
        # Defensive escaping — project names come from folder names,
        # but a stray embedded double-quote would otherwise break out
        # of the AppleScript string literal.
        safe_project = project.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "Code") then
                return "no_process"
            end if
            tell process "Code"
                set matchingWindows to (windows whose name contains "{safe_project}")
                if (count of matchingWindows) is 0 then
                    return "no_window"
                end if
                perform action "AXRaise" of item 1 of matchingWindows
            end tell
        end tell
        tell application "Visual Studio Code" to activate
        return "ok"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — VS Code dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s project=%s result=osascript_not_found", slot, project
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating VS Code window for %s", project)
            events_log.info("DISPATCH slot=%s project=%s result=timeout", slot, project)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            # returncode != 0 with no clean "no_process"/"no_window"
            # output usually means the Accessibility/Automation
            # permission prompt hasn't been granted yet — stderr will
            # say so explicitly the first time.
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning(
                "VS Code window activation failed for project %r: %s", project, reason
            )
            events_log.info(
                "DISPATCH slot=%s project=%s result=vscode_error reason=%r",
                slot, project, reason,
            )
            return False

        log.info("activated VS Code window for project %r (slot %s)", project, slot)
        events_log.info("DISPATCH slot=%s project=%s result=ok", slot, project)
        return True

    def _dispatch_intellij(self, slot, project):
        """Try to raise an IntelliJ IDEA window whose title contains
        the project folder name, via AppleScript/System Events. macOS
        only. Returns True on success, False on any failure.

        Same approach and the same window-level/substring-match
        limitations as _dispatch_vscode above (see its docstring) —
        including needing its own, separate Accessibility/Automation
        grant for "IntelliJ IDEA", distinct from VS Code's — plus one
        more specific to this app:
          - Assumes the System Events process name is "idea", which
            matches a standalone IntelliJ IDEA install (Community or
            Ultimate). A JetBrains Toolbox install, a differently
            named .app bundle, or another JetBrains IDE (PyCharm,
            WebStorm, ...) can report a different process name and
            won't match — adjust the process/application names below
            if that's your setup.
        """
        safe_project = project.replace("\\", "\\\\").replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if not (exists process "idea") then
                return "no_process"
            end if
            tell process "idea"
                set matchingWindows to (windows whose name contains "{safe_project}")
                if (count of matchingWindows) is 0 then
                    return "no_window"
                end if
                perform action "AXRaise" of item 1 of matchingWindows
            end tell
        end tell
        tell application "IntelliJ IDEA" to activate
        return "ok"
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
                text=True,
            )
        except FileNotFoundError:
            log.warning("osascript not found — IntelliJ dispatch requires macOS")
            events_log.info(
                "DISPATCH slot=%s project=%s result=osascript_not_found", slot, project
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out activating IntelliJ window for %s", project)
            events_log.info("DISPATCH slot=%s project=%s result=timeout", slot, project)
            return False

        output = result.stdout.strip()
        if result.returncode != 0 or output != "ok":
            reason = output or result.stderr.strip() or "unknown_error"
            log.warning(
                "IntelliJ window activation failed for project %r: %s", project, reason
            )
            events_log.info(
                "DISPATCH slot=%s project=%s result=intellij_error reason=%r",
                slot, project, reason,
            )
            return False

        log.info("activated IntelliJ window for project %r (slot %s)", project, slot)
        events_log.info("DISPATCH slot=%s project=%s result=ok", slot, project)
        return True

    # host -> device: one line of raw hook JSON, piped straight through
    # by hook.sh with no transformation. hook_event_name is provided by
    # Claude Code itself in every event's payload (confirmed in the
    # official hooks reference) — no need to inject it, unlike the
    # brief's original jq one-liner assumed.
    def handle_hook_event(self, payload):
        event_name = payload.get("hook_event_name")
        session_id = payload.get("session_id")

        # Log every event unconditionally, before any mapping logic.
        # Without this, a Notification that arrives but maps to
        # state=None (unrecognized notification_type, or the field
        # missing entirely) is completely silent — you'd see nothing
        # in the logs and have no way to tell "event never arrived"
        # apart from "event arrived but didn't map to a state change".
        log.info(
            "hook event: %s (session=%s tool=%s notification_type=%s)",
            event_name,
            session_id,
            payload.get("tool_name"),
            payload.get("notification_type"),
        )
        # Full payload, persisted — handle_connection's RAW line covers
        # the same bytes, but logging the parsed dict here too means a
        # grep for "PAYLOAD" finds only things that parsed successfully
        # and reached this function, without wading through RAW/
        # PARSE_FAILED noise from other connections.
        events_log.info("PAYLOAD %s", json.dumps(payload))

        if not session_id:
            log.warning("hook event missing session_id: %s", payload)
            events_log.info("DROPPED reason=no_session_id")
            return

        if event_name == "SessionStart":
            i = self.slots.allocate(session_id)
            pane = payload.get("tmux_pane")
            if pane:
                self.session_panes[session_id] = pane
            tty = payload.get("controlling_tty")
            if tty:
                self.session_ttys[session_id] = tty
            label = Path(payload.get("cwd", "")).name or session_id[:8]
            self.session_projects[session_id] = label
            if i is not None:
                self.pad.write_json(
                    {"t": "slot", "i": i, "state": "idle", "label": label}
                )
                events_log.info(
                    "MAPPED slot=%s state=idle pane=%s tty=%s project=%s (SessionStart)",
                    i, pane or "<none>", tty or "<none>", label,
                )
            return

        if event_name == "SessionEnd":
            i = self.slots.free(session_id)
            self.pending_calls.pop(session_id, None)
            self.session_panes.pop(session_id, None)
            self.session_ttys.pop(session_id, None)
            self.session_projects.pop(session_id, None)
            if i is not None:
                self.pad.write_json({"t": "clear", "i": i})
                events_log.info("MAPPED slot=%s cleared (SessionEnd)", i)
            return

        i = self.slots.slot_for(session_id)
        if i is None:
            # Hook arrived before SessionStart, we're past the pad's
            # slot capacity (self.slots.num_slots — sized from the
            # handshake in serve(), see Phase 3), or (confirmed
            # 2026-08-02) the daemon
            # was restarted mid-session so this process never saw that
            # session's SessionStart. Allocate lazily rather than drop
            # the event on the floor, and backfill project/pane here
            # too — cwd and tmux_pane are present on every hook event,
            # not just SessionStart — otherwise dispatch_bring_to_front
            # never learns a target for this session and every keypress
            # logs no_target_known until the session ends.
            i = self.slots.allocate(session_id)
            if i is None:
                events_log.info("DROPPED reason=no_free_slot")
                return
            pane = payload.get("tmux_pane")
            if pane:
                self.session_panes[session_id] = pane
            label = Path(payload.get("cwd", "")).name or session_id[:8]
            self.session_projects[session_id] = label

        state = hook_to_state(
            event_name, payload.get("tool_name"), payload.get("notification_type")
        )

        # Stall-detection bookkeeping — independent of whether this
        # event changes the displayed state. See STALL_THRESHOLD_SECONDS
        # for why this exists: Notification:permission_prompt can't be
        # trusted to fire on its own.
        if event_name == "PreToolUse" and state == "working":
            self.pending_calls[session_id] = {
                "slot": i,
                "tool_name": payload.get("tool_name"),
                "since": time.monotonic(),
                "escalated": False,
            }
        elif event_name in ("PostToolUse", "PostToolUseFailure"):
            self.pending_calls.pop(session_id, None)

        if state is None:
            # This is the exact case that made the jq bug invisible
            # before: an event arrives and parses fine, but maps to no
            # state change. Logging it explicitly means "no mapping"
            # is now distinguishable from "never arrived" by grepping
            # the events log instead of having to reason about it.
            events_log.info(
                "MAPPED slot=%s state=<none> event=%s tool=%s notification_type=%s",
                i, event_name, payload.get("tool_name"), payload.get("notification_type"),
            )
            return  # e.g. SubagentStop — no direct display change yet

        msg = {"t": "slot", "i": i, "state": state}
        if event_name == "PreToolUse" and payload.get("tool_name"):
            msg["label"] = payload["tool_name"]
        self.pad.write_json(msg)
        events_log.info("MAPPED slot=%s state=%s event=%s", i, state, event_name)

    async def watch_stalled_calls(self):
        """Backstop for Notification:permission_prompt's unreliability.

        Polls pending_calls once a second; anything sitting past
        STALL_THRESHOLD_SECONDS without a PostToolUse/PostToolUseFailure
        gets escalated to "question" exactly once. No further action
        needed on the daemon's part after that — the normal PostToolUse
        handling in handle_hook_event already downgrades back to
        "working"/"done" whenever the tool call actually finishes,
        whether or not it was escalated first.
        """
        if STALL_THRESHOLD_SECONDS is None:
            return
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for session_id, pending in list(self.pending_calls.items()):
                if pending["escalated"]:
                    continue
                if now - pending["since"] < STALL_THRESHOLD_SECONDS:
                    continue
                pending["escalated"] = True
                self.pad.write_json({"t": "slot", "i": pending["slot"], "state": "question"})
                events_log.info(
                    "MAPPED slot=%s state=question event=stall_detected tool=%s elapsed=%.1fs",
                    pending["slot"], pending["tool_name"], now - pending["since"],
                )
                log.info(
                    "stall detected: session=%s tool=%s elapsed=%.1fs — escalating to question",
                    session_id, pending["tool_name"], now - pending["since"],
                )

    async def handle_connection(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                # Log the raw bytes exactly as received, before parsing,
                # so a framing bug (e.g. multi-line jq output splitting
                # one object into several "lines") is visible in the
                # persisted log even though json.loads() below never
                # succeeds for it.
                events_log.info("RAW %r", line)

                try:
                    payload = json.loads(line)
                except ValueError:
                    log.warning("bad json on socket: %r", line)
                    events_log.info("PARSE_FAILED %r", line)
                    continue
                self.handle_hook_event(payload)
        finally:
            writer.close()

    def apply_handshake(self, handshake):
        """Size self.slots from a pad's handshake() result (Phase 3),
        so "if it supports 1, fine; if it supports 100, great" — the
        daemon never hardcodes a number, it asks the hardware. Split
        out from serve() so this decision is unit-testable without an
        asyncio event loop.

        Falls back to keeping __init__'s NUM_SLOTS-sized default if
        `handshake` is None (headless, or the pad didn't answer in
        time) — a best-effort capability query, not a hard
        requirement. Any session mappings already recorded in the old
        SlotManager are intentionally discarded: this only ever runs
        once, before start_unix_server in serve(), so there aren't
        any yet.
        """
        if handshake and handshake.get("slots"):
            self.slots = SlotManager(handshake["slots"])
            log.info(
                "pad handshake reports %d slot(s) — resized SlotManager accordingly",
                handshake["slots"],
            )
        else:
            log.info(
                "no slot-count handshake (headless, or pad didn't answer) — "
                "using default NUM_SLOTS=%d", NUM_SLOTS,
            )

    async def serve(self):
        sock_path = Path(SOCKET_PATH)
        if sock_path.exists():
            sock_path.unlink()

        self.pad.open()
        # Must happen before start_unix_server below so no hook event
        # can arrive and get mapped under the old (possibly wrong)
        # sizing.
        self.apply_handshake(self.pad.handshake())

        server = await asyncio.start_unix_server(
            self.handle_connection, path=str(sock_path)
        )
        log.info("listening on %s", SOCKET_PATH)

        watcher_task = asyncio.create_task(self.watch_stalled_calls())

        async with server:
            await server.serve_forever()

        watcher_task.cancel()


def main():
    daemon = Daemon()

    def shutdown(*_):
        daemon.pad.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()

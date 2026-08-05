"""
Wire-level protocol for the HID transport (NuPhy Air75 V2 and, in
principle, any other QMK-based pad with RAW_ENABLE) — fixed-size
binary reports over raw HID, the counterpart to rp2040/code.py's
line-delimited JSON over serial. See README's "Pad messages" section
for the serial protocol this mirrors.

Consumed by HidPadLink (Phase 2) to translate daemon.py's
write_json(dict) calls into reports the device understands, and to
parse inbound reports back into the same {"t": ...} dict shapes
SerialPadLink already produces — so everything downstream of PadLink
stays transport-agnostic.

Report layout (REPORT_SIZE bytes, unused trailing bytes zero-padded):

    byte 0: message type (MSG_*)
    byte 1: message-specific
    byte 2: message-specific
    byte 3..: unused, reserved

MSG_PING   (host -> device): no payload. Requests a MSG_HELLO reply —
            necessary because unlike the serial side (which can push
            "hello" unprompted the moment it's plugged in), raw HID is
            call-and-response: the device only sends an IN report
            after receiving an OUT report from the host.
MSG_HELLO  (device -> host): byte 1 = device id, byte 2 = num_slots.
MSG_SLOT   (host -> device): byte 1 = slot index, byte 2 = state
            (see STATE_*). A cleared slot (the serial protocol's
            {"t": "clear"}) is just MSG_SLOT with STATE_OFF — rp2040/
            code.py's handle_message() shows "clear" and "idle" are
            NOT visually identical (idle is a dim gray glow, meaning
            a session is mapped here but quiet; cleared is fully
            black, meaning no session is mapped at all), so both need
            their own state value here too — they just don't need a
            separate wire *message type*, since there's no label to
            clear on an RGB-only pad, only a color.
MSG_KEY    (device -> host): byte 1 = slot index. Sent only on
            key-down — mirrors rp2040/code.py's main() loop, which
            only ever sends {"t": "key", ...} on event.pressed. There
            is no key-up report; on_device_event() has no key-up
            concept to consume one anyway.
"""

from collections import namedtuple

REPORT_SIZE = 32  # QMK's RAW_EPSIZE default for ChibiOS boards (confirmed unchanged in
                   # NuPhy's fork during Phase 0 — see keyboards/nuphy/air75_v2/ansi/)

# MSG_SLOT/MSG_PING/MSG_KEY live at 0x20+ deliberately: 0x01-0x15 is
# QMK VIA's own reserved via_command_id range (quantum/via.h), which
# the claude_macropad keymap now builds with VIA_ENABLE=yes — a value
# inside that range would collide with VIA's own commands on that same
# raw HID endpoint. See claude_macropad.h for the firmware side of
# this.
#
# MSG_HELLO is deliberately not the next sequential byte after MSG_KEY
# below — it's the value discover_hid_device() treats as proof this is
# our pad (see parse_report()'s msg_type dispatch), and 0x01 is exactly
# what an unrelated raw-HID interface's first-ever report is likely to
# contain by coincidence. 0xA1 ("AI") is distinctive enough that a
# collision would mean something is actually wrong (and is well clear
# of VIA's range too).
MSG_HELLO = 0xA1
MSG_SLOT = 0x20
MSG_PING = 0x21
MSG_KEY = 0x22

# Mirrors STATE_COLORS's keys in rp2040/code.py 1:1 — including "off",
# which turns out NOT to be device-local only: rp2040/code.py's
# handle_message() sends it explicitly on "clear" (see MSG_SLOT above),
# distinct from "idle". hook_to_state() itself never produces "off" —
# only write_json({"t": "clear", ...})'s translation does.
STATE_IDLE = 0
STATE_WORKING = 1
STATE_WAITING = 2
STATE_DONE = 3
STATE_ERROR = 4
STATE_QUESTION = 5
STATE_OFF = 6

STATE_TO_CODE = {
    "idle": STATE_IDLE,
    "working": STATE_WORKING,
    "waiting": STATE_WAITING,
    "done": STATE_DONE,
    "error": STATE_ERROR,
    "question": STATE_QUESTION,
    "off": STATE_OFF,
}
CODE_TO_STATE = {v: k for k, v in STATE_TO_CODE.items()}

# Confirmed against NuPhy's own keyboard.json during Phase 0.
DEVICE_ID_AIR75_V2 = 0xA7

# Keychron K1 Pro (ANSI) — see qmk-userspace/keyboards/keychron/k1_pro/
# ansi/rgb/keymaps/claude_macropad/keymap.c. Unlike DEVICE_ID_AIR75_V2,
# this one hasn't been confirmed against real hardware — see that
# keymap's and the README's Keychron K1 Pro section for why.
DEVICE_ID_K1_PRO = 0xC1

# Single source of truth for every known QMK HID pad's identity: `vid`/
# `pid` for USB discovery (daemon.py's discover_hid_pad()), `device_id`
# for the MSG_HELLO handshake byte above. Previously vid/pid lived in
# daemon.py and device_id lived here, with hid_bringup_test.py keeping
# a third, hand-duplicated copy of vid/pid alongside (deliberately, to
# avoid importing daemon.py's pyserial dependency) — daemon.py and
# hid_bringup_test.py both import this module already regardless, so
# there's no longer a reason for any of them to keep their own copy.
KnownPad = namedtuple("KnownPad", ["name", "vid", "pid", "device_id"])

NUPHY_AIR75_V2 = KnownPad("NuPhy Air75 V2 (ANSI)", 0x19F5, 0x3246, DEVICE_ID_AIR75_V2)
KEYCHRON_K1_PRO = KnownPad("Keychron K1 Pro (ANSI)", 0x3434, 0x0210, DEVICE_ID_K1_PRO)

# Tried in this order by daemon.py's discover_hid_pad() — add a new
# KnownPad here for any future board rather than hardcoding its VID/PID
# somewhere else.
KNOWN_HID_PADS = (NUPHY_AIR75_V2, KEYCHRON_K1_PRO)


def pack_ping():
    """Host -> device: request a MSG_HELLO reply."""
    return bytes([MSG_PING]) + bytes(REPORT_SIZE - 1)


def pack_slot(index, state):
    """Host -> device: set one slot's state.

    `index` must fit in a byte (0..255 — SlotManager's num_slots is
    never that large in practice, but this isn't the place to assume
    a cap); `state` is one of hook_to_state's string outputs.
    """
    if not (0 <= index <= 0xFF):
        raise ValueError(f"slot index {index} does not fit in a byte")
    if state not in STATE_TO_CODE:
        raise ValueError(f"unknown state {state!r}")
    report = bytearray(REPORT_SIZE)
    report[0] = MSG_SLOT
    report[1] = index
    report[2] = STATE_TO_CODE[state]
    return bytes(report)


def parse_report(report):
    """Device -> host: decode one inbound report into the same dict
    shapes SerialPadLink's _read_loop already hands to on_device_event.

    Returns None for anything not recognized (unknown message type,
    or a report shorter than the fields it claims to carry) rather
    than raising, matching the serial side's "drop malformed input"
    posture in rp2040/code.py's read_json_lines().
    """
    if len(report) < 1:
        return None
    msg_type = report[0]
    if msg_type == MSG_HELLO:
        if len(report) < 3:
            return None
        return {"t": "hello", "device": report[1], "slots": report[2]}
    if msg_type == MSG_KEY:
        if len(report) < 2:
            return None
        return {"t": "key", "i": report[1]}
    return None

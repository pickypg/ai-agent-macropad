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

Numbering matches qmk-air75v2-implementation-plan.md's Phase 1 sketch.
"""

REPORT_SIZE = 32  # QMK's RAW_EPSIZE default for ChibiOS boards (confirmed unchanged in
                   # NuPhy's fork during Phase 0 — see keyboards/nuphy/air75_v2/ansi/)

MSG_HELLO = 0x01
MSG_SLOT = 0x02
MSG_PING = 0x03

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

# Placeholder until a second QMK board is actually in hand (see plan's
# "explicitly out of scope" — multi-board support isn't needed yet).
DEVICE_ID_AIR75_V2 = 0x01


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
    return None

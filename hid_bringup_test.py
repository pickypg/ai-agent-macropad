#!/usr/bin/env python3
"""
Phase 6 step 1: minimal hello/RGB round-trip test against the real
Air75 V2, independent of daemon.py's threading/discovery machinery —
"Blink before Renderer": prove the wire protocol actually works on
real hardware before trusting the full daemon stack to it.

Requires the claude_macropad keymap already flashed (see
qmk-air75v2-implementation-plan.md's Phase 6) and the board plugged in
over USB, in wired mode.

Usage:
    python3 hid_bringup_test.py
"""
import sys
import time

import hid

import hid_protocol

VID = 0x19F5
PID = 0x3246
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE_ID = 0x61

HANDSHAKE_TIMEOUT = 2.0
STATE_HOLD_SECONDS = 2.0
QUESTION_HOLD_SECONDS = 3.0  # longer, to see the blink


def find_raw_hid_path():
    candidates = [
        d for d in hid.enumerate(VID, PID)
        if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE_ID
    ]
    print(f"found {len(candidates)} raw-HID interface(s) at vid=0x{VID:04x} pid=0x{PID:04x}")
    for d in candidates:
        print(f"  path={d['path']!r} interface_number={d.get('interface_number')}")
    return candidates[0]["path"] if candidates else None


def do_handshake(dev):
    ping = hid_protocol.pack_ping()
    print(f"-> ping:  {ping.hex()}")
    dev.write(bytes([0]) + ping)

    deadline = time.monotonic() + HANDSHAKE_TIMEOUT
    while time.monotonic() < deadline:
        data = dev.read(hid_protocol.REPORT_SIZE, timeout=200)
        if not data:
            continue
        print(f"<- raw:   {bytes(data).hex()}")
        reply = hid_protocol.parse_report(bytes(data))
        if reply:
            return reply
    return None


def cycle_states(dev):
    print("\nCycling all 4 slots through every state — watch PageUp/PageDn/Home/End...")
    for state_name in ("working", "waiting", "done", "error", "question", "idle"):
        print(f"  state={state_name}")
        for i in range(4):
            report = hid_protocol.pack_slot(i, state_name)
            dev.write(bytes([0]) + report)
        hold = QUESTION_HOLD_SECONDS if state_name == "question" else STATE_HOLD_SECONDS
        time.sleep(hold)


def main():
    path = find_raw_hid_path()
    if not path:
        print(
            "FAILED: no raw HID interface found. Is the board plugged in "
            "(wired, not just charging) and flashed with claude_macropad?"
        )
        sys.exit(1)

    dev = hid.Device(path=path)
    print(f"opened {path!r}")

    try:
        reply = do_handshake(dev)
        if not reply or reply.get("t") != "hello":
            print("FAILED: no hello reply within {}s".format(HANDSHAKE_TIMEOUT))
            sys.exit(1)

        print(f"hello: device={reply['device']} slots={reply['slots']}")
        if reply["device"] != hid_protocol.DEVICE_ID_AIR75_V2:
            print(f"WARNING: unexpected device id {reply['device']!r}")
        if reply["slots"] != 4:
            print(f"WARNING: expected 4 slots, got {reply['slots']}")
        print("PASS: hello round-trip OK")

        cycle_states(dev)
    finally:
        dev.close()

    print(
        "\nDone. PASS if all 4 keys visibly changed color together at each "
        "step above, and 'question' blinked rather than staying solid."
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Minimal hello/RGB/key-press round-trip test against a real QMK pad
(NuPhy Air75 V2 or Keychron K1 Pro — see KNOWN_HID_PADS), independent
of daemon.py's threading/discovery machinery — "Blink before Renderer":
prove the wire protocol actually works on real hardware before trusting
the full daemon stack to it.

Requires the claude_macropad keymap already flashed and the board
plugged in over USB, in wired mode.

Usage:
    python3 hid_bringup_test.py
"""
import sys
import time

import hid

import hid_protocol

# Deliberately duplicated from daemon.py's KNOWN_HID_PADS rather than
# imported — this script stays standalone (no daemon.py import, so no
# pyserial dependency just to run a HID-only bring-up check). Keep in
# sync by hand if a pad's VID/PID changes or a new one is added.
KNOWN_HID_PADS = (
    (0x19F5, 0x3246),  # NuPhy Air75 V2 (ANSI)
    (0x3434, 0x0210),  # Keychron K1 Pro (ANSI) — unverified, see README
)
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE_ID = 0x61

HANDSHAKE_TIMEOUT = 2.0
STATE_HOLD_SECONDS = 2.0
QUESTION_HOLD_SECONDS = 3.0  # longer, to see the blink


def find_raw_hid_path():
    for vid, pid in KNOWN_HID_PADS:
        candidates = [
            d for d in hid.enumerate(vid, pid)
            if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE_ID
        ]
        print(f"found {len(candidates)} raw-HID interface(s) at vid=0x{vid:04x} pid=0x{pid:04x}")
        for d in candidates:
            print(f"  path={d['path']!r} interface_number={d.get('interface_number')}")
        if candidates:
            return candidates[0]["path"]
    return None


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


def cycle_states(dev, num_slots):
    print(f"\nCycling all {num_slots} slots through every state — watch PageUp/PageDn/Home/End "
          "(slots 4+ only light up if you've assigned them to a key via VIA)...")
    for state_name in ("working", "waiting", "done", "error", "question", "idle"):
        print(f"  state={state_name}")
        for i in range(num_slots):
            report = hid_protocol.pack_slot(i, state_name)
            dev.write(bytes([0]) + report)
        hold = QUESTION_HOLD_SECONDS if state_name == "question" else STATE_HOLD_SECONDS
        time.sleep(hold)


def listen_for_keys(dev):
    """Isolates "does the firmware send the right MSG_KEY report" from
    "does the daemon correctly dispatch a window" — press PageUp, PageDn,
    Home, and End (slots 0-3 by default) and confirm the printed slot
    index matches (0=PageUp, 1=PageDn, 2=Home, 3=End); if you've assigned
    any AI_AGENT_KEY_4.. to a spare key via VIA, press that too and
    confirm its index. No daemon or session mapping involved yet.
    """
    print(
        "\nListening for key presses — press PageUp, PageDn, Home, End, and "
        "any VIA-assigned slot keys (Ctrl+C to stop)..."
    )
    try:
        while True:
            data = dev.read(hid_protocol.REPORT_SIZE, timeout=200)
            if not data:
                continue
            msg = hid_protocol.parse_report(bytes(data))
            if msg and msg.get("t") == "key":
                print(f"  key i={msg['i']}")
    except KeyboardInterrupt:
        print("\nstopped")


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
        known_device_ids = (hid_protocol.DEVICE_ID_AIR75_V2, hid_protocol.DEVICE_ID_K1_PRO)
        if reply["device"] not in known_device_ids:
            print(f"WARNING: unexpected device id {reply['device']!r}")
        num_slots = reply["slots"]
        print("PASS: hello round-trip OK")

        cycle_states(dev, num_slots)
        print(
            "\nDone with RGB cycling. PASS if PageUp/PageDn/Home/End visibly "
            "changed color together at each step above (plus any keys "
            "you've VIA-assigned to slots 4+), and 'question' blinked rather "
            "than staying solid."
        )

        listen_for_keys(dev)
    finally:
        dev.close()


if __name__ == "__main__":
    main()

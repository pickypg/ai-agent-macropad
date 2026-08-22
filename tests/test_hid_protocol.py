import pytest

import hid_protocol


def test_report_size_matches_qmk_raw_epsize():
    assert hid_protocol.REPORT_SIZE == 32


def test_pack_ping():
    report = hid_protocol.pack_ping()
    assert len(report) == hid_protocol.REPORT_SIZE
    assert report[0] == hid_protocol.MSG_PING
    assert report[1:] == bytes(hid_protocol.REPORT_SIZE - 1)


def test_pack_slot_encodes_index_and_state():
    report = hid_protocol.pack_slot(3, "working")
    assert len(report) == hid_protocol.REPORT_SIZE
    assert report[0] == hid_protocol.MSG_SLOT
    assert report[1] == 3
    assert report[2] == hid_protocol.STATE_WORKING
    assert report[3:] == bytes(hid_protocol.REPORT_SIZE - 3)


def test_pack_slot_covers_every_hook_to_state_output():
    # hook_to_state (daemon.py) and watch_stalled_calls only ever produce
    # these eight strings (or None, which never reaches write_json) —
    # every one must round-trip.
    for state in (
        "idle", "working", "waiting", "done", "error", "question",
        "tool_running", "tool_stalled",
    ):
        report = hid_protocol.pack_slot(0, state)
        assert hid_protocol.CODE_TO_STATE[report[2]] == state


def test_state_off_is_pinned_with_headroom_for_future_states():
    # STATE_OFF is deliberately parked well above the states defined
    # today, rather than "whatever's defined last" — so a new state can
    # be added without ever renumbering STATE_OFF (and everything
    # anchored to its value on the QMK side) again. If this ever fails,
    # either headroom ran out (bump STATE_OFF further) or STATE_OFF got
    # accidentally renumbered down near the real states.
    assert hid_protocol.STATE_OFF == 31
    assert hid_protocol.STATE_OFF - hid_protocol.STATE_TOOL_STALLED > 10


def test_pack_slot_off_is_distinct_from_idle():
    # "off" (cleared, no session mapped) and "idle" (a session is
    # mapped but quiet) must be different wire values — the QMK
    # firmware renders them as different colors (fully dark vs. a dim
    # glow).
    idle_report = hid_protocol.pack_slot(0, "idle")
    off_report = hid_protocol.pack_slot(0, "off")
    assert idle_report[2] == hid_protocol.STATE_IDLE
    assert off_report[2] == hid_protocol.STATE_OFF
    assert idle_report[2] != off_report[2]
    assert hid_protocol.CODE_TO_STATE[off_report[2]] == "off"


def test_pack_slot_rejects_unknown_state():
    with pytest.raises(ValueError):
        hid_protocol.pack_slot(0, "not-a-real-state")


def test_pack_slot_rejects_out_of_range_index():
    with pytest.raises(ValueError):
        hid_protocol.pack_slot(256, "idle")
    with pytest.raises(ValueError):
        hid_protocol.pack_slot(-1, "idle")


def test_parse_report_hello():
    report = bytes([hid_protocol.MSG_HELLO, hid_protocol.DEVICE_ID_AIR75_V2, 16]) + bytes(29)
    assert hid_protocol.parse_report(report) == {
        "t": "hello",
        "device": hid_protocol.DEVICE_ID_AIR75_V2,
        "slots": 16,
    }


def test_parse_report_key():
    report = bytes([hid_protocol.MSG_KEY, 2]) + bytes(30)
    assert hid_protocol.parse_report(report) == {"t": "key", "i": 2}


def test_parse_report_key_held():
    report = bytes([hid_protocol.MSG_KEY_HELD, 2]) + bytes(30)
    assert hid_protocol.parse_report(report) == {"t": "key_held", "i": 2}


def test_parse_report_truncated_key_held_returns_none():
    assert hid_protocol.parse_report(bytes([hid_protocol.MSG_KEY_HELD])) is None


def test_parse_report_unknown_type_returns_none():
    report = bytes([0xFF]) + bytes(31)
    assert hid_protocol.parse_report(report) is None


def test_parse_report_truncated_hello_returns_none():
    assert hid_protocol.parse_report(bytes([hid_protocol.MSG_HELLO, 0x01])) is None


def test_parse_report_truncated_key_returns_none():
    assert hid_protocol.parse_report(bytes([hid_protocol.MSG_KEY])) is None


def test_parse_report_empty_returns_none():
    assert hid_protocol.parse_report(b"") is None

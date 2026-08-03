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
    # hook_to_state (daemon.py) only ever returns these six strings (or
    # None, which never reaches write_json) — every one must round-trip.
    for state in ("idle", "working", "waiting", "done", "error", "question"):
        report = hid_protocol.pack_slot(0, state)
        assert hid_protocol.CODE_TO_STATE[report[2]] == state


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


def test_parse_report_unknown_type_returns_none():
    report = bytes([0xFF]) + bytes(31)
    assert hid_protocol.parse_report(report) is None


def test_parse_report_truncated_hello_returns_none():
    assert hid_protocol.parse_report(bytes([hid_protocol.MSG_HELLO, 0x01])) is None


def test_parse_report_empty_returns_none():
    assert hid_protocol.parse_report(b"") is None

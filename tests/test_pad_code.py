import json


def test_read_json_lines_yields_complete_line(pad):
    pad.serial.feed(b'{"t": "clear", "i": 0}\n')
    assert list(pad.read_json_lines()) == [{"t": "clear", "i": 0}]


def test_read_json_lines_buffers_partial_reads(pad):
    pad.serial.feed(b'{"t": "clear",')
    assert list(pad.read_json_lines()) == []  # no newline yet — nothing yielded

    pad.serial.feed(b' "i": 0}\n')
    assert list(pad.read_json_lines()) == [{"t": "clear", "i": 0}]


def test_read_json_lines_drops_malformed_line(pad):
    pad.serial.feed(b"not json\n" + b'{"t": "ping"}\n')
    assert list(pad.read_json_lines()) == [{"t": "ping"}]


def test_handle_message_slot_updates_state_label_and_pixel(pad):
    pad.handle_message({"t": "slot", "i": 2, "state": "working", "label": "Read"})

    assert pad.slots[2] == {"state": "working", "label": "Read"}
    assert pad.macropad.pixels[2] == pad.STATE_COLORS["working"]
    assert pad.label_rows[2].text == "2:work Read"


def test_handle_message_slot_out_of_range_is_ignored(pad):
    before = list(pad.macropad.pixels._colors)
    pad.handle_message({"t": "slot", "i": 99, "state": "working"})
    assert list(pad.macropad.pixels._colors) == before  # no crash, no change


def test_handle_message_clear_resets_slot(pad):
    pad.handle_message({"t": "slot", "i": 0, "state": "error", "label": "Bash"})
    pad.handle_message({"t": "clear", "i": 0})

    assert pad.slots[0] == {"state": "idle", "label": ""}
    assert pad.macropad.pixels[0] == pad.STATE_COLORS["off"]


def test_handle_message_ping_replies_with_hello(pad):
    pad.handle_message({"t": "ping"})

    line = pad.serial.written.strip().split(b"\n")[-1]
    reply = json.loads(line)
    assert reply == {"t": "hello", "device": pad.DEVICE_ID, "slots": pad.NUM_SLOTS}


def test_pixel_color_blinks_off_phase_for_blink_states(pad):
    assert pad.pixel_color("question", blink_on=True) == pad.STATE_COLORS["question"]
    assert pad.pixel_color("question", blink_on=False) == pad.STATE_COLORS["off"]


def test_pixel_color_non_blink_state_ignores_blink_phase(pad):
    assert pad.pixel_color("working", blink_on=False) == pad.STATE_COLORS["working"]


def test_redraw_truncates_to_character_budget(pad):
    pad.slots[0] = {"state": "working", "label": "a-very-long-project-folder-name"}
    pad.redraw()
    full = "0:work a-very-long-project-folder-name"
    assert pad.label_rows[0].text == full[:21]
    assert len(pad.label_rows[0].text) == 21

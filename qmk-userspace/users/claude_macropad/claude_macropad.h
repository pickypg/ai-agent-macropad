// Shared protocol/state logic for any QMK keyboard acting as a
// Claude Code session-status pad. One copy of this file backs every
// board's keymap (see keyboards/*/.../keymaps/claude_macropad/keymap.c) —
// each keymap supplies only its own layout, LED-index table, and device
// ID, then calls into these functions.
//
// Wire format matches hid_protocol.py exactly; state values match
// rp2040/code.py's STATE_COLORS keys 1:1 and in the same order. Any
// change here must be mirrored in both.
#pragma once

#include "quantum.h"

// Raw HID reports are always 32 bytes on this protocol.
#define CLAUDE_MACROPAD_REPORT_SIZE 32

// Upper bound on slots any single board can expose — sized generously
// (matches the RP2040 MacroPad's 12 keys) so it doesn't need bumping
// for a denser board later.
#define CLAUDE_MACROPAD_MAX_SLOTS 12

// Message types — must match hid_protocol.py.
//
// MSG_SLOT/MSG_PING/MSG_KEY live at 0x20+ deliberately: 0x01-0x15 is
// QMK VIA's own reserved via_command_id range (quantum/via.h) for
// boards built with VIA_ENABLE=yes, which share this raw HID
// endpoint — claude_macropad_raw_hid_receive() gets wired in ahead of
// VIA's own dispatch (see its doc comment), so a value inside that
// range would either get swallowed by VIA or corrupt VIA's own
// command handling, depending on which one runs first.
//
// MSG_HELLO deliberately isn't the next sequential byte after MSG_KEY
// below — it's what daemon.py's discover_hid_device() treats as proof
// this is our pad, and 0x01 is exactly what an unrelated raw-HID
// interface's first-ever report is likely to contain by coincidence.
// 0xA1 ("AI") is distinctive enough that a collision would mean
// something is actually wrong (and is well clear of VIA's range too).
enum claude_macropad_msg {
    MSG_HELLO = 0xA1,
    MSG_SLOT  = 0x20,
    MSG_PING  = 0x21,
    MSG_KEY   = 0x22,
};

// Pad states — must match hid_protocol.py's STATE_*/rp2040/code.py's
// STATE_COLORS keys, 1:1 and in the same order. STATE_OFF (cleared,
// no session mapped) is intentionally distinct from STATE_IDLE (a
// session is mapped here but quiet) — rp2040/code.py's
// handle_message() renders them as different colors (fully dark vs.
// a dim glow), not just different labels, so this side needs to
// preserve that distinction too.
enum claude_macropad_state {
    STATE_IDLE = 0,
    STATE_WORKING,
    STATE_WAITING,
    STATE_DONE,
    STATE_ERROR,
    STATE_QUESTION,
    STATE_OFF,
};

// Resets all `num_slots` slots to STATE_OFF. Call once from
// keyboard_post_init_user() — a plain static array would default to
// STATE_IDLE (0), not STATE_OFF, so this can't be skipped.
void claude_macropad_init(uint8_t num_slots);

// Call from process_record_user(). `slot_key_base` is the keymap's
// first SLOT_KEY_* custom keycode (its remaining SLOT_KEY_1.. must be
// the next `num_slots - 1` sequential values). Returns false (keycode
// handled, swallowed) for a slot key press/release, true otherwise —
// pass that straight back as process_record_user's return value.
bool claude_macropad_process_record(uint16_t keycode, keyrecord_t *record, uint16_t slot_key_base, uint8_t num_slots);

// Call from raw_hid_receive() on boards without VIA_ENABLE, or from
// via_command_kb() on boards with it (via_command_kb() runs before
// VIA's own dispatch and, per its contract, a `true` return means the
// command was fully handled — including any raw_hid_send() reply —
// so VIA never sees it). Answers MSG_PING with this board's
// device_id/num_slots, and applies MSG_SLOT updates to slot state.
// Returns true if `data` was one of this protocol's message types,
// false otherwise (VIA_ENABLE boards should fall through to VIA's own
// dispatch in that case; non-VIA boards can ignore the return value).
bool claude_macropad_raw_hid_receive(uint8_t *data, uint8_t length, uint8_t device_id, uint8_t num_slots);

// Call from rgb_matrix_indicators_user(). `slot_to_led` maps each
// slot index to its RGB matrix LED index (keyboard-specific, read off
// keyboard.json's rgb_matrix.layout).
void claude_macropad_paint_indicators(const uint8_t *slot_to_led, uint8_t num_slots);

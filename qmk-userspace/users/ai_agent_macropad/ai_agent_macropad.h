// Shared protocol/state logic for any QMK keyboard acting as an
// AI agent session-status pad. One copy of this file backs every
// board's keymap (see keyboards/*/.../keymaps/ai_agent_macropad/keymap.c) —
// each keymap supplies only its own layout, LED-index table, and device
// ID, then calls into these functions.
//
// Wire format matches hid_protocol.py exactly; state values match
// rp2040/code.py's STATE_COLORS keys 1:1 and in the same order. Any
// change here must be mirrored in both.
#pragma once

#include "quantum.h"

// Raw HID reports are always 32 bytes on this protocol.
#define AI_AGENT_MACROPAD_REPORT_SIZE 32

// Upper bound on slots any single board can expose — sized generously
// (matches the RP2040 MacroPad's 12 keys) so it doesn't need bumping
// for a denser board later.
#define AI_AGENT_MACROPAD_MAX_SLOTS 12

// How long a slot key must stay held (ms) before MSG_KEY_HELD fires —
// see ai_agent_macropad_task(). Deliberately generous: this manually
// evicts whatever session is mapped to the slot, so it needs to be
// long enough that an ordinary firm tap can never trigger it by
// accident.
#define AI_AGENT_MACROPAD_HOLD_THRESHOLD_MS 5000

// Message types — must match hid_protocol.py.
//
// MSG_SLOT/MSG_PING/MSG_KEY live at 0x20+ deliberately: 0x01-0x15 is
// QMK VIA's own reserved via_command_id range (quantum/via.h) for
// boards built with VIA_ENABLE=yes, which share this raw HID
// endpoint — ai_agent_macropad_raw_hid_receive() gets wired in ahead of
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
enum ai_agent_macropad_msg {
    MSG_HELLO    = 0xA1,
    MSG_SLOT     = 0x20,
    MSG_PING     = 0x21,
    MSG_KEY      = 0x22,
    MSG_KEY_HELD = 0x23,
};

// Pad states — must match hid_protocol.py's STATE_* values 1:1 (same
// names, same numbers — order alone isn't enough once STATE_OFF is
// pinned below). STATE_OFF (cleared, no session mapped) is intentionally
// distinct from STATE_IDLE (a session is mapped here but quiet) —
// rp2040/code.py's handle_message() renders them as different colors
// (fully dark vs. a dim glow), not just different labels, so this side
// needs to preserve that distinction too.
//
// STATE_OFF is pinned at a fixed high value, deliberately far above the
// states actually defined today, instead of "whatever's defined last" —
// so a future state gets added by inserting another member before
// STATE_OFF, without ever renumbering STATE_OFF (or the raw_hid_receive
// bounds check below, which is anchored to it) again. The unused values
// in between are reserved headroom: a byte in that range that reaches
// state_to_rgb() without matching a known case (e.g. this firmware
// build predates a state the daemon has since added) falls through to
// state_to_rgb()'s "unknown" fallback color instead of silently
// reusing STATE_IDLE's.
enum ai_agent_macropad_state {
    STATE_IDLE = 0,
    STATE_WORKING,
    STATE_WAITING,
    STATE_DONE,
    STATE_ERROR,
    STATE_QUESTION,
    STATE_TOOL_RUNNING,
    STATE_TOOL_STALLED,
    STATE_OFF = 31,
};

// Resets all `num_slots` slots to STATE_OFF, and seeds their LED
// mapping from `slot_to_led` (a static slot-index -> LED-index table,
// keyboard-specific, read off keyboard.json's rgb_matrix.layout). On
// VIA_ENABLE boards, pass NULL — a static table can't know where the
// user has actually put each key — and call ai_agent_macropad_scan_slots()
// right after instead. Call once from keyboard_post_init_user().
void ai_agent_macropad_init(uint8_t num_slots, const uint8_t *slot_to_led);

// Call from process_record_user(). `slot_key_base` is the keymap's
// first AI_AGENT_KEY_* custom keycode (its remaining AI_AGENT_KEY_1..
// must be the next `num_slots - 1` sequential values). Returns false
// (keycode handled, swallowed) for a slot key press/release, true
// otherwise — pass that straight back as process_record_user's return
// value.
bool ai_agent_macropad_process_record(uint16_t keycode, keyrecord_t *record, uint16_t slot_key_base, uint8_t num_slots);

// Call from matrix_scan_user() — unconditionally, every scan, cheap
// (just a timer comparison per currently-held slot key). Fires
// MSG_KEY_HELD as soon as a held key crosses
// AI_AGENT_MACROPAD_HOLD_THRESHOLD_MS, without waiting for release —
// deliberately not tied to the RGB matrix task (unlike
// ai_agent_macropad_paint_indicators() below), so this still works if
// RGB is toggled off, same as every other message this protocol sends.
void ai_agent_macropad_task(uint8_t num_slots);

#ifdef VIA_ENABLE
// VIA_ENABLE-only: (re)builds the slot -> LED table from scratch by
// scanning every matrix position's *dynamic* (EEPROM, user-remappable)
// keycode on layer 0 for anything in [slot_key_base, slot_key_base +
// num_slots). Call once from keyboard_post_init_user(), right after
// ai_agent_macropad_init(NUM_SLOTS, NULL) — QMK's own boot sequence
// always runs via_init() (which loads the dynamic keymap EEPROM)
// before keyboard_post_init_user(), so the data is ready by then.
void ai_agent_macropad_scan_slots(uint16_t slot_key_base, uint8_t num_slots);

// VIA_ENABLE-only. Call from via_command_kb(), alongside (order
// doesn't matter) ai_agent_macropad_raw_hid_receive() — this only peeks
// at VIA's own dynamic-keymap commands, it never claims them. Keeps
// the slot -> LED table in sync whenever the user remaps a key via
// VIA: clears a slot immediately if its key gets remapped away (so its
// LED goes dark instead of showing stale state), and picks up a key
// newly assigned to an unused AI_AGENT_KEY_* the moment it's dropped
// there. Tracks single-key remaps (VIA's "drag a keycode onto a key"
// UI — by far the common path) and "reset to default" precisely; a
// bulk keymap import (VIA's advanced JSON-import feature) needs one
// follow-up key edit, or a power cycle, to resync — a deliberate scope
// cut, not an oversight.
void ai_agent_macropad_track_via_remap(uint8_t *data, uint8_t length, uint16_t slot_key_base, uint8_t num_slots);
#endif

// Call from raw_hid_receive() on boards without VIA_ENABLE, or from
// via_command_kb() on boards with it (via_command_kb() runs before
// VIA's own dispatch and, per its contract, a `true` return means the
// command was fully handled — including any raw_hid_send() reply —
// so VIA never sees it). Answers MSG_PING with this board's
// device_id/num_slots, and applies MSG_SLOT updates to slot state.
// Returns true if `data` was one of this protocol's message types,
// false otherwise (VIA_ENABLE boards should fall through to VIA's own
// dispatch in that case; non-VIA boards can ignore the return value).
bool ai_agent_macropad_raw_hid_receive(uint8_t *data, uint8_t length, uint8_t device_id, uint8_t num_slots);

// Call from rgb_matrix_indicators_user(). Paints each slot's state
// onto its current LED, per the table ai_agent_macropad_init()/
// ai_agent_macropad_scan_slots() built — a slot with no key currently
// assigned (NO_LED) is simply skipped.
void ai_agent_macropad_paint_indicators(uint8_t num_slots);

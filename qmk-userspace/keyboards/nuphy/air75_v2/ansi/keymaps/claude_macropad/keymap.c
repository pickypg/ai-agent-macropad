// NuPhy Air75 V2 keymap — Claude Code session-status pad.
//
// Ships 4 physical keys wired by default (PageUp/PageDn/Home/End —
// the board's right-edge nav column; a small cluster to bring up and
// verify against real hardware before committing to a bigger one) as
// AI_AGENT_KEY_0..3, each showing one Claude Code session's state via
// per-key RGB. AI_AGENT_KEY_4..11 exist as valid keycodes but aren't
// wired to any physical key here — VIA_ENABLE lets an end user drag
// them onto spare keys themselves (see claude_macropad.c's dynamic
// slot/LED discovery, which picks up wherever they end up), though
// only AI_AGENT_KEY_4..7 are actually reachable from VIA's picker UI
// today (via.json in this same directory names them "AI Slot 4".."AI
// Slot 7") — the VIA app hard-caps customKeycodes at 32 total entries,
// and NuPhy's own stock entries plus 2 unavoidable placeholders (see
// the enum's doc comment below) already use 24 of those, leaving room
// for exactly 8 (AI_AGENT_KEY_0..7, of which 0..3 are already spoken
// for by the default wiring). AI_AGENT_KEY_8..11 stay real, working
// keycodes at the firmware/protocol level (the dynamic scan and
// process_record_user() code doesn't care about JSON at all) — they're
// just not currently nameable in VIA's UI, a JSON budget problem, not
// a firmware one. Protocol/state logic (shared across every board's
// keymap) lives in users/claude_macropad/claude_macropad.c; this file
// holds only what's specific to this board.
#include QMK_KEYBOARD_H
#include "claude_macropad.h"

// QK_KB_0-based, picking up immediately after ansi.h's own enum
// custom_keycodes ends (RF_DFU..RGB_TEST — 24 values, QK_KB_0..QK_KB_0+23;
// note ansi.h's enum itself runs 2 further than what NuPhy's stock
// via.json exposes as named customKeycodes entries, at BAT_NUM/RGB_TEST
// — verified by reading ansi.h directly, not inferred from the JSON's
// array length). This deliberately ISN'T QK_USER_0: VIA's customKeycodes
// mechanism binds to QK_KB_0 — CONFIRMED on real hardware: the VIA app
// correctly displayed customKeycodes[24]'s label ("AI Slot 0") back for
// PageUp, which holds AI_AGENT_KEY_0 = QK_KB_0+24 in the compiled
// keymap. That same hardware test is also what caught VIA's 32-entry
// customKeycodes cap (see via.json and the top-of-file comment) — an
// earlier attempt at naming all 12 slots made the VIA app stop
// recognizing the keyboard entirely, not just reject the JSON. Also
// can't reuse the tag name "custom_keycodes" itself — ansi.h's enum
// already claims it, and C enum tags collide independently of the
// value range chosen. claude_macropad_keycodes is specific enough to
// this keymap that it won't run into the same problem again.
enum claude_macropad_keycodes {
    AI_AGENT_KEY_0 = QK_KB_0 + 24,  // PageUp position (default)
    AI_AGENT_KEY_1,               // PageDn position (default)
    AI_AGENT_KEY_2,               // Home position (default)
    AI_AGENT_KEY_3,               // End position (default)
    AI_AGENT_KEY_4,               // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_5,               // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_6,               // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_7,               // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_8,               // unwired by default — valid keycode, but not in via.json's picker (32-entry cap)
    AI_AGENT_KEY_9,               // unwired by default — valid keycode, but not in via.json's picker (32-entry cap)
    AI_AGENT_KEY_10,              // unwired by default — valid keycode, but not in via.json's picker (32-entry cap)
    AI_AGENT_KEY_11,              // unwired by default — valid keycode, but not in via.json's picker (32-entry cap)
};

#define NUM_MACROPAD_SLOTS CLAUDE_MACROPAD_MAX_SLOTS
#define DEVICE_ID_AIR75_V2 0xA7

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
[0] = LAYOUT_ansi_84(
	KC_ESC, 	KC_BRID,  	KC_BRIU,  	MAC_TASK, 	MAC_SEARCH, MAC_VOICE,  MAC_DND,  	KC_MPRV,  	KC_MPLY,  	KC_MNXT, 	KC_MUTE, 	KC_VOLD, 	KC_VOLU, 	MAC_PRTA,	KC_INS,		KC_DEL,
	KC_GRV, 	KC_1,   	KC_2,   	KC_3,  		KC_4,   	KC_5,   	KC_6,   	KC_7,   	KC_8,   	KC_9,  		KC_0,   	KC_MINS,	KC_EQL, 				KC_BSPC,	AI_AGENT_KEY_0,
	KC_TAB, 	KC_Q,   	KC_W,   	KC_E,  		KC_R,   	KC_T,   	KC_Y,   	KC_U,   	KC_I,   	KC_O,  		KC_P,   	KC_LBRC,	KC_RBRC, 				KC_BSLS,	AI_AGENT_KEY_1,
	KC_CAPS,	KC_A,   	KC_S,   	KC_D,  		KC_F,   	KC_G,   	KC_H,   	KC_J,   	KC_K,   	KC_L,  		KC_SCLN,	KC_QUOT, 	 						KC_ENT,		AI_AGENT_KEY_2,
	KC_LSFT,				KC_Z,   	KC_X,   	KC_C,  		KC_V,   	KC_B,   	KC_N,   	KC_M,   	KC_COMM,	KC_DOT,		KC_SLSH,				KC_RSFT,	KC_UP,		AI_AGENT_KEY_3,
	KC_LCTL,	KC_LALT,	KC_LGUI,										KC_SPC, 							KC_RGUI,	KC_NO,   	KC_RCTL,				KC_LEFT,	KC_DOWN,    KC_RGHT),
};

#ifndef VIA_ENABLE
// LED index per slot, in AI_AGENT_KEY_0..3 order — read directly off
// keyboard.json's rgb_matrix.layout array position for each key's
// matrix cell (PageUp=[1,16], PageDn=[2,16], Home=[1,15], End=[2,15]),
// confirmed programmatically during Phase 4, not guessed. Slots 4..11
// aren't wired to any physical key without VIA_ENABLE (no way to
// remap one there), so they stay NO_LED.
static const uint8_t slot_to_led[NUM_MACROPAD_SLOTS] = {16, 45, 46, 73, NO_LED, NO_LED, NO_LED, NO_LED, NO_LED, NO_LED, NO_LED, NO_LED};
#endif

void keyboard_post_init_user(void) {
#ifdef VIA_ENABLE
    // NULL: a static table can't know where the user's put each key —
    // claude_macropad_scan_slots() (safe here; via_init() already ran
    // and loaded the dynamic keymap EEPROM by this point in QMK's boot
    // sequence) builds the real one from the live keymap instead.
    claude_macropad_init(NUM_MACROPAD_SLOTS, NULL);
    claude_macropad_scan_slots(AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
#else
    claude_macropad_init(NUM_MACROPAD_SLOTS, slot_to_led);
#endif
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    return claude_macropad_process_record(keycode, record, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

// VIA_ENABLE boards route raw HID through via_command_kb() instead of
// raw_hid_receive() — quantum/via.c owns that symbol outright once
// VIA_ENABLE=yes, so defining it here too would be a duplicate-symbol
// link error. via_command_kb() runs first and, per its contract, a
// `true` return (our message types all live outside VIA's reserved
// command-id range — see claude_macropad.h) fully claims the report
// before VIA's own dispatch ever sees it.
#ifdef VIA_ENABLE
bool via_command_kb(uint8_t *data, uint8_t length) {
    // Peek only — never claims VIA's own commands (set_keycode/reset),
    // just keeps our slot -> LED table in sync with them. Order versus
    // the claude_macropad_raw_hid_receive() call below doesn't matter,
    // they act on disjoint command-id ranges.
    claude_macropad_track_via_remap(data, length, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
    return claude_macropad_raw_hid_receive(data, length, DEVICE_ID_AIR75_V2, NUM_MACROPAD_SLOTS);
}
#else
void raw_hid_receive(uint8_t *data, uint8_t length) {
    claude_macropad_raw_hid_receive(data, length, DEVICE_ID_AIR75_V2, NUM_MACROPAD_SLOTS);
}
#endif

bool rgb_matrix_indicators_user(void) {
    claude_macropad_paint_indicators(NUM_MACROPAD_SLOTS);
    return true;
}

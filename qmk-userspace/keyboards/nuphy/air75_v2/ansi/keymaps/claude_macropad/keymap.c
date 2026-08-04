// NuPhy Air75 V2 keymap — Claude Code session-status pad.
//
// Dedicates 4 physical keys (PageUp/PageDn/Home/End — the board's
// right-edge nav column; a small cluster to bring up and verify
// against real hardware before committing to a bigger one) as
// SLOT_KEY_0..3, each showing one Claude Code session's state via
// per-key RGB. Protocol/state logic (shared across every board's
// keymap) lives in users/claude_macropad/claude_macropad.c; this file
// holds only what's specific to this board.
#include QMK_KEYBOARD_H
#include "claude_macropad.h"

// QK_USER_0-based (keymap-level range) — NOT QK_KB_0, which ansi.h's
// own enum custom_keycodes (RF_DFU, LNK_USB, ...) already occupies.
// Also can't reuse the tag name "custom_keycodes" itself — ansi.h's
// enum already claims it, and C enum tags collide independently of
// the value range chosen. claude_macropad_keycodes is specific enough
// to this keymap that it won't run into the same problem again.
enum claude_macropad_keycodes {
    SLOT_KEY_0 = QK_USER_0,  // PageUp position
    SLOT_KEY_1,              // PageDn position
    SLOT_KEY_2,              // Home position
    SLOT_KEY_3,              // End position
};

#define NUM_MACROPAD_SLOTS 4
#define DEVICE_ID_AIR75_V2 0xA7

// LED index per slot, in SLOT_KEY_0..3 order — read directly off
// keyboard.json's rgb_matrix.layout array position for each key's
// matrix cell (PageUp=[1,16], PageDn=[2,16], Home=[1,15], End=[2,15]),
// confirmed programmatically during Phase 4, not guessed.
static const uint8_t slot_to_led[NUM_MACROPAD_SLOTS] = {16, 45, 46, 73};

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
[0] = LAYOUT_ansi_84(
	KC_ESC, 	KC_BRID,  	KC_BRIU,  	MAC_TASK, 	MAC_SEARCH, MAC_VOICE,  MAC_DND,  	KC_MPRV,  	KC_MPLY,  	KC_MNXT, 	KC_MUTE, 	KC_VOLD, 	KC_VOLU, 	MAC_PRTA,	KC_INS,		KC_DEL,
	KC_GRV, 	KC_1,   	KC_2,   	KC_3,  		KC_4,   	KC_5,   	KC_6,   	KC_7,   	KC_8,   	KC_9,  		KC_0,   	KC_MINS,	KC_EQL, 				KC_BSPC,	SLOT_KEY_0,
	KC_TAB, 	KC_Q,   	KC_W,   	KC_E,  		KC_R,   	KC_T,   	KC_Y,   	KC_U,   	KC_I,   	KC_O,  		KC_P,   	KC_LBRC,	KC_RBRC, 				KC_BSLS,	SLOT_KEY_1,
	KC_CAPS,	KC_A,   	KC_S,   	KC_D,  		KC_F,   	KC_G,   	KC_H,   	KC_J,   	KC_K,   	KC_L,  		KC_SCLN,	KC_QUOT, 	 						KC_ENT,		SLOT_KEY_2,
	KC_LSFT,				KC_Z,   	KC_X,   	KC_C,  		KC_V,   	KC_B,   	KC_N,   	KC_M,   	KC_COMM,	KC_DOT,		KC_SLSH,				KC_RSFT,	KC_UP,		SLOT_KEY_3,
	KC_LCTL,	KC_LALT,	KC_LGUI,										KC_SPC, 							KC_RGUI,	KC_NO,   	KC_RCTL,				KC_LEFT,	KC_DOWN,    KC_RGHT),
};

void keyboard_post_init_user(void) {
    claude_macropad_init(NUM_MACROPAD_SLOTS);
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    return claude_macropad_process_record(keycode, record, SLOT_KEY_0, NUM_MACROPAD_SLOTS);
}

void raw_hid_receive(uint8_t *data, uint8_t length) {
    claude_macropad_raw_hid_receive(data, length, DEVICE_ID_AIR75_V2, NUM_MACROPAD_SLOTS);
}

bool rgb_matrix_indicators_user(void) {
    claude_macropad_paint_indicators(slot_to_led, NUM_MACROPAD_SLOTS);
    return true;
}

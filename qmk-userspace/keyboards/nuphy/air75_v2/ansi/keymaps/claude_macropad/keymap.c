// PLACEHOLDER — proves the QMK userspace overlay pipeline builds this
// keymap end to end (RAW_ENABLE + per-key rgb_matrix hooks), ported
// from Phase 0's spike keymap (see qmk-air75v2-implementation-plan.md).
// Still pending the rest of Phase 4: how many keys to dedicate as
// slots, SLOT_KEY_0..N-1, real process_record_user()/
// rgb_matrix_indicators_user() slot logic, and slot_to_led[] built
// from keyboard.json's real LED table.
#include QMK_KEYBOARD_H
#include "raw_hid.h"

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
[0] = LAYOUT_ansi_84(
	KC_ESC, 	KC_BRID,  	KC_BRIU,  	MAC_TASK, 	MAC_SEARCH, MAC_VOICE,  MAC_DND,  	KC_MPRV,  	KC_MPLY,  	KC_MNXT, 	KC_MUTE, 	KC_VOLD, 	KC_VOLU, 	MAC_PRTA,	KC_INS,		KC_DEL,
	KC_GRV, 	KC_1,   	KC_2,   	KC_3,  		KC_4,   	KC_5,   	KC_6,   	KC_7,   	KC_8,   	KC_9,  		KC_0,   	KC_MINS,	KC_EQL, 				KC_BSPC,	KC_PGUP,
	KC_TAB, 	KC_Q,   	KC_W,   	KC_E,  		KC_R,   	KC_T,   	KC_Y,   	KC_U,   	KC_I,   	KC_O,  		KC_P,   	KC_LBRC,	KC_RBRC, 				KC_BSLS,	KC_PGDN,
	KC_CAPS,	KC_A,   	KC_S,   	KC_D,  		KC_F,   	KC_G,   	KC_H,   	KC_J,   	KC_K,   	KC_L,  		KC_SCLN,	KC_QUOT, 	 						KC_ENT,		KC_HOME,
	KC_LSFT,				KC_Z,   	KC_X,   	KC_C,  		KC_V,   	KC_B,   	KC_N,   	KC_M,   	KC_COMM,	KC_DOT,		KC_SLSH,				KC_RSFT,	KC_UP,		KC_END,
	KC_LCTL,	KC_LALT,	KC_LGUI,										KC_SPC, 							KC_RGUI,	KC_NO,   	KC_RCTL,				KC_LEFT,	KC_DOWN,    KC_RGHT),
};

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    return true;
}

// Trivial MSG_PING/hello reply, mirroring Phase 1's report shape.
void raw_hid_receive(uint8_t *data, uint8_t length) {
    uint8_t response[32] = {0};
    response[0] = data[0];
    raw_hid_send(response, sizeof(response));
}

// Confirms per-key rgb_matrix_set_color is reachable from user code, not
// just global effects.
bool rgb_matrix_indicators_user(void) {
    rgb_matrix_set_color(0, 0xFF, 0x00, 0x00);
    return true;
}

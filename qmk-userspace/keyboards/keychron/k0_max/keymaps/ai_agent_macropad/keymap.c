// Keychron K0 Max keymap — AI agent session-status pad.
//
// Built against Keychron's official firmware source
// (Keychron/qmk_firmware, 2025q3 branch). Layout, matrix, RGB LED
// indices, VID/PID, and the stock keymap below are all read from
// keyboards/keychron/k0_max/ — nothing here is guessed.
//
// Base layers are Keychron's own stock keymap
// (keymaps/keychron/keymap.c), unmodified except for:
//   - M1..M5 (left macro column) become AI_AGENT_KEY_0..4
//   - the encoder click, which was mute, becomes MO(FN) so the Fn
//     layer (Bluetooth pairing, RGB, 2.4 GHz) stays reachable after
//     M5 is no longer the Fn key
// Mute is on the Fn layer under the 0 key (hold encoder, tap 0).
// Encoder rotate is still volume on the base layer.
//
// 2025q3 already defines a strong via_command_kb() in
// keyboards/keychron/common/keychron_raw_hid.c, so this keymap
// cannot hook that symbol itself. Apply
// qmk-userspace/keyboards/keychron/k0_max/keychron_raw_hid.c.patch
// first — it adds a weak raw_hid_receive_kb() fallthrough, which is
// where this file plugs in.
#include QMK_KEYBOARD_H
#include "keychron_common.h"
#include "ai_agent_macropad.h"

enum layers {
    BASE,
    FN,
};

// NEW_SAFE_RANGE (from keychron_common.h) is this board's own stock
// enum's end — KC_LOPTN..BAT_LVL plus wireless extras. Starting here
// (not a hardcoded offset) tracks that automatically if it ever
// changes. VIA's customKeycodes array in via.json must stay aligned
// with that same QK_KB_0-based numbering.
enum ai_agent_macropad_keycodes {
    AI_AGENT_KEY_0 = NEW_SAFE_RANGE, // M1 (default)
    AI_AGENT_KEY_1,                  // M2 (default)
    AI_AGENT_KEY_2,                  // M3 (default)
    AI_AGENT_KEY_3,                  // M4 (default)
    AI_AGENT_KEY_4,                  // M5 (default; stock was MO(FN))
    AI_AGENT_KEY_5,                  // unwired by default — VIA-assignable
    AI_AGENT_KEY_6,
    AI_AGENT_KEY_7,
    AI_AGENT_KEY_8,
    AI_AGENT_KEY_9,
    AI_AGENT_KEY_10,
    AI_AGENT_KEY_11,
};

#define NUM_MACROPAD_SLOTS AI_AGENT_MACROPAD_MAX_SLOTS
#define DEVICE_ID_K0_MAX 0xC0

// clang-format off
const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    [BASE] = LAYOUT_tenkey_27(
        MO(FN),         KC_ESC,  KC_DEL,  KC_TAB,  KC_BSPC,
        AI_AGENT_KEY_0, KC_NUM,  KC_PSLS, KC_PAST, KC_PMNS,
        AI_AGENT_KEY_1, KC_P7,   KC_P8,   KC_P9,   KC_PPLS,
        AI_AGENT_KEY_2, KC_P4,   KC_P5,   KC_P6,
        AI_AGENT_KEY_3, KC_P1,   KC_P2,   KC_P3,
        AI_AGENT_KEY_4, KC_P0,            KC_PDOT, KC_PENT ),

    [FN] = LAYOUT_tenkey_27(
        _______, BT_HST1, BT_HST2, BT_HST3, P2P4G,
        _______, UG_TOGG, UG_NEXT, UG_VALU, UG_HUEU,
        _______, UG_PREV, UG_VALD, UG_HUED, _______,
        _______, UG_SATU, UG_SPDU, KC_MPRV,
        _______, UG_SATD, UG_SPDD, KC_MPLY,
        _______, KC_MUTE,          KC_MNXT, _______),
};

// clang-format on
#if defined(ENCODER_MAP_ENABLE)
const uint16_t PROGMEM encoder_map[][NUM_ENCODERS][2] = {
    [BASE] = {ENCODER_CCW_CW(KC_VOLD, KC_VOLU)},
    [FN]   = {ENCODER_CCW_CW(UG_VALD, UG_VALU)},
};
#endif

void keyboard_post_init_user(void) {
    // NULL: a static table can't know where the user has put each key —
    // ai_agent_macropad_scan_slots() (safe here; via_init() already ran
    // and loaded the dynamic keymap EEPROM) builds the real one from
    // the live keymap instead.
    ai_agent_macropad_init(NUM_MACROPAD_SLOTS, NULL);
    ai_agent_macropad_scan_slots(AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    return ai_agent_macropad_process_record(keycode, record, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

void matrix_scan_user(void) {
    ai_agent_macropad_task(NUM_MACROPAD_SLOTS);
}

// raw_hid_receive_kb() only exists once keychron_raw_hid.c.patch is
// applied — see this file's top comment. Peeks at VIA remaps, then
// claims this protocol's own MSG_* reports. Returning true fully
// claims the report (kc_raw_hid_rx passes this straight back to
// via_command_kb / via.c); false lets VIA's normal handling run.
bool raw_hid_receive_kb(uint8_t *data, uint8_t length) {
    ai_agent_macropad_track_via_remap(data, length, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
    return ai_agent_macropad_raw_hid_receive(data, length, DEVICE_ID_K0_MAX, NUM_MACROPAD_SLOTS);
}

bool rgb_matrix_indicators_user(void) {
    ai_agent_macropad_paint_indicators(NUM_MACROPAD_SLOTS);
    return true;
}

// Keychron K0 Max keymap — AI agent session-status pad.
//
// Built against Keychron's official firmware source
// (Keychron/qmk_firmware, 2025q3 branch). Layout, matrix, RGB LED
// indices, VID/PID, and the stock keymap below are all read from
// keyboards/keychron/k0_max/ — nothing here is guessed.
//
// Base layers are Keychron's own stock keymap
// (keymaps/keychron/keymap.c), unmodified except:
//   - the four top-row shape keys (stock Esc / Del / Tab / Bksp,
//     circle / triangle / square / X in the printed manual) become
//     AI_AGENT_KEY_0..3. Those keys' own LEDs face up with nothing
//     above them, so slot colors are painted on the row below
//     (Num Lock / * -, LED indices 5–8 in led_config.c) where they
//     catch the keycaps. The shape keys still send the slot presses.
//   - M1..M4 stay stock macros; M5 stays MO(FN) for Bluetooth pairing
//     and lighting. Hold M5 and tap 0 (or click the encoder) to suppress
//     the numpad rainbow so only slot colors remain — QMK's stock
//     UG_TOGG would disable the RGB engine entirely and take the slot
//     LEDs with it.
// Encoder click is mute; encoder rotate is volume.
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
    AI_AGENT_KEY_0 = NEW_SAFE_RANGE, // circle  (stock Esc)
    AI_AGENT_KEY_1,                  // triangle (stock Del)
    AI_AGENT_KEY_2,                  // square  (stock Tab)
    AI_AGENT_KEY_3,                  // X       (stock Bksp)
    AI_AGENT_KEY_4,                  // unwired by default — VIA-assignable
    AI_AGENT_KEY_5,
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
        KC_MUTE,        AI_AGENT_KEY_0, AI_AGENT_KEY_1, AI_AGENT_KEY_2, AI_AGENT_KEY_3,
        MC_1,           KC_NUM,         KC_PSLS,        KC_PAST,        KC_PMNS,
        MC_2,           KC_P7,          KC_P8,          KC_P9,          KC_PPLS,
        MC_3,           KC_P4,          KC_P5,          KC_P6,
        MC_4,           KC_P1,          KC_P2,          KC_P3,
        MO(FN),         KC_P0,                          KC_PDOT,        KC_PENT ),

    [FN] = LAYOUT_tenkey_27(
        UG_TOGG, BT_HST1, BT_HST2, BT_HST3, P2P4G,
        _______, UG_NEXT, UG_VALU, UG_HUEU, _______,
        _______, UG_PREV, UG_VALD, UG_HUED, _______,
        _______, UG_SATU, UG_SPDU, KC_MPRV,
        _______, UG_SATD, UG_SPDD, KC_MPLY,
        _______, UG_TOGG,          KC_MNXT, _______),
};

// clang-format on
#if defined(ENCODER_MAP_ENABLE)
const uint16_t PROGMEM encoder_map[][NUM_ENCODERS][2] = {
    [BASE] = {ENCODER_CCW_CW(KC_VOLD, KC_VOLU)},
    [FN]   = {ENCODER_CCW_CW(UG_VALD, UG_VALU)},
};
#endif

// Top-row shape keys are LEDs 0–3; the keys in the same columns one
// row down (Num Lock, /, *, -) are 5–8. Paint there so the color is
// visible from above. LED 4 is M1, left of Num Lock — not in this map.
static void k0_max_paint_shape_slots_on_row_below(void) {
    for (uint8_t i = 0; i < NUM_MACROPAD_SLOTS; i++) {
        uint8_t led = ai_agent_macropad_get_slot_led(i);
        if (led <= 3) {
            ai_agent_macropad_set_slot_led(i, (uint8_t)(led + 5));
        }
    }
}

void keyboard_post_init_user(void) {
    // NULL: a static table can't know where the user has put each key —
    // ai_agent_macropad_scan_slots() (safe here; via_init() already ran
    // and loaded the dynamic keymap EEPROM) builds the real one from
    // the live keymap instead.
    ai_agent_macropad_init(NUM_MACROPAD_SLOTS, NULL);
    ai_agent_macropad_scan_slots(AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
    k0_max_paint_shape_slots_on_row_below();
    // Slot indicators only run while the RGB engine is enabled. Hide
    // the stock animation (EEPROM often boots into Mix RGB / rainbow)
    // without calling rgb_matrix_disable() — that would skip
    // rgb_matrix_indicators_user() too.
    rgb_matrix_enable_noeeprom();
    rgb_matrix_set_flags_noeeprom(LED_FLAG_NONE);
    rgb_matrix_set_color_all(0, 0, 0);
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    if (keycode == UG_TOGG && record->event.pressed) {
        if (rgb_matrix_get_flags() == LED_FLAG_NONE) {
            rgb_matrix_set_flags_noeeprom(LED_FLAG_ALL);
        } else {
            rgb_matrix_set_flags_noeeprom(LED_FLAG_NONE);
            rgb_matrix_set_color_all(0, 0, 0);
        }
        return false;
    }
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
    k0_max_paint_shape_slots_on_row_below();
    return ai_agent_macropad_raw_hid_receive(data, length, DEVICE_ID_K0_MAX, NUM_MACROPAD_SLOTS);
}

bool rgb_matrix_indicators_user(void) {
    ai_agent_macropad_paint_indicators(NUM_MACROPAD_SLOTS);
    return true;
}

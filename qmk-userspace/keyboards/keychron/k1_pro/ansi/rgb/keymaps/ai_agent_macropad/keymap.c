// Keychron K1 Pro (ANSI) keymap — AI agent session-status pad.
//
// UNVERIFIED ON REAL HARDWARE — no K1 Pro in hand to flash and test,
// unlike the NuPhy Air75 V2 keymap in this repo (confirmed on real
// hardware at every step). Unlike an earlier version of this port,
// though, this one is built against Keychron's own official firmware
// source (keychron/qmk_firmware, wireless_playground branch — a peer
// checkout, not part of this repo) rather than a third-party fork, so
// the layout, matrix, LED indices, VID/PID, and stock keymap content
// below are all read directly from Keychron's real
// keyboards/keychron/k1_pro/ansi/rgb/ — nothing here is derived or
// guessed anymore. Base layers are Keychron's own stock K1 Pro keymap
// (keyboards/keychron/k1_pro/ansi/rgb/keymaps/via/keymap.c), carried
// over unchanged except for the 4 AI_AGENT_KEY substitutions below.
//
// One real (small, documented) deviation from stock firmware is still
// required: k1_pro.c (board-level, shared by every keymap for this
// board) already defines via_command_kb() — QMK's normal raw-HID
// early-intercept hook, the same one the Air75 keymap in this repo
// uses directly — to handle two vendor commands (bluetooth DFU,
// factory test). A keymap can't also define via_command_kb() itself
// (duplicate strong symbol, link error), so this port needs
// k1_pro.c.patch (in this same directory) applied to that file first —
// it adds one new, empty-by-default weak hook (raw_hid_receive_kb())
// that via_command_kb() now falls through to for anything it doesn't
// already claim, which is where this keymap plugs in below. See the
// Keychron K1 Pro section of the top-level README for the exact patch
// step; nothing here works without it.
//
// Ships 4 physical keys wired by default (PageUp/PageDn/Home/End, same
// choice as the Air75 keymap) as AI_AGENT_KEY_0..3, each showing one
// AI agent session's state via per-key RGB. AI_AGENT_KEY_4..11 exist
// as valid keycodes but aren't wired to any physical key here —
// VIA_ENABLE lets an end user drag them onto spare keys themselves (see
// ai_agent_macropad.c's dynamic slot/LED discovery). This board's own 13
// stock custom keycodes (KC_LOPTN..BAT_LVL in k1_pro.h) use up 13 of
// VIA's 32-entry customKeycodes budget, leaving room for all 12
// AI_AGENT_KEY_0..11 to be named in via.json (see that file) — unlike
// the Air75 board, which could only fit 8 of its 12.
#include QMK_KEYBOARD_H
#include "ai_agent_macropad.h"

enum layers {
    MAC_BASE,
    MAC_FN,
    WIN_BASE,
    WIN_FN,
};

// NEW_SAFE_RANGE (from k1_pro.h, exported via QMK_KEYBOARD_H) is this
// board's own stock enum's end — KC_LOPTN..BAT_LVL (13 entries,
// starting at QK_KB_0) plus the always-on bluetooth stack this board's
// rules.mk unconditionally includes (see k1_pro.h's KC_BLUETOOTH_ENABLE
// branch — BT_HST1..BAT_LVL only consume real enum slots in that
// branch, and this board is always built with it, per
// keyboards/keychron/k1_pro/rules.mk's unconditional
// `include keyboards/keychron/bluetooth/bluetooth.mk`). Starting here
// (not a hardcoded offset) tracks that automatically if it ever changes.
enum ai_agent_macropad_keycodes {
    AI_AGENT_KEY_0 = NEW_SAFE_RANGE,  // PageUp position (default)
    AI_AGENT_KEY_1,               // PageDn position (default)
    AI_AGENT_KEY_2,               // Home position (default)
    AI_AGENT_KEY_3,               // End position (default)
    AI_AGENT_KEY_4,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_5,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_6,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_7,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_8,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_9,                // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_10,               // unwired by default — VIA-assignable (named in via.json)
    AI_AGENT_KEY_11,               // unwired by default — VIA-assignable (named in via.json)
};

#define NUM_MACROPAD_SLOTS AI_AGENT_MACROPAD_MAX_SLOTS
#define DEVICE_ID_K1_PRO 0xC1

// clang-format off
const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    [MAC_BASE] = LAYOUT_tkl_ansi(
        KC_ESC,             KC_BRID,  KC_BRIU,  KC_MCTL,  KC_LPAD,  RGB_VAD,  RGB_VAI,  KC_MPRV,  KC_MPLY,  KC_MNXT,  KC_MUTE,  KC_VOLD,  KC_VOLU,  KC_SNAP,  KC_SIRI,  RGB_MOD,
        KC_GRV,   KC_1,     KC_2,     KC_3,     KC_4,     KC_5,     KC_6,     KC_7,     KC_8,     KC_9,     KC_0,     KC_MINS,  KC_EQL,   KC_BSPC,  KC_INS,   AI_AGENT_KEY_2, AI_AGENT_KEY_0,
        KC_TAB,   KC_Q,     KC_W,     KC_E,     KC_R,     KC_T,     KC_Y,     KC_U,     KC_I,     KC_O,     KC_P,     KC_LBRC,  KC_RBRC,  KC_BSLS,  KC_DEL,   AI_AGENT_KEY_3, AI_AGENT_KEY_1,
        KC_CAPS,  KC_A,     KC_S,     KC_D,     KC_F,     KC_G,     KC_H,     KC_J,     KC_K,     KC_L,     KC_SCLN,  KC_QUOT,            KC_ENT,
        KC_LSFT,            KC_Z,     KC_X,     KC_C,     KC_V,     KC_B,     KC_N,     KC_M,     KC_COMM,  KC_DOT,   KC_SLSH,            KC_RSFT,            KC_UP,
        KC_LCTL,  KC_LOPTN, KC_LCMMD,                               KC_SPC,                                 KC_RCMMD, KC_ROPTN, MO(MAC_FN),KC_RCTL, KC_LEFT,  KC_DOWN,  KC_RGHT),

    [MAC_FN] = LAYOUT_tkl_ansi(
        _______,            KC_F1,    KC_F2,    KC_F3,    KC_F4,    KC_F5,    KC_F6,    KC_F7,    KC_F8,    KC_F9,    KC_F10,   KC_F11,   KC_F12,   _______,  _______,  RGB_TOG,
        _______,  BT_HST1,  BT_HST2,  BT_HST3,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,
        RGB_TOG,  RGB_MOD,  RGB_VAI,  RGB_HUI,  RGB_SAI,  RGB_SPI,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,
        _______,  RGB_RMOD, RGB_VAD,  RGB_HUD,  RGB_SAD,  RGB_SPD,  _______,  _______,  _______,  _______,  _______,  _______,            _______,
        _______,            _______,  _______,  _______,  _______,  BAT_LVL,  NK_TOGG,  _______,  _______,  _______,  _______,            _______,            _______,
        _______,  _______,  _______,                                _______,                                _______,  _______,  _______,  _______,  _______,  _______,  _______),

    [WIN_BASE] = LAYOUT_tkl_ansi(
        KC_ESC,             KC_F1,    KC_F2,    KC_F3,    KC_F4,    KC_F5,    KC_F6,    KC_F7,    KC_F8,    KC_F9,    KC_F10,   KC_F11,   KC_F12,   KC_PSCR,  KC_CTANA, RGB_MOD,
        KC_GRV,   KC_1,     KC_2,     KC_3,     KC_4,     KC_5,     KC_6,     KC_7,     KC_8,     KC_9,     KC_0,     KC_MINS,  KC_EQL,   KC_BSPC,  KC_INS,   AI_AGENT_KEY_2, AI_AGENT_KEY_0,
        KC_TAB,   KC_Q,     KC_W,     KC_E,     KC_R,     KC_T,     KC_Y,     KC_U,     KC_I,     KC_O,     KC_P,     KC_LBRC,  KC_RBRC,  KC_BSLS,  KC_DEL,   AI_AGENT_KEY_3, AI_AGENT_KEY_1,
        KC_CAPS,  KC_A,     KC_S,     KC_D,     KC_F,     KC_G,     KC_H,     KC_J,     KC_K,     KC_L,     KC_SCLN,  KC_QUOT,            KC_ENT,
        KC_LSFT,            KC_Z,     KC_X,     KC_C,     KC_V,     KC_B,     KC_N,     KC_M,     KC_COMM,  KC_DOT,   KC_SLSH,            KC_RSFT,            KC_UP,
        KC_LCTL,  KC_LWIN,  KC_LALT,                                KC_SPC,                                 KC_RALT,  KC_RGUI, MO(WIN_FN),KC_RCTL,  KC_LEFT,  KC_DOWN,  KC_RGHT),

    [WIN_FN] = LAYOUT_tkl_ansi(
        _______,            KC_BRID,  KC_BRIU,  KC_TASK,  KC_FILE,  RGB_VAD,  RGB_VAI,  KC_MPRV,  KC_MPLY,  KC_MNXT,  KC_MUTE,  KC_VOLD,  KC_VOLU,  _______,  _______,  RGB_TOG,
        _______,  BT_HST1,  BT_HST2,  BT_HST3,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,
        RGB_TOG,  RGB_MOD,  RGB_VAI,  RGB_HUI,  RGB_SAI,  RGB_SPI,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,  _______,
        _______,  RGB_RMOD, RGB_VAD,  RGB_HUD,  RGB_SAD,  RGB_SPD,  _______,  _______,  _______,  _______,  _______,  _______,            _______,
        _______,            _______,  _______,  _______,  _______,  BAT_LVL,  NK_TOGG,  _______,  _______,  _______,  _______,            _______,            _______,
        _______,  _______,  _______,                                _______,                                _______,  _______,  _______,  _______,  _______,  _______,  _______)
};
// clang-format on

// This keymap requires VIA_ENABLE=yes (see this directory's rules.mk,
// and the raw_hid_receive_kb() comment further down for why a non-VIA
// build isn't an option on this particular board) — so, unlike the
// Air75 keymap, there's no static slot_to_led fallback table here: a
// live VIA/Vial keymap scan (ai_agent_macropad_scan_slots()) is the only
// path, not one of two.
void keyboard_post_init_user(void) {
    // NULL: a static table can't know where the user's put each key —
    // ai_agent_macropad_scan_slots() (safe here; via_init() already ran
    // and loaded the dynamic keymap EEPROM by this point in QMK's boot
    // sequence) builds the real one from the live keymap instead.
    ai_agent_macropad_init(NUM_MACROPAD_SLOTS, NULL);
    ai_agent_macropad_scan_slots(AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    return ai_agent_macropad_process_record(keycode, record, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
}

// This keymap only supports VIA_ENABLE=yes builds (this directory's
// rules.mk sets it, matching Keychron's own stock `via` keymap for this
// board) — unlike the Air75 board, k1_pro.c (board-level, shared by
// every keymap) already claims raw_hid_receive() outright in the
// non-VIA case, so a non-VIA fallback here would be a duplicate-symbol
// link error, not just dead code.
//
// raw_hid_receive_kb() only exists once k1_pro.c.patch (see this
// directory) is applied to the board-level k1_pro.c — see this file's
// top comment. It plays exactly the role the Air75 keymap's
// via_command_kb() does (early-intercept, ahead of via.c's own
// dispatch): ai_agent_macropad_track_via_remap() peeks at VIA's dynamic
// keymap commands to keep the slot -> LED table in sync with
// VIA-driven remaps, then ai_agent_macropad_raw_hid_receive() handles
// this protocol's own MSG_* messages. Returning true here fully claims
// the report (via_command_kb() in k1_pro.c passes this return value
// straight back to via.c, so a `true` here has the same "don't run
// via.c's own dispatch, don't send its own reply" effect the Air75
// keymap's comment describes) — false lets it fall through to via.c's
// normal handling, same as any other unrecognized command.
bool raw_hid_receive_kb(uint8_t *data, uint8_t length) {
    ai_agent_macropad_track_via_remap(data, length, AI_AGENT_KEY_0, NUM_MACROPAD_SLOTS);
    return ai_agent_macropad_raw_hid_receive(data, length, DEVICE_ID_K1_PRO, NUM_MACROPAD_SLOTS);
}

bool rgb_matrix_indicators_user(void) {
    ai_agent_macropad_paint_indicators(NUM_MACROPAD_SLOTS);
    return true;
}

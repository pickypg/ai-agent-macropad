// NuPhy Air75 V2 keymap — Claude Code session-status pad.
//
// Dedicates 4 physical keys (PageUp/PageDn/Home/End — the board's
// right-edge nav column; a small cluster to bring up and verify
// against real hardware before committing to a bigger one) as
// SLOT_KEY_0..3, each showing one Claude Code session's state via
// per-key RGB. Wire format matches hid_protocol.py (Phase 1) exactly;
// LED indices matches keyboard.json's rgb_matrix.layout array
// position (confirmed in Phase 0 — not hand-guessed).
//
// Pressing a SLOT_KEY sends a MSG_KEY report (slot index, key-down
// only — mirrors rp2040/code.py's press-only send) instead of typing
// anything; daemon.py's dispatch_bring_to_front() (transport-agnostic,
// unchanged) does the rest.
#include QMK_KEYBOARD_H
#include "raw_hid.h"

// QK_USER_0-based (keymap-level range) — NOT QK_KB_0, which ansi.h's
// own enum custom_keycodes (RF_DFU, LNK_USB, ...) already occupies.
// Also can't reuse the tag name "custom_keycodes" itself — ansi.h's
// enum already claims it, and C enum tags collide independently of
// the value range chosen.
enum keymap_keycodes {
    SLOT_KEY_0 = QK_USER_0,  // PageUp position
    SLOT_KEY_1,              // PageDn position
    SLOT_KEY_2,              // Home position
    SLOT_KEY_3,              // End position
};

#define NUM_MACROPAD_SLOTS 4
#define DEVICE_ID_AIR75_V2 0x01

// Message types — must match hid_protocol.py.
enum {
    MSG_HELLO = 0x01,
    MSG_SLOT  = 0x02,
    MSG_PING  = 0x03,
    MSG_KEY   = 0x04,
};

// Pad states — must match hid_protocol.py's STATE_*/rp2040/code.py's
// STATE_COLORS keys, 1:1 and in the same order. STATE_OFF (cleared,
// no session mapped) is intentionally distinct from STATE_IDLE (a
// session is mapped here but quiet) — rp2040/code.py's
// handle_message() renders them as different colors (fully dark vs.
// a dim glow), not just different labels, so this side needs to
// preserve that distinction too.
enum {
    STATE_IDLE = 0,
    STATE_WORKING,
    STATE_WAITING,
    STATE_DONE,
    STATE_ERROR,
    STATE_QUESTION,
    STATE_OFF,
};

// LED index per slot, in SLOT_KEY_0..3 order — read directly off
// keyboard.json's rgb_matrix.layout array position for each key's
// matrix cell (PageUp=[1,16], PageDn=[2,16], Home=[1,15], End=[2,15]),
// confirmed programmatically during Phase 4, not guessed.
static const uint8_t slot_to_led[NUM_MACROPAD_SLOTS] = {16, 45, 46, 73};

// Starts STATE_OFF, not STATE_IDLE — at boot no session is mapped to
// any slot yet, matching rp2040/code.py's NeoPixels defaulting dark
// before the first "slot"/"clear" message ever arrives.
static uint8_t slot_states[NUM_MACROPAD_SLOTS] = {STATE_OFF, STATE_OFF, STATE_OFF, STATE_OFF};

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
[0] = LAYOUT_ansi_84(
	KC_ESC, 	KC_BRID,  	KC_BRIU,  	MAC_TASK, 	MAC_SEARCH, MAC_VOICE,  MAC_DND,  	KC_MPRV,  	KC_MPLY,  	KC_MNXT, 	KC_MUTE, 	KC_VOLD, 	KC_VOLU, 	MAC_PRTA,	KC_INS,		KC_DEL,
	KC_GRV, 	KC_1,   	KC_2,   	KC_3,  		KC_4,   	KC_5,   	KC_6,   	KC_7,   	KC_8,   	KC_9,  		KC_0,   	KC_MINS,	KC_EQL, 				KC_BSPC,	SLOT_KEY_0,
	KC_TAB, 	KC_Q,   	KC_W,   	KC_E,  		KC_R,   	KC_T,   	KC_Y,   	KC_U,   	KC_I,   	KC_O,  		KC_P,   	KC_LBRC,	KC_RBRC, 				KC_BSLS,	SLOT_KEY_1,
	KC_CAPS,	KC_A,   	KC_S,   	KC_D,  		KC_F,   	KC_G,   	KC_H,   	KC_J,   	KC_K,   	KC_L,  		KC_SCLN,	KC_QUOT, 	 						KC_ENT,		SLOT_KEY_2,
	KC_LSFT,				KC_Z,   	KC_X,   	KC_C,  		KC_V,   	KC_B,   	KC_N,   	KC_M,   	KC_COMM,	KC_DOT,		KC_SLSH,				KC_RSFT,	KC_UP,		SLOT_KEY_3,
	KC_LCTL,	KC_LALT,	KC_LGUI,										KC_SPC, 							KC_RGUI,	KC_NO,   	KC_RCTL,				KC_LEFT,	KC_DOWN,    KC_RGHT),
};

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    switch (keycode) {
        case SLOT_KEY_0:
        case SLOT_KEY_1:
        case SLOT_KEY_2:
        case SLOT_KEY_3:
            // Still swallowed either way (see file header) — these
            // stay dedicated, inert keys, never typed. Slot index is
            // SLOT_KEY_0..3's position, valid since they're sequential
            // enum values starting at QK_USER_0.
            if (record->event.pressed) {
                uint8_t report[32] = {0};
                report[0] = MSG_KEY;
                report[1] = keycode - SLOT_KEY_0;
                raw_hid_send(report, sizeof(report));
            }
            return false;
        default:
            return true;
    }
}

// Host -> device: MSG_PING replies with MSG_HELLO (daemon's
// discover_hid_device()/handshake() handshake); MSG_SLOT updates one
// slot's displayed state.
void raw_hid_receive(uint8_t *data, uint8_t length) {
    if (length < 1) return;

    switch (data[0]) {
        case MSG_PING: {
            uint8_t response[32] = {0};
            response[0] = MSG_HELLO;
            response[1] = DEVICE_ID_AIR75_V2;
            response[2] = NUM_MACROPAD_SLOTS;
            raw_hid_send(response, sizeof(response));
            break;
        }
        case MSG_SLOT: {
            if (length < 3) return;
            uint8_t index = data[1];
            uint8_t state = data[2];
            if (index < NUM_MACROPAD_SLOTS && state <= STATE_OFF) {
                slot_states[index] = state;
            }
            break;
        }
        default:
            break;
    }
}

// Mirrors STATE_COLORS in rp2040/code.py 1:1 — including "off" (fully
// dark) being visually distinct from "idle" (dim gray glow).
static void state_to_rgb(uint8_t state, uint8_t *r, uint8_t *g, uint8_t *b) {
    switch (state) {
        case STATE_WORKING:  *r = 0;   *g = 0;   *b = 255; break;
        case STATE_WAITING:  *r = 255; *g = 170; *b = 0;   break;
        case STATE_DONE:     *r = 0;   *g = 255; *b = 0;   break;
        case STATE_ERROR:    *r = 255; *g = 0;   *b = 0;   break;
        case STATE_QUESTION: *r = 255; *g = 127; *b = 0;   break;
        case STATE_OFF:       *r = 0;   *g = 0;   *b = 0;   break;
        case STATE_IDLE:
        default:              *r = 40;  *g = 40;  *b = 40;  break;
    }
}

// "question" blinks, same as rp2040/code.py's BLINK_STATES/
// BLINK_PERIOD (500ms on/off) — derived from the free-running frame
// timer rather than tracked state, since this runs every RGB matrix
// tick already.
bool rgb_matrix_indicators_user(void) {
    bool blink_on = (timer_read32() / 500) % 2 == 0;

    for (uint8_t i = 0; i < NUM_MACROPAD_SLOTS; i++) {
        uint8_t state = slot_states[i];
        uint8_t r, g, b;
        if (state == STATE_QUESTION && !blink_on) {
            r = g = b = 0;
        } else {
            state_to_rgb(state, &r, &g, &b);
        }
        rgb_matrix_set_color(slot_to_led[i], r, g, b);
    }
    return true;
}

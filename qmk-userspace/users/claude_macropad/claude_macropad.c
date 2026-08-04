#include "claude_macropad.h"
#include "raw_hid.h"

static uint8_t slot_states[CLAUDE_MACROPAD_MAX_SLOTS];

void claude_macropad_init(uint8_t num_slots) {
    for (uint8_t i = 0; i < num_slots && i < CLAUDE_MACROPAD_MAX_SLOTS; i++) {
        slot_states[i] = STATE_OFF;
    }
}

bool claude_macropad_process_record(uint16_t keycode, keyrecord_t *record, uint16_t slot_key_base, uint8_t num_slots) {
    if (keycode < slot_key_base || keycode >= (uint16_t)(slot_key_base + num_slots)) {
        return true;
    }

    // Still swallowed either way — these stay dedicated, inert keys,
    // never typed. Slot index is the keycode's position past
    // slot_key_base, valid since the keymap's SLOT_KEY_* enum values
    // are sequential starting there.
    if (record->event.pressed) {
        uint8_t report[CLAUDE_MACROPAD_REPORT_SIZE] = {0};
        report[0] = MSG_KEY;
        report[1] = keycode - slot_key_base;
        raw_hid_send(report, sizeof(report));
    }
    return false;
}

// Host -> device: MSG_PING replies with MSG_HELLO (daemon's
// discover_hid_device()/handshake() handshake); MSG_SLOT updates one
// slot's displayed state. Returns whether `data[0]` was one of ours
// (see header for why this matters on VIA_ENABLE boards).
bool claude_macropad_raw_hid_receive(uint8_t *data, uint8_t length, uint8_t device_id, uint8_t num_slots) {
    if (length < 1) return false;

    switch (data[0]) {
        case MSG_PING: {
            uint8_t response[CLAUDE_MACROPAD_REPORT_SIZE] = {0};
            response[0] = MSG_HELLO;
            response[1] = device_id;
            response[2] = num_slots;
            raw_hid_send(response, sizeof(response));
            return true;
        }
        case MSG_SLOT: {
            if (length < 3) return true;  // ours, just malformed — drop it
            uint8_t index = data[1];
            uint8_t state = data[2];
            if (index < num_slots && index < CLAUDE_MACROPAD_MAX_SLOTS && state <= STATE_OFF) {
                slot_states[index] = state;
            }
            return true;
        }
        default:
            return false;
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
void claude_macropad_paint_indicators(const uint8_t *slot_to_led, uint8_t num_slots) {
    bool blink_on = (timer_read32() / 500) % 2 == 0;

    // rgb_matrix_set_color() writes the LED buffer directly, bypassing
    // the HSV "value" scaling QMK's animation effects apply — without
    // this, RGB_MATRIX_VAI/VAD (brightness keys) would have no effect
    // on the status indicators.
    uint8_t val = rgb_matrix_get_val();

    for (uint8_t i = 0; i < num_slots; i++) {
        uint8_t state = slot_states[i];
        uint8_t r, g, b;
        if (state == STATE_QUESTION && !blink_on) {
            r = g = b = 0;
        } else {
            state_to_rgb(state, &r, &g, &b);
            r = (uint16_t)r * val / 255;
            g = (uint16_t)g * val / 255;
            b = (uint16_t)b * val / 255;
        }
        rgb_matrix_set_color(slot_to_led[i], r, g, b);
    }
}

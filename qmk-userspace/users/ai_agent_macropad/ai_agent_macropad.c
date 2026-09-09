#include "ai_agent_macropad.h"
#include "raw_hid.h"
#include "rgb_matrix.h"

#ifdef VIA_ENABLE
#    include "via.h"
#    include "dynamic_keymap.h"
#    include "keymap_introspection.h"
#endif

// -- Two index spaces ------------------------------------------------
//
// **Keycode index**: which AI_AGENT_KEY_* a slot belongs to, i.e.
// `keycode - slot_key_base`. Always 0..num_slots, sparse — the user may
// have placed only AI Slot 0, 7 and 11 on real keys, leaving the rest
// on nothing.
//
// **Wire index**: what the daemon addresses, and what MSG_HELLO's slot
// count describes. Dense: those same three become 0, 1, 2.
//
// The daemon has no idea which AI_AGENT_KEY_* is behind a wire index,
// and doesn't need one — it only needs the count to be honest, so that
// every key it can address lights something. Reporting
// AI_AGENT_MACROPAD_MAX_SLOTS instead (which every keymap's
// NUM_MACROPAD_SLOTS is) told a stock 4-key board's daemon it had 12,
// and the 8 phantom keys silently swallowed everything sent to them.
//
// `slot_led` stays in keycode space, because that is what the VIA remap
// tracking and any board's LED fixup work in. Everything the daemon
// touches — slot_states, the hold timers, hello, MSG_SLOT, MSG_KEY —
// is in wire space. rebuild_dense() is the only bridge.

// Wire index -> displayed state.
static uint8_t slot_states[AI_AGENT_MACROPAD_MAX_SLOTS];

// Keycode index -> RGB matrix LED index; NO_LED (quantum/rgb_matrix)
// means no key is currently assigned to that slot. Static tables
// (non-VIA boards) and the VIA dynamic-keymap scan both funnel into
// this same array, so rebuild_dense() never needs to care which one
// populated it.
static uint8_t slot_led[AI_AGENT_MACROPAD_MAX_SLOTS];

// Wire index -> RGB matrix LED index, and keycode index -> wire index
// (AI_AGENT_NO_SLOT where that keycode is on no key). Both rebuilt
// together by rebuild_dense(); dense_count is what MSG_HELLO reports.
static uint8_t dense_led[AI_AGENT_MACROPAD_MAX_SLOTS];
static uint8_t dense_of[AI_AGENT_MACROPAD_MAX_SLOTS];
static uint8_t dense_count;

// Wire index -> timer_read() at the most recent press of that slot's key.
// key_hold_pending mirrors it: true from press until either the hold
// threshold fires (ai_agent_macropad_task()) or the key is released,
// whichever comes first — see both for how they use it together.
static uint16_t key_down_time[AI_AGENT_MACROPAD_MAX_SLOTS];
static bool     key_hold_pending[AI_AGENT_MACROPAD_MAX_SLOTS];

// Recompute the wire view from slot_led. Called at the tail of every
// place that writes slot_led — including the public setter, so a
// board's own post-scan LED fixup composes with this for free — and
// cheap enough (one pass over 12) to run on every VIA keystroke.
//
// Deliberately does NOT preserve slot_states across the rebuild: a
// remap changes which session a wire index refers to, and the daemon
// resends the truth on its next handshake. Carrying a stale color onto
// a newly-numbered key would be worse than going dark.
static void rebuild_dense(void) {
    dense_count = 0;
    for (uint8_t i = 0; i < AI_AGENT_MACROPAD_MAX_SLOTS; i++) {
        dense_of[i] = AI_AGENT_NO_SLOT;
    }
    for (uint8_t i = 0; i < AI_AGENT_MACROPAD_MAX_SLOTS; i++) {
        if (slot_led[i] == NO_LED) continue;
        dense_led[dense_count] = slot_led[i];
        dense_of[i]            = dense_count;
        slot_states[dense_count] = STATE_OFF;
        dense_count++;
    }
}

uint8_t ai_agent_macropad_slot_count(void) {
    return dense_count;
}

void ai_agent_macropad_init(uint8_t num_slots, const uint8_t *slot_to_led) {
    for (uint8_t i = 0; i < AI_AGENT_MACROPAD_MAX_SLOTS; i++) {
        slot_states[i] = STATE_OFF;
        slot_led[i]    = (slot_to_led && i < num_slots) ? slot_to_led[i] : NO_LED;
    }
    rebuild_dense();
}

#ifdef VIA_ENABLE
// Clears any slot currently pointing at `led` (i.e. whatever used to be
// assigned to this physical position), then, if `keycode` is one of
// ours, points that slot at `led`. Handles remap-away, remap-in, and
// swap-in-place with the same two steps — no reverse row/col -> slot
// lookup needed, since an LED index can only ever back one slot at a
// time.
static void set_slot_led_for_keycode(uint16_t slot_key_base, uint8_t num_slots, uint8_t led, uint16_t keycode) {
    for (uint8_t i = 0; i < num_slots && i < AI_AGENT_MACROPAD_MAX_SLOTS; i++) {
        if (slot_led[i] == led) {
            slot_led[i] = NO_LED;
        }
    }
    if (keycode >= slot_key_base && keycode < (uint16_t)(slot_key_base + num_slots)) {
        uint8_t index = keycode - slot_key_base;
        if (index < AI_AGENT_MACROPAD_MAX_SLOTS) {
            slot_led[index] = led;
        }
    }
    // No rebuild_dense() here: rescan_slots() calls this once per matrix
    // cell and rebuilds once at the end. The single-remap path in
    // track_via_remap() rebuilds for itself.
}

static void rescan_slots(uint16_t slot_key_base, uint8_t num_slots, bool from_static_layer) {
    for (uint8_t i = 0; i < num_slots && i < AI_AGENT_MACROPAD_MAX_SLOTS; i++) {
        slot_led[i] = NO_LED;
    }
    for (uint8_t row = 0; row < MATRIX_ROWS; row++) {
        for (uint8_t col = 0; col < MATRIX_COLS; col++) {
            uint16_t keycode = from_static_layer ? keycode_at_keymap_location_raw(0, row, col) : dynamic_keymap_get_keycode(0, row, col);
            set_slot_led_for_keycode(slot_key_base, num_slots, g_led_config.matrix_co[row][col], keycode);
        }
    }
    rebuild_dense();
}

void ai_agent_macropad_scan_slots(uint16_t slot_key_base, uint8_t num_slots) {
    rescan_slots(slot_key_base, num_slots, false);
}

void ai_agent_macropad_track_via_remap(uint8_t *data, uint8_t length, uint16_t slot_key_base, uint8_t num_slots) {
    if (length < 1) return;

    switch (data[0]) {
        case id_dynamic_keymap_set_keycode: {
            // data = [cmd, layer, row, col, keycode_hi, keycode_lo]. Read
            // straight off the incoming command rather than re-reading
            // dynamic_keymap_get_keycode() afterward — this runs before
            // via.c's own dispatch actually writes the change to EEPROM,
            // so a read here would still see the OLD value.
            if (length < 6 || data[1] != 0) return;  // only layer 0 is ours to track
            uint8_t  row = data[2], col = data[3];
            uint16_t new_keycode = ((uint16_t)data[4] << 8) | data[5];
            set_slot_led_for_keycode(slot_key_base, num_slots, g_led_config.matrix_co[row][col], new_keycode);
            rebuild_dense();
            break;
        }
        case id_dynamic_keymap_reset: {
            // Deterministic outcome (copies the static layer back into
            // EEPROM), so unlike a single set_keycode this is safe to
            // resolve by rescanning the static layer immediately —  no
            // need to wait for via.c to actually finish the EEPROM write.
            rescan_slots(slot_key_base, num_slots, true);
            break;
        }
        default:
            break;
    }
}
#endif

void ai_agent_macropad_set_slot_led(uint8_t index, uint8_t led) {
    if (index < AI_AGENT_MACROPAD_MAX_SLOTS) {
        slot_led[index] = led;
        rebuild_dense();
    }
}

uint8_t ai_agent_macropad_get_slot_led(uint8_t index) {
    if (index >= AI_AGENT_MACROPAD_MAX_SLOTS) {
        return NO_LED;
    }
    return slot_led[index];
}

bool ai_agent_macropad_process_record(uint16_t keycode, keyrecord_t *record, uint16_t slot_key_base, uint8_t num_slots) {
    if (keycode < slot_key_base || keycode >= (uint16_t)(slot_key_base + num_slots)) {
        return true;
    }

    // Still swallowed either way — these stay dedicated, inert keys,
    // never typed. The keycode's position past slot_key_base is its
    // keycode index (valid since the keymap's AI_AGENT_KEY_* enum values
    // are sequential from there); the daemon is told the wire index.
    uint8_t index = dense_of[keycode - slot_key_base];
    if (index == AI_AGENT_NO_SLOT) {
        // Pressed a slot keycode that rebuild_dense() says is on no
        // key. Shouldn't be reachable, but swallow it rather than
        // reporting a slot the daemon can't resolve.
        return false;
    }

    if (record->event.pressed) {
        uint8_t report[AI_AGENT_MACROPAD_REPORT_SIZE] = {0};
        report[0] = MSG_KEY;
        report[1] = index;
        raw_hid_send(report, sizeof(report));
        if (index < AI_AGENT_MACROPAD_MAX_SLOTS) {
            key_down_time[index]   = timer_read();
            key_hold_pending[index] = true;
        }
    } else if (index < AI_AGENT_MACROPAD_MAX_SLOTS) {
        // Released before crossing the hold threshold (the common,
        // tap case) — ai_agent_macropad_task() already stops checking
        // once it fires MSG_KEY_HELD, but a tap never reaches that
        // point at all, so this just cancels the pending check.
        key_hold_pending[index] = false;
    }
    return false;
}

// Call from matrix_scan_user() (every board using this protocol wires
// this in) — continuously polls every currently-held slot key so a
// hold fires MSG_KEY_HELD the instant it crosses the threshold, rather
// than waiting for key-up. Each key fires at most once per press:
// key_hold_pending is cleared the moment it does, or on release,
// whichever happens first.
void ai_agent_macropad_task(uint8_t num_slots) {
    (void)num_slots; // the wire count is what has keys behind it
    for (uint8_t i = 0; i < dense_count; i++) {
        if (!key_hold_pending[i]) continue;
        if (timer_elapsed(key_down_time[i]) < AI_AGENT_MACROPAD_HOLD_THRESHOLD_MS) continue;

        key_hold_pending[i] = false;
        uint8_t report[AI_AGENT_MACROPAD_REPORT_SIZE] = {0};
        report[0] = MSG_KEY_HELD;
        report[1] = i;
        raw_hid_send(report, sizeof(report));
    }
}

// Host -> device: MSG_PING replies with MSG_HELLO (daemon's
// discover_hid_device()/handshake() handshake); MSG_SLOT updates one
// slot's displayed state. Returns whether `data[0]` was one of ours
// (see header for why this matters on VIA_ENABLE boards).
bool ai_agent_macropad_raw_hid_receive(uint8_t *data, uint8_t length, uint8_t device_id, uint8_t num_slots) {
    if (length < 1) return false;

    switch (data[0]) {
        case MSG_PING: {
            // Byte 1 is the daemon's PROTOCOL_VERSION. A report too
            // short to carry it is ours to claim so VIA doesn't see
            // it, but not a handshake we'll answer. A version that
            // *differs* from ours still gets a hello: the daemon
            // compares and warns; we don't refuse, so a newer pad can
            // still identify itself to an older daemon.
            if (length < 2) {
                return true;
            }
            uint8_t response[AI_AGENT_MACROPAD_REPORT_SIZE] = {0};
            response[0] = MSG_HELLO;
            response[1] = device_id;
            // The keys that actually exist right now, not the size of
            // the keycode enum — see the index-space note up top.
            response[2] = dense_count;
            response[3] = AI_AGENT_MACROPAD_PROTOCOL_VERSION;
            raw_hid_send(response, sizeof(response));
            return true;
        }
        case MSG_SLOT: {
            if (length < 3) return true;  // ours, just malformed — drop it
            uint8_t index = data[1];
            uint8_t state = data[2];
            // state <= STATE_OFF is a coarse "in the reserved wire
            // range" check, not "a state this firmware recognizes" —
            // STATE_OFF is pinned well above the states actually
            // defined today (see the enum), so this happily accepts
            // states added by a newer daemon than this firmware build.
            // state_to_rgb() is what decides whether a given value is
            // actually a known case or falls through to the "unknown"
            // fallback color.
            if (index < dense_count && state <= STATE_OFF) {
                slot_states[index] = state;
            }
            return true;
        }
        default:
            return false;
    }
}

// Mirrors STATE_COLORS in rp2040/code.py 1:1 — including "off" (fully
// dark) being visually distinct from "idle" (dim gray glow), and
// "unknown" (magenta) for a state byte in the valid wire range (see
// raw_hid_receive) that isn't one of the cases below — e.g. this
// firmware build predates a state the daemon has since added. Magenta
// rather than falling back to idle's gray, so version skew is visually
// obvious instead of quietly looking like nothing's happening.
static void state_to_rgb(uint8_t state, uint8_t *r, uint8_t *g, uint8_t *b) {
    switch (state) {
        case STATE_IDLE:          *r = 40;  *g = 40;  *b = 40;  break;
        case STATE_WORKING:       *r = 0;   *g = 0;   *b = 255; break;
        case STATE_WAITING:       *r = 255; *g = 170; *b = 0;   break;
        case STATE_DONE:          *r = 0;   *g = 255; *b = 0;   break;
        case STATE_ERROR:         *r = 255; *g = 0;   *b = 0;   break;
        case STATE_QUESTION:      *r = 255; *g = 127; *b = 0;   break;
        case STATE_TOOL_RUNNING:  *r = 128; *g = 0;   *b = 255; break;
        case STATE_TOOL_STALLED:  *r = 128; *g = 0;   *b = 255; break;
        case STATE_OFF:           *r = 0;   *g = 0;   *b = 0;   break;
        default:                  *r = 255; *g = 0;   *b = 255; break;
    }
}

// "question" and "tool_stalled" blink, same as rp2040/code.py's
// BLINK_STATES/BLINK_PERIOD (500ms on/off) — derived from the
// free-running frame timer rather than tracked state, since this runs
// every RGB matrix tick already.
void ai_agent_macropad_paint_indicators(uint8_t num_slots) {
    (void)num_slots;
    bool blink_on = (timer_read32() / 500) % 2 == 0;

    // rgb_matrix_set_color() writes the LED buffer directly, bypassing
    // the HSV "value" scaling QMK's animation effects apply — without
    // this, RGB_MATRIX_VAI/VAD (brightness keys) would have no effect
    // on the status indicators.
    uint8_t val = rgb_matrix_get_val();

    for (uint8_t i = 0; i < dense_count; i++) {
        uint8_t state = slot_states[i];
        uint8_t r, g, b;
        if ((state == STATE_QUESTION || state == STATE_TOOL_STALLED) && !blink_on) {
            r = g = b = 0;
        } else {
            state_to_rgb(state, &r, &g, &b);
            r = (uint16_t)r * val / 255;
            g = (uint16_t)g * val / 255;
            b = (uint16_t)b * val / 255;
        }
        rgb_matrix_set_color(dense_led[i], r, g, b);
    }
}

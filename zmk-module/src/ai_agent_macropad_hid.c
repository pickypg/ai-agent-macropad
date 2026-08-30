/*
 * AI Agent Macropad — raw-HID ping/hello listener (ZMK Phase 1).
 *
 * Wire format matches hid_protocol.py exactly (see that file and
 * qmk-userspace/users/ai_agent_macropad/ai_agent_macropad.h, its QMK-side
 * twin): report[0] is the message type. MSG_PING (host -> device) asks
 * for a MSG_HELLO reply carrying device id / slot count / protocol
 * version in report[1..3]. Any change to these values must be mirrored
 * in hid_protocol.py's MSG_* / PROTOCOL_VERSION and in the QMK header.
 *
 * Built on zzeneg/zmk-raw-hid (CONFIG_RAW_HID) for the underlying
 * transport. This listener only answers the handshake — MSG_SLOT (RGB)
 * and MSG_KEY/MSG_KEY_HELD are Phase 2.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include <raw_hid/raw_hid.h>
#include <raw_hid/events.h>

LOG_MODULE_REGISTER(ai_agent_macropad_hid, CONFIG_ZMK_LOG_LEVEL);

// Message types — must match hid_protocol.py's MSG_HELLO/MSG_PING.
#define MSG_HELLO 0xA1
#define MSG_PING 0x21

// Must match hid_protocol.py's PROTOCOL_VERSION.
#define PROTOCOL_VERSION 1

static int ai_agent_macropad_hid_listener(const zmk_event_t *eh) {
    struct raw_hid_received_event *event = as_raw_hid_received_event(eh);
    if (event == NULL || event->length < 2 || event->data[0] != MSG_PING) {
        return ZMK_EV_EVENT_BUBBLE;
    }

    LOG_INF("ai_agent_macropad: MSG_PING -> MSG_HELLO (device=0x%02x slots=%d)",
             CONFIG_AI_AGENT_MACROPAD_DEVICE_ID, CONFIG_AI_AGENT_MACROPAD_NUM_SLOTS);

    uint8_t report[CONFIG_RAW_HID_REPORT_SIZE];
    memset(report, 0, sizeof(report));
    report[0] = MSG_HELLO;
    report[1] = CONFIG_AI_AGENT_MACROPAD_DEVICE_ID;
    report[2] = CONFIG_AI_AGENT_MACROPAD_NUM_SLOTS;
    report[3] = PROTOCOL_VERSION;

    raise_raw_hid_sent_event(
        (struct raw_hid_sent_event){.data = report, .length = sizeof(report)});

    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(ai_agent_macropad_hid, ai_agent_macropad_hid_listener);
ZMK_SUBSCRIPTION(ai_agent_macropad_hid, raw_hid_received_event);

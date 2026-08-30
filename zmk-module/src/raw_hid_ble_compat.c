/*
 * Compatibility shim, NOT a real implementation: zzeneg/zmk-raw-hid's
 * src/hog.c (compiled whenever CONFIG_ZMK_BLE=y) calls
 * zmk_ble_active_profile_conn() to get the active profile's struct
 * bt_conn*, expecting the ZMK core API it was written against. This
 * fork's zmk/app/include/zmk/ble.h has no such function -- it only
 * exposes zmk_ble_active_profile_index()/_btid(), not a direct
 * connection object accessor -- so linking zmk-raw-hid against this
 * fork fails with an undefined reference without this.
 *
 * Phase 1 only needs raw HID over USB (see zmk_plan.md); Bluetooth is
 * Phase 3. Always returning NULL here means hog.c's send_report() logs
 * "Not connected to active profile" and no-ops instead of crashing or
 * failing to link -- correct behavior for "BLE raw-HID isn't wired up
 * yet", wrong once Phase 3 actually needs it. Replace with a real
 * lookup (e.g. via bt_conn_lookup_addr_le() using the active profile's
 * bonded address) then.
 */

#include <zephyr/bluetooth/conn.h>

struct bt_conn *zmk_ble_active_profile_conn(void) { return NULL; }

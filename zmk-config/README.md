# ZMK bring-up (Phase 1) — Keychron K3 Ultra 8K via ai-agent-macropad

See `../zmk_plan.md` for the full plan. This directory is the Phase 1
firmware skeleton: a west workspace manifest (`config/west.yml`) plus a
build target (`build.yaml`) for the raw-HID ping/hello handshake. The
actual listener code lives in `../zmk-module/` (a Zephyr module rooted
at `../zephyr/module.yml`, so this whole repo doubles as a west
project without needing a second GitHub repo).

## Status (as of 2026-08-26 — build verified end-to-end)

**K3 Ultra 8K has no public ZMK board/shield anywhere** — not in
Keychron's fork, not in the community. Phase 1 therefore targets the
**Keychron Q3 Ultra 8K** instead, as a stand-in: same chip family, same
RGB driver, and — critically — a real, publicly published shield.
Once K3 Ultra 8K's own matrix/RGB pinout is known (reverse-engineered
or published), swapping `keychron_q3_ultra_ansi` for a new
`keychron_k3_ultra_ansi` shield in `build.yaml` should be close to a
drop-in change, since the raw-HID listener in `zmk-module/` doesn't
touch board-specific code at all.

**The build has actually been run to completion on real macOS (Apple
Silicon) hardware.** `zmk.elf` links successfully:

```
Memory region         Used Size  Region Size  %age Used
           FLASH:      299820 B       800 KB     36.60%
             RAM:       82100 B       100 KB     80.18%
           TRACE:       13713 B       512 KB      2.62%
            ITCM:       99576 B       104 KB     93.50%
```

The only remaining step — Realtek's own post-build image-signing tool
(`zmk/app/tools/prepend_header/linux-x86_64/prepend_header`) — is a
prebuilt **Linux x86_64 ELF binary** with no macOS/ARM equivalent
shipped, so it can't run natively on this machine ("cannot execute
binary file"). That's a host-platform limitation, not a code or config
problem — run this same recipe under Linux (Docker, a Linux CI runner,
or Keychron's own `ghcr.io/zephyrproject-rtos/ci` image) to get past it
to an actual flashable `.bin`.

Everything below this got fixed along the way; each one was root-caused
against real error output, not guessed:

1. **The MCU is Realtek RTL8762G, not Nordic nRF52** as `zmk_plan.md`'s
   original Phase 0 section guessed. Confirmed via
   `CONFIG_BT_RTL87X2G=y` in Keychron's own shield `.conf` files, and
   again by the actual `arm-zephyr-eabi-gcc -mcpu=cortex-m55` build
   flags.
2. Mainline Zephyr has no RTL8762G support at all. Keychron ships their
   own **Zephyr fork** (`Keychron/zephyr`, branch `rtl87x2g`) with the
   Realtek HAL baked in, imported by their ZMK fork's `app/west.yml`
   (`Keychron/zmk`, branch `rtl8762g` — not `main`, which just tracks
   upstream `zmkfirmware/zmk` with none of this).
3. This is **Zephyr 3.5.0**, which needs **Zephyr SDK 0.16.x**
   specifically (`0.16.9` used here) — the current SDK release (`1.0.1`
   at research time) is rejected outright by a CMake version check.
4. Every Keychron Ultra shield's `Kconfig.shield` `select`s
   `KEYCHRON_RGB_ENABLE`, `SNAP_CLICK_ENABLE`, `RETAIL_DEMO_ENABLE`,
   etc. — symbols **never `config`-defined** anywhere in either
   `Keychron/zmk` or `Keychron/zephyr`. Concretely, `keychron_q3_ultra_
   ansi.conf` sets `CONFIG_CKLED2001=y` / `CONFIG_CKLED2001_SPI=y` (the
   RGB driver chip) for symbols that don't exist — and Zephyr's Kconfig
   parser treats "assignment to an undefined symbol" as a **hard,
   unconditional build-aborting error** ("Aborting due to Kconfig
   warnings"), not a soft warning. `zmk-module/Kconfig` stubs both as
   plain bools purely to unblock the build; the real CKLED2001 driver
   source is presumably also missing, so RGB is not and cannot be
   functional this way — correct for Phase 1 (RGB is Phase 2), not a
   real fix.
5. That same missing-RGB-glue chain leaves `CONFIG_SPI_RTL87X2G_DMA=y`
   (also set by Keychron's shield conf) with unmet dependencies
   (`SPI`/`SPI_RTL87X2G` both `=n`) — also fatal. Turned off in
   `config/ai_agent_macropad.conf` since Phase 1 doesn't need SPI/RGB.
6. **Unrelated to Keychron entirely**: this exact ZMK snapshot has a
   pre-existing bug in the (upstream, mainline-inherited)
   `clueboard_california` shield — `Kconfig.defconfig` assigns a
   default to `ZMK_KSCAN_DIRECT_POLLING` without ever declaring its
   type. Because Zephyr's Kconfig loader parses *every* shield's config
   up front regardless of which one is selected, this one bug broke
   *every* build from this checkout, Keychron boards included. Fixed
   by `keychron-zmk-fork.patch` in this directory (apply after `west
   update`, see below).
7. **A genuine bug in Keychron's own `behavior_keychron.c`**:
   `get_charge_state()` is defined unconditionally, but the
   `bat_charge_state` variable it returns only exists inside an
   `#if DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT)` block that's false
   whenever the devicetree lacks whatever charger-IC node this
   `DT_DRV_COMPAT` expects (true for `keychron` + `keychron_q3_ultra_
   ansi`, since no such node is defined). Also fixed by
   `keychron-zmk-fork.patch`.
8. **Build-invocation gotcha**: passing `-DZEPHYR_EXTRA_MODULES=<path>`
   on the command line *replaces* the value ZMK's own
   `app/CMakeLists.txt` would otherwise set — which is how it includes
   its own required `app/module` and `app/keymap-module` (the latter
   parses `.keymap` files into devicetree; without it, no `zmk,keymap`
   node exists at all, and `keymap.c` fails with
   `ZMK_LAYER_CHILD_LEN_PLUS_ONE undeclared`). ZMK provides a separate
   pass-through variable, **`ZMK_EXTRA_MODULES`**, specifically so a
   downstream module doesn't clobber its own — use that instead, never
   `ZEPHYR_EXTRA_MODULES` directly, when building a ZMK app.
9. `zzeneg/zmk-raw-hid`'s BLE path (`src/hog.c`, compiled whenever
   `CONFIG_ZMK_BLE=y`) calls `zmk_ble_active_profile_conn()`, which
   this fork's `zmk/ble.h` doesn't provide (it only exposes
   `zmk_ble_active_profile_index()`/`_btid()`, not a direct connection
   accessor) — a real core-API difference between the community module
   and this particular ZMK fork, not something `zzeneg/zmk-raw-hid` was
   ever tested against. `zmk-module/src/raw_hid_ble_compat.c` stubs it
   to always return `NULL` (hog.c already handles that as "not
   connected" and no-ops) — fine for Phase 1 (USB only; Bluetooth is
   Phase 3), not a real implementation. Replace with a real lookup
   (e.g. `bt_conn_lookup_addr_le()` against the active profile's
   bonded address) when Phase 3 needs BLE raw-HID to actually work.
10. Q3 Ultra 8K's USB VID/PID (`0x3434`/`0x1230`) is confirmed from its
    own published `.conf` — see `hid_protocol.KEYCHRON_Q3_ULTRA_8K`.
    K3 Ultra 8K's own VID/PID (`0x3434`/`0x1630`) is now confirmed too,
    via `hid.enumerate()` against real hardware — see
    `hid_protocol.KEYCHRON_K3_ULTRA_8K`. Its **stock** firmware already
    exposes a raw-HID interface at usage page `0xFF60` / usage `0x61`
    (Keychron's own proprietary Launcher channel — pinging it with
    `hid_protocol.pack_ping()` gets a real reply, just not our
    `MSG_HELLO`), so opening/reading/writing that interface is confirmed
    safe well before any custom firmware exists.

## Building

Requires Homebrew packages `cmake ninja dtc ccache wget` and a Zephyr
SDK **0.16.x** (not the current `1.0.x` release line — see point 3
above). Get the SDK's `arm-zephyr-eabi`-only installer from
[sdk-ng releases](https://github.com/zephyrproject-rtos/sdk-ng/releases)
(`zephyr-sdk-0.16.9_<platform>_minimal.tar.xz`, then
`./setup.sh -t arm-zephyr-eabi -h -c`).

```sh
python3 -m venv zmk-config/.venv && source zmk-config/.venv/bin/activate
pip install west
cd zmk-config
west init -l config
west update
git -C zmk apply ../keychron-zmk-fork.patch   # point 6 + 7 above
pip install -r zephyr/scripts/requirements.txt

export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR=/path/to/zephyr-sdk-0.16.9
REPO_ROOT=/path/to/ai-agent-macropad   # this repo's own checkout root

west build -p -d build/q3_ultra -b keychron -s zmk/app -- \
  -DSHIELD="keychron_q3_ultra_ansi raw_hid_adapter" \
  -DEXTRA_CONF_FILE="$(pwd)/config/ai_agent_macropad.conf" \
  -DZMK_EXTRA_MODULES="$REPO_ROOT" \
  -DZephyr_DIR="$(pwd)/zephyr/share/zephyr-package/cmake" \
  -DBOARD_ROOT="$(pwd)/zmk/app"
```

Note `ZMK_EXTRA_MODULES`, not `ZEPHYR_EXTRA_MODULES` (point 8), and the
explicit `-DZephyr_DIR=`/`-DBOARD_ROOT=` — `west build`'s own
auto-detection of both didn't work in this environment; passing them
explicitly did. This gets you to a linked `build/q3_ultra/zephyr/
zmk.elf`; getting the rest of the way to a flashable `.bin` needs the
Linux-only `prepend_header` step (see Status above) run under Linux.

`build.yaml` encodes the same `board`/`SHIELD`/`EXTRA_CONF_FILE` combo
for a GitHub Actions-style west build action (using `${ZMK_CONFIG}`
instead of a hardcoded path) — the `ZMK_EXTRA_MODULES`/`Zephyr_DIR`/
`BOARD_ROOT` overrides above were only needed for this local,
one-off-checkout build and aren't reflected there.

Once flashed, plug the board in wired and run `../hid_bringup_test.py`
from the repo root — it already knows about
`hid_protocol.KEYCHRON_Q3_ULTRA_8K` and just needs a `MSG_HELLO` reply
to pass its handshake check. RGB cycling and key-press listening in
that script are Phase 2 (`MSG_SLOT`/`MSG_KEY` aren't implemented in
`zmk-module/` yet — only the `MSG_PING` → `MSG_HELLO` handshake is).

## Layout

| Path | Purpose |
| --- | --- |
| `config/west.yml` | West manifest: Keychron's ZMK fork (`rtl8762g`) as the base, plus `zzeneg/zmk-raw-hid` and this repo (for `zmk-module/`) as extra projects |
| `config/ai_agent_macropad.conf` | Extra Kconfig layered on top of Keychron's shield defaults via `EXTRA_CONF_FILE` — enables `CONFIG_RAW_HID`, bumps `CONFIG_USB_HID_DEVICE_COUNT`, disables the unneeded SPI DMA path, enables our listener |
| `build.yaml` | The board+shield combo this bring-up targets |
| `keychron-zmk-fork.patch` | Two small fixes to the *cloned* `zmk/` checkout (not tracked by west) — an upstream ZMK Kconfig-type bug and a genuine Keychron `behavior_keychron.c` bug. Apply with `git -C zmk apply ../keychron-zmk-fork.patch` after `west update` |
| `../zephyr/module.yml` | Registers this whole repo as a Zephyr module rooted at `zmk-module/` |
| `../zmk-module/` | The actual listener: `Kconfig` (incl. the CKLED2001 stub), `CMakeLists.txt`, `src/ai_agent_macropad_hid.c` (ping/hello), `src/raw_hid_ble_compat.c` (BLE stub) |

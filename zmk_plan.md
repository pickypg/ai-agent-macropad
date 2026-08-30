# Plan: Adapt ai-agent-macropad for Keychron K3 Ultra 8K (ZMK)

**Goal**
Make the [ai-agent-macropad](https://github.com/pickypg/ai-agent-macropad) daemon work with a Keychron K3 Ultra 8K keyboard running ZMK, supporting both **wired (USB)** and **wireless (Bluetooth)** modes. The keyboard should display AI agent session states via per-key RGB and send key-press events back to the host so sessions can be focused.

**Date**
2026-08-25

---

## 1. Current State of the Project

### Host-side (already exists)

- Repo: https://github.com/pickypg/ai-agent-macropad
- Core files:
  - `daemon.py` — Unix socket server + agent state → pad state mapping
  - `pad_link.py` — HID discovery, open/close, read/write, reconnection
  - `hid_protocol.py` — binary wire protocol (32-byte reports)
- Protocol (Raw HID, usage page `0xFF60`, usage `0x61`):
  - `MSG_PING` (0x21) → host requests hello
  - `MSG_HELLO` (0xA1) → device replies with device ID, slot count, protocol version
  - `MSG_SLOT` (0x20) → set slot index + state (idle/working/question/done/error/etc.)
  - `MSG_KEY` / `MSG_KEY_HELD` → device → host key events
- Currently proven on **QMK** boards (NuPhy Air75 V2, Keychron K1 Pro) over USB.
- Uses Python `hid` (hidapi). Discovery filters by VID/PID + usage page/usage.
- Reconnection logic already exists (important for wireless).

### Target Hardware

- **Keychron K3 Ultra 8K**
  - Low-profile 75% layout
  - **ZMK firmware** (not QMK)
  - Per-key RGB (north-facing)
  - Connectivity: USB-C + Bluetooth + 2.4 GHz
  - Claimed ~550 h battery life (backlight off)
  - Customization today is primarily via Keychron Launcher (web app)

### Key Constraint

Keychron has **not** published complete, ready-to-build ZMK board/shield definitions for the K3 Ultra (or most Ultra series boards).
They publish some hardware design files (CAD/PCB) and maintain a ZMK fork, but the practical board support needed to add external modules (matrix, RGB driver, power management, etc.) is incomplete or not public. Official updates and remapping go through their proprietary Launcher.

---

## 2. High-Level Approach

1. **Firmware side (ZMK)**
   - Obtain or reverse-engineer enough board support for the K3 Ultra to build custom firmware.
   - Add the community module [zzeneg/zmk-raw-hid](https://github.com/zzeneg/zmk-raw-hid).
   - Implement the same binary protocol used by the host (ping/hello/slot/key).
   - Map slot states → per-key RGB colors.
   - Send key events back over Raw HID.

2. **Host side**
   - Extend discovery to recognize the K3 Ultra’s USB VID/PID (and later BLE identity).
   - Keep the existing 32-byte protocol (minimal changes).
   - Harden reconnection for BLE sleep/disconnect cycles.
   - Prefer USB for initial bring-up; add BLE once USB works.

3. **Phased delivery**
   - Phase 0: Research / board support status
   - Phase 1: USB Raw HID + basic LED control
   - Phase 2: Full protocol + key events
   - Phase 3: Bluetooth support + reconnection
   - Phase 4: Polish (power, multiple slots, VIA/Launcher coexistence if possible)

---

## 3. Detailed Implementation Plan

### Phase 0 – Research & Feasibility (1–3 days)

- [x] Confirm current public status of Keychron ZMK sources:
  - https://github.com/Keychron/zmk
  - https://github.com/Keychron/Keychron-Keyboards-Hardware-Design (look for K3 Ultra 8K)
- [x] Search for any community zmk-config or board definitions for K3 Ultra / other Ultra boards.
- [x] Identify the MCU (likely nRF52840 or similar Nordic part used in Ultra series).
- [x] Determine the exact USB VID/PID of a stock K3 Ultra (probe with `hid.enumerate()` or system tools). — confirmed 2026-08-26 via `hid.enumerate()` against real hardware: VID `0x3434`, PID `0x1630`. Stock firmware already exposes a raw-HID interface at usage page `0xFF60` / usage `0x61` (Keychron's own proprietary Launcher channel) — pinging it with `hid_protocol.pack_ping()` gets a real reply (`ff 01 00...`, not our `MSG_HELLO`), confirming the interface is live and safe to open/write/read without any adverse effect, well before any custom firmware exists.
- [x] Decide whether to:
  - A) Wait for / contribute board support, or
  - B) Reverse-engineer from hardware files + running firmware, or
  - C) Pivot to a different ZMK board that already has solid public support + per-key RGB.

**Exit criteria**: Clear decision on whether the K3 Ultra is viable in the short term, or a recommended alternative board.

**Findings (2026-08-26):**

- The MCU guess was wrong: Keychron's Ultra series (and K3 Ultra
  presumably) runs **Realtek RTL8762G**, not Nordic nRF52. Confirmed
  via `CONFIG_BT_RTL87X2G=y` in Keychron's own published shield
  configs. Mainline Zephyr has no RTL8762G support; Keychron ships
  their own Zephyr fork (`Keychron/zephyr`, branch `rtl87x2g`) with the
  Realtek HAL, imported by their ZMK fork's `app/west.yml`
  (`Keychron/zmk`, branch `rtl8762g` — **not** `main`, which is a plain
  upstream mirror with none of Keychron's own board work on it).
- That `rtl8762g` branch has real, seemingly-complete public shields
  for Q1/Q3/Q6 Ultra, V0–V10 Ultra, and Z270 Ultra — full USB/BLE
  config, per-key RGB via a `CKLED2001` driver, the works. **No K3
  Ultra 8K shield exists anywhere** — Keychron's own fork included.
- But every one of those existing Ultra shields' `Kconfig.shield`
  `select`s several Kconfig symbols (`SHIELD_KEYCHRON_V1MAX`,
  `KEYCHRON_RGB_ENABLE`, `SNAP_CLICK_ENABLE`, `RETAIL_DEMO_ENABLE`,
  `ADAPATIVE_NKRO`, `MAC_VIA_FUNC`, `ADD_PPT_REPORT_RATE`,
  `ENABLE_GPIO_LED`) that are **never defined** in either
  `Keychron/zmk` or `Keychron/zephyr` — the public fork is missing at
  least one more piece even for the boards it does publish shields
  for. This is Phase 0's predicted "incomplete... board support" risk,
  confirmed concretely rather than just suspected. Not yet known
  whether this breaks a `west build` outright or just silently drops
  some features — no Zephyr/west toolchain was available to test.
- **Decision: (B/C hybrid)**. Phase 1 targets **Q3 Ultra 8K** (closest
  available board with a real public shield, same chip family) as a
  stand-in to prove the raw-HID protocol pipeline, while K3 Ultra
  8K itself waits on either Keychron publishing its shield or someone
  reverse-engineering the physical board. See `zmk-config/README.md`
  for the full writeup and exact repos/branches/symbols involved.

### Phase 1 – Minimal USB Raw HID Bring-up

**Firmware**

- [x] Create a zmk-config (or module) that can build for the K3 Ultra (or closest available board). — targets Q3 Ultra 8K; see `zmk-config/`. **Verified**: `west build` runs to a fully linked `zmk.elf` (36.6% flash, 80.2% RAM) on real macOS/arm64 hardware, after 6 real bugs/gaps found and fixed (documented in `zmk-config/README.md`'s Status section: 2 upstream Kconfig gaps, 1 unrelated pre-existing ZMK bug, 1 genuine Keychron source bug, 1 build-invocation footgun, 1 zmk-raw-hid/fork API mismatch). Only the final proprietary image-signing step (`prepend_header`, Linux x86_64-only binary) is blocked, purely by host OS — not by code.
- [x] Add `zzeneg/zmk-raw-hid` as a module (`west.yml`). — `zmk-config/config/west.yml`; confirmed it actually links against this fork (after stubbing one missing BLE API function — see README).
- [x] Enable `CONFIG_RAW_HID=y` and confirm the interface appears with usage page `0xFF60` / usage `0x61`. — enabled in `zmk-config/config/ai_agent_macropad.conf`; builds cleanly. Runtime confirmation (device actually enumerating the interface) still needs a flashed board, blocked on the Linux-only signing step above.
- [x] Implement a simple listener that answers `MSG_PING` with a `MSG_HELLO` (hard-code device ID + slot count for now). — `zmk-module/src/ai_agent_macropad_hid.c`; compiles and links cleanly into the firmware image.
- [ ] Confirm the host can discover and open the device with the existing `pad_link.py` discovery logic (or a small test script). — blocked on getting an actual flashable `.bin` (needs the Linux-only signing step, then a real flash + USB test).

**Host**

- [x] Add a `KnownPad` entry for the K3 Ultra in `hid_protocol.py` (VID/PID + device_id). — added both `KEYCHRON_K3_ULTRA_8K` (real, confirmed VID `0x3434`/PID `0x1630`) and `KEYCHRON_Q3_ULTRA_8K` (build stand-in).
- [ ] Run `hid_bringup_test.py` (or equivalent) and verify ping → hello round-trip over USB. — needs the flashable image (see above) and real Q3 Ultra 8K hardware to flash it onto (not the same unit as the K3 Ultra 8K — different physical board, don't flash Q3 firmware onto K3 hardware).

**Exit criteria**: Host can open the keyboard over USB Raw HID and receive a valid hello. — the firmware itself is now proven to build and link; what's left is entirely infrastructure (a Linux host/container for the signing step) and hardware (a Q3 Ultra 8K unit to flash and test on, since K3 Ultra 8K still has no board files of its own).

### Phase 2 – Full Protocol + RGB + Key Events

**Firmware**

- [ ] Implement full message handling:
  - `MSG_SLOT` → set color of the corresponding key/LED according to state codes already defined in `hid_protocol.py`.
  - `MSG_KEY` / `MSG_KEY_HELD` on physical key press/hold of the designated slot keys.
- [ ] Map a reasonable set of keys (e.g. 4–8 keys in a convenient location) to slots.
- [ ] Use ZMK’s RGB / underglow / per-key LED APIs (or whatever the board exposes) to drive colors.
      States from the host:
  - idle, working, tool_running, tool_stalled, question, waiting, done, error, off
- [ ] Keep report size at 32 bytes to match the host.

**Host**

- [ ] No major protocol changes needed if firmware mirrors the existing messages.
- [ ] Verify end-to-end: agent hook → daemon → slot color change, and key press → window focus.

**Exit criteria**: Full bidirectional protocol works over USB; agent states appear as colored keys; pressing a key focuses the corresponding session.

### Phase 3 – Bluetooth / Wireless Support

- [ ] Confirm `zmk-raw-hid` correctly exposes the same Raw HID interface over BLE (module claims both USB and BT are supported).
- [ ] Update host discovery to also find the device when connected via Bluetooth (hidapi supports BLE HID on macOS/Linux/Windows, but paths and bus types differ).
- [ ] Test and harden the existing reconnection logic in `pad_link.py` for BLE sleep, disconnect, and re-advertise behavior.
- [ ] Decide how to handle the 2.4 GHz proprietary mode (most likely out of scope; stick to USB + BLE).

**Exit criteria**: Same functionality works when the keyboard is connected only via Bluetooth.

### Phase 4 – Polish & Production Readiness

- [ ] Power considerations: avoid keeping many LEDs at full brightness; consider dimming or timeout.
- [ ] Multiple profiles / multi-host behavior if desired.
- [ ] Coexistence with Keychron Launcher (may require releasing the Raw HID interface when idle, similar to how the daemon already releases for VIA).
- [ ] Documentation: how to build, flash, and pair the custom firmware.
- [ ] Optional: contribute any board definition improvements back upstream or to a community repo.

---

## 4. Key Technical References

| Item                               | Link / Notes                                                   |
| ---------------------------------- | -------------------------------------------------------------- |
| Host repo                          | https://github.com/pickypg/ai-agent-macropad                   |
| Protocol definition                | `hid_protocol.py` (MSG*\*, STATE*\*, REPORT_SIZE = 32)         |
| ZMK Raw HID module                 | https://github.com/zzeneg/zmk-raw-hid                          |
| ZMK docs – Bluetooth / HID         | https://zmk.dev/docs/features/bluetooth                        |
| Keychron ZMK fork                  | https://github.com/Keychron/zmk                                |
| Keychron hardware files            | https://github.com/Keychron/Keychron-Keyboards-Hardware-Design |
| Example of Raw HID over BLE on ZMK | Community discussions + zmk-raw-hid README                     |

---

## 5. Risks & Mitigations

| Risk                                         | Mitigation                                                                              |
| -------------------------------------------- | --------------------------------------------------------------------------------------- |
| Incomplete public board support for K3 Ultra | Phase 0 research; be ready to pivot to another ZMK board with good RGB + public shields |
| RGB API differences vs QMK                   | Expect to write board-specific LED mapping code                                         |
| BLE HID discovery quirks on macOS            | Test early; hidapi generally works but needs path/bus filtering                         |
| 2.4 GHz mode                                 | Explicitly out of scope; document that only USB + BLE are supported                     |
| Battery impact of per-key RGB                | Make brightness configurable; default to lower brightness or auto-off                   |

---

## 6. Suggested First Actions for Grok Build

1. Probe or look up the exact USB VID/PID of a Keychron K3 Ultra 8K.
2. Check the current contents of Keychron’s ZMK fork and hardware design repo for any K3 Ultra board files.
3. Search for community zmk-config repositories that already support any Keychron Ultra board.
4. If board support is missing, outline the minimal set of files needed (board.dts, shield, RGB node, etc.) based on similar Nordic-based ZMK keyboards.
5. Produce a skeleton `west.yml` + basic raw-hid listener that can be expanded once board support exists.

---

## 7. Success Criteria

- [ ] Daemon discovers and talks to the K3 Ultra over USB Raw HID using the existing protocol.
- [ ] Agent session states appear as distinct colors on designated keys.
- [ ] Pressing a slot key focuses the corresponding agent window (macOS AppleScript path already exists).
- [ ] Same functionality works over Bluetooth with reasonable reconnection behavior.
- [ ] Firmware is buildable from a public or documented zmk-config (even if it requires a few private board files initially).

---

_This plan is intended to be self-contained. Hand it to Grok Build as the starting context for implementing ZMK + Raw HID support for the Keychron K3 Ultra 8K with the ai-agent-macropad project._

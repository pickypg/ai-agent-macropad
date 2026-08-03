# QMK Generic Macropad Support — NuPhy Air75 V2 Implementation Plan

Builds on [`pickypg/claude-macropad`](https://github.com/pickypg/claude-macropad), extending the
existing serial-based `daemon.py` / `rp2040/code.py` pair with a second, HID-based transport so the
same daemon can drive a QMK-based keyboard — specifically a NuPhy Air75 V2 — with no OLED, status
communicated purely via per-key RGB, and the daemon never assuming a fixed slot count.

---

## Phase 0 — Verify the target before writing any code — ✅ Verified 2026-08-03

Don't start `keymap.c` until these are confirmed against NuPhy's actual QMK fork for Air75 V2:

- [x] `RAW_ENABLE = yes` builds cleanly (raw HID support present, not stripped in NuPhy's fork).
- [x] `RGB_MATRIX_ENABLE` / `rgb_matrix_set_color()` works per-key (not just global effects).
- [x] The board's `g_led_config` (LED index table) is present and matches the physical layout, so
  `slot_to_led[]` can be built from real data rather than guessed.
- [x] `qmk compile` succeeds for this board+keymap combo end to end — ideally with a trivial test
  keymap first, before any custom logic is added.

This is the step most likely to surface a dead end (NuPhy's fork being incomplete or unmerged
upstream) — worth confirming before investing in the daemon-side work. **No dead end found —
cleared to proceed.**

### Findings

- **Fork used**: [`nuphy-src/qmk_firmware`](https://github.com/nuphy-src/qmk_firmware), branch
  `nuphy-keyboards` — this is NuPhy's own official repo (not a third-party community fork).
  Cloned locally to `/Users/pickypg/dev/pickypg/nuphy-qmk-firmware` (sibling to this repo, not
  nested inside it — it's QMK's own large tree with its own submodules, unrelated git history to
  `claude-qmk`). Phase 4's `keymap.c` work should happen in that checkout.
- **Real keyboard path is `keyboards/nuphy/air75_v2/ansi/`**, not `keyboards/nuphy/air75_v2/`
  directly — there's an `ansi/` variant subdirectory (Phase 4's target path below is corrected to
  match).
- **RAW_ENABLE**: not set by default, but nothing in the fork strips or blocks it. NuPhy's own
  `keymaps/via/rules.mk` sets `VIA_ENABLE = yes`, which auto-sets `RAW_ENABLE := yes`
  (`builddefs/common_features.mk`). Confirmed both that keymap *and* a from-scratch keymap with
  `RAW_ENABLE = yes` set directly compile and link cleanly. Note: `quantum.h` does **not**
  auto-include `raw_hid.h` — keymap.c needs `#include "raw_hid.h"` explicitly to call
  `raw_hid_send()`.
- **Per-key RGB**: NuPhy's own `ansi.c` already calls `rgb_matrix_set_color(single_index, ...)`
  and forwards to a keymap-level `rgb_matrix_indicators_user()` hook via
  `rgb_matrix_indicators_kb()`. Confirmed a custom `rgb_matrix_indicators_user()` compiles and
  links.
- **`g_led_config`**: not hand-written in the board's `.c`/`.h` files — it's generated at build
  time from `keyboard.json`'s `rgb_matrix.layout` array, which maps every real matrix position
  (Esc, F-row, QWERTY block, arrows, etc.) to real x/y coordinates. Real data, not guessed; no
  manual table needed for Phase 4's `slot_to_led[]` derivation, just read it out of
  `keyboard.json`.
- **Board facts relevant to later phases**: USB VID/PID `0x19F5`/`0x3246` (needed for Phase 2's
  `HidPadLink` discovery). MCU is STM32F072 running ChibiOS (not RP2040/AVR) — ARM toolchain,
  not AVR, though the AVR toolchain was also installed since the generic `qmk` Homebrew formula
  depends on both. Physical layout is `LAYOUT_ansi_84` (84 keys) — relevant input for Phase 4's
  key-count decision.
- **Toolchain**: installed via `brew install qmk/qmk/qmk`, which required trusting three taps
  (`qmk/qmk`, `osx-cross/arm`, `osx-cross/avr` — all standard, long-established sources, not
  random). Two gotchas hit during setup, in case they recur:
  - The `qmk` bottle's Python venv (`pyvenv.cfg`) pinned to `python@3.13`, which isn't installed
    by default alongside `python@3.14` — fixed with `brew install python@3.13`.
  - `arm-none-eabi-gcc@8` / `avr-gcc@8` are keg-only formulas, not linked onto `PATH` by default
    — now exported in `~/.zshrc`.
  - After `git clone --recurse-submodules`, some submodules (`lib/lufa`, `lib/lvgl`, `lib/vusb`,
    `lib/printf`) ended up with **staged deletions** in their working trees (empty checkouts
    despite `git submodule status` showing them initialized) — fixed with
    `git submodule foreach 'git reset --hard HEAD'`. Worth trying first if a fresh clone fails to
    compile with "No such file" errors inside `lib/`.
- **Proof artifact**: a trivial spike keymap at
  `keyboards/nuphy/air75_v2/ansi/keymaps/claude_test/` (in the local checkout, not this repo)
  exercises `RAW_ENABLE`, `raw_hid_receive()`/`raw_hid_send()`, and
  `rgb_matrix_indicators_user()` end to end and compiles clean (61,046 bytes). Safe to delete once
  Phase 4's real `claude_macropad` keymap replaces it, or reuse as its starting point.

---

## Phase 1 — Define the wire-level `hello` and `slot` messages for HID — ✅ Done 2026-08-03

Reuse the existing serial protocol's shape, but as fixed-size binary reports instead of JSON lines,
extending `hello` to carry capability the way `rp2040/code.py` already does:

```c
// Device -> host, on connect or on request
// report[0] = 0x01 (MSG_HELLO), report[1] = device id, report[2] = num_slots
```

```c
// Host -> device
// report[0] = 0x02 (MSG_SLOT), report[1] = slot index, report[2] = state enum
```

State enum values should mirror `STATE_COLORS`'s keys in `rp2040/code.py` (`idle` / `working` /
`waiting` / `done` / `error` / `question`) 1:1, numbered, so both transports' `hook_to_state`
output maps identically regardless of which pad is attached.

### Implementation

Landed as `hid_protocol.py` (+ `tests/test_hid_protocol.py`, + README's new "Pad messages (HID
reports)" subsection under Protocol) — Python-side only for now, since there's no `keymap.c` for
the C side to live in yet (that's Phase 4; it should mirror these numbers with a comment pointing
back at this file, the same loose-sync convention already used between `rp2040/code.py` and
`daemon.py`'s `STATE_COLORS`/`hook_to_state`).

- `REPORT_SIZE = 32`, matching `RAW_EPSIZE`'s default for ChibiOS boards (confirmed in
  `tmk_core/protocol/usb_descriptor.h` in the Phase 0 checkout — NuPhy's fork doesn't override
  it).
- `MSG_HELLO = 0x01`, `MSG_SLOT = 0x02` as sketched above, plus `MSG_PING = 0x03` (host → device,
  requests a `MSG_HELLO` reply) — needed because raw HID is call-and-response, unlike serial where
  the device can push `hello` unprompted the instant `ping` arrives on a channel it's already
  streaming on. Phase 4's keymap needs a `raw_hid_receive()` that replies to this.
- State byte values: `idle=0, working=1, waiting=2, done=3, error=4, question=5` — matches
  `STATE_COLORS`'s iteration order in `rp2040/code.py` (`off` excluded; it's device-local only,
  never sent by the host).
- No separate "clear" report type: the serial protocol's `{"t": "clear", "i": i}` has no HID
  equivalent because there's no label to clear on an RGB-only pad — Phase 2's `HidPadLink` should
  translate a `write_json({"t": "clear", ...})` call into `MSG_SLOT` with `state="idle"`.
- `parse_report()` returns `None` for anything malformed or unrecognized rather than raising,
  matching `read_json_lines()`'s "drop bad input" posture in `rp2040/code.py`.

---

## Phase 2 — Introduce a `PadLink` abstraction in `daemon.py` — ✅ Done 2026-08-03

`PadLink` is currently serial-specific (`pyserial`, `discover_port()` scanning for Adafruit's
vendor ID). Refactor to:

- A small `PadTransport` interface: `open()`, `close()`, `write_json(obj)`, plus a callback for
  inbound device events — close to `PadLink`'s existing shape, so this is extraction more than
  redesign.
- `SerialPadLink` — today's implementation, renamed, behavior unchanged.
- `HidPadLink` — new, using `hidapi`: discovers by NuPhy's VID/PID (`0x19F5`/`0x3246` for Air75
  V2 ANSI, confirmed in Phase 0's `keyboard.json`) and the raw-HID usage page, to avoid grabbing
  the keyboard's normal HID interface, translates `write_json`'s dict into the
  Phase 1 binary `MSG_SLOT` report, and parses inbound reports back into the same
  `{"t": "hello", "slots": N}` / `{"t": "key", "i": N}` shapes the rest of the daemon already
  expects.
- Everything downstream — `SlotManager`, `hook_to_state`, `Daemon.handle_hook_event`, the stall
  watcher — needs **zero changes**, since they only ever interact with `self.pad.write_json(dict)`
  and `on_device_event(dict)`.
- Transport selection: env var (`MACROPAD_TRANSPORT=serial|hid`, default auto-try-both) alongside
  the existing `MACROPAD_SERIAL_PORT` override.

### Implementation

Landed directly in `daemon.py` (no new module — `PadTransport`/`SerialPadLink`/`HidPadLink`/
`AutoPadLink` are all daemon-internal, unlike Phase 1's standalone `hid_protocol.py`), plus
`tests/test_hid_pad_link.py` (19 new tests) and README updates (Setup's transport section,
Testing's fakes bullet, Repo layout). 83/83 tests passing.

- `PadTransport` is a plain base class (`open()`/`close()`/`write_json()` raise
  `NotImplementedError`, `__init__` stores `on_device_event`) — not an `abc.ABC`, matching the
  rest of the codebase's style of not reaching for that machinery.
- `PadLink` → `SerialPadLink`, behavior byte-for-byte unchanged, plus one new `self.attached`
  bool (set once `serial.Serial()` succeeds) that `AutoPadLink` needs to decide fallback order —
  the only new surface on it.
- `HidPadLink` discovers via a new `discover_hid_device(vid, pid, handshake_timeout)`, structured
  as the HID counterpart to `discover_port()`: filter `hid.enumerate(vid, pid)` down to
  interfaces matching QMK's raw-HID `usage_page`/`usage` (`0xFF60`/`0x61` —
  `RAW_USAGE_PAGE`/`RAW_USAGE_ID` in `tmk_core/protocol/usb_descriptor_common.h`, confirmed in
  Phase 0's checkout), then MSG_PING each candidate and accept the first that answers MSG_HELLO
  within the timeout. Unlike `discover_port()`, no device-id string check on the reply — Air75
  V2's VID/PID already disambiguates it, unlike Adafruit's VID being shared across every
  CircuitPython board (see Phase 0's note on `DEVICE_ID_AIR75_V2`'s reduced role for HID).
- Only `"slot"` and `"clear"` ever reach `write_json()` in the current codebase (grepped to
  confirm) — `HidPadLink` only encodes those two (`_pack_for_hid()`), matching Phase 1's "clear ==
  MSG_SLOT state=idle" design note. No `"ping"` case needed there since nothing calls
  `write_json({"t": "ping"})`; discovery sends `MSG_PING` directly via `hid_protocol.pack_ping()`.
- **Open question flagged for Phase 6, not resolved here**: `HidPadLink` prepends a `0x00`
  report-ID byte on every `hid.Device.write()` call (hidapi convention for HID interfaces with
  implicit report ID 0) and does not strip anything on `read()`. This is the common convention
  but unverified against real hardware — Phase 6's minimal round-trip test script is exactly
  where to confirm or fix it.
- `hid` (the pip package wrapping hidapi) is imported lazily (`try/except ImportError`) and is
  **not** a hard dependency — commented out in `requirements.txt` — so `daemon.py` still runs
  fine for RP2040-only users with neither the package nor the native hidapi library installed;
  `HidPadLink`/`discover_hid_device` just log a warning and stay headless.
- `AutoPadLink` (the `MACROPAD_TRANSPORT`-unset default) tries `SerialPadLink` discovery first,
  then `HidPadLink`, then runs headless — a discovery-order wrapper, not a fan-out, since only
  one physical pad is ever attached at a time.
- **Phase 5 overlap**: Phase 5 originally scoped "a fake `hidapi` device in `tests/`... so
  `HidPadLink` gets the same coverage `SerialPadLink` has" — that landed here instead
  (`tests/test_hid_pad_link.py`'s `FakeHidDevice`/`make_fake_hid()`, mirroring
  `test_discover_port.py`'s `FakeSerialPort` pattern), rather than waiting for a separate phase.
  Phase 5 is now just `fake_hooks.py` end-to-end coverage plus anything this pass didn't already
  cover — see its updated scope below. Note `HidPadLink._read_loop`'s background-thread dispatch
  itself is *not* directly tested, same as `SerialPadLink._read_loop` never has been — both are
  thin glue over already-tested decode logic (`hid_protocol.parse_report` / `json.loads`), and
  the existing codebase never tested the serial version's thread either.

---

## Phase 3 — Wire `NUM_SLOTS` from the device's own `hello`, not a constant — ✅ Done 2026-08-03

`discover_port()` already treats `hello`'s `slots` field as authoritative for the RP2040. Extend
`SlotManager` to be instantiated *after* the handshake, sized from whatever `hello` reports, for
either transport:

```python
handshake = self.pad.handshake()  # blocks briefly at startup, both transports implement it
self.slots = SlotManager(handshake["slots"])
```

This directly satisfies "if it supports 1, fine; if it supports 100, great" — the daemon never
hardcodes a number, it asks the hardware. Keep a guard so `pending_calls` doesn't record a `None`
slot for sessions that land past capacity.

### Implementation

`handshake()` landed as one shared method on `PadTransport` itself (not duplicated per
transport) — `tests/test_pad_handshake.py` (12 new tests), README's Setup/Protocol/Testing
sections updated. 95/95 tests passing.

- **Race avoided, not worked around**: `handshake()` can't just do a second blocking read on
  `self._ser`/`self._dev` after `open()` — the background reader thread from Phase 2 is already
  consuming that same stream. Instead, every subclass's `_read_loop` now calls a new
  `PadTransport._dispatch(msg)` instead of `on_device_event(msg)` directly; `_dispatch()` forwards
  to `on_device_event` as before *and* short-circuits `"hello"` replies to
  `threading.Event`/`self._last_hello`. `handshake(timeout=1.5)` just pings
  (`_send_ping()`, one small method per subclass) and waits on that event — same thread does both
  jobs, so there's nothing to race. `AutoPadLink.handshake()` is the one override, delegating to
  whichever transport `open()` picked.
- `handshake()` short-circuits to `None` immediately (no waiting) when `self.attached` is False
  — i.e. `open()` already established there's no pad, so there's nothing to ping. Verified this
  returns well under the timeout, not just eventually.
- **`Daemon.apply_handshake(handshake)`**: the actual "what to do with a handshake result" logic
  is a small, plain (non-async) method, called from `serve()` right after `open()`/`handshake()`
  and before `start_unix_server` (so no hook event can land under stale sizing). Deliberately
  extracted from `serve()` itself — `serve()` is an `asyncio` `serve_forever()` loop with no
  existing tests (confirmed nothing in `tests/` calls it), so keeping the actual decision in a
  separate sync method is what makes it unit-testable at all.
- Resizing replaces `self.slots` with a **fresh** `SlotManager`, not an in-place resize — fine
  because this only ever runs once at startup before the socket server accepts anything, so there
  are no pre-existing allocations to preserve or lose. Locked in with a test that seeds a stale
  allocation first and confirms it's gone after `apply_handshake()`.
- Falls back to the `NUM_SLOTS` (12) default already set in `__init__` when `handshake` is
  `None`/missing `"slots"` — headless runs and tests that construct `Daemon()` directly (every
  existing test does, per `recording_daemon` in `conftest.py`) are completely unaffected, since
  `apply_handshake()` is only ever called from `serve()`, which they never invoke.
- The `pending_calls`-guard the plan calls out was, on inspection, **already correct** —
  `handle_hook_event`'s lazy-allocate branch already returns early on `SlotManager.allocate()`
  returning `None` before any `pending_calls[session_id] = ...` write is reachable. No behavior
  change needed there, just an updated comment (was hardcoded to reference the `NUM_SLOTS`
  constant; now points at `self.slots.num_slots`, the value that's actually authoritative once a
  pad's handshake has resized it). This guard matters far more now than when it was first
  written, though — a small dedicated-key-cluster board (Phase 4) could realistically report a
  single-digit slot count, making "past capacity" routine rather than a rare 13th-session edge
  case.
- `on_device_event`'s key-press handler also switched its bounds check from the `NUM_SLOTS`
  constant to `self.slots.num_slots`, for the same reason — a key index from a small-N board must
  be validated against the size the daemon is actually using, not the fallback default.

---

## Phase 4 — NuPhy Air75 V2 keymap — in progress (userspace scaffold done)

- ~~Create `keyboards/nuphy/air75_v2/ansi/keymaps/claude_macropad/keymap.c` in NuPhy's fork~~.
  **Superseded**: rather than forking `nuphy-src/qmk_firmware` (285MB+, several hundred MB more in
  its own submodules) just to version-control ~150 lines of custom keymap, the keymap lives in a
  **QMK userspace overlay** at `qmk-userspace/` in *this* repo —
  `qmk-userspace/keyboards/nuphy/air75_v2/ansi/keymaps/claude_macropad/keymap.c`. QMK's own
  userspace mechanism (confirmed supported by NuPhy's fork — `QMK_USERSPACE` is referenced in its
  `builddefs/build_json.mk`) merges this directory with a `keyboards/<board>/keymaps/<name>/`
  overlay at build time; `qmk_home` stays pointed at the disposable, re-cloneable
  `/Users/pickypg/dev/pickypg/nuphy-qmk-firmware` checkout, only the keymap itself is owned/
  version-controlled here. Compile with:
  ```
  cd qmk-userspace
  QMK_USERSPACE="$(pwd)" qmk compile -kb nuphy/air75_v2/ansi -km claude_macropad
  ```
  (or `qmk config user.overlay_dir=<path>` to avoid passing `QMK_USERSPACE` every time). Verified
  end to end 2026-08-03 with a placeholder keymap (Phase 0's spike keymap, ported over — proves
  `RAW_ENABLE`/`raw_hid_receive`/`rgb_matrix_indicators_user` still build/link correctly through
  the overlay, byte-identical output size to the direct-in-tree Phase 0 build: 61,046 bytes) —
  the *real* slot logic below is still pending. `qmk-userspace/qmk.json` tracks the registered
  build target (`qmk userspace-add`/`-remove` manage it); the compiled `.bin`/`.hex` QMK copies
  into `qmk-userspace/` itself is gitignored, only source is tracked. `claude-qmk` itself was
  `git init`'d as part of this (previously had no git history at all).
- Remember `#include "raw_hid.h"` explicitly — `quantum.h` doesn't pull it in automatically
  (confirmed in Phase 0).
- Decide how many physical keys to dedicate — worth deciding explicitly rather than defaulting to
  "however many fit." A 75% board has real estate; a natural choice is a function-layer row
  (e.g. `Fn` + number row, or the arrow-key cluster) so the feature doesn't consume primary typing
  keys. This decision directly sets `NUM_MACROPAD_SLOTS`, and thus what `hello` reports.
- Implement `SLOT_KEY_0..N-1`, `process_record_user()`, `raw_hid_receive()`, and
  `rgb_matrix_indicators_user()` — parameterized to whatever N is chosen, plus a `raw_hid_send()`
  reply to a `MSG_PING`/hello request so `HidPadLink`'s handshake has something to receive.
- Build `slot_to_led[]` from Air75 V2's real LED index table — confirmed in Phase 0 that this
  lives in `keyboards/nuphy/air75_v2/ansi/keyboard.json`'s `rgb_matrix.layout` array (matrix
  position → x/y), not in a hand-written board `.c`/`.h` file. Match chosen `matrix`/label
  entries in that array to the physical keys chosen for slots to get real LED indices.

---

## Phase 5 — Test without hardware — partially done (pulled into Phase 2)

The repo already tests `daemon.py` against fakes via `monkeypatch` (`serial.Serial`,
`subprocess.run`, `SerialPadLink.write_json`) and has `fake_hooks.py` for exercising the socket
path without real hooks. Extend the same approach:

- ~~A fake `hidapi` device in `tests/` mirroring the existing serial fakes, so `HidPadLink` gets
  the same coverage `SerialPadLink` has.~~ **Done in Phase 2**: `tests/test_hid_pad_link.py`'s
  `FakeHidDevice`/`make_fake_hid()` cover `discover_hid_device()`, `HidPadLink.open()`/
  `write_json()`, and `AutoPadLink`'s fallback order (19 tests). Not covered: `_read_loop`'s
  background-thread dispatch itself — consistent with `SerialPadLink._read_loop` never having
  been tested either (see Phase 2's implementation notes).
- `fake_hooks.py` needs no changes — it talks to the daemon's socket, not the pad transport, so
  it's already transport-agnostic. Still worth an actual run (`fake_hooks.py --sessions 3` against
  `MACROPAD_TRANSPORT=hid` headless, no real board) once Phase 4's keymap exists, as an end-to-end
  smoke check distinct from the unit tests above.

---

## Phase 6 — Bring-up on real hardware

1. Flash the custom keymap, confirm `hello`/RGB round-trip with a minimal test script before
   wiring the full daemon (mirrors the repo's own build history — "Blink" before "Renderer").
2. Point `daemon.py` at it (`MACROPAD_TRANSPORT=hid`), run `fake_hooks.py --sessions 3` against a
   slot count you'd expect to be small (per the Phase 4 key-count decision), and confirm slots
   beyond capacity are dropped cleanly, matching Phase 3's behavior.
3. Wire real Claude Code hooks last, same as the existing README's step 4.

---

## Explicitly out of scope

- Key-press dispatch on Air75 V2 (window-bring-to-front) — optional; can be added later via
  `raw_hid_send()` following the same shape as Phase 1's inbound reports, reusing
  `dispatch_bring_to_front` unchanged.
- Urgency-based slot reallocation for small-N boards — deferred until a board with genuinely
  limited keys is actually in hand.

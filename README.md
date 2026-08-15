# claude-macropad

A daemon that mirrors the state of your Claude Code sessions onto a
physical keyboard — one key/LED slot per active session, color-coded
by what that session is doing (working, waiting on you, done, errored,
etc.) — pressing a key brings that session's window (Terminal, VS
Code, or IntelliJ) to the front.

Two pads are supported out of the box: the [Adafruit MacroPad
RP2040](https://www.adafruit.com/product/5100) (12 keys, plus an OLED
that shows a label per slot) over USB serial, and any RGB QMK
keyboard — proven on a [NuPhy Air75
V2](https://nuphy.com/products/air75-v2) — over USB HID. Porting to
another QMK board should take little more than adding its own keymap
(VID/PID plus per-key mapping) — the daemon, HID wire protocol, and
dispatch logic are already keyboard-agnostic; see [QMK keyboard (NuPhy
Air75 V2)](#qmk-keyboard-nuphy-air75-v2) for the pattern to follow.

A third QMK board, the [Keychron K1
Pro](https://www.keychron.com/products/keychron-k1-pro-qmk-via-wireless-custom-mechanical-keyboard)
(ANSI), is also wired up, built against Keychron's own official
firmware source — but **unverified on real hardware**, and needs one
small source patch applied before it'll build; see [QMK keyboard
(Keychron K1 Pro, unverified)](#qmk-keyboard-keychron-k1-pro-unverified)
before relying on it.

This repo covers the full path end to end: an example Claude Code
`settings.json` wiring plus the hook script it invokes, a host-side
daemon that speaks a small JSON protocol with the pad over USB serial
or USB HID, and the on-device code (CircuitPython for the MacroPad,
QMK keymap C for Air75-style boards) that runs on the pad itself.

## Pad states

Each slot's NeoPixel color reflects that session's current state, per
`STATE_COLORS` in [`rp2040/code.py`](rp2040/code.py):

|     | Label        | Color                      | Internal state | When                                                                                                           |
| --- | ------------ | --------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------- |
| ⚪  | idle         | dim gray `#282828`         | `idle`         | `SessionStart` — slot allocated, nothing happening yet                                                        |
| 🔵  | thinking     | blue `#0000FF`              | `working`      | Claude is reasoning between tool calls (`UserPromptSubmit`, `PostToolUse`)                                    |
| 🟣  | tool running | purple `#8000FF`            | `tool_running` | A tool call is actively executing (`PreToolUse`)                                                              |
| 🟢  | complete     | green `#00FF00`            | `done`         | `Stop` — Claude finished responding                                                                           |
| 🟠  | needs input  | orange `#FF7F00`, blinking | `question`     | Blocked on you: `AskUserQuestion`, `ExitPlanMode`, `PermissionRequest`, or `Notification:agent_needs_input`   |
| 🟣  | tool stalled | purple `#8000FF`, blinking | `tool_stalled` | A tool call has been pending past `STALL_THRESHOLD_SECONDS` with no `PostToolUse` — may or may not be blocked |
| 🟡  | waiting      | amber `#FFAA00`             | `waiting`      | Claude's been idle 60s+ with nothing blocking (`Notification:idle_prompt`) — lower urgency than "needs input" |
| 🔴  | error        | red `#FF0000`              | `error`        | `PostToolUseFailure`                                                                                           |

"needs input" and "tool stalled" blink (0.5s on/off, `BLINK_PERIOD` in
`rp2040/code.py`) so each reads as distinct from its solid-color
sibling at a glance — "needs input" from "waiting" despite sharing a
similarly warm color, and "tool stalled" from "tool running" despite
sharing the same purple hue.

A slot that receives a state it doesn't recognize — e.g. an older
firmware/`rp2040/code.py` build talking to a newer daemon that's added a
state since it was last flashed — renders solid magenta `#FF00FF`
instead of silently falling back to idle or off, which would look like
nothing's wrong. This is a fallback rendering behavior, not a state
`hook_to_state` ever produces on purpose.

**Only tested on macOS.** Window-dispatch (tmux/Terminal.app/VS
Code/IntelliJ activation) uses AppleScript and is macOS-only outright;
the rest (daemon, hook.sh, serial protocol) may work elsewhere but
hasn't been tried.

### Adafruit MacroPad RP2040 Working Example

![MacroPad in action example](./adafruit.macropad.rp2040.png)

### NuPhy Air75 V2 (QMK) Working Example

![Air75 V2 in action example](./nuphy.air75.v2.png)

## Repo layout

| Path                                | Description                                                                                               |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `daemon.py`                         | Host-side daemon: Unix socket server + serial/HID link to the pad                                         |
| `hid_protocol.py`                   | Wire-level binary report format for the HID transport (QMK-based pads) — see [Protocol](#protocol)        |
| `fake_hooks.py`                     | Simulates a Claude Code session's hook events, for testing the daemon without real hooks wired up         |
| `hid_bringup_test.py`               | Standalone hello/RGB round-trip check against a real QMK pad, independent of `daemon.py`                  |
| `qmk-userspace/`                    | QMK userspace overlay, built against a separate local QMK checkout — `users/claude_macropad/` holds the protocol/state logic shared by every board's keymap; `keyboards/.../keymaps/claude_macropad/` holds each board's own layout, LED map, and device ID. Keychron K1 Pro also ships `keyboards/keychron/k1_pro/k1_pro.c.patch`, a small patch applied to that board's own (unmodified-otherwise) firmware checkout — see [QMK keyboard (Keychron K1 Pro, unverified)](#qmk-keyboard-keychron-k1-pro-unverified) for why |
| `requirements.txt`                  | Python dependencies for the daemon                                                                        |
| `requirements-dev.txt`              | Adds `pytest` on top of `requirements.txt`, for running the test suite                                    |
| `tests/`                            | `pytest` suite for `daemon.py` and `rp2040/code.py` (see [Testing](#testing))                             |
| `rp2040/boot.py`                    | Enables the USB serial data endpoint (runs on device boot) — copied onto the MacroPad's CIRCUITPY drive   |
| `rp2040/code.py`                    | Main device loop: renders pad state, reads key/encoder input — copied onto the MacroPad's CIRCUITPY drive |
| `claude/example_hook_settings.json` | `hooks` block to merge into `settings.json`, wiring every relevant event to `hook.sh`                     |
| `claude/hook.sh`                    | Reads a hook payload from stdin, enriches it, and forwards it to the daemon's socket                      |

## How it fits together

```
Claude Code hooks --> hook.sh -->   daemon.py    <-- USB serial --> MacroPad RP2040
                                  (Unix socket)  <-- USB HID    --> QMK keyboard
```

`daemon.py` listens on a Unix domain socket at `~/.claude-macropad/daemon.sock`
for line-delimited JSON hook payloads, maps each one to a display state
for the originating session, and pushes that state to the pad over
USB serial (MacroPad) or USB HID (QMK keyboard). It also reads events
back from the pad (key presses, encoder turns on the MacroPad) and
logs them.

## Hardware

Any pad needs **a USB-C cable that carries data, not just power.** A
lot of USB-C cables are charge-only; both the serial link (MacroPad)
and the HID link (QMK boards) need one that actually supports data
transfer. Beyond that, requirements depend on which pad you're
building.

### Adafruit MacroPad RP2040

Beyond the [MacroPad RP2040](https://www.adafruit.com/product/5100)
board itself, getting a fully functional pad requires:

1. **The base board** (linked above).
2. **12 MX-compatible mechanical switches, with RGB support.** This
   project is built on Cherry MX Red RGB switches. Get RGB-capable
   switches specifically — without them, the [color-coded states above](#pad-states)
   have nowhere to show, and the pad's usefulness drops to just the
   OLED text and window-selection on keypress.
3. **12 MX-compatible keycaps** (technically optional — the switches
   work bare). To actually see the color, the keycaps need to
   shine-through (translucent), not opaque.

If you can get the [MacroPad RP2040 Starter
Kit](https://www.adafruit.com/product/5128), it bundles the board, RGB
switches, and keycaps together — the simplest path when it's in stock.

Otherwise, buy the bare-bones board, switches, and keycaps separately.
An [acrylic enclosure](https://www.adafruit.com/product/5103) is also
available and optional, but recommended for protecting the board.

### QMK keyboards (NuPhy Air75 V2 and others)

No separate switches/keycaps shopping list here — a prebuilt keyboard
with per-key RGB is the whole requirement. This repo's keymap is
proven on the [NuPhy Air75 V2](https://nuphy.com/products/air75-v2);
any other QMK board with per-key RGB matrix support (`RGB_MATRIX_ENABLE`)
should work with a keymap of its own — see [QMK keyboard (NuPhy Air75
V2)](#qmk-keyboard-nuphy-air75-v2) for the pattern to follow when
porting to a different board. A [Keychron K1
Pro](https://www.keychron.com/products/keychron-k1-pro-qmk-via-wireless-custom-mechanical-keyboard)
(ANSI) keymap is also included, but unverified — see [QMK keyboard
(Keychron K1 Pro, unverified)](#qmk-keyboard-keychron-k1-pro-unverified).

## Setup

### 1. Flash your pad

Flashing is entirely different between the two pads — CircuitPython
file copy for the MacroPad, a QMK firmware build/flash for QMK boards.
Follow whichever subsection matches your hardware; the rest of Setup
(steps 2-4 below) is shared.

#### Adafruit MacroPad RP2040

1. Put the MacroPad RP2040 into CircuitPython (see
   [Adafruit's guide](https://learn.adafruit.com/adafruit-macropad-rp2040)
   if it isn't already).
2. From the [Adafruit CircuitPython Library
   Bundle](https://circuitpython.org/libraries) matching your device's
   CircuitPython version, copy these into `CIRCUITPY/lib`:
   - `adafruit_macropad`
   - `adafruit_display_text`
   - `adafruit_debouncer`
   - `neopixel`
   - `adafruit_bus_device`
3. Copy [`rp2040/boot.py`](rp2040/boot.py) and [`rp2040/code.py`](rp2040/code.py)
   to the root of `CIRCUITPY`, overwriting any existing `boot.py`/`code.py`.
4. Reset the board (a fresh `boot.py` only takes effect after a reset,
   not a soft reload). All 12 keys should light up dim gray ("idle").

#### QMK keyboard (NuPhy Air75 V2)

Verified against real hardware. 4 slots wired by default (PageUp/PageDn/Home/End), each
showing one Claude Code session's state via per-key RGB, and pressing one brings that
session's window to the front (same `dispatch_bring_to_front` the RP2040 uses). On boards
built with `VIA_ENABLE` (this one is), up to 8 slots are reachable from the VIA app — drag
one of the "AI Slot 4".."AI Slot 7" custom keycodes (see `via.json` in the keymap directory)
onto any spare key in the [VIA app](https://www.caniusevia.com/) and it lights up
automatically; remap a slot key away and its LED goes dark just as automatically. (The shared
firmware actually supports up to 12 slots, but VIA's app hard-caps `customKeycodes` at 32
total entries, and NuPhy's own stock entries already use most of that budget — see the
comment above `enum claude_macropad_keycodes` in `keymap.c` for the exact accounting.) The
keymap source lives in this repo under
`qmk-userspace/` (a [QMK userspace
overlay](https://docs.qmk.fm/newbs_external_userspace)), built against a separate local QMK
checkout that isn't part of this repo:

```
git clone --branch nuphy-keyboards https://github.com/nuphy-src/qmk_firmware.git ../nuphy-qmk-firmware
cd ../nuphy-qmk-firmware && git submodule update --init --recursive
brew install qmk/qmk/qmk    # plus arm-none-eabi-gcc@8 (osx-cross/arm tap) for this board's STM32F072
qmk config user.qmk_home=../nuphy-qmk-firmware

cd ../claude-qmk/qmk-userspace
QMK_USERSPACE="$(pwd)" qmk compile -kb nuphy/air75_v2/ansi -km claude_macropad
```

To flash: unplug the board (or just turn it off), hold **Esc**, plug it back in over USB-C
(or turn it back on) — this is QMK bootmagic (default row/col 0,0 = Esc on this board), not
anything keymap-specific, so it works for recovery too regardless of what firmware is
currently on the board:

```
QMK_USERSPACE="$(pwd)" qmk flash -kb nuphy/air75_v2/ansi -km claude_macropad
```

Then verify the wire protocol works before trusting the full daemon to it —
[`hid_bringup_test.py`](hid_bringup_test.py) pings the board directly (bypassing `daemon.py`
entirely) and cycles every slot the board reports through every state so you can watch the
real LEDs:

```
python3 hid_bringup_test.py
```

##### Using the VIA app

**The VIA app and `daemon.py` can't run at the same time.** Both talk to the same raw HID
interface (our protocol deliberately shares VIA's endpoint rather than using a separate one),
and macOS enforces exclusive access to it at the OS level — whichever one opens it first locks
the other out, and VIA will report the keyboard as "not responding like a VIA-enabled keyboard"
if it loses that race. **Stop the daemon before opening VIA**, and restart it once you're done
reassigning keys.

To reassign slots (e.g. to move a default slot off PageUp/PageDn/Home/End, or to put "AI Slot
4".."AI Slot 7" on a spare key):

1. In VIA's Settings page, enable **Show Design tab**.
2. In the new Design tab, manually load
   [`via.json`](qmk-userspace/keyboards/nuphy/air75_v2/ansi/keymaps/claude_macropad/via.json)
   (this board isn't in VIA's official keyboard registry, so it won't be auto-detected —
   loading the file directly is what tells VIA how to talk to it).
3. Switch to the Configure tab, make sure **layer 0** is selected, and drag any of the custom
   "AI Slot N" keycodes (bottom-left CUSTOM section) onto the key you want it to live on — or
   drag any other keycode onto PageUp/PageDn/Home/End to move a default slot elsewhere.

![VIA Configure tab showing the AI Slot custom keycodes](./via.nuphy.air75.v2.png)

Reassignments take effect immediately — no reflashing needed. `claude_macropad.c`'s dynamic
scan picks up wherever a slot key actually is the moment you make the change (see the "Dynamic
AI-agent slots" work in this repo's history for how that works), and once you restart the
daemon it'll rediscover the board with whatever layout you left it in.

Porting to a different QMK board follows the same shape: a new keymap directory under
`qmk-userspace/keyboards/`, with just its layout, LED-index table, and device ID — the
HID protocol and `dispatch_bring_to_front` logic itself is shared code in
`qmk-userspace/users/claude_macropad/`, not duplicated per board. Keep the keymap named
`claude_macropad` (i.e. still `-km claude_macropad`) so QMK's build picks up that shared
`users/claude_macropad/` directory automatically.

#### QMK keyboard (Keychron K1 Pro, unverified)

**Unverified on real hardware.** Unlike the Air75 keymap above, nobody has built, flashed, or
tested this one against a real board — treat everything below as a documented best-effort, not
a confirmed working path. That said, it's built against Keychron's own official firmware source
(the [`Keychron/qmk_firmware`](https://github.com/Keychron/qmk_firmware) fork, `wireless_playground`
branch), not a third-party reverse-engineered one — the ANSI layout, matrix, RGB LED indices, and
VID/PID all come directly from Keychron's real `keyboards/keychron/k1_pro/ansi/rgb/`, and the base
keymap layers are Keychron's own stock K1 Pro keymap, unmodified except for the 4 AI-slot key
substitutions. What's unverified is specifically "does it work on a real board" — not "is this
guessed at."

One small, unavoidable wrinkle: `k1_pro.c` (board-level code, shared by every keymap for this
board — not something this repo's keymap directory touches) already defines `via_command_kb()`,
the same raw-HID early-intercept hook the Air75 keymap uses directly, to handle two vendor
commands (bluetooth DFU, factory test). A keymap can't also define `via_command_kb()` itself —
duplicate strong symbol, hard link error — so this board needs one small patch applied to that
file first, adding a new empty-by-default hook (`raw_hid_receive_kb()`) that `via_command_kb()`
falls through to for anything it doesn't already claim, which is where this keymap's own
`raw_hid_receive_kb()` (in `keymap.c`) plugs in. The patch is 12 lines, touches nothing any other
keymap for this board relies on, and ships in this repo as a diff. It's also been submitted
upstream as [Keychron/qmk_firmware#506](https://github.com/Keychron/qmk_firmware/pull/506) — if
that gets merged, this manual step goes away for anyone building against a checkout that
includes it; worth checking before you patch by hand.

```
git clone --branch wireless_playground https://github.com/Keychron/qmk_firmware.git ../keychron-qmk-firmware
cd ../keychron-qmk-firmware && git submodule update --init --recursive
brew install qmk/qmk/qmk    # plus an ARM cross-compiler for this board's STM32L432
qmk config user.qmk_home=../keychron-qmk-firmware

git apply ../claude-macropad/qmk-userspace/keyboards/keychron/k1_pro/k1_pro.c.patch
# (already cd'd into ../keychron-qmk-firmware above — the patch's paths
# are relative to that repo's root, so no --directory needed here)

cd ../claude-macropad/qmk-userspace
QMK_USERSPACE="$(pwd)" qmk compile -kb keychron/k1_pro/ansi/rgb -km claude_macropad
```

To flash, per [Keychron's own
readme](https://github.com/Keychron/qmk_firmware/blob/wireless_playground/keyboards/keychron/k1_pro/readme.md):
connect the USB-C cable, toggle the board's Mac/Win mode switch to **Off**, hold down **Esc**
(or the reset button underneath the spacebar), then toggle the switch to **Cable**:

```
QMK_USERSPACE="$(pwd)" qmk flash -kb keychron/k1_pro/ansi/rgb -km claude_macropad
```

Then, same as the Air75 board, verify the wire protocol directly before trusting the daemon to
it — `python3 hid_bringup_test.py` — and only move on once you've watched the real LEDs cycle
through every state correctly.

Slot wiring, VIA reassignment, and the VIA/daemon exclusivity rule are all the same as [the
Air75 board above](#qmk-keyboard-nuphy-air75-v2) — same 4 default slots (PageUp/PageDn/Home/End),
same `claude_macropad`-named keymap directory, same "stop the daemon before opening VIA" rule,
same `via.json`-loading Design-tab step (load
[this board's `via.json`](qmk-userspace/keyboards/keychron/k1_pro/ansi/rgb/keymaps/claude_macropad/via.json)
instead, which extends Keychron's own official VIA definition for this board rather than
replacing it). One difference: this board's 13 stock custom keycodes (left/right Option, left/right
Cmd, Task View, File Explorer, Screenshot, Cortana, Siri, 3 bluetooth host slots, battery level)
use up less of VIA's 32-entry `customKeycodes` budget than the Air75 board's 24 do, so all 12
`AI_AGENT_KEY_0`..`11` slots are nameable ("AI Slot 0".."AI Slot 11"), not just 8 of them.

### 2. Run the daemon

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python3 daemon.py
```

The daemon auto-detects the pad on startup (`AutoPadLink` in
[`daemon.py`](daemon.py)): it tries serial discovery first — scanning
USB serial devices for Adafruit's vendor ID, sending each a
`{"t": "ping"}`, and attaching to whichever one answers
`{"t": "hello", "device": "claude-macropad-v1"}` (see the `ping`/`hello`
handshake in [`rp2040/code.py`](rp2040/code.py) and `discover_port()`
in `daemon.py`) — then falls back to HID discovery for a QMK-based pad,
trying each board in `hid_protocol.KNOWN_HID_PADS` (NuPhy Air75 V2,
Keychron K1 Pro) in turn via `discover_hid_pad()` until one answers.
No need to look up `/dev/cu.usbmodem*` by hand or update it after a
replug.

Force one transport explicitly with `MACROPAD_TRANSPORT`, and/or skip
serial discovery with `MACROPAD_SERIAL_PORT`:

```
MACROPAD_TRANSPORT=serial MACROPAD_SERIAL_PORT=/dev/cu.usbmodem14201 python3 daemon.py
MACROPAD_TRANSPORT=hid python3 daemon.py
```

**Use the `/dev/cu.*` device, not `/dev/tty.*`** — the `tty` node blocks
on carrier-detect and can hang `pyserial`'s `Serial()` open call until
the board is unplugged.

HID transport additionally needs `pip install hid` (uncomment it in
`requirements.txt`) plus the native hidapi library (`brew install
hidapi` on macOS) — both optional, and skipped gracefully (falls back
to headless, same as finding no pad at all) if either is missing.

If no pad is found on either transport, the daemon still runs — it
just logs what it _would_ send instead of writing to a port. This lets
you develop against the socket/slot-mapping logic without any
hardware plugged in.

Before it starts accepting hook events, the daemon also seeds slots
for any Claude Code sessions that were *already* running — e.g. a
daemon restart while sessions are mid-conversation — by shelling out
to `claude agents --json` (Claude Code ≥2.1.224) and allocating a slot
per session it reports, so the pad shows them immediately instead of
waiting for each one to happen to fire a hook event first. Requires an
up-to-date `claude` on `PATH`; an older CLI (or none at all) just
means seeding finds nothing, and pre-existing sessions fall back to
picking up a slot lazily on their first hook event instead. Every
*other* slot — anything not claimed by a real session in that
`claude agents --json` output — gets explicitly cleared to off, so a
slot left glowing by a session that died without a clean `SessionEnd`
(a crash, `kill -9`, or a previous daemon run that never shut down
properly) doesn't linger forever; the pad has no way to know the old
daemon process is gone, so nothing else would ever revisit that slot
otherwise (see `Daemon.seed_existing_sessions()` in `daemon.py`).

### 3. Try it without real hooks

To exercise the daemon before wiring up real hooks (or anytime you don't
have a live Claude Code session handy), use `fake_hooks.py` to simulate
a session's hook lifecycle against a running daemon:

```
python3 fake_hooks.py                # one simulated session
python3 fake_hooks.py --sessions 3   # three concurrent sessions, staggered
```

Each run walks through `SessionStart` → prompt → tool calls (including
one that should light the slot up as "question", e.g. `AskUserQuestion`)
→ `Stop` → `SessionEnd`, with pauses in between so you can watch the pad
react in real time.

### 4. Wire up real Claude Code hooks

1. Copy the script and make it executable. `daemon.py` also creates this
   directory itself on startup (it hosts `daemon.sock` and `events.log`
   too), so this step just needs to happen before the first real hook
   fires:

   ```
   mkdir -p "$HOME/.claude-macropad"
   cp claude/hook.sh "$HOME/.claude-macropad/hook.sh"
   chmod +x "$HOME/.claude-macropad/hook.sh"
   ```

   It needs `jq` and a `nc` build that supports Unix-domain sockets
   (`-U`, e.g. macOS's built-in `nc`) on `PATH`.

2. Merge the `"hooks"` block from
   [`claude/example_hook_settings.json`](claude/example_hook_settings.json)
   into your Claude Code `settings.json` (global `~/.claude/settings.json`
   or a project's `.claude/settings.json`). It registers
   `$HOME/.claude-macropad/hook.sh` as a command hook for every event
   `handle_hook_event()` cares about (`SessionStart`, `UserPromptSubmit`,
   `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PostToolUseFailure`,
   `Notification`, `Stop`, `SubagentStop`, `SessionEnd`). The
   `Notification` entries split on `matcher` (`agent_needs_input` vs.
   `idle_prompt`) and pass `MACROPAD_NOTIFICATION_TYPE` as an env var,
   since that's the reliable way to know which subtype fired for a given
   invocation (`Notification:permission_prompt` itself is not wired up —
   see [`hook_to_state`'s
   docstring](daemon.py) for why).

[`claude/hook.sh`](claude/hook.sh) reads the hook's JSON payload from
stdin (Claude Code already includes `hook_event_name` and `session_id`
in it) and forwards it to `~/.claude-macropad/daemon.sock` via `nc -U`, after
using `jq` to fill in a few fields the payload doesn't reliably carry on
its own:

- `notification_type`, from the `MACROPAD_NOTIFICATION_TYPE` env var set
  by the matcher branch in `settings.json`.
- `tmux_pane`, from the script's own `$TMUX_PANE` (empty if not running
  inside tmux).
- `controlling_tty`, at `SessionStart` only: Claude Code runs hook
  commands detached with no controlling terminal, so the script instead
  reads the _parent_ process's tty via `ps -o tty= -p "$PPID"` — the
  process Claude Code actually spawned still has one.

It fails open by design (redirects `nc`'s output away, always exits 0)
so a daemon that isn't running never blocks a tool call or a session,
and caps the socket write at one second so `PreToolUse`/`PostToolUse` —
which fire on every tool call — stay fast.

## Testing

```
pip install -r requirements-dev.txt
python3 -m pytest
```

All tests run against fakes — no real serial port, HID device, socket,
or hardware needed (not even the native hidapi library — a fake `hid`
module stands in for it):

- `daemon.py`'s logic (`hook_to_state`, `SlotManager`, `Daemon.handle_hook_event`,
  stall escalation, port `discover_port()`, window-dispatch fallthrough)
  is tested directly, with `serial.Serial`, `subprocess.run`, and
  `SerialPadLink.write_json` swapped for recording fakes via `monkeypatch`.
- `HidPadLink`, `discover_hid_device()`, and `AutoPadLink`'s
  serial-then-HID fallback order are tested the same way, against a
  fake `hid` module (`tests/test_hid_pad_link.py`) mirroring the
  serial fakes above — including a fake raw-HID interface that only
  answers on the right `usage_page`/`usage`, like the real board's
  raw HID endpoint alongside its normal keyboard interfaces.
- `hid_protocol.py`'s report encode/decode round-trips
  (`tests/test_hid_protocol.py`).
- The slots-from-handshake path (`PadTransport.handshake()` on both
  transports, `AutoPadLink`'s delegation, `Daemon.apply_handshake()`)
  is tested in `tests/test_pad_handshake.py`, including the
  timeout/no-reply/headless cases.
- Startup seeding of pre-existing sessions (`discover_running_sessions()`,
  `Daemon.seed_existing_sessions()`, and the tty/tmux-pane backfill
  helpers) is tested in `tests/test_seed_existing_sessions.py`, with
  `subprocess.run` swapped for a recording fake the same way as the
  other discovery tests.
- `rp2040/code.py`'s protocol/state logic (`read_json_lines`,
  `handle_message`, `redraw`, `pixel_color`) is tested by importing the
  file with its CircuitPython-only imports (`usb_cdc`, `displayio`,
  `adafruit_macropad`, ...) swapped for minimal fakes — see the `pad`
  fixture in `tests/conftest.py`. Its `while True` main loop is behind
  `def main(): ... if __name__ == "__main__": main()`, so importing it
  for tests doesn't hang the way running it as a script would.

## Protocol

### Hook events in (socket → daemon)

Each line written to `~/.claude-macropad/daemon.sock` is a single JSON object
with (at minimum) `hook_event_name` and `session_id`, matching the shape
of a Claude Code hook payload. Recognized fields:

| Field               | Used for                                                                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `hook_event_name`   | Selects the resulting pad state (see below)                                                                      |
| `session_id`        | Identifies which pad slot this event belongs to                                                                  |
| `cwd`               | Project folder name, used as the slot's OLED label (MacroPad only — QMK pads are RGB-only, no label)             |
| `tool_name`         | Distinguishes attention-worthy tools (`AskUserQuestion`, `ExitPlanMode`) and labels the slot during `PreToolUse` |
| `notification_type` | Distinguishes `Notification` subtypes (`agent_needs_input`, `idle_prompt`, ...)                                  |
| `tmux_pane`         | tmux pane id, for the "bring to front" key-press dispatch                                                        |
| `controlling_tty`   | Terminal.app tty, for the same dispatch when not in tmux                                                         |

`hook_event_name` maps to a display state roughly as:

| Event                                                                      | State          |
| -------------------------------------------------------------------------- | -------------- |
| `SessionStart`                                                             | `idle`         |
| `UserPromptSubmit`, `PostToolUse`                                          | `working`      |
| `PreToolUse` (generic tool)                                                | `tool_running` |
| `PreToolUse` with `AskUserQuestion`/`ExitPlanMode`, or `PermissionRequest` | `question`     |
| `PostToolUseFailure`                                                       | `error`        |
| `Stop`                                                                     | `done`         |
| `Notification` (`agent_needs_input`)                                       | `question`     |
| `Notification` (`idle_prompt`)                                             | `waiting`      |
| `SessionEnd`                                                               | slot cleared   |

A `PreToolUse` with no matching `PostToolUse`/`PostToolUseFailure` within
`STALL_THRESHOLD_SECONDS` (default 10s) is escalated to `tool_stalled`
(blinking purple) as a backstop, since `Notification:permission_prompt`
isn't reliable enough to depend on alone. This deliberately stops short
of claiming `question` (definitely blocked on you) — the daemon can't
actually tell whether a stalled tool call is an unreported permission
prompt or just a slow tool, so `tool_stalled` only claims "this is
taking a while." If a definite `question` signal (`PermissionRequest`,
`Notification:agent_needs_input`) does arrive for that same pending
call, the stall tracking for it is dropped — the slot's already showing
a stronger, more specific state than a guess, and shouldn't get
clobbered back to `tool_stalled` once the threshold elapses from the
original `PreToolUse`.

Slots are allocated first-fit and freed on `SessionEnd`. The number of
slots comes from the pad's own `hello` handshake at startup (12 for the
MacroPad RP2040, matching its key count 1:1; whatever a QMK-based pad
reports otherwise — see `Daemon.apply_handshake()` in `daemon.py`), with
`NUM_SLOTS` (12) used as a fallback if the pad is headless or doesn't
answer the handshake in time.

Sessions already running when the daemon starts are seeded into slots
up front via `claude agents --json`, rather than waiting for their next
hook event — see `Daemon.seed_existing_sessions()` in `daemon.py` and
[Run the daemon](#2-run-the-daemon) above.

### Pad messages (daemon ↔ device)

Line-delimited JSON over serial, in both directions.

Daemon → device:

```jsonc
{"t": "slot", "i": 0, "state": "working", "label": "Read"}
{"t": "clear", "i": 0}
{"t": "ping"}
```

Device → daemon:

```jsonc
{"t": "key", "i": 0}
{"t": "enc", "d": 1}
{"t": "enc_click"}
{"t": "hello", "device": "claude-macropad-v1", "slots": 12}
```

`ping`/`hello` is the handshake port auto-discovery uses (see above) to
confirm a given serial device is actually the pad, not some other
CircuitPython board.

A key press currently just logs which session it corresponds to and
attempts to bring that session's window to the front (tried in order:
tmux pane, Terminal.app tab by tty, VS Code window by project name,
IntelliJ IDEA window by project name) — all via AppleScript, so this is
macOS-only for now. Encoder events are logged only; nothing consumes
them yet.

### Pad messages (HID reports)

For QMK-based pads (e.g. a NuPhy Air75 V2) instead of the RP2040, the same
messages travel as fixed-size 32-byte raw HID reports rather than
JSON lines — see `hid_protocol.py` for the encode/decode helpers and
exact byte layout:

| Byte 0 (type) | Direction       | Bytes 1-2                          |
| ------------- | --------------- | ---------------------------------- |
| `MSG_PING`    | daemon → device | (none)                             |
| `MSG_HELLO`   | device → daemon | device id, `slots`                 |
| `MSG_SLOT`    | daemon → device | slot index, state (0-31, see below) |
| `MSG_KEY`     | device → daemon | slot index                         |

State bytes mirror `STATE_COLORS`'s keys in `rp2040/code.py` 1:1
(`idle`=0, `working`=1, `waiting`=2, `done`=3, `error`=4, `question`=5,
`tool_running`=6, `tool_stalled`=7, ..., `off`=31), so `hook_to_state`'s
output maps identically regardless of which transport is attached.
`off`=31 is deliberately pinned well above the states defined today
rather than "whatever's defined last" — adding a future state only
means picking the next unused number below it, never renumbering `off`
(and the QMK side's `state <= STATE_OFF` bounds check, which is anchored
to its value) again. Values in between that are reserved-but-unused
today, or a value newer than what a given firmware build understands,
render as the "unknown" fallback color described above. There's no
separate "clear" report — an RGB-only pad has no label to clear, so a
cleared slot is just `MSG_SLOT` with state `off` (fully dark — distinct
from `idle`'s dim glow, matching `rp2040/code.py`'s own
`handle_message()`).

Key-press dispatch works the same as the serial protocol's
`{"t": "key", "i": N}` — sent on key-down only, no key-up equivalent,
since `on_device_event()` doesn't distinguish transports and has no
key-up concept to consume one anyway.

## Logs

- Console: human-readable, `INFO` level.
- `~/.claude-macropad/events.log`: rotating (5MB × 3 files) raw event
  log — every socket line (parsed or not), every state mapping decision,
  and every window-dispatch attempt/result. Useful for diagnosing a
  framing or mapping bug after the fact without reproducing it live.

## Status

This is early-stage, but the path from a real Claude Code session to the
pad now works end to end:

- ✅ Claude Code hook wiring (`settings.json` block + `hook.sh`)
- ✅ Socket server + hook-event → pad-state mapping
- ✅ Serial protocol to/from the MacroPad, with pad auto-discovery
- ✅ HID protocol to/from QMK keyboards (e.g. NuPhy Air75 V2), same auto-discovery
- ✅ Slot allocation for concurrent sessions
- ✅ Key-press → bring-window-to-front dispatch (macOS)
- ✅ Startup seeding of already-running sessions (`claude agents --json`)
- ⚠️ Keychron K1 Pro (ANSI) QMK keymap — written against Keychron's own official firmware
  source, unverified on real hardware (see [QMK keyboard (Keychron K1 Pro,
  unverified)](#qmk-keyboard-keychron-k1-pro-unverified))

## License

MIT — see [LICENSE](LICENSE).

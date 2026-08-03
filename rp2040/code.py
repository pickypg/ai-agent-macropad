"""
MacroPad RP2040
Reads JSON lines from usb_cdc.data, updates NeoPixels + OLED,
and emits key/encoder events back over the same serial channel.

Requires boot.py to have already enabled the data endpoint:
    import usb_cdc
    usb_cdc.enable(console=True, data=True)

Requires these on CIRCUITPY/lib (from the Adafruit CircuitPython
Library Bundle matching your CircuitPython version):
    adafruit_macropad, adafruit_display_text, adafruit_debouncer,
    neopixel, adafruit_bus_device (macropad library dependency)
"""
import time
import json
import usb_cdc
import displayio
import terminalio
from adafruit_display_text import label
from adafruit_macropad import MacroPad

# --- Setup -----------------------------------------------------------------

macropad = MacroPad()  # gives us .pixels, .keys, .encoder, .encoder_switch, .display

serial = usb_cdc.data
if serial is None:
    # boot.py hasn't enabled the data endpoint — blink red so it's obvious
    # at a glance rather than failing silently.
    while True:
        macropad.pixels.fill((255, 0, 0))
        time.sleep(0.2)
        macropad.pixels.fill((0, 0, 0))
        time.sleep(0.2)

serial.timeout = 0  # non-blocking reads

NUM_SLOTS = 12

# Identifies this device to daemon.py's port auto-discovery — see the
# "ping"/"hello" handshake in handle_message() below.
DEVICE_ID = "claude-macropad-v1"

STATE_COLORS = {
    "idle":     (40, 40, 40),
    "working":  (0, 0, 255),
    "waiting":  (255, 170, 0),
    "done":     (0, 255, 0),
    "error":    (255, 0, 0),
    "question": (255, 127, 0),
    "off":      (0, 0, 0),
}

# Per-slot state, kept so we can redraw the OLED without re-parsing
# everything on every loop.
slots = [{"state": "idle", "label": ""} for _ in range(NUM_SLOTS)]

# --- OLED setup --------------------------------------------------------

group = displayio.Group()
macropad.display.root_group = group

label_rows = []
for i in range(NUM_SLOTS):
    row = label.Label(
        terminalio.FONT,
        text="",
        color=0xFFFFFF,
        x=0,
        y=6 + i * 5,  # 128x64 panel — Phase 6 handles paging past what fits
    )
    label_rows.append(row)
    group.append(row)


def redraw():
    for i, slot in enumerate(slots):
        if i >= len(label_rows):
            break  # paging past NUM_SLOTS visible rows is a Phase 6 problem
        text = "{}:{:<4} {}".format(i, slot["state"][:4], slot["label"])
        label_rows[i].text = text[:21]  # rough character budget for the width


# --- Line-buffered read from usb_cdc.data -----------------------------

_rx_buf = b""


def read_json_lines():
    """Yield parsed JSON objects as complete lines arrive.

    Buffers partial reads across loop iterations since serial.read()
    can return mid-line chunks. Malformed lines are dropped rather
    than raising, so one bad message can't crash the render loop.
    """
    global _rx_buf
    n = serial.in_waiting
    if n:
        _rx_buf += serial.read(n)
    while b"\n" in _rx_buf:
        line, _rx_buf = _rx_buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            pass


def handle_message(msg):
    t = msg.get("t")

    if t == "slot":
        i = msg.get("i")
        if i is None or not (0 <= i < NUM_SLOTS):
            return
        slots[i]["state"] = msg.get("state", slots[i]["state"])
        if "label" in msg:
            slots[i]["label"] = msg["label"]
        macropad.pixels[i] = STATE_COLORS.get(slots[i]["state"], (0, 0, 0))
        redraw()

    elif t == "clear":
        i = msg.get("i")
        if i is None or not (0 <= i < NUM_SLOTS):
            return
        slots[i] = {"state": "idle", "label": ""}
        macropad.pixels[i] = STATE_COLORS["off"]
        redraw()

    elif t == "toast":
        # Stub for Phase 2 — a dedicated toast row with its own
        # timeout belongs in Phase 6 polish, not here.
        pass

    elif t == "ping":
        # Handshake so the daemon can auto-detect which /dev/cu.usbmodem*
        # node is actually the pad, instead of requiring
        # MACROPAD_SERIAL_PORT to be set by hand — that suffix changes
        # every time the board is replugged or another CircuitPython
        # device is attached. See discover_port() in daemon.py.
        send({"t": "hello", "device": DEVICE_ID, "slots": NUM_SLOTS})


def send(obj):
    serial.write((json.dumps(obj) + "\n").encode("utf-8"))


# --- Main loop -----------------------------------------------------------

last_encoder_pos = macropad.encoder
last_switch_pressed = False

macropad.pixels.brightness = 0.3
redraw()

while True:
    # host -> device
    for msg in read_json_lines():
        handle_message(msg)

    # device -> host: keypresses
    event = macropad.keys.events.get()
    if event and event.pressed:
        send({"t": "key", "i": event.key_number})

    # device -> host: encoder rotation
    pos = macropad.encoder
    if pos != last_encoder_pos:
        send({"t": "enc", "d": pos - last_encoder_pos})
        last_encoder_pos = pos

    # device -> host: encoder click
    macropad.encoder_switch_debounced.update()
    pressed = macropad.encoder_switch_debounced.pressed
    if pressed and not last_switch_pressed:
        send({"t": "enc_click"})
    last_switch_pressed = pressed

    time.sleep(0.01)

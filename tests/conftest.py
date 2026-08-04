import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --- rp2040/code.py hardware fakes --------------------------------------
#
# code.py imports CircuitPython-only modules (usb_cdc, displayio,
# terminalio, adafruit_display_text, adafruit_macropad) that don't exist
# off-device. These fakes stand in for just enough of their surface area
# to let code.py's module-level setup run under plain CPython, so its
# actual protocol/state logic (read_json_lines, handle_message, redraw,
# pixel_color) can be exercised directly.


class FakeSerialData:
    """Stands in for usb_cdc.data. Tests push bytes in with feed() to
    simulate host->device traffic, and inspect .written for what the
    device sent back."""

    def __init__(self):
        self._rx = b""
        self.written = b""
        self.timeout = 0

    def feed(self, data):
        self._rx += data

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        chunk, self._rx = self._rx[:n], self._rx[n:]
        return chunk

    def write(self, data):
        self.written += data
        return len(data)


class FakePixels:
    def __init__(self, n):
        self._colors = [(0, 0, 0)] * n
        self.brightness = 1.0

    def __setitem__(self, i, color):
        self._colors[i] = color

    def __getitem__(self, i):
        return self._colors[i]

    def fill(self, color):
        self._colors = [color] * len(self._colors)


class FakeKeyEvent:
    def __init__(self, key_number, pressed=True):
        self.key_number = key_number
        self.pressed = pressed


class FakeKeyEvents:
    def __init__(self):
        self._queue = []

    def get(self):
        return self._queue.pop(0) if self._queue else None


class FakeKeys:
    def __init__(self):
        self.events = FakeKeyEvents()


class FakeEncoderSwitch:
    def __init__(self):
        self.pressed = False

    def update(self):
        pass


class FakeDisplay:
    def __init__(self):
        self.root_group = None


class FakeMacroPad:
    def __init__(self):
        self.pixels = FakePixels(12)
        self.keys = FakeKeys()
        self.encoder = 0
        self.encoder_switch_debounced = FakeEncoderSwitch()
        self.display = FakeDisplay()


class FakeGroup:
    def __init__(self):
        self.children = []

    def append(self, child):
        self.children.append(child)


class FakeLabel:
    def __init__(self, font, text="", color=0, x=0, y=0):
        self.text = text


@pytest.fixture
def pad(monkeypatch):
    """Imports rp2040/code.py fresh, with fake hardware modules swapped
    in, under the name "rp2040_code" (not "code" — that's a stdlib
    module, and shadowing it process-wide would be a much nastier bug
    than the one this fixture is meant to avoid).

    Function-scoped and re-imports every test so module-level state
    (slots, _rx_buf, blink_on, ...) never leaks between tests.
    """
    fake_usb_cdc = types.ModuleType("usb_cdc")
    fake_usb_cdc.data = FakeSerialData()

    fake_displayio = types.ModuleType("displayio")
    fake_displayio.Group = FakeGroup

    fake_terminalio = types.ModuleType("terminalio")
    fake_terminalio.FONT = object()

    fake_adafruit_display_text = types.ModuleType("adafruit_display_text")
    fake_label_mod = types.ModuleType("adafruit_display_text.label")
    fake_label_mod.Label = FakeLabel
    fake_adafruit_display_text.label = fake_label_mod

    fake_adafruit_macropad = types.ModuleType("adafruit_macropad")
    fake_adafruit_macropad.MacroPad = FakeMacroPad

    for name, mod in [
        ("usb_cdc", fake_usb_cdc),
        ("displayio", fake_displayio),
        ("terminalio", fake_terminalio),
        ("adafruit_display_text", fake_adafruit_display_text),
        ("adafruit_display_text.label", fake_label_mod),
        ("adafruit_macropad", fake_adafruit_macropad),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    code_path = ROOT / "rp2040" / "code.py"
    spec = importlib.util.spec_from_file_location("rp2040_code", code_path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "rp2040_code", mod)
    spec.loader.exec_module(mod)
    return mod


# --- daemon.py fixtures --------------------------------------------------


@pytest.fixture
def recording_daemon(monkeypatch):
    """A Daemon() with pad.write_json replaced by a recorder, so tests
    can assert on what would've been sent to the pad without a real
    connection. The pad transport's open() is never called in these
    tests (only serve() calls it), so no discovery probing happens
    either, regardless of which PadTransport MACROPAD_TRANSPORT would
    pick.
    """
    import daemon as daemon_mod

    d = daemon_mod.Daemon()
    sent = []
    monkeypatch.setattr(d.pad, "write_json", sent.append)
    return d, sent


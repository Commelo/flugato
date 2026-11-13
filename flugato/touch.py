"""Reads the panel's resistive touch (ADS7846) straight from /dev/input,
no evdev package needed. Reports a single tap, a double tap and a long hold."""

import select
import struct
import threading
import time

EVENT = struct.Struct("llHHi")  # native input_event: timeval, type, code, value
EV_KEY, EV_ABS, BTN_TOUCH, ABS_X, ABS_Y = 0x01, 0x03, 0x14A, 0x00, 0x01
# raw ADS7846 reading -> screen pixel, affine: x = a*rx + b*ry + c, y = d*rx + e*ry + f,
# one per panel (PANEL in flugato.conf); TOUCH_CAL there overrides it.
# mz61581: the Tontec panel, measured with `--calibrate` (worst error 4 px).
# ili9486: the "RPi LCD (A)" kind on the Pi 4, a first fit from five stylus taps on the
# corners and the centre (worst error 6 px; a finger lands up to 30 px in from a corner);
# `--calibrate --write` on the device refines it.
PANEL_CAL = {
    "mz61581": (0.126902, 0.000457, -21.127574, 0.000332, 0.084454, -7.139456),
    "ili9486": (0.129844, 0.001468, -36.807960, 0.001225, -0.088564, 336.368795),
}
DEFAULT_CAL = PANEL_CAL["mz61581"]
HOLD_SECONDS = 2.0
DEBOUNCE = 0.15     # a resistive panel can lift for a moment mid-hold
DOUBLE_TAP = 0.35   # a second tap within this time makes a double tap
MIN_SAMPLES = 6     # position readings needed before a touch's position is trusted


def find_touch_device(name_hint="ADS7846"):
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        if name_hint.lower() in block.lower():
            for line in block.splitlines():
                if line.startswith("H: Handlers="):
                    for h in line.split("=", 1)[1].split():
                        if h.startswith("event"):
                            return f"/dev/input/{h}"
    return None


def parse_cal(text, panel=""):
    """'a,b,c,d,e,f' -> tuple, or the panel's default when empty or malformed."""
    default = PANEL_CAL.get(panel.strip().lower(), DEFAULT_CAL)
    try:
        cal = tuple(float(v) for v in text.split(","))
    except ValueError:
        return default
    return cal if len(cal) == 6 else default


def to_screen(rx, ry, cal, width=480, height=320):
    a, b, c, d, e, f = cal
    x = min(max(round(a * rx + b * ry + c), 0), width - 1)
    y = min(max(round(d * rx + e * ry + f), 0), height - 1)
    return x, y


def _median(values):
    return sorted(values)[len(values) // 2] if values else None


class Touch:
    """Calls on_tap(pos), on_double(pos) or on_hold(pos) from a background thread; pos is
    the screen pixel (x, y) of the touch (of the second tap for a double), or None if the
    panel reported no position. Keeps looking for the touch device, so a panel that
    probes late still gets touch."""

    def __init__(self, on_tap, on_double, on_hold, cal=DEFAULT_CAL, start=True):
        self.on_tap, self.on_double, self.on_hold = on_tap, on_double, on_hold
        self.cal = cal
        if start:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            dev = find_touch_device()
            if dev:
                try:
                    self._read(dev)
                except OSError:
                    pass
            time.sleep(5)

    def _pos(self, xs, ys):
        """Median of the press after its first third: a resistive panel's first readings
        come before the pressure settles and can be 100+ px off. Too few samples to
        trust: None, so the screen ignores the position instead of guessing."""
        if min(len(xs), len(ys)) < MIN_SAMPLES:
            return None
        xs, ys = xs[len(xs) // 3:], ys[len(ys) // 3:]
        return to_screen(_median(xs), _median(ys), self.cal)

    def _read(self, dev):
        pressed_at = released_at = None   # current touch
        tap_waiting = None                # a finished short tap, waiting for a second one
        xs, ys = [], []                   # raw positions of the current touch
        pos = tap_pos = None
        with open(dev, "rb", buffering=0) as f:
            while True:
                deadlines = []
                if released_at is not None:
                    deadlines.append(released_at + DEBOUNCE)
                if tap_waiting is not None and pressed_at is None:
                    deadlines.append(tap_waiting + DOUBLE_TAP)
                timeout = max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
                if not select.select([f], [], [], timeout)[0]:
                    now = time.monotonic()
                    if released_at is not None and now >= released_at + DEBOUNCE:
                        # no new press inside the debounce window: the touch really ended
                        held = released_at - pressed_at
                        pressed_at = released_at = None
                        pos = self._pos(xs, ys)
                        if held >= HOLD_SECONDS:
                            tap_waiting = None
                            self.on_hold(pos)
                        elif tap_waiting is not None:
                            tap_waiting = None
                            self.on_double(pos)
                        else:
                            tap_waiting, tap_pos = now, pos
                    elif tap_waiting is not None and pressed_at is None \
                            and now >= tap_waiting + DOUBLE_TAP:
                        tap_waiting = None
                        self.on_tap(tap_pos)
                    continue
                data = f.read(EVENT.size)
                if not data or len(data) < EVENT.size:
                    return
                _, _, etype, code, value = EVENT.unpack(data)
                if etype == EV_ABS and pressed_at is not None:
                    if code == ABS_X:
                        xs.append(value)
                    elif code == ABS_Y:
                        ys.append(value)
                    continue
                if etype != EV_KEY or code != BTN_TOUCH:
                    continue
                if value == 1:
                    if released_at is None:
                        pressed_at = time.monotonic()
                        xs, ys = [], []
                    released_at = None  # a bounce keeps the original press time
                elif value == 0 and pressed_at is not None:
                    released_at = time.monotonic()

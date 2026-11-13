"""Touch calibration: draws a cross at five known points, reads a firm tap on each and
fits the affine map from raw ADS7846 readings to screen pixels.

    sudo systemctl stop flugato
    sudo python3 -m flugato --calibrate --write   # writes TOUCH_CAL to the settings file
    sudo systemctl start flugato
"""

import struct

from PIL import Image, ImageDraw

from . import config, display, touch

TARGETS = [(30, 30), (450, 30), (450, 290), (30, 290), (240, 160)]


def fit(raw, screen):
    """Least squares for x = a*rx + b*ry + c and y = d*rx + e*ry + f."""
    def solve(rhs):
        rows = [(rx, ry, 1.0) for rx, ry in raw]
        m = [[sum(r[i] * r[j] for r in rows) for j in range(3)]
             + [sum(r[i] * v for r, v in zip(rows, rhs))] for i in range(3)]
        for c in range(3):
            p = max(range(c, 3), key=lambda r: abs(m[r][c]))
            m[c], m[p] = m[p], m[c]
            for r in range(3):
                if r != c:
                    k = m[r][c] / m[c][c]
                    m[r] = [a - k * b for a, b in zip(m[r], m[c])]
        return [m[i][3] / m[i][i] for i in range(3)]
    return tuple(solve([s[0] for s in screen]) + solve([s[1] for s in screen]))


def worst_error(cal, raw, screen):
    return max(max(abs(a - b) for a, b in zip(touch.to_screen(*r, cal), s))
               for r, s in zip(raw, screen))


def target_screen(i, x, y):
    img = Image.new("RGB", (display.W, display.H), display.BG)
    d = ImageDraw.Draw(img)
    d.line((x - 14, y, x + 14, y), fill=(240, 80, 70), width=3)
    d.line((x, y - 14, x, y + 14), fill=(240, 80, 70), width=3)
    d.ellipse((x - 5, y - 5, x + 5, y + 5), outline=display.TEXT, width=2)
    d.text((display.W // 2, display.H // 2 + (40 if y < display.H // 2 else -40)),
           f"press the cross firmly  ({i}/{len(TARGETS)})", font=display.F_VALUE,
           fill=display.MUTED, anchor="mm")
    return img


def read_press(f):
    """Raw (x, y) of one firm press: the median after its first third, as in touch.py."""
    while True:
        xs, ys, down = [], [], False
        while True:
            _, _, etype, code, value = touch.EVENT.unpack(f.read(touch.EVENT.size))
            if etype == touch.EV_KEY and code == touch.BTN_TOUCH:
                if value == 1:
                    down, xs, ys = True, [], []
                elif down:
                    break
            elif etype == touch.EV_ABS and down:
                (xs if code == touch.ABS_X else ys if code == touch.ABS_Y else []).append(value)
        if min(len(xs), len(ys)) >= touch.MIN_SAMPLES * 2:  # a firm press, not a flick
            xs, ys = xs[len(xs) // 3:], ys[len(ys) // 3:]
            return sorted(xs)[len(xs) // 2], sorted(ys)[len(ys) // 2]


def write_conf(path, cal):
    """Into the settings file the app reads (the boot partition's, see config.path)."""
    return config.save({"TOUCH_CAL": ",".join(f"{v:.6f}" for v in cal)}, path)


def run(fb_dev, write_path=None):
    out = display.Framebuffer(fb_dev)
    dev = touch.find_touch_device()
    if not dev:
        raise SystemExit("no touch device found")
    raw = []
    with open(dev, "rb", buffering=0) as f:
        for i, (x, y) in enumerate(TARGETS, 1):
            out.show(target_screen(i, x, y))
            raw.append(read_press(f))
            print(f"target {i} at {x},{y}: raw {raw[-1]}", flush=True)
    cal = fit(raw, TARGETS)
    err = worst_error(cal, raw, TARGETS)
    print(f"TOUCH_CAL={','.join(f'{v:.6f}' for v in cal)}   (worst error {err} px)")
    if write_path:
        print(f"written to {write_conf(write_path, cal)}")
    out.show(display.render_message("CALIBRATED", f"worst error {err} px", "touch"))

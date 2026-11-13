"""Places for the cat on the NO LINK and desk-mode screens: the server closet, the desk, the
window (the weather happens outside it) and the rooftop at night.

Drawn in 5 px cells, like the cat, kept dim so the text on them stays readable, and cached:
each background is drawn once (a few, for the blinking LEDs and the falling rain). Every
place has a surface at the height of the cat's feet that runs the whole width of the
screen (the closet's long shelf, the desk, the window sill, the rooftop ledge): the cat
walks, plays its show and chases the laser dot along it."""

import math
import random

from PIL import Image, ImageDraw

W, H, K = 480, 320, 5
GW, GH = W // K, H // K        # 96 x 64 cells
TOP = 8                        # first row under the top bar
BG = (12, 14, 18)

# the surface each place puts under the cat's feet (row), matching display's cat positions
SHELF_ROW = 46                 # NO LINK: the cat at y 140, 18 rows tall
DESK_ROW = 48                  # desk mode: the cats at y 150
LEDGE_ROW = 46                 # the night screen: the cat at y 140

_cache = {}


def _scene(key, paint):
    if key not in _cache:
        if len(_cache) > 40:
            _cache.pop(next(iter(_cache)))
        im = Image.new("RGB", (GW, GH), BG)
        paint(ImageDraw.Draw(im))
        _cache[key] = im.resize((W, H), Image.NEAREST)
    return _cache[key].copy()


# --- NO LINK: the server closet ----------------------------------------------------------------

SIGN = (46, 21, 86, 41)        # the panel on the wall behind the NO LINK text (cells)


def closet(frame=0):
    """A long shelf over a row of rack units, the port LEDs blinking (two frames); a dark
    panel on the wall for the message."""
    def paint(d):
        d.rectangle((0, 0, GW, GH), fill=(22, 25, 32))                   # wall
        for x in range(0, GW, 12):
            d.line((x, TOP, x, SHELF_ROW - 1), fill=(26, 29, 37))         # wall panels
        d.rectangle(SIGN, fill=(28, 31, 39), outline=(44, 49, 60))
        d.point((SIGN[2] - 2, SIGN[1] + 2), fill=(60, 200, 110))         # the sign's LED
        # the long shelf: the top of a row of rack units, the whole width
        d.rectangle((0, SHELF_ROW, GW, SHELF_ROW + 1), fill=(70, 76, 88))
        d.rectangle((0, SHELF_ROW + 2, GW, GH), fill=(38, 42, 50))
        rnd = random.Random(4 + frame)
        for y in (SHELF_ROW + 4, SHELF_ROW + 9, SHELF_ROW + 14):
            if y + 3 >= GH:
                break
            for x0 in (2, 36):                                           # two units a row
                d.rectangle((x0, y, x0 + 31, y + 3), fill=(28, 31, 38), outline=(50, 55, 66))
                for x in range(x0 + 2, x0 + 30, 2):
                    d.point((x, y + 1), fill=(14, 15, 19))               # the ports
                    if rnd.random() < 0.45:
                        d.point((x, y + 2), fill=rnd.choice([(60, 200, 110), (60, 200, 110),
                                                            (230, 170, 40)]))
        d.line([(70, SHELF_ROW + 6), (72, SHELF_ROW + 10), (76, SHELF_ROW + 12), (79, GH)],
               fill=(70, 130, 230))                                       # patch cables
        d.line([(70, SHELF_ROW + 11), (73, SHELF_ROW + 14), (84, GH)], fill=(230, 190, 60))
    return _scene(("closet", frame % 2), paint)


# --- desk mode, the clock: the desk --------------------------------------------------------------

SCREEN = (46, 12, 91, 41)      # the monitor's screen: the clock and the date go on it (cells)


STEAM_FRAMES = 4  # the mug's steam: a loop, a step a second (desk mode redraws once a second)


def _steam(d, frame):
    """Two wisps over the mug, swaying as they rise a cell a frame, fading towards the top."""
    top = DESK_ROW - 9
    for base, phase in ((38, 0), (41, 2)):
        for h in range(6):                             # cells above the mug, bottom up
            if (h + phase - frame) % STEAM_FRAMES == 3:
                continue                               # a gap: the wisp breaks as it rises
            x = base + round(0.8 * math.sin((h - frame + phase) * math.pi / 2))
            fade = 1 - h / 6
            d.point((x, top - h), fill=tuple(round(24 + (c - 24) * fade) for c in (128, 130, 140)))


def desk(frame=0):
    """The desk top the whole width, a monitor with a glowing screen, a keyboard, a mug
    with steam (it rises: STEAM_FRAMES), a plant."""
    frame %= STEAM_FRAMES
    def paint(d):
        d.rectangle((0, 0, GW, GH), fill=(24, 22, 26))                   # a warm wall
        d.ellipse((-20, -10, 40, 40), fill=(30, 27, 30))                 # lamp light on it
        d.rectangle((0, DESK_ROW, GW, GH), fill=(58, 40, 28))            # desk top
        d.line((0, DESK_ROW, GW, DESK_ROW), fill=(86, 60, 40))
        d.rectangle((0, DESK_ROW + 4, GW, GH), fill=(44, 30, 22))        # desk front
        x0, y0, x1, y1 = SCREEN
        d.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2), fill=(18, 19, 24), outline=(60, 62, 70))
        d.rectangle(SCREEN, fill=(22, 34, 56))                           # the screen's glow
        d.rectangle((66, y1 + 3, 71, DESK_ROW - 2), fill=(50, 52, 60))   # stand
        d.rectangle((60, DESK_ROW - 2, 77, DESK_ROW - 1), fill=(60, 62, 70))
        d.rectangle((48, DESK_ROW + 1, 74, DESK_ROW + 3), fill=(40, 42, 48), outline=(60, 62, 70))
        for x in range(50, 73, 2):
            d.point((x, DESK_ROW + 2), fill=(70, 72, 80))                # keyboard keys
        d.rectangle((37, DESK_ROW - 7, 42, DESK_ROW - 1), fill=(200, 200, 205), outline=(120, 120, 128))
        d.arc((41, DESK_ROW - 5, 45, DESK_ROW - 2), 270, 90, fill=(160, 160, 168))   # mug
        _steam(d, frame)
        d.rectangle((84, DESK_ROW - 5, 90, DESK_ROW - 1), fill=(150, 80, 50))   # plant pot
        for dx, dy in ((-2, -3), (0, -5), (2, -4), (4, -2), (-3, -1)):
            d.line((87, DESK_ROW - 4, 87 + dx, DESK_ROW - 4 + dy), fill=(60, 140, 70))
    return _scene(("desk", frame), paint)


# --- desk mode, the weather: the window ------------------------------------------------------------

WINDOW = (43, 10, 93, 47)      # cells: where the weather board was (display.BOARD_BOX)


def window(kind="sun", night=False, frame=0):
    """The window sill the whole width, and the weather outside: kind as in weather.KINDS,
    night (moon and stars when clear). Rain and snow fall (three frames)."""
    wet = kind in ("rain", "snow", "storm")

    def paint(d):
        d.rectangle((0, 0, GW, GH), fill=(26, 28, 34))                   # wall
        x0, y0, x1, y1 = WINDOW
        clear = kind in ("sun", "part")
        if night:
            sky = (16, 22, 40) if clear else (24, 27, 36)
        else:
            sky = {"sun": (52, 78, 112), "part": (56, 80, 110), "fog": (70, 76, 86),
                   "storm": (40, 44, 56)}.get(kind, (52, 58, 70))
        d.rectangle(WINDOW, fill=sky)
        rnd = random.Random(11)
        if night and clear:
            for _ in range(18):
                d.point((rnd.randrange(x0 + 1, x1), rnd.randrange(y0 + 1, y0 + 20)), fill=(190, 190, 210))
            d.ellipse((x0 + 3, y0 + 10, x0 + 11, y0 + 18), fill=(235, 225, 170))     # the moon,
            d.ellipse((x0 + 6, y0 + 8, x0 + 14, y0 + 16), fill=sky)                 # a crescent
        elif clear:
            d.ellipse((x0 + 3, y0 + 10, x0 + 11, y0 + 18), fill=(255, 205, 70))     # the sun
        cloud = (200, 208, 222) if not night else (70, 76, 92)
        heavy = (98, 104, 118) if not night else (52, 56, 68)
        if kind == "part":
            for cx, cy in ((x0 + 9, y0 + 15), (x0 + 22, y0 + 6)):
                d.ellipse((cx, cy, cx + 12, cy + 5), fill=cloud)
                d.ellipse((cx + 3, cy - 2, cx + 9, cy + 3), fill=cloud)
        elif kind == "sun" and not night:
            d.ellipse((x0 + 16, y0 + 20, x0 + 25, y0 + 23), fill=cloud)       # clear of the text
        elif kind in ("cloud", "rain", "snow", "storm"):
            for cx in range(x0 - 2, x1, 11):  # kept inside the glass
                d.ellipse((max(cx, x0 + 1), y0 + 1, min(cx + 16, x1 - 1), y0 + 9),
                          fill=heavy if kind != "cloud" else cloud)
        elif kind == "fog":
            for y in range(y0 + 6, y1 - 4, 4):
                d.line((x0 + 1, y, x1 - 1, y), fill=(110, 116, 126))
        if kind == "storm":
            bx = x0 + 16
            d.line(((bx, y0 + 9), (bx - 2, y0 + 14), (bx + 1, y0 + 14), (bx - 2, y0 + 20)),
                   fill=(255, 205, 70))
        for x, h in ((x0 + 2, 6), (x0 + 9, 9), (x0 + 17, 5), (x0 + 24, 8), (x0 + 33, 6), (x0 + 42, 10)):
            d.rectangle((x, y1 - h, min(x + 6, x1 - 1), y1 - 1), fill=(30, 35, 46))   # rooftops
            if night:
                for wy in range(y1 - h + 1, y1 - 1, 2):
                    if rnd.random() < 0.3:
                        d.point((x + 2, wy), fill=(150, 130, 70))
        if wet:  # falling in the glass's clear part: left of the numbers, between the lines
            drops = random.Random(frame % 3)
            for _ in range(18):
                x, y = drops.randrange(x0 + 1, x0 + 26), drops.randrange(y0 + 10, y1 - 6)
                if kind == "snow":
                    d.point((x, y), fill=(235, 238, 245))
                else:
                    d.line((x, y, x, y + 1), fill=(120, 170, 230))
        d.rectangle((x0 - 1, y0 - 1, x1 + 1, y1 + 1), outline=(120, 100, 80))   # frame
        d.rectangle((0, DESK_ROW, GW, DESK_ROW + 2), fill=(140, 116, 92))       # the sill
        d.line((0, DESK_ROW + 2, GW, DESK_ROW + 2), fill=(100, 80, 62))
        d.rectangle((0, DESK_ROW + 3, GW, GH), fill=(20, 21, 26))
    return _scene(("window", kind, bool(night), (frame % 3) if wet else 0), paint)


# --- the night screen: the rooftop --------------------------------------------------------------------

def rooftop(frame=0):
    """A ledge the whole width, the sleeping skyline with a few lit windows, an antenna's red
    light, stars (they twinkle: two frames) and the moon."""
    def paint(d):
        for y in range(GH):
            t = y / GH
            d.line((0, y, GW, y), fill=(int(10 + 10 * t), int(14 + 12 * t), int(30 + 16 * t)))
        rnd = random.Random(9)
        for _ in range(40):
            x, y = rnd.randrange(GW), rnd.randrange(TOP, 30)
            if (x + y + frame) % 7:
                d.point((x, y), fill=(200, 200, 220) if rnd.random() < .5 else (120, 124, 150))
        d.ellipse((80, 10, 89, 19), fill=(235, 225, 170))
        d.ellipse((83, 8, 92, 17), fill=(13, 17, 34))                     # the moon
        lit = random.Random(3)
        for x, h, w in ((2, 9, 9), (12, 13, 8), (22, 7, 10), (34, 11, 7), (42, 14, 9),
                        (52, 8, 8), (61, 12, 10), (72, 16, 9), (82, 10, 8), (90, 13, 6)):
            d.rectangle((x, LEDGE_ROW - h, x + w, LEDGE_ROW), fill=(20, 24, 36))
            for wy in range(LEDGE_ROW - h + 2, LEDGE_ROW - 1, 3):
                for wx in range(x + 1, x + w - 1, 2):
                    if lit.random() < 0.25:
                        d.point((wx, wy), fill=(150, 130, 70))
        d.line((93, LEDGE_ROW - 13, 93, LEDGE_ROW - 19), fill=(60, 64, 80))
        d.point((93, LEDGE_ROW - 20), fill=(200, 60, 60) if frame % 2 else (90, 30, 30))   # antenna
        d.rectangle((0, LEDGE_ROW, GW, LEDGE_ROW + 1), fill=(48, 52, 64))  # our ledge
        d.rectangle((0, LEDGE_ROW + 2, GW, GH), fill=(30, 32, 40))
    return _scene(("rooftop", frame % 2), paint)

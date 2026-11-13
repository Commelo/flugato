"""The pet: a chubby tamagotchi-style tabby whose mood follows the test results.

Everything is drawn on a 28 x 18 cell canvas: the bodies with Pillow primitives, the faces
as cell overlays, then scaled with whole pixels so it stays chunky at any size. No image
files. The module is meant to be ported as is:

    cells(mood, pose="sit", frame=0) -> list of 18 strings (one letter per cell)
    sprite(mood, frame=0, scale=2, pose="sit") -> RGBA image (cached)
    cat(mood, frame) -> cells, the round-1 API (sit pose, tail swish, blink every 4th frame)

    IDLE_BASE / IDLE_ACTS   the cat on the results screen (0.5 s ticks)
    INLINE_BASE / INLINE_ACTS  inline mode: glasses and a laptop, easily distracted (0.5 s)
    ROUTER_BASE / ROUTER_ACTS  Wi-Fi router mode: fitting an antenna, messing with cables (0.5 s)
    SLEEP_BASE / SLEEP_ACTS the big cat on the NO LINK screen (0.25 s ticks)
    POKE                    reaction to a touch on the NO LINK screen (0.25 s ticks)
    Idle(...)               tick scheduler: base loop, random acts, play(POKE)

Sequences are lists of (pose, ticks, dx): show `pose` for `ticks` ticks, moving the cat by
`dx` cells per tick (walking). A pose name ending in "_L" faces left (mirrored)."""

import random

from PIL import Image, ImageDraw

PAL = {
    "o": (236, 150, 60),    # orange fur
    "d": (62, 34, 18),      # outline, stripes, whiskers
    "w": (250, 240, 225),   # muzzle, belly, eye whites
    "k": (20, 16, 16),      # eyes, mouth
    "p": (235, 120, 140),   # nose, tongue, inner ear
    "g": (60, 230, 110),    # eyes glowing (port LED on)
    "y": (130, 200, 255),   # sweat drop
    "r": (255, 245, 210),   # alarm mark
    "c": (210, 220, 235),   # z's and other text
    "b": (20, 24, 30),      # bubble background
    "s": (200, 120, 50),    # second fur colour (lineup designs)
    "e": (20, 16, 16),      # eye colour when not black (lineup designs)
    "m": (150, 155, 165),   # metal: laptop lid, antenna base, cable plug
    "l": (120, 200, 255),   # laptop logo light
    "u": (70, 130, 230),    # cable
    "x": (196, 158, 98),    # cardboard box
    "Y": (255, 205, 70),    # the big "?" after the pounce
}
CW, CH = 28, 18


# --- cell canvas ---------------------------------------------------------------------------

def _canvas():
    return [["."] * CW for _ in range(CH)]


def _draw_cells(cells, fn):
    """Draws with Pillow on an L image, one grey level per palette letter, then copies the
    letters into the cell grid."""
    keys = list(PAL)
    im = Image.new("L", (CW, CH), 0)
    d = ImageDraw.Draw(im)
    fn(d, {k: i + 1 for i, k in enumerate(keys)})
    px = im.load()
    for y in range(CH):
        for x in range(CW):
            v = px[x, y]
            if v:
                cells[y][x] = keys[v - 1]


def _put(g, x, y, ch):
    if 0 <= y < CH and 0 <= x < CW:
        g[y][x] = ch


# --- bodies ---------------------------------------------------------------------------------
# Each body is a painter plus landmarks: eye_l, eye_r (top-left of a 2x2 eye), nose (x, y),
# mouth (cx, y), drop (x, y) for the sweat drop, mark (x) for the "!".

def _ears(d, c, kind, dy=0):
    if kind == "up":
        d.polygon(((4, 6 + dy), (6, 0 + dy), (10, 4 + dy)), fill=c["o"], outline=c["d"])
        d.polygon(((15, 4 + dy), (19, 0 + dy), (21, 6 + dy)), fill=c["o"], outline=c["d"])
        d.point((6, 2 + dy), fill=c["p"]); d.point((19, 2 + dy), fill=c["p"])
    elif kind == "flat":        # pinned back (scared)
        d.polygon(((3, 8 + dy), (0, 2 + dy), (8, 5 + dy)), fill=c["o"], outline=c["d"])
        d.polygon(((17, 5 + dy), (25, 2 + dy), (22, 8 + dy)), fill=c["o"], outline=c["d"])
    elif kind == "twitch":      # left ear flicks sideways
        d.polygon(((4, 6 + dy), (9, 1 + dy), (11, 4 + dy)), fill=c["o"], outline=c["d"])
        d.polygon(((15, 4 + dy), (19, 0 + dy), (21, 6 + dy)), fill=c["o"], outline=c["d"])
        d.point((19, 2 + dy), fill=c["p"])


def _stripes(d, c, dy=0):
    d.line(((10, 4 + dy), (10, 6 + dy)), fill=c["d"])
    d.line(((12, 3 + dy), (12, 6 + dy)), fill=c["d"])
    d.line(((14, 4 + dy), (14, 6 + dy)), fill=c["d"])


def _whiskers(d, c, dy=0):
    d.line(((0, 10 + dy), (2, 11 + dy)), fill=c["d"]); d.line(((0, 13 + dy), (2, 12 + dy)), fill=c["d"])
    d.line(((23, 11 + dy), (25, 10 + dy)), fill=c["d"]); d.line(((23, 12 + dy), (25, 13 + dy)), fill=c["d"])


def _tail(d, c, frame):
    if frame % 2 == 0:
        d.arc((21, 5, 28, 14), 270, 90, fill=c["d"], width=3)
        d.arc((22, 6, 27, 13), 270, 90, fill=c["o"], width=1)
    else:
        d.arc((22, 8, 28, 17), 250, 60, fill=c["d"], width=3)
        d.arc((23, 9, 27, 16), 250, 60, fill=c["o"], width=1)


def body_sit(d, c, ears="up", tail=0, wide=False, hop=False):
    """The fat sitting cat, face on the body, white belly spilling over the feet."""
    dy = 1 if wide else (-1 if hop else 0)
    if wide:
        d.ellipse((0, 4, 25, 17), fill=c["o"], outline=c["d"])
        d.ellipse((4, 13, 21, 19), fill=c["w"], outline=c["d"])
        d.ellipse((2, 15, 7, 18), fill=c["o"], outline=c["d"])
        d.ellipse((18, 15, 23, 18), fill=c["o"], outline=c["d"])
    elif hop:
        d.ellipse((1, 2, 24, 15), fill=c["o"], outline=c["d"])
        d.ellipse((5, 11, 20, 17), fill=c["w"], outline=c["d"])
        d.rectangle((4, 15, 7, 17), fill=c["o"], outline=c["d"])       # legs out
        d.rectangle((18, 15, 21, 17), fill=c["o"], outline=c["d"])
    else:
        d.ellipse((1, 3, 24, 17), fill=c["o"], outline=c["d"])
        d.ellipse((5, 13, 20, 19), fill=c["w"], outline=c["d"])
        d.ellipse((3, 15, 8, 18), fill=c["o"], outline=c["d"])
        d.ellipse((17, 15, 22, 18), fill=c["o"], outline=c["d"])
    _ears(d, c, ears, dy)
    _stripes(d, c, dy)
    d.ellipse((8, 9 + dy, 17, 13 + dy), fill=c["w"])
    _whiskers(d, c, dy)
    _tail(d, c, tail)


SIT_LM = dict(eye_l=(7, 7), eye_r=(17, 7), nose=(12, 10), mouth=(13, 12), drop=(24, 1), mark=26)


def body_loaf(d, c, inhale=False, ears="up"):
    """Lying flat: one big bun, face on the left, belly on the right, tail along the floor."""
    top = 4 if inhale else 5
    d.ellipse((0, top, 27, 17), fill=c["o"], outline=c["d"])
    e = 1 if inhale else 0
    if ears == "flat":
        d.polygon(((3, 8 - e), (0, 3 - e), (7, 6 - e)), fill=c["o"], outline=c["d"])
        d.polygon(((9, 6 - e), (16, 3 - e), (14, 8 - e)), fill=c["o"], outline=c["d"])
    else:
        d.polygon(((3, 8 - e), (4, 2 - e), (8, 6 - e)), fill=c["o"], outline=c["d"])
        d.polygon(((11, 6 - e), (14, 2 - e), (16, 8 - e)), fill=c["o"], outline=c["d"])
    for x, y0 in ((8, 6), (10, 5), (12, 6)):
        d.line(((x, y0 - e), (x, 8 - e)), fill=c["d"])
    d.ellipse((4, 11, 13, 15), fill=c["w"])
    if inhale:
        d.ellipse((14, 7, 26, 15), fill=c["w"])
    else:
        d.ellipse((15, 8, 25, 15), fill=c["w"])
    d.rectangle((14, 15, 25, 17), fill=c["o"], outline=c["d"])
    d.ellipse((23, 14, 27, 17), fill=c["o"], outline=c["d"])
    d.line(((15, 16), (24, 16)), fill=c["o"])
    d.line(((0, 12), (2, 13)), fill=c["d"]); d.line(((0, 15), (2, 14)), fill=c["d"])


LOAF_LM = dict(eye_l=(4, 9), eye_r=(11, 9), nose=(8, 12), mouth=(9, 14), drop=(14, 0), mark=17)


def body_stretch(d, c):
    """Front down, bottom up, tail straight up, front paws out: the long cat."""
    d.rectangle((22, 0, 25, 6), fill=c["d"]); d.rectangle((23, 0, 24, 5), fill=c["o"])   # tail up
    d.polygon(((6, 17), (12, 9), (20, 4), (27, 7), (27, 17)), fill=c["o"], outline=c["d"])   # rising back
    d.ellipse((17, 3, 27, 14), fill=c["o"], outline=c["d"])              # bottom in the air
    d.rectangle((18, 8, 26, 14), fill=c["o"])
    d.ellipse((10, 10, 26, 17), fill=c["w"], outline=c["d"])             # belly hanging
    d.rectangle((11, 11, 24, 14), fill=c["w"])
    d.ellipse((0, 8, 12, 17), fill=c["o"], outline=c["d"])               # head, low, at the front
    d.polygon(((1, 11), (2, 5), (5, 9)), fill=c["o"], outline=c["d"])
    d.polygon(((7, 9), (10, 5), (11, 11)), fill=c["o"], outline=c["d"])
    d.line(((6, 9), (6, 10)), fill=c["d"])
    d.ellipse((2, 13, 9, 17), fill=c["w"])
    d.rectangle((0, 15, 4, 17), fill=c["o"], outline=c["d"])             # paws stretched forward
    d.rectangle((7, 15, 11, 17), fill=c["o"], outline=c["d"])


STRETCH_LM = dict(eye_l=(2, 11), eye_r=(8, 11), nose=(5, 13), mouth=(6, 15), drop=(14, 1), mark=27)


# --- side view: walking (strut) and stalking (prowl), facing right; "_L" poses mirror them ---
# A cat walks in diagonal pairs: pair A = near back + far front, pair B = far back + near front.
# Per frame (4 a step) the feet on the ground move back as far as the body moves forward (one
# cell a tick), so they don't slide; the swinging pair is lifted one cell. Far legs in the
# second fur colour, behind the body.
_STRIDE_A, _STRIDE_B = (2, 0, -2, 0), (-2, 0, 2, 0)
_LIFT_A, _LIFT_B = (0, 0, 0, 1), (0, 1, 0, 0)


def _side_leg(d, c, x, top, bottom, fur):
    d.rectangle((x, top, x + 2, bottom), fill=c[fur], outline=c["d"])
    d.line((x, bottom, x + 3, bottom), fill=c["d"])                 # the paw, a touch forward


def _side_tail(d, c, pts, sway):
    pts = [(x + (sway if i >= 2 else 0), y) for i, (x, y) in enumerate(pts)]
    d.line(pts, fill=c["d"], width=3, joint="curve")
    d.line(pts, fill=c["o"], width=1, joint="curve")


def body_strut(d, c, f=None):
    """Walking in profile: head level, tail high with a curl. f 0-3: the step; None: standing."""
    bob = 0 if f is None else (1, 0, 1, 0)[f]
    a = 0 if f is None else _STRIDE_A[f]
    b = 0 if f is None else _STRIDE_B[f]
    la = 0 if f is None else _LIFT_A[f]
    lb = 0 if f is None else _LIFT_B[f]
    _side_tail(d, c, [(5, 8 + bob), (3, 5), (2, 2), (3, 0), (5, 0)], 0 if f is None else (0, 1, 0, -1)[f])
    _side_leg(d, c, 8 + b, 12 + bob, 16 - lb, "s")                   # far back
    _side_leg(d, c, 18 + a, 12 + bob, 16 - la, "s")                  # far front
    d.ellipse((3, 5 + bob, 21, 14 + bob), fill=c["o"], outline=c["d"])
    d.ellipse((7, 10 + bob, 18, 15 + bob), fill=c["w"], outline=c["d"])
    for x in (9, 12, 15):
        d.line((x, 6 + bob, x, 8 + bob), fill=c["d"])
    _side_leg(d, c, 5 + a, 12 + bob, 17 - la, "o")                   # near back
    _side_leg(d, c, 15 + b, 12 + bob, 17 - lb, "o")                  # near front
    d.ellipse((17, 1 + bob, 26, 10 + bob), fill=c["o"], outline=c["d"])
    d.polygon(((18, 4 + bob), (19, 0 + bob), (21, 2 + bob)), fill=c["o"], outline=c["d"])
    d.polygon(((22, 2 + bob), (24, 0 + bob), (25, 4 + bob)), fill=c["o"], outline=c["d"])
    d.point((19, 2 + bob), fill=c["p"])
    d.ellipse((22, 5 + bob, 26, 9 + bob), fill=c["w"])
    d.line((25, 7 + bob, 27, 6 + bob), fill=c["d"])
    d.line((25, 8 + bob, 27, 9 + bob), fill=c["d"])


def _strut_lm(bob):
    eye = (21, 4 + bob)  # one eye in profile: both landmarks on it
    return dict(eye_l=eye, eye_r=eye, nose=(25, 6 + bob), mouth=(25, 8 + bob), drop=(16, 0), mark=27)


def body_prowl(d, c, f=0):
    """Stalking in profile (the laser dot): head low and forward, long strides, tail low."""
    bob = (1, 0, 1, 0)[f]
    a, b = _STRIDE_A[f], _STRIDE_B[f]
    _side_tail(d, c, [(4, 9 + bob), (2, 9), (1, 7), (0, 5), (1, 3)], (0, 1, 0, -1)[f])
    _side_leg(d, c, 7 + b, 12 + bob, 16 - _LIFT_B[f], "s")
    _side_leg(d, c, 18 + a, 12 + bob, 16 - _LIFT_A[f], "s")
    d.ellipse((3, 6 + bob, 22, 14 + bob), fill=c["o"], outline=c["d"])
    d.ellipse((7, 10 + bob, 19, 15 + bob), fill=c["w"], outline=c["d"])
    for x in (8, 11, 14, 17):
        d.line((x, 7 + bob, x, 9 + bob), fill=c["d"])
    _side_leg(d, c, 5 + a, 12 + bob, 17 - _LIFT_A[f], "o")
    _side_leg(d, c, 16 + b, 12 + bob, 17 - _LIFT_B[f], "o")
    d.ellipse((18, 5 + bob, 27, 13 + bob), fill=c["o"], outline=c["d"])
    d.polygon(((19, 7 + bob), (21, 3 + bob), (22, 6 + bob)), fill=c["o"], outline=c["d"])
    d.polygon(((23, 6 + bob), (25, 3 + bob), (26, 7 + bob)), fill=c["o"], outline=c["d"])
    d.ellipse((23, 8 + bob, 27, 12 + bob), fill=c["w"])


def _prowl_lm(bob):
    eye = (22, 7 + bob)
    return dict(eye_l=eye, eye_r=eye, nose=(25, 9 + bob), mouth=(26, 11 + bob), drop=(17, 2), mark=27)


# --- the pounce on the laser dot, in profile: crouch and wiggle, leap, pin it, peek -------------

def body_crouch(d, c, wiggle=0):
    """Low, legs folded, head down and forward; wiggle 1 lifts the rear (the butt wiggle)."""
    _side_tail(d, c, [(4, 11), (2, 11), (1, 9 - wiggle), (0, 8 - wiggle), (1, 7 - wiggle)], 0)
    _side_leg(d, c, 8, 14, 16, "s")
    _side_leg(d, c, 19, 14, 16, "s")
    d.ellipse((3, 8 - wiggle, 13, 15), fill=c["o"], outline=c["d"])           # the rear
    d.ellipse((7, 9, 22, 16), fill=c["o"], outline=c["d"])
    d.ellipse((9, 13, 19, 16), fill=c["w"], outline=c["d"])
    for x in (7, 10, 13, 16):
        d.line((x, 9 + (x < 11) * -wiggle, x, 11), fill=c["d"])
    _side_leg(d, c, 5, 14, 17, "o")
    _side_leg(d, c, 16, 15, 17, "o")
    d.ellipse((18, 8, 27, 16), fill=c["o"], outline=c["d"])
    d.polygon(((19, 10), (21, 6), (22, 9)), fill=c["o"], outline=c["d"])
    d.polygon(((23, 9), (25, 6), (26, 10)), fill=c["o"], outline=c["d"])
    d.ellipse((23, 11, 27, 15), fill=c["w"])


CROUCH_LM = dict(eye_l=(22, 10), eye_r=(22, 10), nose=(25, 12), mouth=(26, 14), drop=(17, 4), mark=27)


def body_leap(d, c, stage=0):
    """Stage 0: taking off, the front rising, the back legs pushing; 1: in the air, stretched
    out at the top of the arc, front paws reaching down for the dot."""
    if stage == 0:
        _side_tail(d, c, [(4, 12), (2, 13), (1, 15), (0, 16), (0, 17)], 0)
        d.line((7, 13, 3, 17), fill=c["d"], width=3); d.line((7, 13, 3, 17), fill=c["s"])  # far back leg
        d.ellipse((3, 8, 15, 16), fill=c["o"], outline=c["d"])
        d.ellipse((9, 4, 22, 13), fill=c["o"], outline=c["d"])
        d.ellipse((10, 9, 19, 14), fill=c["w"], outline=c["d"])
        d.line((6, 14, 2, 17), fill=c["d"], width=3); d.line((6, 14, 2, 17), fill=c["o"])  # near back leg
        _side_leg(d, c, 18, 10, 13, "o")                                            # front paws tucked
        d.ellipse((17, 0, 26, 9), fill=c["o"], outline=c["d"])
        d.polygon(((18, 3), (18, 0), (21, 1)), fill=c["o"], outline=c["d"])
        d.polygon(((22, 1), (25, 0), (25, 3)), fill=c["o"], outline=c["d"])
        d.ellipse((22, 4, 26, 8), fill=c["w"])
    else:
        _side_tail(d, c, [(4, 4), (2, 3), (1, 1), (0, 0), (0, 0)], 0)
        d.line((6, 7, 1, 9), fill=c["d"], width=3); d.line((6, 7, 1, 9), fill=c["o"])    # back legs out
        d.ellipse((3, 1, 21, 9), fill=c["o"], outline=c["d"])
        d.ellipse((8, 5, 18, 10), fill=c["w"], outline=c["d"])
        for x in (8, 11, 14):
            d.line((x, 2, x, 4), fill=c["d"])
        d.line((19, 8, 24, 12), fill=c["d"], width=3); d.line((19, 8, 24, 12), fill=c["o"])  # front paws
        d.line((24, 12, 26, 12), fill=c["d"])
        d.ellipse((18, 1, 27, 9), fill=c["o"], outline=c["d"])
        d.polygon(((19, 3), (20, 0), (22, 2)), fill=c["o"], outline=c["d"])
        d.polygon(((23, 2), (25, 0), (26, 3)), fill=c["o"], outline=c["d"])
        d.ellipse((23, 4, 27, 8), fill=c["w"])


LEAP_LM = (dict(eye_l=(21, 3), eye_r=(21, 3), nose=(25, 5), mouth=(25, 7), drop=(15, 0), mark=27),
           dict(eye_l=(22, 3), eye_r=(22, 3), nose=(26, 5), mouth=(26, 7), drop=(15, 0), mark=27))


def body_pin(d, c, peek=False):
    """Landed: both front paws flat on the dot, rear in the air, head down between them;
    peek lifts the near paw a little to look under it."""
    _side_tail(d, c, [(5, 6), (3, 4), (2, 2), (2, 0), (4, 0)], 0)
    _side_leg(d, c, 6, 11, 16, "s")
    d.ellipse((3, 3, 18, 12), fill=c["o"], outline=c["d"])                    # rear up
    d.ellipse((7, 8, 16, 13), fill=c["w"], outline=c["d"])
    for x in (7, 10, 13):
        d.line((x, 4, x, 6), fill=c["d"])
    _side_leg(d, c, 4, 11, 17, "o")
    d.ellipse((13, 8, 22, 16), fill=c["o"], outline=c["d"])                   # head down
    d.polygon(((14, 10), (15, 6), (17, 9)), fill=c["o"], outline=c["d"])
    d.polygon(((18, 9), (20, 6), (21, 10)), fill=c["o"], outline=c["d"])
    d.ellipse((18, 11, 22, 15), fill=c["w"])
    d.rectangle((20, 15, 26, 17), fill=c["s"], outline=c["d"])                # far paw on the floor
    up = 2 if peek else 0
    d.rectangle((19, 14 - up, 25, 16 - up), fill=c["o"], outline=c["d"])     # near paw, on it


PIN_LM = dict(eye_l=(17, 10), eye_r=(17, 10), nose=(21, 12), mouth=(21, 14), drop=(12, 0), mark=27)

BODIES = {
    "sit": (lambda d, c: body_sit(d, c), SIT_LM),
    "sit1": (lambda d, c: body_sit(d, c, tail=1), SIT_LM),
    "wide": (lambda d, c: body_sit(d, c, wide=True), dict(SIT_LM, eye_l=(7, 8), eye_r=(17, 8), nose=(12, 11), mouth=(13, 13))),
    "hop": (lambda d, c: body_sit(d, c, hop=True), dict(SIT_LM, eye_l=(7, 6), eye_r=(17, 6), nose=(12, 9), mouth=(13, 11))),
    "scared": (lambda d, c: body_sit(d, c, ears="flat"), SIT_LM),
    "twitch": (lambda d, c: body_sit(d, c, ears="twitch"), SIT_LM),
    "loaf": (lambda d, c: body_loaf(d, c), LOAF_LM),
    "loaf_in": (lambda d, c: body_loaf(d, c, inhale=True), LOAF_LM),
    "loaf_scared": (lambda d, c: body_loaf(d, c, ears="flat"), LOAF_LM),
    "stretch": (lambda d, c: body_stretch(d, c), STRETCH_LM),
    "stand": (lambda d, c: body_strut(d, c), _strut_lm(0)),
    **{f"walk{i + 1}": ((lambda i: lambda d, c: body_strut(d, c, i))(i), _strut_lm((1, 0, 1, 0)[i]))
       for i in range(4)},
    **{f"prowl{i + 1}": ((lambda i: lambda d, c: body_prowl(d, c, i))(i), _prowl_lm((1, 0, 1, 0)[i]))
       for i in range(4)},
    "crouch": (lambda d, c: body_crouch(d, c), CROUCH_LM),
    "crouch_up": (lambda d, c: body_crouch(d, c, 1), CROUCH_LM),
    "leap1": (lambda d, c: body_leap(d, c, 0), LEAP_LM[0]),
    "leap2": (lambda d, c: body_leap(d, c, 1), LEAP_LM[1]),
    "pin": (lambda d, c: body_pin(d, c), PIN_LM),
    "pin_peek": (lambda d, c: body_pin(d, c, True), PIN_LM),
}


# --- faces ---------------------------------------------------------------------------------

def _eye(g, x, y, kind):
    if kind == "open":
        for dx in (0, 1):
            for dy in (0, 1):
                _put(g, x + dx, y + dy, "k")
        _put(g, x, y, "w")
    elif kind == "happy":
        _put(g, x - 1, y + 1, "k"); _put(g, x, y, "k"); _put(g, x + 1, y, "k"); _put(g, x + 2, y + 1, "k")
    elif kind == "closed":
        for dx in (-1, 0, 1, 2):
            _put(g, x + dx, y + 1, "k")
    elif kind == "left":
        for dy in (0, 1):
            _put(g, x - 1, y + dy, "k"); _put(g, x, y + dy, "k"); _put(g, x + 1, y + dy, "w")
    elif kind == "right":
        for dy in (0, 1):
            _put(g, x, y + dy, "w"); _put(g, x + 1, y + dy, "k"); _put(g, x + 2, y + dy, "k")
    elif kind == "up":
        _put(g, x, y - 1, "k"); _put(g, x + 1, y - 1, "k"); _put(g, x, y, "k"); _put(g, x + 1, y, "k")
        _put(g, x, y + 1, "w"); _put(g, x + 1, y + 1, "w")
    elif kind == "wide":
        for dx in (-1, 0, 1, 2):
            for dy in (-1, 0, 1, 2):
                _put(g, x + dx, y + dy, "w")
        for dx in (0, 1):
            for dy in (0, 1):
                _put(g, x + dx, y + dy, "k")
    elif kind == "squint":
        _put(g, x - 1, y, "k"); _put(g, x, y, "k"); _put(g, x + 1, y, "k"); _put(g, x + 2, y, "k")
        _put(g, x, y + 1, "k"); _put(g, x + 1, y + 1, "k")
    elif kind == "annoyed":       # half-lidded
        for dx in (-1, 0, 1, 2):
            _put(g, x + dx, y, "k")
        _put(g, x, y + 1, "k"); _put(g, x + 1, y + 1, "k")
    elif kind == "glow":
        for dx in (0, 1):
            for dy in (0, 1):
                _put(g, x + dx, y + dy, "g")
    elif kind == "sad":           # v, the brows down
        _put(g, x - 1, y, "k"); _put(g, x, y + 1, "k"); _put(g, x + 1, y + 1, "k"); _put(g, x + 2, y, "k")


def _mouth(g, cx, y, kind):
    if kind == "cat":
        _put(g, cx - 3, y - 1, "k"); _put(g, cx - 2, y, "k"); _put(g, cx - 1, y - 1, "k"); _put(g, cx, y, "k"); _put(g, cx + 1, y - 1, "k")
    elif kind == "smile":
        _put(g, cx - 3, y - 1, "k"); _put(g, cx - 2, y, "k"); _put(g, cx - 1, y, "k"); _put(g, cx, y, "k"); _put(g, cx + 1, y - 1, "k")
    elif kind == "flat":
        for x in (cx - 2, cx - 1, cx):
            _put(g, x, y, "k")
    elif kind == "tiny":
        _put(g, cx - 1, y, "k")
    elif kind == "open":
        for x in (cx - 2, cx - 1, cx):
            _put(g, x, y - 1, "k"); _put(g, x, y, "k")
        _put(g, cx - 1, y, "p")
    elif kind == "yawn":          # big
        for x in (cx - 3, cx - 2, cx - 1, cx, cx + 1):
            for yy in (y - 2, y - 1, y):
                _put(g, x, yy, "k")
        _put(g, cx - 2, y, "p"); _put(g, cx - 1, y, "p"); _put(g, cx, y, "p")
    elif kind == "wavy":
        _put(g, cx - 3, y, "k"); _put(g, cx - 2, y - 1, "k"); _put(g, cx - 1, y, "k"); _put(g, cx, y - 1, "k"); _put(g, cx + 1, y, "k")
    elif kind == "tongue":
        _put(g, cx - 3, y - 1, "k"); _put(g, cx - 2, y, "k"); _put(g, cx - 1, y, "k"); _put(g, cx, y, "k"); _put(g, cx + 1, y - 1, "k")
        _put(g, cx - 1, y + 1, "p"); _put(g, cx, y + 1, "p"); _put(g, cx - 1, y + 2, "p"); _put(g, cx, y + 2, "p")


def _paw_lick(g, lm, up):
    """A paw raised over the left half of the muzzle; the tongue shows when up is False."""
    cx, my = lm["mouth"]
    x0, y0 = cx - 5, my - (4 if up else 3)
    for y in range(y0, y0 + 5):
        for x in range(x0, x0 + 5):
            _put(g, x, y, "o")
    for x in range(x0, x0 + 5):                                      # outline top and bottom
        _put(g, x, y0, "d"); _put(g, x, y0 + 4, "d")
    for y in range(y0, y0 + 5):
        _put(g, x0 + 4, y, "d")                                      # outline on the muzzle side
    _put(g, x0 + 1, y0 + 1, "d"); _put(g, x0 + 3, y0 + 1, "d")        # toes
    if not up:
        _put(g, x0 + 5, my - 1, "p"); _put(g, x0 + 5, my, "p")


MOOD_FACE = {   # mood: (eyes, mouth) default face for poses that do not set one
    "good": ("happy", "cat"),
    "testing": ("left", "tiny"),
    "warn": ("open", "wavy"),
    "fail": ("wide", "open"),
    "sleep": ("open", "tiny"),      # awake face for the sitting poses on the NO LINK screen
    "blink_on": ("glow", "open"),
    "blink_off": ("closed", "flat"),
    "silly": ("happy", "tongue"),
}

# pose: body, eyes, mouth, extras. No eyes/mouth = take them from the mood.
POSES = {
    "sit": dict(body="sit"),
    "sit1": dict(body="sit1"),
    "blink": dict(body="sit", eyes="closed"),
    "blink1": dict(body="sit1", eyes="closed"),
    "look_l": dict(body="sit", eyes="left"),
    "look_r": dict(body="sit1", eyes="right"),
    "look_up": dict(body="sit", eyes="up"),
    "twitch": dict(body="twitch"),
    "yawn1": dict(body="sit", eyes="closed", mouth="open"),
    "yawn2": dict(body="sit1", eyes="closed", mouth="yawn"),
    "stretch": dict(body="stretch", eyes="closed", mouth="tiny"),
    "lick1": dict(body="sit", eyes="happy", mouth="tiny", extras=["paw_up"]),
    "lick2": dict(body="sit", eyes="closed", mouth="tiny", extras=["paw_lick"]),
    "jiggle": dict(body="wide"),
    "tongue": dict(body="sit", mouth="tongue"),
    "annoyed": dict(body="sit", eyes="annoyed", mouth="flat"),
    "annoyed1": dict(body="sit1", eyes="annoyed", mouth="flat"),
    "hop": dict(body="hop", eyes="wide", mouth="open", extras=["mark"]),
    "scared": dict(body="scared", eyes="wide", mouth="open", extras=["mark"]),
    "loaf": dict(body="loaf", eyes="closed", mouth="tiny"),
    "loaf_in": dict(body="loaf_in", eyes="closed", mouth="tiny"),
    "loaf_awake": dict(body="loaf", eyes="open", mouth="tiny"),
    "loaf_look_l": dict(body="loaf", eyes="left", mouth="tiny"),
    "loaf_look_r": dict(body="loaf", eyes="right", mouth="tiny"),
    "loaf_yawn": dict(body="loaf_in", eyes="closed", mouth="yawn"),
    "loaf_scared": dict(body="loaf_scared", eyes="wide", mouth="open", extras=["mark"]),
    "stand": dict(body="stand", eyes="open", mouth="tiny"),
    **{f"walk{i}": dict(body=f"walk{i}", eyes="open", mouth="tiny") for i in range(1, 5)},
    **{f"prowl{i}": dict(body=f"prowl{i}", eyes="open", mouth="tiny") for i in range(1, 5)},
    "crouch": dict(body="crouch", eyes="wide", mouth="tiny"),          # eyes on the dot
    "crouch_up": dict(body="crouch_up", eyes="wide", mouth="tiny"),
    "leap1": dict(body="leap1", eyes="wide", mouth="open"),
    "leap2": dict(body="leap2", eyes="wide", mouth="open"),
    "pin": dict(body="pin", eyes="wide", mouth="tiny"),
    "pin_peek": dict(body="pin_peek", eyes="open", mouth="flat"),       # ...nothing under it
}


# --- props for the inline and router sets ----------------------------------------------------
# Painted after the face, in the order the pose lists them. All of them keep sprite rows
# 0..3 empty left of cell 5, like the ears, so the text limits beside the corner cat hold.

def _block(g, x0, y0, x1, y1, fill, edge=None):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            on_edge = edge and (x in (x0, x1) or y in (y0, y1))
            _put(g, x, y, edge if on_edge else fill)


def _paw(g, x0, y0, w=4, h=2):
    """A paw: an orange block with a dark edge and two toe marks."""
    _block(g, x0, y0, x0 + w - 1, y0 + h - 1, "o", "d")
    for x in (x0 + 1, x0 + w - 2):
        _put(g, x, y0, "d")


def _glasses(g, lm, p):
    """Round glasses over both eyes; the pupils follow p["gaze"]."""
    gaze = p.get("gaze", "screen")
    for ex, ey in (lm["eye_l"], lm["eye_r"]):
        x0, y0 = ex - 1, ey - 1
        _block(g, x0, y0, x0 + 4, y0 + 4, "w", "k")
        if gaze == "closed":
            for x in range(x0 + 1, x0 + 4):
                _put(g, x, y0 + 2, "k")
            continue
        if gaze == "half":
            for x in range(x0 + 1, x0 + 4):
                _put(g, x, y0 + 1, "k")
        px, py = {"screen": (x0 + 1, y0 + 2), "away": (x0 + 2, y0 + 1), "centre": (x0 + 2, y0 + 2),
                  "half": (x0 + 2, y0 + 2), "side": (x0 + 3, y0 + 2)}[gaze]
        _put(g, px, py, "k"); _put(g, px + 1, py, "k"); _put(g, px, py + 1, "k"); _put(g, px + 1, py + 1, "k")
        if gaze == "half":
            _put(g, px, py + 1, "k"); _put(g, px + 1, py + 1, "k")
    xl, xr = lm["eye_l"][0] + 4, lm["eye_r"][0] - 2       # the bridge
    for x in range(xl, xr + 1):
        _put(g, x, lm["eye_l"][1] + 1, "k")


def _laptop(g, lm, p):
    """The lid of a small laptop in front of the cat's left half, seen from behind."""
    _block(g, 0, 11, 9, 17, "m", "d")
    for x in range(1, 9):
        _put(g, x, 12, "c")                                            # the lid's rim
    _put(g, 4, 14, "l"); _put(g, 5, 14, "l"); _put(g, 4, 15, "l"); _put(g, 5, 15, "l")


def _paw_lid(g, lm, p):
    _paw(g, 7, 10)


def _paw_lid_up(g, lm, p):
    _paw(g, 7, 9)


def _paw_lazy(g, lm, p):
    _paw(g, 3, 10, w=8)


def _paw_glasses(g, lm, p):
    """Pushing the glasses up: the paw on the bridge."""
    _paw(g, lm["eye_l"][0] + 4, lm["eye_l"][1], w=4, h=3)


def _paw_chin(g, lm, p):
    cx, my = lm["mouth"]
    _paw(g, cx + 3, my - 1, w=5, h=3)


def _mug(g, lm, p, near=False):
    cx, my = lm["mouth"]
    x0, y0 = (cx + 2, my - 2) if near else (cx + 8, my - 3)
    _block(g, x0, y0, x0 + 3, y0 + 4, "w", "d")
    _put(g, x0 + 4, y0 + 1, "d"); _put(g, x0 + 4, y0 + 3, "d"); _put(g, x0 + 5, y0 + 2, "d")
    for x, y in ((x0 + 1, y0 - 2), (x0 + 2, y0 - 3), (x0 + 1, y0 - 4)):
        _put(g, x, y, "c")
    _paw(g, x0 - 2, y0 + 2, w=3, h=3)


def _mug_far(g, lm, p):
    _mug(g, lm, p, near=False)


def _mug_near(g, lm, p):
    _mug(g, lm, p, near=True)


def _fly(g, lm, p):
    x, y = p.get("fly", (24, 2))
    _put(g, x, y, "k"); _put(g, x + 1, y, "k"); _put(g, x, y - 1, "c"); _put(g, x + 1, y - 1, "c")


def _antenna_up(g, lm, p):
    """Held up in the right paw."""
    _paw(g, 21, 7, w=4, h=3)
    _block(g, 22, 5, 24, 6, "m", "d")
    for y in range(0, 5):
        _put(g, 23, y, "k")
    _put(g, 22, 0, "k"); _put(g, 24, 0, "k")


def _antenna_head(g, lm, p):
    """Screwed on top of the head, between the ears."""
    _block(g, 11, 3, 14, 4, "m", "d")
    for y in range(0, 3):
        _put(g, 12, y, "k"); _put(g, 13, y, "k")
    _put(g, 11, 0, "k"); _put(g, 14, 0, "k")


def _screw_paw(g, lm, p):
    frame = p.get("screw", 0)
    _paw(g, 15 if frame else 7, 2, w=4, h=3)


def _waves(g, lm, p):
    for x, y in ((9, 1), (8, 0), (16, 1), (17, 0), (7, 2), (18, 2)):
        _put(g, x, y, "c")


def _antenna_beside(g, lm, p):
    """Standing on its base on the floor to the right, over the tail."""
    _block(g, 23, 15, 27, 17, "m", "d")
    for y in range(5, 15):
        _put(g, 25, y, "k"); _put(g, 26, y, "k")
    _put(g, 24, 5, "k"); _put(g, 27, 5, "k")


def _cable_tangle(g, lm, p):
    """A cable looped around the body and over the head, two frames."""
    o = p.get("shift", 0)
    for x in range(5, 21):
        _put(g, x, 5 + o, "u")
    for i in range(0, 16):
        _put(g, 3 + i, 9 + i // 2 - o, "u")
    for i in range(0, 10):
        _put(g, 12 + i, 16 - i // 3 + o, "u")
    _block(g, 24, 15 - o, 26, 16 - o, "m", "d")


def _cable_chew(g, lm, p):
    """A plug in the mouth, the cable trailing down to the right."""
    cx, my = lm["mouth"]
    drop = p.get("shift", 0)
    _block(g, cx - 2, my - 1 + drop, cx + 1, my + drop, "m", "d")
    pts = ((cx + 2, my + drop), (cx + 3, my + 1), (cx + 4, my + 2), (cx + 5, my + 3), (cx + 6, my + 4),
           (cx + 7, my + 4), (cx + 8, my + 4), (cx + 9, my + 4))
    for x, y in pts:
        _put(g, x, y, "u")


def _cable_bat(g, lm, p):
    """A cable end on the floor and a paw on it."""
    for x in range(18, 27):
        _put(g, x, 17, "u")
    _block(g, 25, 15, 27, 16, "m", "d")
    _paw(g, 18, 14 + p.get("shift", 0), w=4, h=2)


def _knob(g, lm, p):
    """A knob on the floor at the left, its pointer turned by p["turn"]."""
    _block(g, 1, 12, 6, 17, "m", "d")
    px, py = ((3, 13), (5, 14), (3, 16), (1, 14))[p.get("turn", 0) % 4]
    _put(g, px, py, "k"); _put(g, 3, 14, "k"); _put(g, 4, 14, "k"); _put(g, 3, 15, "k"); _put(g, 4, 15, "k")


def _screwdriver(g, lm, p):
    """In the right paw, pointing up and out."""
    _paw(g, 18, 10, w=4, h=3)
    for i in range(5):
        _put(g, 22 + i, 9 - i, "m")
    _put(g, 26, 4, "u"); _put(g, 27, 3, "u"); _put(g, 27, 4, "u"); _put(g, 26, 5, "u")


def _box(g, lm, p):
    """A cardboard box in front of the cat, a row higher when it peeks over the edge."""
    o = p.get("shift", 0)
    _block(g, 2, 12 - o, 25, 17, "x", "d")
    for x in range(3, 25):
        _put(g, x, 13 - o, "d")                                        # the flap line
    _put(g, 13, 14 - o, "d"); _put(g, 14, 14 - o, "d"); _put(g, 13, 15 - o, "d"); _put(g, 14, 15 - o, "d")


def _fish(g, lm, p):
    """A fish held up in the right paw."""
    o = p.get("shift", 0)
    _paw(g, 17, 10 + o, w=4, h=3)
    for x, y in ((20, 8), (21, 7), (22, 7), (23, 7), (24, 8), (21, 9), (22, 9), (23, 9), (20, 8),
                 (25, 6), (25, 9), (26, 6), (26, 10), (25, 8)):
        _put(g, x, y + o, "l")
    _put(g, 21, 8 + o, "k"); _put(g, 22, 8 + o, "c"); _put(g, 23, 8 + o, "c")


def _question(g, lm, p):
    for x, y in ((24, 0), (25, 0), (26, 1), (25, 2), (25, 3), (25, 5)):
        _put(g, x, y, "r")


_QUESTION_BIG = (".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#..")


def _question_big(g, lm, p):
    """A big yellow "?" over the right ear, outlined dark so it reads over the NO LINK text
    (right of a hat's brim)."""
    x0, y0 = 22, 0
    cells = [(x0 + x, y0 + y) for y, row in enumerate(_QUESTION_BIG) for x, ch in enumerate(row) if ch == "#"]
    for x, y in cells:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if 0 <= y + dy < CH and 0 <= x + dx < CW and (x + dx, y + dy) not in cells:
                    _put(g, x + dx, y + dy, "d")
    for x, y in cells:
        _put(g, x, y, "Y")


def _tear(g, lm, p):
    x, y = lm["eye_l"]
    o = p.get("shift", 0)
    _put(g, x + 2, y + 3 + o, "y"); _put(g, x + 2, y + 4 + o, "y")


EXTRAS = {
    "glasses": _glasses, "laptop": _laptop, "paw_lid": _paw_lid, "paw_lid_up": _paw_lid_up,
    "paw_lazy": _paw_lazy, "paw_glasses": _paw_glasses, "paw_chin": _paw_chin,
    "mug_far": _mug_far, "mug_near": _mug_near, "fly": _fly,
    "antenna_up": _antenna_up, "antenna_head": _antenna_head, "screw_paw": _screw_paw,
    "waves": _waves, "antenna_beside": _antenna_beside, "cable_tangle": _cable_tangle,
    "cable_chew": _cable_chew, "cable_bat": _cable_bat,
    "knob": _knob, "screwdriver": _screwdriver, "box": _box, "fish": _fish, "question": _question,
    "tear": _tear, "question_big": _question_big,
}

# web pages: settings, captures, empty states and errors
POSES.update({
    "fiddle1": dict(body="sit", eyes="left", mouth="tongue", turn=0, extras=["knob", "screwdriver"]),
    "fiddle2": dict(body="sit1", eyes="left", mouth="tiny", turn=1, extras=["knob", "screwdriver"]),
    "fiddle3": dict(body="sit", eyes="left", mouth="tongue", turn=2, extras=["knob", "screwdriver"]),
    "fiddle_blink": dict(body="sit1", eyes="closed", mouth="tiny", turn=3, extras=["knob", "screwdriver"]),
    "box": dict(body="sit", eyes="happy", mouth="cat", shift=0, extras=["box"]),
    "box_peek": dict(body="wide", eyes="open", mouth="tiny", shift=1, extras=["box"]),
    "box_blink": dict(body="sit1", eyes="closed", mouth="cat", shift=0, extras=["box"]),
    "fish1": dict(body="sit", eyes="happy", mouth="cat", shift=0, extras=["fish"]),
    "fish2": dict(body="sit1", eyes="happy", mouth="tongue", shift=1, extras=["fish"]),
    "confused": dict(body="twitch", eyes="open", mouth="wavy", extras=["question"]),
    "confused2": dict(body="twitch", eyes="right", mouth="wavy", extras=["question"]),
    # after the pounce: where did the dot go?
    "huh": dict(body="twitch", eyes="open", mouth="wavy", extras=["question_big"]),
    "huh_r": dict(body="twitch", eyes="right", mouth="wavy", extras=["question_big"]),
    "huh_l": dict(body="twitch", eyes="left", mouth="wavy", extras=["question_big"]),
    "sad": dict(body="scared", eyes="sad", mouth="wavy", shift=0, extras=["tear"]),
    "sad2": dict(body="scared", eyes="sad", mouth="wavy", shift=1, extras=["tear"]),
})

# inline mode: sitting behind the laptop with its glasses on. "eyes" is set so the mood face
# stays out of it (the cat is nonchalant whatever the results say); the glasses repaint the eyes.
_W = dict(body="sit", eyes="open", mouth="tiny")
POSES.update({
    "work": dict(_W, gaze="screen", extras=["glasses", "laptop", "paw_lid"]),
    "work1": dict(_W, body="sit1", gaze="screen", extras=["glasses", "laptop", "paw_lid"]),
    "type": dict(_W, gaze="screen", extras=["glasses", "laptop", "paw_lid_up"]),
    "work_blink": dict(_W, gaze="closed", extras=["glasses", "laptop", "paw_lid"]),
    "push": dict(_W, gaze="centre", extras=["glasses", "laptop", "paw_lid", "paw_glasses"]),
    "fly1": dict(_W, gaze="away", fly=(24, 2), extras=["glasses", "laptop", "paw_lid", "fly"]),
    "fly2": dict(_W, gaze="away", fly=(25, 3), extras=["glasses", "laptop", "paw_lid", "fly"]),
    "fly3": dict(_W, gaze="side", fly=(23, 4), extras=["glasses", "laptop", "paw_lid", "fly"]),
    "work_yawn1": dict(_W, gaze="closed", mouth="open", extras=["glasses", "laptop", "paw_lid"]),
    "work_yawn2": dict(_W, body="sit1", gaze="closed", mouth="yawn", extras=["glasses", "laptop", "paw_lid"]),
    "chin": dict(_W, gaze="half", mouth="flat", extras=["glasses", "laptop", "paw_chin"]),
    "chin_closed": dict(_W, gaze="closed", mouth="flat", extras=["glasses", "laptop", "paw_chin"]),
    "drift1": dict(_W, gaze="half", mouth="flat", extras=["glasses", "laptop", "paw_lid"]),
    "drift2": dict(_W, gaze="closed", mouth="flat", extras=["glasses", "laptop", "paw_lid"]),
    "drift3": dict(_W, body="wide", gaze="closed", mouth="flat", extras=["glasses", "laptop", "paw_lid"]),
    "snap": dict(_W, gaze="centre", mouth="open", extras=["glasses", "laptop", "paw_lid"]),
    "lazy": dict(_W, gaze="side", mouth="flat", extras=["glasses", "laptop", "paw_lazy"]),
    "lazy1": dict(_W, body="sit1", gaze="side", mouth="flat", extras=["glasses", "laptop", "paw_lazy"]),
    "sip1": dict(_W, gaze="closed", mouth="tiny", extras=["glasses", "laptop", "paw_lid", "mug_far"]),
    "sip2": dict(_W, gaze="closed", mouth="tiny", extras=["glasses", "laptop", "paw_lid", "mug_near"]),
    "stare": dict(_W, gaze="centre", mouth="flat", extras=["glasses", "laptop", "paw_lid"]),
})

# router mode: the antenna and the cables
_R = dict(body="sit", mouth="tiny")
POSES.update({
    "ant_look": dict(_R, eyes="right", extras=["antenna_beside"]),
    "ant_look1": dict(_R, body="sit1", eyes="right", extras=["antenna_beside"]),
    "ant_blink": dict(_R, eyes="closed", extras=["antenna_beside"]),
    "ant_happy": dict(_R, eyes="happy", mouth="cat", extras=["antenna_beside"]),
    "ant_hold": dict(_R, eyes="up", extras=["antenna_up"]),
    "ant_hold1": dict(_R, body="sit1", eyes="up", extras=["antenna_up"]),
    "screw1": dict(_R, eyes="closed", mouth="flat", screw=0, extras=["antenna_head", "screw_paw"]),
    "screw2": dict(_R, eyes="closed", mouth="wavy", screw=1, extras=["antenna_head", "screw_paw"]),
    "proud": dict(_R, body="wide", eyes="happy", mouth="cat", extras=["antenna_head", "waves"]),
    "proud1": dict(_R, eyes="happy", mouth="cat", extras=["antenna_head", "waves"]),
    "tangle1": dict(_R, eyes="wide", mouth="open", shift=0, extras=["cable_tangle"]),
    "tangle2": dict(_R, body="sit1", eyes="wide", mouth="open", shift=1, extras=["cable_tangle"]),
    "tangle_sigh": dict(_R, eyes="annoyed", mouth="flat", shift=0, extras=["cable_tangle"]),
    "chew1": dict(_R, eyes="happy", mouth="tiny", shift=0, extras=["cable_chew"]),
    "chew2": dict(_R, body="sit1", eyes="closed", mouth="tiny", shift=1, extras=["cable_chew"]),
    "bat1": dict(_R, eyes="open", shift=0, extras=["cable_bat"]),
    "bat2": dict(_R, body="sit1", eyes="open", shift=1, extras=["cable_bat"]),
})


def cells(mood, pose="sit", frame=0):
    """Cell grid for a mood and pose. A pose ending in _L is mirrored (facing left).
    frame only matters for the plain "sit" pose: tail swish, blink on every 4th frame."""
    mirror = pose.endswith("_L")
    if mirror:
        pose = pose[:-2]
    if pose == "sit":
        pose = ("sit", "sit1", "sit", "blink1")[frame % 4]
    p = POSES[pose]
    body = p["body"]
    use_mood_face = "eyes" not in p
    if mood == "fail" and body in ("sit", "sit1", "twitch") and use_mood_face:
        body = "scared"
    paint, lm = BODIES[body]
    g = _canvas()
    _draw_cells(g, paint)
    eyes, mouth = MOOD_FACE.get(mood, ("happy", "cat"))
    if mood == "testing" and use_mood_face:
        eyes = ("left", "left", "right", "right")[frame % 4]
    eyes, mouth = p.get("eyes", eyes), p.get("mouth", mouth)
    nx, ny = lm["nose"]
    _put(g, nx, ny, "p"); _put(g, nx + 1, ny, "p")
    _eye(g, *lm["eye_l"], eyes)
    _eye(g, *lm["eye_r"], "squint" if (mood == "warn" and use_mood_face) else eyes)
    _mouth(g, *lm["mouth"], mouth)
    extras = list(p.get("extras", []))
    if use_mood_face and mood == "warn":
        extras.append("drop")
    if use_mood_face and mood == "fail":
        extras.append("mark")
    for ex in extras:
        if ex == "drop":
            x, y = lm["drop"]
            for dx, dy in ((0, 0), (-1, 1), (0, 1), (1, 1), (-1, 2), (0, 2), (1, 2), (0, 3)):
                _put(g, x + dx, y + dy, "y")
        elif ex == "mark":          # "!" beside the head, inside the sprite box
            for y in (0, 1, 2, 3, 5):
                _put(g, lm["mark"], y, "r")
        elif ex == "paw_up":
            _paw_lick(g, lm, True)
        elif ex == "paw_lick":
            _paw_lick(g, lm, False)
        else:
            EXTRAS[ex](g, lm, p)
    rows = ["".join(r) for r in g]
    if mirror:
        rows = [r[::-1] for r in rows]
    return rows


def cat(mood, frame=0):
    """Round-1 API: the sitting cat with its idle tail swish and blink; the loaf for sleep."""
    return cells(mood, "loaf" if mood == "sleep" else "sit", frame)


def render(rows, scale, pal=PAL):
    w, h = max(len(r) for r in rows), len(rows)
    img = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in pal:
                d.rectangle((x * scale, y * scale, (x + 1) * scale - 1, (y + 1) * scale - 1), fill=pal[ch])
    return img


_cache = {}


def sprite(mood, frame=0, scale=2, pose="sit"):
    key = (mood, frame % 4 if pose == "sit" else 0, scale, pose)
    if key not in _cache:
        _cache[key] = render(cells(mood, pose, frame), scale)
    return _cache[key]


def paste(img, sp, x, y):
    img.paste(sp, (x, y), sp)


# --- sequences ------------------------------------------------------------------------------
# (pose, ticks, dx cells per tick)

# results screen, 0.5 s ticks. Base loop: tail swish, a blink now and then.
IDLE_BASE = [("sit", 2, 0), ("sit1", 2, 0), ("sit", 2, 0), ("blink1", 1, 0), ("sit1", 1, 0)]
IDLE_ACTS = {
    "look_around": [("look_l", 2, 0), ("look_r", 2, 0), ("look_up", 2, 0), ("sit", 1, 0)],
    "ear_twitch": [("twitch", 1, 0), ("sit", 1, 0), ("twitch", 1, 0), ("sit", 1, 0)],
    "yawn": [("yawn1", 1, 0), ("yawn2", 3, 0), ("yawn1", 1, 0), ("blink", 1, 0)],
    "stretch": [("stretch", 4, 0), ("sit", 1, 0), ("jiggle", 1, 0), ("sit", 1, 0)],
    "lick": [("lick1", 1, 0), ("lick2", 1, 0), ("lick1", 1, 0), ("lick2", 1, 0), ("lick1", 1, 0), ("sit", 1, 0)],
    "wiggle": [("jiggle", 1, 0), ("sit", 1, 0), ("jiggle", 1, 0), ("sit", 1, 0)],
    "tongue": [("tongue", 3, 0), ("sit", 1, 0)],
}
IDLE_WEIGHTS = {"look_around": 4, "ear_twitch": 4, "yawn": 2, "stretch": 2, "lick": 2, "wiggle": 2, "tongue": 1}
# in the warn and fail moods only the small acts; the face stays worried
IDLE_ACTS_SERIOUS = {k: IDLE_ACTS[k] for k in ("look_around", "ear_twitch", "wiggle")}

# inline mode, 0.5 s ticks. Base loop: typing with the odd blink. The acts are the lapses:
# the gap between them should be short (INLINE_GAP) so the focus slips often.
INLINE_BASE = [("type", 1, 0), ("work", 1, 0), ("type", 1, 0), ("work1", 1, 0), ("type", 1, 0),
               ("work_blink", 1, 0), ("work", 1, 0), ("type", 1, 0), ("work1", 1, 0)]
INLINE_ACTS = {
    "push_glasses": [("push", 2, 0), ("work", 1, 0)],
    "fly": [("fly1", 1, 0), ("fly2", 1, 0), ("fly1", 1, 0), ("fly3", 2, 0), ("fly2", 1, 0), ("work", 1, 0)],
    "yawn": [("work_yawn1", 1, 0), ("work_yawn2", 3, 0), ("work_yawn1", 1, 0), ("work_blink", 1, 0)],
    "chin": [("chin", 4, 0), ("chin_closed", 2, 0), ("chin", 1, 0), ("work", 1, 0)],
    "drift": [("drift1", 2, 0), ("drift2", 2, 0), ("drift3", 2, 0), ("snap", 1, 0), ("work", 1, 0)],
    "lazy_paw": [("lazy", 2, 0), ("lazy1", 2, 0), ("lazy", 1, 0), ("work", 1, 0)],
    "sip": [("sip1", 1, 0), ("sip2", 3, 0), ("sip1", 1, 0), ("work", 1, 0)],
    "stare": [("stare", 3, 0), ("work", 1, 0)],
}
INLINE_WEIGHTS = {"push_glasses": 3, "fly": 3, "yawn": 2, "chin": 2, "drift": 3, "lazy_paw": 2, "sip": 2, "stare": 1}
INLINE_GAP = (4, 12)        # ticks of work between two lapses

# router mode, 0.5 s ticks. Base loop: sitting by the antenna, looking at it.
ROUTER_BASE = [("ant_look", 3, 0), ("ant_look1", 2, 0), ("ant_blink", 1, 0), ("ant_look", 2, 0)]
ROUTER_ACTS = {
    "fit": [("ant_hold", 2, 0), ("ant_hold1", 2, 0), ("screw1", 1, 0), ("screw2", 1, 0), ("screw1", 1, 0),
            ("screw2", 1, 0), ("screw1", 1, 0), ("proud1", 2, 0), ("proud", 2, 0), ("proud1", 2, 0),
            ("ant_happy", 1, 0)],
    "tangle": [("tangle1", 1, 0), ("tangle2", 1, 0), ("tangle1", 1, 0), ("tangle2", 1, 0),
               ("tangle_sigh", 3, 0), ("ant_look", 1, 0)],
    "chew": [("chew1", 1, 0), ("chew2", 1, 0), ("chew1", 1, 0), ("chew2", 1, 0), ("chew1", 1, 0),
             ("ant_happy", 1, 0)],
    "bat": [("bat1", 1, 0), ("bat2", 1, 0), ("bat1", 1, 0), ("bat2", 1, 0), ("ant_look", 1, 0)],
    "proud": [("proud1", 2, 0), ("proud", 1, 0), ("proud1", 1, 0), ("proud", 1, 0), ("ant_happy", 2, 0)],
}
ROUTER_WEIGHTS = {"fit": 3, "tangle": 2, "chew": 3, "bat": 2, "proud": 2}
ROUTER_GAP = (6, 20)

# web pages, 0.5 s ticks: short loops, one per page or state
WEB_ACTS = {
    "fiddle": [("fiddle1", 2, 0), ("fiddle2", 2, 0), ("fiddle3", 2, 0), ("fiddle_blink", 1, 0), ("fiddle1", 1, 0)],
    "box": [("box", 3, 0), ("box_peek", 2, 0), ("box", 1, 0), ("box_peek", 3, 0), ("box_blink", 1, 0)],
    "fish": [("fish1", 2, 0), ("fish2", 2, 0), ("fish1", 2, 0), ("fish2", 1, 0)],
    "confused": [("confused", 3, 0), ("confused2", 2, 0), ("confused", 1, 0)],
    "sad": [("sad", 3, 0), ("sad2", 3, 0)],
}

# one step of the profile walk, 0.25 s ticks: 4 frames, one cell a tick (the feet don't slide)
WALK = [("walk1", 1, 1), ("walk2", 1, 1), ("walk3", 1, 1), ("walk4", 1, 1)]

# NO LINK screen, 0.25 s ticks. Base loop: breathing, 2 s per breath.
SLEEP_BASE = [("loaf", 4, 0), ("loaf_in", 4, 0)]
SLEEP_ACTS = {
    "peek": [("loaf_awake", 3, 0), ("loaf_look_l", 3, 0), ("loaf_look_r", 3, 0), ("loaf_awake", 2, 0), ("loaf", 2, 0)],
    "yawn": [("loaf_awake", 2, 0), ("loaf_yawn", 5, 0), ("loaf", 2, 0)],
    "stretch": [("loaf_awake", 2, 0), ("sit", 2, 0), ("stretch", 8, 0), ("sit", 2, 0), ("yawn2", 3, 0),
                ("sit", 2, 0), ("loaf_awake", 2, 0), ("loaf", 2, 0)],
    "lick": [("loaf_awake", 2, 0), ("sit", 2, 0)] + [("lick1", 2, 0), ("lick2", 2, 0)] * 4
            + [("sit", 2, 0), ("blink", 2, 0), ("loaf_awake", 2, 0), ("loaf", 2, 0)],
    "walk": [("loaf_awake", 2, 0), ("sit", 2, 0), ("stand", 2, 0)]
            + WALK * 5 + [("stand", 2, 0), ("stand_L", 2, 0)]
            + [(p + "_L", t, -dx) for p, t, dx in WALK] * 5
            + [("stand", 2, 0), ("sit", 2, 0), ("jiggle", 2, 0), ("sit", 2, 0), ("loaf_awake", 2, 0), ("loaf", 2, 0)],
    "turn": [("loaf_awake", 2, 0), ("sit", 2, 0), ("stand", 2, 0), ("stand_L", 3, 0), ("sit_L", 4, 0),
             ("blink_L", 2, 0), ("sit_L", 3, 0), ("stand_L", 2, 0), ("stand", 2, 0), ("sit", 2, 0),
             ("loaf_awake", 2, 0), ("loaf", 2, 0)],
}
SLEEP_WEIGHTS = {"peek": 4, "yawn": 3, "stretch": 2, "lick": 2, "walk": 2, "turn": 2}
SLEEP_GAP = (60, 160)       # ticks of plain sleep between two acts: 15 to 40 s (was 3 to 10)

# touch on the NO LINK screen, 0.25 s ticks (5 s)
POKE = [("loaf_scared", 2, 0), ("hop", 2, 0), ("scared", 2, 0), ("annoyed", 3, 0), ("annoyed1", 3, 0),
        ("yawn1", 1, 0), ("yawn2", 3, 0), ("blink", 1, 0), ("loaf_awake", 2, 0), ("loaf", 1, 0)]


def expand(seq):
    """[(pose, ticks, dx)] -> [(pose, dx)], one entry per tick."""
    out = []
    for pose, ticks, dx in seq:
        out += [(pose, dx)] * ticks
    return out


class Idle:
    """Tick scheduler. Loops `base`; after a random gap (in ticks) plays one act picked by
    weight; play(seq) interrupts with a sequence (the poke). tick() returns (pose, x) with x
    the walking offset in cells; every act ends back at x = 0. `current` names what plays."""

    def __init__(self, base, acts, weights, gap=(8, 30), rng=None):
        self.base, self.acts, self.weights = expand(base), acts, weights
        self.gap, self.rng = gap, rng or random.Random()
        self.queue, self.i, self.x = [], 0, 0
        self.current = None  # the act or play() name running; None on the base loop
        self.until_act = self.rng.randint(*gap)

    def play(self, seq, name="play"):
        self.queue, self.current = expand(seq), name

    def tick(self):
        if not self.queue:
            if self.until_act <= 0 and self.acts:
                names = list(self.acts)
                name = self.rng.choices(names, [self.weights.get(n, 1) for n in names])[0]
                self.queue, self.current = expand(self.acts[name]), name
                self.until_act = self.rng.randint(*self.gap)
            else:
                self.until_act -= 1
                pose, dx = self.base[self.i % len(self.base)]
                self.i += 1
                return pose, self.x
        pose, dx = self.queue.pop(0)
        self.x += dx
        if not self.queue:
            self.x, self.current = 0, None
        return pose, self.x


MOODS = ["good", "testing", "warn", "fail", "sleep", "blink_on", "blink_off", "silly"]


# --- outfits --------------------------------------------------------------------------------
# Things worn over any pose that may reach outside the 28 x 18 box: hats, the umbrella, the
# weather presenter's pointer, rain and snow behind the cat. dressed() paints them on a canvas
# PAD_L / PAD_T / PAD_R cells bigger, the cat at (PAD_L, PAD_T).

PAL.update({
    "n": (78, 104, 200),    # nightcap
    "N": (180, 200, 255),   # nightcap outline and shine
    "R": (215, 55, 55),     # scarf, Santa hat, the bow's knot
    "S": (255, 170, 160),   # Santa hat shine
    "h": (104, 66, 160),    # witch hat
    "H": (205, 175, 255),   # witch hat outline and shine: the hats sit on the dark screen
})
PAD_L, PAD_T, PAD_R = 4, 11, 14
SITTING = ("sit", "sit1", "wide", "hop", "scared", "twitch")  # bodies that hold things
HATS = ("bow", "nightcap", "witch", "santa")

# the top of the head between the ears (x, y) where the eyes don't tell it
_HEAD = {"loaf": (10, 5), "loaf_in": (10, 4), "loaf_scared": (10, 5), "stretch": (6, 8),
         "crouch": (22, 9), "crouch_up": (22, 9), "leap1": (21, 1), "leap2": (22, 2),
         "pin": (17, 9), "pin_peek": (17, 9),
         "stand": (21, 2), **{f"walk{i + 1}": (21, 2 + (1, 0, 1, 0)[i]) for i in range(4)},
         **{f"prowl{i + 1}": (22, 6 + (1, 0, 1, 0)[i]) for i in range(4)}}


def _head(body, lm):
    if body in _HEAD:
        return _HEAD[body]
    (lx, ly), (rx, _) = lm["eye_l"], lm["eye_r"]
    return (lx + rx) // 2 + 1, ly - 4


def _w_sunglasses(d, c, X, Y, head, lm, p, frame):
    for ex, ey in (lm["eye_l"], lm["eye_r"]):
        d.rectangle((X + ex - 1, Y + ey - 1, X + ex + 3, Y + ey + 2), fill=c["k"])
        d.point((X + ex, Y + ey), fill=c["c"])
    (lx, ly), (rx, _) = lm["eye_l"], lm["eye_r"]
    d.line((X + lx + 3, Y + ly, X + rx - 1, Y + ly), fill=c["k"])


def _w_bow(d, c, X, Y, head, lm, p, frame):
    """The presenter's pink bow between the ears."""
    x, y = X + head[0], Y + head[1]
    d.polygon(((x - 4, y - 3), (x, y - 1), (x - 4, y + 1)), fill=c["p"], outline=c["d"])
    d.polygon(((x + 4, y - 3), (x, y - 1), (x + 4, y + 1)), fill=c["p"], outline=c["d"])
    d.rectangle((x - 1, y - 2, x + 1, y), fill=c["R"], outline=c["d"])


def _w_pointer(d, c, X, Y, head, lm, p, frame):
    """A stick from the right paw up to the board; p["point"] taps lower on it."""
    tx, ty = (36, 7) if p.get("point") else (37, 2)
    d.line((X + 22, Y + 11, X + tx, Y + ty), fill=c["x"])
    d.point((X + tx, Y + ty), fill=c["r"])
    d.ellipse((X + 20, Y + 10, X + 24, Y + 13), fill=c["o"], outline=c["d"])


def _w_umbrella(d, c, X, Y, head, lm, p, frame):
    d.line((X + 22, Y - 4, X + 22, Y + 12), fill=c["d"])
    d.arc((X + 22, Y + 11, X + 25, Y + 14), 0, 180, fill=c["d"])
    d.pieslice((X, Y - 10, X + 28, Y + 4), 180, 360, fill=c["u"], outline=c["d"])
    for x in (7, 14, 21):
        d.line((X + x, Y - 8, X + x, Y - 4), fill=c["l"])
    d.point((X + 14, Y - 11), fill=c["d"])
    d.ellipse((X + 20, Y + 10, X + 24, Y + 13), fill=c["o"], outline=c["d"])


def _w_scarf(d, c, X, Y, head, lm, p, frame):
    y = Y + lm["mouth"][1] + 1
    d.rectangle((X + 5, y, X + 20, y + 1), fill=c["R"], outline=c["d"])
    d.rectangle((X + 16, y + 1, X + 18, y + 4), fill=c["R"], outline=c["d"])


def _w_nightcap(d, c, X, Y, head, lm, p, frame):
    x, y = X + head[0], Y + head[1]
    d.polygon(((x - 7, y), (x + 7, y - 1), (x - 2, y - 7), (x - 7, y - 8)), fill=c["n"], outline=c["N"])
    d.line((x - 4, y - 2, x - 5, y - 6), fill=c["N"])               # shine
    d.line((x - 7, y, x + 7, y - 1), fill=c["w"])
    d.ellipse((x - 10, y - 10, x - 7, y - 7), fill=c["w"], outline=c["N"])


def _w_witch(d, c, X, Y, head, lm, p, frame):
    x, y = X + head[0], Y + head[1]
    d.polygon(((x - 5, y - 1), (x + 5, y - 1), (x + 2, y - 7), (x + 6, y - 10), (x, y - 8)),
              fill=c["h"], outline=c["H"])
    d.line((x - 2, y - 3, x, y - 7), fill=c["H"])                   # shine
    d.line((x - 4, y - 2, x + 4, y - 2), fill=c["o"])
    d.rectangle((x - 9, y - 1, x + 9, y), fill=c["h"], outline=c["H"])


def _w_santa(d, c, X, Y, head, lm, p, frame):
    x, y = X + head[0], Y + head[1]
    d.polygon(((x - 6, y - 1), (x + 6, y - 1), (x + 3, y - 7), (x + 10, y - 5)), fill=c["R"], outline=c["R"])
    d.line((x - 3, y - 2, x, y - 5), fill=c["S"])                   # shine
    d.rectangle((x - 7, y - 1, x + 7, y), fill=c["w"], outline=c["w"])
    d.ellipse((x + 9, y - 6, x + 12, y - 3), fill=c["w"], outline=c["w"])


def _w_rain(d, c, X, Y, head, lm, p, frame):
    w, h = X + CW + 2, d.im.size[1]  # not over the right pad: the pointer's room
    for i in range(16):
        x, y = (i * 11 + 3) % w, (i * 7 + frame * 3) % h
        d.line((x, y, x, y + 1), fill=c["y"])


def _w_snow(d, c, X, Y, head, lm, p, frame):
    w, h = X + CW + 2, d.im.size[1]
    for i in range(14):
        x, y = (i * 13 + 5 + frame % 2) % w, (i * 5 + frame * 2) % h
        d.point((x, y), fill=c["w"])


BEHIND = {"rain": _w_rain, "snow": _w_snow}
WEAR = {"sunglasses": _w_sunglasses, "bow": _w_bow, "pointer": _w_pointer, "umbrella": _w_umbrella,
        "scarf": _w_scarf, "nightcap": _w_nightcap, "witch": _w_witch, "santa": _w_santa}
HELD = ("pointer", "umbrella", "scarf", "sunglasses")  # only on the sitting bodies


def _layer(fns, size, args):
    keys = list(PAL)
    im = Image.new("L", size, 0)
    d = ImageDraw.Draw(im)
    for fn in fns:
        fn(d, {k: i + 1 for i, k in enumerate(keys)}, *args)
    px = im.load()
    return lambda x, y: keys[px[x, y] - 1] if px[x, y] else None


def dressed(mood, pose="sit", frame=0, wear=()):
    """(rows, x, y): the pose with `wear` on (names from WEAR and BEHIND), on the bigger
    canvas; the cat's own cell (0, 0) is at (x, y). The held things and the glasses only go
    on the sitting bodies; hats on every body."""
    mirror = pose.endswith("_L")
    name = pose[:-2] if mirror else pose
    rows = cells(mood, name, frame)
    if name == "sit":
        name = ("sit", "sit1", "sit", "blink1")[frame % 4]
    p = POSES[name]
    body = p["body"]
    if mood == "fail" and body in ("sit", "sit1", "twitch") and "eyes" not in p:
        body = "scared"
    lm = BODIES[body][1]
    wear = [w for w in wear if body in SITTING or w not in HELD]
    size = (CW + PAD_L + PAD_R, CH + PAD_T)
    args = (PAD_L, PAD_T, _head(body, lm), lm, p, frame)
    behind = _layer([BEHIND[w] for w in wear if w in BEHIND], size, args)
    front = _layer([WEAR[w] for w in wear if w in WEAR], size, args)
    out = []
    for y in range(size[1]):
        row = []
        for x in range(size[0]):
            ch = front(x, y)
            if ch is None:
                cy, cx = y - PAD_T, x - PAD_L
                inside = 0 <= cy < CH and 0 <= cx < CW and rows[cy][cx] != "."
                ch = rows[cy][cx] if inside else (behind(x, y) or ".")
            row.append(ch)
        out.append("".join(row))
    if mirror:
        return [r[::-1] for r in out], PAD_R, PAD_T
    return out, PAD_L, PAD_T


_dressed_cache = {}


def dressed_sprite(mood, frame=0, scale=2, pose="sit", wear=()):
    """(image, dx, dy): paste the image at (x - dx, y - dy) to put the cat where sprite()
    would be at (x, y). A small cache: the outfits change with the weather and the date."""
    key = (mood, frame % 4, scale, pose, tuple(wear))
    if key not in _dressed_cache:
        if len(_dressed_cache) > 96:
            _dressed_cache.pop(next(iter(_dressed_cache)))
        rows, x, y = dressed(mood, pose, frame, wear)
        _dressed_cache[key] = (render(rows, scale), x * scale, y * scale)
    return _dressed_cache[key]


def season(month, day):
    """The hat for the date: all of October the witch's, Christmas time Santa's."""
    if month == 10:
        return "witch"
    if month == 12 and day <= 26:
        return "santa"
    return None


def outfit(kind, night=False, hat=None):
    """(wear, mood) for the desk presenter and a forecast kind (weather.KINDS): sunglasses
    for the sun, an umbrella in the rain, a scarf in the snow, fur up in a storm. `hat`
    (season()) replaces the bow."""
    top = [hat or "bow"]
    if kind == "storm":
        return ["rain"] + ([hat] if hat else []), "fail"
    if kind == "rain":
        return ["rain", "umbrella"] + top, "sleep"
    if kind == "snow":
        return ["snow", "scarf", "pointer"] + top, "sleep"
    if kind == "sun" and not night:
        return ["sunglasses", "pointer"] + top, "sleep"
    return ["pointer"] + top, "sleep"


# --- the desk presenter, the variety show, the laser dot (NO LINK, 0.25 s ticks) ------------

POSES.update({
    "point": dict(body="sit", eyes="right", mouth="cat", point=1),
    "happy": dict(body="sit", eyes="happy", mouth="cat"),
    "bow_down": dict(body="loaf", eyes="happy", mouth="cat"),
})

# the weather presenter: sits by the board, taps it, looks at the sky now and then
DESK_BASE = [("sit", 4, 0), ("sit1", 4, 0), ("sit", 4, 0), ("blink", 2, 0), ("sit1", 2, 0)]
DESK_ACTS = {
    "point": [("point", 3, 0), ("sit", 2, 0), ("point", 3, 0), ("sit", 4, 0)],
    "look": [("look_r", 6, 0), ("look_l", 4, 0), ("sit", 2, 0)],
    "sky": [("look_up", 6, 0), ("sit", 2, 0)],
    "twitch": [("twitch", 2, 0), ("sit", 2, 0), ("twitch", 2, 0), ("sit", 2, 0)],
    "wiggle": [("jiggle", 2, 0), ("sit", 2, 0), ("jiggle", 2, 0), ("sit", 2, 0)],
}
DESK_WEIGHTS = {"point": 4, "look": 3, "sky": 2, "twitch": 2, "wiggle": 1}
DESK_GAP = (16, 48)


def variety(rng=None, start="loaf"):
    """The variety show for a long NO LINK: every act of every set once, shuffled, at 0.25 s
    ticks (the 0.5 s sets at their own speed). It starts and ends in `start`: "loaf" for the
    sleeper, "sit" for the desk presenter; between the acts the cat sits, at the end it bows."""
    rng = rng or random.Random()

    def slow(seq):
        return [(p, t * 2, dx) for p, t, dx in seq]

    acts = [slow(a) for a in IDLE_ACTS.values()] + [slow(a) for a in INLINE_ACTS.values()]
    acts += [slow(a) for a in ROUTER_ACTS.values()] + [slow(a) * 2 for a in WEB_ACTS.values()]
    acts += [[s for s in a if not s[0].startswith("loaf")] for a in SLEEP_ACTS.values()]
    acts = [a for a in acts if a]
    rng.shuffle(acts)
    seq = [("loaf_awake", 4, 0), ("loaf_yawn", 4, 0)] if start == "loaf" else [("look_up", 4, 0)]
    seq += [("sit", 4, 0)]
    for act in acts:
        seq += act + [("sit", 3, 0)]
    seq += [("happy", 4, 0), ("bow_down", 6, 0), ("happy", 4, 0), ("blink", 2, 0)]
    if start == "loaf":
        seq += [("loaf_awake", 4, 0), ("loaf", 2, 0)]
    return seq


PAW_REACH = 8  # cells from the sprite's centre to the paws that pin the dot (pin pose)


def chase(cells_away):
    """The laser dot `cells_away` cells from where the cat is (negative: left): it wakes,
    looks, stalks over (the prowl), crouches and wiggles its rear, leaps (up to 6 cells, an
    arc inside the sprite), pins the dot with both front paws, peeks under them, finds
    nothing, sits up confused, then struts back and settles. Returns (seq, catch) with catch
    the tick it lands on the dot, when the dot should go. `cells_away` is measured from the
    sprite's centre; the cat stops PAW_REACH short of it, so the paws land on the dot."""
    n = int(cells_away)
    side, step = ("", 1) if n >= 0 else ("_L", -1)
    back = "_L" if n >= 0 else ""
    look = "loaf_look_r" if n >= 0 else "loaf_look_l"
    travel = max(0, abs(n) - PAW_REACH)                                    # a dot under its nose: no steps
    jump = min(6, travel)
    hops = [jump // 3 + (i < jump % 3) for i in range(3)]                  # the leap's 3 ticks
    to = [("loaf_awake", 1, 0), (look, 3, 0), ("sit" + side, 1, 0), ("stand" + side, 1, 0)]
    to += [(f"prowl{i % 4 + 1}" + side, 1, step) for i in range(travel - jump)]  # stalking it
    to += [("crouch" + side, 2, 0), ("crouch_up" + side, 1, 0), ("crouch" + side, 1, 0),
           ("crouch_up" + side, 1, 0), ("crouch" + side, 2, 0)]            # the wiggle
    to += [("leap1" + side, 1, hops[0] * step), ("leap2" + side, 1, hops[1] * step),
           ("leap2" + side, 1, hops[2] * step)]
    catch = sum(t for _, t, _ in to)
    seq = to + [("pin" + side, 4, 0), ("pin_peek" + side, 3, 0), ("pin" + side, 1, 0),
                ("pin_peek" + side, 3, 0)]
    seq += [("huh", 4, 0), ("huh_r", 3, 0), ("huh_l", 3, 0), ("huh", 2, 0),
            ("blink", 2, 0), ("stand" + back, 1, 0)]                       # where did it go?
    seq += [(f"walk{i % 4 + 1}" + back, 1, -step) for i in range(travel)]  # strolling back
    seq += [("stand", 1, 0), ("sit", 2, 0), ("loaf_awake", 2, 0), ("loaf", 1, 0)]
    return seq, catch

"""Draws the report with Pillow and pushes it to a Linux framebuffer
(the SPI panel shows up as /dev/fbN), or saves a PNG for preview."""

import fcntl
import glob
import ipaddress
import math
import os
import random
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import pet, scenes
from .checks import FAIL, OK, PENDING, SKIP, WARN

W, H = 480, 320
HEADER_H, FOOTER_H = 38, 39  # the cat sits in the bottom-right corner, mostly on the footer
ROW_H = (H - HEADER_H - FOOTER_H) // 9
LABEL_W = 116

BG = (12, 14, 18)
ROW_ALT = (20, 23, 29)
TEXT = (230, 233, 238)
MUTED = (128, 136, 150)
STATE_COLOR = {
    OK: (46, 204, 113),
    WARN: (241, 196, 15),
    FAIL: (231, 76, 60),
    SKIP: (95, 104, 118),
    PENDING: (95, 104, 118),
}
FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF")


def _font(name, size):
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 (Bookworm) has no sized default font
        return ImageFont.load_default()


F_TITLE = _font("DejaVuSans-Bold.ttf", 20)
F_LABEL = _font("DejaVuSansCondensed-Bold.ttf", 15)
F_VALUE = _font("DejaVuSansCondensed.ttf", 17)
F_SMALL = _font("DejaVuSansCondensed.ttf", 12)
F_HINT = _font("DejaVuSansCondensed.ttf", 15)  # the footer hints: read at arm's length


def _fit(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    lo, hi = 0, len(text)  # longest prefix that fits with the ellipsis, by bisection
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + "…", font=font) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def overall(report):
    states = [r.state for r in report.values()]
    for s in (FAIL, WARN, PENDING):
        if s in states:
            return s
    return OK


def verdict(report):
    """The top bar's word for the results."""
    return {OK: "ALL GOOD", WARN: "CHECK WARNINGS", FAIL: "PROBLEM FOUND",
            PENDING: "TESTING…"}[overall(report)]


MOOD = {OK: "good", WARN: "warn", FAIL: "fail", PENDING: "testing"}
PET_SCALE = 3                   # corner cat: 84 x 54 px
PET_X, PET_Y = W - pet.CW * PET_SCALE, H - pet.CH * PET_SCALE
BIG_PET_SCALE = 5               # message screens
CAT_BAND = (PET_Y, H)           # framebuffer rows the idle animation rewrites


def _left_edges(mood):
    """Per cell row, the leftmost cell the corner cat can fill in this mood, over every pose
    the results-screen idle uses, so text beside it stays clear while it moves."""
    poses = {p for p, _, _ in pet.IDLE_BASE}
    for seq in pet.IDLE_ACTS.values():
        poses |= {p for p, _, _ in seq}
    edges = [pet.CW] * pet.CH
    for pose in poses:
        for frame in range(4):
            for r, row in enumerate(pet.cells(mood, pose, frame)):
                filled = [i for i, ch in enumerate(row) if ch != "."]
                if filled:
                    edges[r] = min(edges[r], filled[0])
    return edges


# the scared ears of the fail mood reach further left than the others
LEFT_EDGES = {m: _left_edges(m) for m in MOOD.values()}


def text_limit(row_top, row_h, state=OK):
    """Right edge for a row's text: the cat's left edge where it crosses the text band."""
    band_top, band_bot = row_top + row_h // 2 - 9, row_top + row_h // 2 + 9
    if band_bot <= PET_Y:
        return W - 8
    rows = {(y - PET_Y) // PET_SCALE for y in range(max(band_top, PET_Y), min(band_bot, H))}
    edges = LEFT_EDGES[MOOD.get(state, "good")]
    return PET_X + min(edges[r] for r in rows if 0 <= r < pet.CH) * PET_SCALE - 2


def _corner_cat(img, state, frame, pose):
    sp = pet.sprite(MOOD.get(state, "good"), frame, PET_SCALE, pose)
    pet.paste(img, sp, PET_X, PET_Y)


def render(report, iface, banner=None, frame=0, pose="sit", footer=None):
    """`frame` and `pose` animate the cat; the app's idle scheduler supplies them."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    state = overall(report)
    status = banner or verdict(report)
    _header(d, state, f"{iface}  ·  {status}")

    y = HEADER_H
    for i, row in enumerate(report.values()):
        if i % 2:
            d.rectangle((0, y, W, y + ROW_H - 1), fill=ROW_ALT)
        cy = y + ROW_H // 2
        d.ellipse((10, cy - 5, 20, cy + 5), fill=STATE_COLOR[row.state])
        d.text((30, cy), row.label, font=F_LABEL, fill=MUTED, anchor="lm")
        value = row.value or ("…" if row.state == PENDING else "")
        color = MUTED if row.state in (PENDING, SKIP) else TEXT
        d.text((LABEL_W, cy), _fit(d, value, F_VALUE, text_limit(y, ROW_H, state) - LABEL_W),
               font=F_VALUE, fill=color, anchor="lm")
        y += ROW_H

    _footer(d, footer or "tap: VLANs   ·   2 taps: re-run   ·   hold top bar: blink")
    _corner_cat(img, state, frame, pose)
    return img


# the hotspot is on: every screen's top bar shows the Wi-Fi mark (set by the app)
wifi_mark = False
# the title's wave: the frame of the pass being drawn, None between passes (set by the app
# around a render, see App.pause)
title_frame = None

TITLE = "fluGATO"
TITLE_PARTS = ("fl", "u", "G", "A", "T", "O")  # "fl" stays one piece: the font joins it
TITLE_X = 19            # 7 px (1 mm) in from where it was: clear of the case's bezel
TITLE_FPS, TITLE_PASS = 8, 20  # a pass: 2.5 s, the title bar redrawn 8 times a second
TITLE_GAP = (10, 30)    # seconds between passes, at random


def title_wave(f, i):
    """(dy, colour) of part i of the title at frame f of a pass: a wave runs left to right,
    each letter rising 3 px and turning the cat's orange as it passes, then back."""
    t = (f - i * 2) / 8
    up = math.sin(math.pi * t) if 0 <= t <= 1 else 0
    return -3 * up, tuple(round(a + (b - a) * up) for a, b in zip(TEXT, FUR))


class TitleWave:
    """frame(): the frame of the pass now running (0 to TITLE_PASS - 1), or None between
    passes, which start TITLE_GAP seconds apart at random. stop(): none until the next gap."""

    def __init__(self, clock=None, rng=None):
        self.clock = clock or (lambda: time.monotonic())
        self.rng = rng or random
        self.start = None
        self.next = self.clock() + self.rng.uniform(*TITLE_GAP)

    def frame(self):
        now = self.clock()
        if self.start is None and now >= self.next:
            self.start = now
        if self.start is None:
            return None
        f = int((now - self.start) * TITLE_FPS)
        if f < TITLE_PASS:
            return f
        self.stop()
        return None

    def stop(self):
        self.start = None
        self.next = self.clock() + self.rng.uniform(*TITLE_GAP)


def _wifi_glyph(d, x, y, colour=TEXT, small=False):
    """Three arcs and a dot, about 18 px wide (small: 12), its base at (x, y)."""
    for r in ((3, 6, 9) if small else (4, 9, 14)):
        d.arc((x - r, y - r, x + r, y + r), 225, 315, fill=colour, width=2 if small else 3)
    dot = 1 if small else 2
    d.ellipse((x - dot, y - dot, x + dot, y + dot), fill=colour)


def _wifi_client(d, wifi, right_text):
    """The Wi-Fi network the Pi is on (wifi: {ssid, address}), in the top bar between the
    title and the right-hand text: a small glyph, the SSID over the address."""
    if not wifi:
        return
    x0 = TITLE_X + d.textlength(TITLE, font=F_TITLE) + 22
    x1 = W - 12 - d.textlength(right_text, font=F_LABEL) - 14
    _wifi_glyph(d, x0 + 9, HEADER_H // 2 + 5, TEXT, small=True)
    room = int(x1 - x0 - 26)  # 15 px, like the footer: read at arm's length
    d.text((x0 + 24, HEADER_H // 2 + 1), _fit(d, wifi.get("ssid", ""), F_LABEL, room), font=F_LABEL,
           fill=TEXT, anchor="ls")
    d.text((x0 + 24, HEADER_H // 2 + 2), _fit(d, wifi.get("address", ""), F_HINT, room), font=F_HINT,
           fill=(170, 178, 192), anchor="lt")


def _title(d):
    """The title, a letter at a time (the wave moves and colours each one; at rest it is the
    plain word), and the Wi-Fi mark after it while the hotspot is on."""
    for i, part in enumerate(TITLE_PARTS):
        dy, colour = title_wave(title_frame, i) if title_frame is not None else (0, TEXT)
        # where the whole word puts it: the font's kerning (A-T) kept
        x = TITLE_X + d.textlength("".join(TITLE_PARTS[:i + 1]), font=F_TITLE) \
            - d.textlength(part, font=F_TITLE)
        d.text((x, HEADER_H // 2 + dy), part, font=F_TITLE, fill=colour, anchor="lm")
    if wifi_mark:
        _wifi_glyph(d, TITLE_X + d.textlength(TITLE, font=F_TITLE) + 18, HEADER_H // 2 + 7)


def _header(d, state, right_text):
    head_bg = {OK: (24, 92, 56), WARN: (110, 88, 10), FAIL: (120, 34, 28)}.get(state, (34, 38, 46))
    d.rectangle((0, 0, W, HEADER_H), fill=head_bg)
    _title(d)
    d.text((W - 12, HEADER_H // 2), right_text, font=F_LABEL, fill=TEXT, anchor="rm")


FUR = (236, 150, 60)  # the cat's orange: the gestures in the footer hints


def _footer(d, text):
    """Left-aligned, so the corner cat has the right end of the footer to itself. In the
    cat's colours: the gesture orange, what it does white ("tap: details · 2 taps: ...")."""
    x, y = 12, H - FOOTER_H // 2 + 4
    for i, part in enumerate(text.split("   ·   ")):
        if i:
            d.text((x, y), "  ·  ", font=F_HINT, fill=MUTED, anchor="lm")
            x += d.textlength("  ·  ", font=F_HINT)
        gesture, colon, action = part.partition(":")
        for chunk, colour in ((gesture + colon, FUR if colon else TEXT), (action, TEXT)):
            d.text((x, y), chunk, font=F_HINT, fill=colour, anchor="lm")
            x += d.textlength(chunk, font=F_HINT)


def offer_text(offer):
    net = ipaddress.ip_interface(f"{offer['address']}/{offer['prefix'] or 32}").network
    via = f"  gw {offer['router']}" if offer.get("router") else ""
    return f"{net}{via}"


# VLAN list layout: two fixed rows, then the VLAN rows, which scroll with the buttons
LIST_TOP, LIST_ROW_H = HEADER_H + 2, 25
LIST_ROWS = (H - HEADER_H - FOOTER_H - 4) // LIST_ROW_H   # 9 rows on the screen
LIST_FIXED = 2                                            # native, mode
LIST_VLAN_ROWS = LIST_ROWS - LIST_FIXED
BTN_UP = (420, 44, 474, 124)      # x0, y0, x1, y1: big enough for a finger, above the cat
BTN_DOWN = (420, 132, 474, 212)
LIST_TEXT_RIGHT = BTN_UP[0] - 6


def _inside(pos, box):
    return pos is not None and box[0] <= pos[0] <= box[2] and box[1] <= pos[1] <= box[3]


# touch zones for the buttons: the whole strip out to the screen edge, split between the
# two buttons, because readings near the edge are the least accurate on this panel
ZONE_X = BTN_UP[0] - 10
ZONE_SPLIT = (BTN_UP[3] + BTN_DOWN[1]) // 2


def list_hit(pos, vlan_count, scroll):
    """What a touch on the VLAN list hit: "up", "down", ("vlan", index) or None."""
    if pos is None:
        return None
    if pos[0] >= ZONE_X and HEADER_H < pos[1] < PET_Y:
        return "up" if pos[1] < ZONE_SPLIT else "down"
    if pos[0] > LIST_TEXT_RIGHT:
        return None
    row = (pos[1] - LIST_TOP) // LIST_ROW_H - LIST_FIXED
    if 0 <= row < LIST_VLAN_ROWS and 0 <= scroll + row < vlan_count:
        return ("vlan", scroll + row)
    return None


def max_scroll(vlan_count):
    return max(0, vlan_count - LIST_VLAN_ROWS)


def _rows(img, d, lines, top, row_h, state, right=W):
    for i, (dot, label, value) in enumerate(lines):
        y = top + i * row_h
        if i % 2:
            d.rectangle((0, y, right, y + row_h - 1), fill=ROW_ALT)
        cy = y + row_h // 2
        if label:
            d.ellipse((10, cy - 5, 20, cy + 5), fill=STATE_COLOR[dot])
        d.text((30, cy), label, font=F_LABEL, fill=MUTED, anchor="lm")
        color = MUTED if dot in (PENDING, SKIP) else TEXT
        limit = min(text_limit(y, row_h, state), right - 4)
        d.text((LABEL_W, cy), _fit(d, value, F_VALUE, limit - LABEL_W), font=F_VALUE,
               fill=color, anchor="lm")


def _button(d, box, up, enabled):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=8, fill=(34, 38, 46) if enabled else (20, 23, 29),
                        outline=(70, 78, 92) if enabled else (40, 44, 52), width=2)
    cx, cy, r = (x0 + x1) // 2, (y0 + y1) // 2, 12
    tip, base = (cy - r, cy + r // 2) if up else (cy + r, cy - r // 2)
    d.polygon(((cx, tip), (cx - r, base), (cx + r, base)), fill=TEXT if enabled else MUTED)


def vlan_line(vlan, offers):
    if vlan not in offers:
        return PENDING, f"VLAN {vlan}", "…"
    if offers[vlan]:
        return OK, f"VLAN {vlan}", offer_text(offers[vlan])
    return SKIP, f"VLAN {vlan}", "no DHCP answer"


def render_details(report, iface, native, tagged, offers, frame=0, pose="sit", scroll=0):
    """VLAN list: the native VLAN, the port mode, then each tagged VLAN with its DHCP
    answer, scrolled with the buttons on the right. `tagged` is None while the listen
    hasn't produced a result; `offers` holds the VLANs probed so far."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    state = overall(report)
    _header(d, state, f"{iface}  ·  VLANS")

    dhcp = report["DHCP"]
    if native:
        value = f"{native}  DHCP {dhcp.value}" if dhcp.state == OK else f"{native}  untagged DHCP: {dhcp.value or '…'}"
        lines = [(dhcp.state if dhcp.state in (OK, FAIL) else OK, "native", value)]
    else:
        lines = [(SKIP, "native", "not advertised")]
    vlans = sorted(tagged) if tagged else []
    if tagged is None:
        lines.append((PENDING, "mode", "listening for tagged frames…"))
    elif not tagged:
        lines.append((OK, "mode", "access (no tagged frames heard)"))
    else:
        answered = sum(1 for o in offers.values() if o)
        lines.append((OK, "mode", f"trunk, {len(vlans)} tagged, DHCP {answered}/{len(offers)}"))
        lines += [vlan_line(v, offers) for v in vlans[scroll:scroll + LIST_VLAN_ROWS]]
    _rows(img, d, lines, LIST_TOP, LIST_ROW_H, state, right=LIST_TEXT_RIGHT)

    if len(vlans) > LIST_VLAN_ROWS:
        _button(d, BTN_UP, True, scroll > 0)
        _button(d, BTN_DOWN, False, scroll < max_scroll(len(vlans)))
        d.text(((BTN_UP[0] + BTN_UP[2]) // 2, BTN_DOWN[3] + 14),
               f"{scroll + 1}-{min(scroll + LIST_VLAN_ROWS, len(vlans))}", font=F_SMALL,
               fill=MUTED, anchor="mm")
        d.text(((BTN_UP[0] + BTN_UP[2]) // 2, BTN_DOWN[3] + 28), f"of {len(vlans)}",
               font=F_SMALL, fill=MUTED, anchor="mm")
    hint = "tap a VLAN: details   ·   tap elsewhere: back" if vlans else "tap: back"
    _footer(d, hint)
    _corner_cat(img, state, frame, pose)
    return img


def render_vlan(report, iface, vlan, offer, probed, frame=0, pose="sit"):
    """One tagged VLAN: what its DHCP server offered, without having taken the lease."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    state = overall(report)
    _header(d, state, f"{iface}  ·  VLAN {vlan}")
    if not probed:
        lines = [(PENDING, "DHCP", "not asked yet…")]
    elif not offer:
        lines = [(SKIP, "DHCP", "no answer"),
                 (SKIP, "", "no server, or no relay on this VLAN")]
    else:
        net = ipaddress.ip_interface(f"{offer['address']}/{offer['prefix'] or 32}").network
        lease = offer.get("lease")
        lines = [(OK, "DHCP", "offer received (not taken)"),
                 (OK, "address", f"{offer['address']}/{offer['prefix']}"),
                 (OK, "network", str(net)),
                 (OK if offer.get("router") else SKIP, "router", offer.get("router") or "none"),
                 (OK, "server", offer.get("server") or "?"),
                 (OK if offer.get("dns") else SKIP, "DNS", ", ".join(offer.get("dns") or []) or "none"),
                 (OK if lease else SKIP, "lease", fmt_lease_s(lease) if lease else "?")]
    _rows(img, d, lines, LIST_TOP, LIST_ROW_H, state)
    _footer(d, "tap: back to the list")
    _corner_cat(img, state, frame, pose)
    return img


def fmt_lease_s(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


# --- inline mode: the path, the detail list, the traffic page --------------------------

def fmt_rate(bps):
    if not bps:
        return "0"
    for unit, div, fmt in (("Gb/s", 1e9, ".1f"), ("Mb/s", 1e6, ".1f"), ("kb/s", 1e3, ".0f")):
        if bps >= div:
            return f"{bps / div:{fmt}} {unit}"
    return f"{bps:.0f} b/s"


def fmt_bytes(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def fmt_link(link):
    if not link or not link.get("speed"):
        return "no link"
    from .checks import fmt_speed
    return " ".join(filter(None, (fmt_speed(link["speed"]), link.get("duplex"))))


def inline_state(snap):
    levels = {lvl for lvl, _ in snap.get("warnings") or []}
    return FAIL if "fail" in levels else WARN if "warn" in levels else OK


def _inline_header(d, snap, page):
    warnings = snap.get("warnings") or []
    state = inline_state(snap)
    if page:
        right = f"INLINE  ·  {page}"
    elif snap.get("state") != "bridged":
        right = f"INLINE  ·  {snap.get('state') or 'starting'}…"
    else:
        right = f"INLINE  ·  {len(warnings)} warning{'s' if len(warnings) != 1 else ''}" if warnings else "INLINE  ·  watching"
    _header(d, state, right)
    return state


def _dhcp_text(dhcp):
    st = (dhcp or {}).get("state")
    if st == "ack":
        from .checks import fmt_lease
        lease = dhcp.get("lease")
        return f"DHCP ACK, lease {fmt_lease(lease)}" if lease else "DHCP ACK", OK
    return {"offer": ("DHCP offer, no ACK yet", WARN), "nak": ("DHCP refused (NAK)", FAIL),
            "pending": ("DHCP asking…", PENDING), "none": ("no DHCP seen", SKIP)}.get(st, ("DHCP not seen yet", SKIP))


def _spark(d, box, values, colour):
    x0, y0, x1, y1 = box
    if len(values) < 2:
        return
    top = max(values) or 1
    pts = [(x0 + i * (x1 - x0) / (len(values) - 1), y1 - v / top * (y1 - y0)) for i, v in enumerate(values)]
    d.line(pts, fill=colour, width=2)


UP_COLOUR, DOWN_COLOUR = (46, 204, 113), (90, 170, 255)


def links_differ(a, b):
    """Speed or duplex differ between the two sides; an unknown value differs from nothing
    (the USB adapter's driver reports no duplex), like inline.link_warnings."""
    sa, sb, da, db = a.get("speed"), b.get("speed"), a.get("duplex"), b.get("duplex")
    return bool((sa and sb and sa != sb) or (da and db and da != db))


def render_inline(snap, iface, frame=0, pose="sit"):
    """Main inline screen: the switch on the left, the device on the right, the traffic
    between them over the last 60 s, the capture and the warnings."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    state = _inline_header(d, snap, None)
    sw, dev = snap.get("switch") or {}, snap.get("device") or {}
    tr, cap = snap.get("traffic") or {}, snap.get("capture") or {}
    dhcp_text, dhcp_state = _dhcp_text(dev.get("dhcp"))
    sw_link, dev_link = sw.get("link") or {}, dev.get("link") or {}
    mismatch = links_differ(sw_link, dev_link)
    link_colour = STATE_COLOR[WARN] if mismatch else STATE_COLOR[OK]

    def box(x0, title, lines, colour):
        d.rounded_rectangle((x0, 46, x0 + 140, 214), radius=10, outline=colour, width=2, fill=ROW_ALT)
        d.text((x0 + 70, 60), title, font=F_LABEL, fill=colour, anchor="mm")
        for i, (text, c) in enumerate(lines[:6]):
            d.text((x0 + 9, 82 + i * 21), _fit(d, text or "", F_SMALL, 124), font=F_SMALL, fill=c,
                   anchor="lm")

    vlan = f"VLAN {sw['native_vlan']}" if sw.get("native_vlan") else "VLAN ?"
    box(8, "SWITCH", [(sw.get("name") or "not heard yet", TEXT), (sw.get("port") or "", TEXT),
                      (vlan, TEXT), (f"voice {sw['voice_vlan']}" if sw.get("voice_vlan") else "", MUTED),
                      (sw.get("via") or "", MUTED), (fmt_link(sw_link), link_colour)],
        STATE_COLOR[OK] if sw.get("name") else STATE_COLOR[SKIP])
    ip = f"{dev['ip']}/{dev['prefix']}" if dev.get("ip") and dev.get("prefix") else dev.get("ip") or "no address seen"
    dev_state = FAIL if dhcp_state == FAIL else WARN if mismatch and dev_link.get("speed") else OK
    box(332, "DEVICE", [(dev.get("hostname") or dev.get("mac") or "waiting for traffic", TEXT),
                        (dev.get("vendor") or "", MUTED), (ip, TEXT),
                        (dhcp_text, STATE_COLOR[dhcp_state] if dhcp_state != PENDING else MUTED),
                        (dev.get("mac") if dev.get("hostname") else "", MUTED),
                        (fmt_link(dev_link), link_colour)],
        STATE_COLOR[dev_state] if dev.get("mac") else STATE_COLOR[SKIP])

    d.text((240, 58), f"→ {fmt_rate(tr.get('up_bps'))}   ← {fmt_rate(tr.get('down_bps'))}",
           font=F_SMALL, fill=TEXT, anchor="mm")
    d.rectangle((154, 72, 326, 150), outline=(40, 44, 52))
    hist = tr.get("history") or []
    _spark(d, (156, 76, 324, 146), [h[1] for h in hist], DOWN_COLOUR)
    _spark(d, (156, 76, 324, 146), [h[0] for h in hist], UP_COLOUR)
    d.text((240, 160), "last 60 s", font=F_SMALL, fill=MUTED, anchor="mm")
    if cap.get("file"):
        dropped = f", {cap['dropped']} dropped" if cap.get("dropped") else ""
        d.text((240, 180), f"● capturing {fmt_bytes(cap.get('bytes') or 0)}{dropped}", font=F_SMALL,
               fill=(231, 76, 60), anchor="mm")
    warnings = snap.get("warnings") or []
    for i, (level, text) in enumerate(warnings[:2]):
        d.text((240, 200 + i * 16), _fit(d, text, F_SMALL, 170), font=F_SMALL,
               fill=STATE_COLOR[FAIL if level == "fail" else WARN], anchor="mm")
    _footer(d, "tap: details   ·   2 taps: re-run   ·   hold top bar: blink")
    _corner_cat(img, state, frame, pose)
    return img


def inline_report(snap):
    """The detail list (page A) as rows, so it uses the results layout."""
    from .checks import Row
    sw, dev = snap.get("switch") or {}, snap.get("device") or {}
    tr = snap.get("traffic") or {}
    dhcp = dev.get("dhcp") or {}
    dhcp_text, dhcp_state = _dhcp_text(dhcp)
    sw_link, dev_link = sw.get("link") or {}, dev.get("link") or {}
    mismatch = links_differ(sw_link, dev_link)
    vlans = [f"{sw['native_vlan']} native"] if sw.get("native_vlan") else []
    if sw.get("voice_vlan"):
        vlans.append(f"voice {sw['voice_vlan']}")
    if dev.get("vlans"):
        vlans.append("device tags " + ", ".join(map(str, dev["vlans"][:4])))
    server = f"from {dhcp['server']}" if dhcp.get("server") else ""
    took = f"  in {dhcp['took']:.1f} s" if dhcp.get("took") else ""
    rows = [
        ("DEVICE", "  ".join(filter(None, (dev.get("mac"), dev.get("vendor"), dev.get("hostname")))) or "waiting for traffic",
         OK if dev.get("mac") else PENDING),
        ("IP", f"{dev['ip']}/{dev.get('prefix') or '?'}" if dev.get("ip") else "no address seen", OK if dev.get("ip") else SKIP),
        ("DHCP", "  ".join(filter(None, (dhcp_text, server))) + took, dhcp_state),
        ("SWITCH", "  ".join(filter(None, (sw.get("name"), sw.get("mgmt"), f"({sw['via']})" if sw.get("via") else None)))
         or "not heard yet", OK if sw.get("name") else PENDING),
        ("PORT", "  ".join(filter(None, (sw.get("port"), sw.get("descr")))) or "", OK if sw.get("port") else SKIP),
        ("VLAN", "  ".join(vlans) or "not advertised", OK if vlans else SKIP),
        ("LINK SW", fmt_link(sw_link) + "  switch side", OK if sw_link.get("speed") else FAIL),
        ("LINK DEV", fmt_link(dev_link) + "  device side", WARN if mismatch else OK if dev_link.get("speed") else FAIL),
        ("TRAFFIC", f"up {tr.get('up_fps') or 0:.0f} / down {tr.get('down_fps') or 0:.0f} frames/s  "
                    f"bcast {tr.get('bcast_ps') or 0:.0f}/s", OK),
    ]
    report = {}
    for label, value, st in rows:
        report[label] = Row(label)
        report[label].set(value, st)
    return report


def render_inline_list(snap, iface, frame=0, pose="sit"):
    return render(inline_report(snap), iface, "INLINE  ·  DETAILS", frame, pose,
                  footer="tap: traffic   ·   2 taps: re-run   ·   hold top bar: blink")


def render_inline_traffic(snap, iface, frame=0, pose="sit"):
    """Traffic page: protocol mix, the device's DNS lookups, top talkers, broadcast rates."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    state = _inline_header(d, snap, "TRAFFIC")
    tr = snap.get("traffic") or {}
    protos = sorted((tr.get("protocols") or {}).items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in protos) or 1
    if len(protos) > 7:
        protos = protos[:6] + [("other", sum(v for _, v in protos[6:]))]
    d.text((12, 52), "protocols", font=F_LABEL, fill=MUTED, anchor="lm")
    if not protos:
        d.text((12, 74), "no traffic yet", font=F_SMALL, fill=MUTED, anchor="lm")
    for i, (name, n) in enumerate(protos):
        y, pct = 66 + i * 19, 100 * n / total
        d.text((12, y + 8), _fit(d, name, F_SMALL, 76), font=F_SMALL, fill=TEXT, anchor="lm")
        d.rectangle((92, y + 3, 92 + max(2, pct * 1.3), y + 13), fill=DOWN_COLOUR)
        d.text((98 + pct * 1.3, y + 8), f"{pct:.0f}%", font=F_SMALL, fill=MUTED, anchor="lm")
    d.text((262, 52), "device looked up", font=F_LABEL, fill=MUTED, anchor="lm")
    names = (tr.get("dns") or [])[:5] or ["nothing yet"]
    for i, name in enumerate(names):
        d.text((262, 72 + i * 19), _fit(d, name, F_SMALL, 210), font=F_SMALL,
               fill=TEXT if tr.get("dns") else MUTED, anchor="lm")
    d.text((262, 178), "top talkers", font=F_LABEL, fill=MUTED, anchor="lm")
    talkers = (tr.get("top") or [])[:3]
    for i, (addr, n) in enumerate(talkers or [("nothing yet", 0)]):
        d.text((262, 198 + i * 19), _fit(d, f"{addr}  {fmt_bytes(n)}" if n else addr, F_SMALL, 130),
               font=F_SMALL, fill=TEXT if talkers else MUTED, anchor="lm")
    d.text((12, 216), f"bcast {tr.get('bcast_ps') or 0:.0f}/s   mcast {tr.get('mcast_ps') or 0:.0f}/s",
           font=F_SMALL, fill=MUTED, anchor="lm")
    _footer(d, "tap: back to the path")
    _corner_cat(img, state, frame, pose)
    return img


F_MESSAGE = _font("DejaVuSans-Bold.ttf", 34)
F_ZZZ = _font("DejaVuSans-Bold.ttf", 22)


# the Wi-Fi button on NO LINK and the Wi-Fi screen: bottom right, clear of the big cat, at the
# panel's edge (its right end runs under the case's bezel, about 5 px a side)
WIFI_BTN = (W - 183, H - 64, W - 1, H - 14)


def wifi_hit(pos):
    """A tap on the Wi-Fi button, with a margin round it (resistive touch)."""
    x0, y0, x1, y1 = WIFI_BTN
    return pos is not None and x0 - 16 <= pos[0] <= x1 + 14 and y0 - 20 <= pos[1] <= H


def _wifi_button(d, text, active=True):
    x0, y0, x1, y1 = WIFI_BTN
    edge = FUR if active else MUTED
    d.rounded_rectangle(WIFI_BTN, radius=10, outline=edge, width=2,
                        fill=(46, 34, 22) if active else (30, 32, 38))
    _wifi_glyph(d, x0 + 26, (y0 + y1) // 2 + 8, edge)
    d.text((x0 + 48, (y0 + y1) // 2), text, font=F_LABEL, fill=TEXT if active else MUTED,
           anchor="lm")


# the power button on NO LINK: bottom left, far from the Wi-Fi button, under the shelf
POWER_BTN = (14, H - 64, 72, H - 14)


def power_hit(pos):
    """A tap on the power button, with a margin round it (only a little above: the cat)."""
    x0, y0, x1, y1 = POWER_BTN
    return pos is not None and 0 <= pos[0] <= x1 + 16 and y0 - 10 <= pos[1] <= H


def _power_glyph(d, cx, cy, r, colour, width=3):
    d.arc((cx - r, cy - r, cx + r, cy + r), start=-50, end=230, fill=colour, width=width)
    d.line((cx, cy - r - 2, cx, cy + 1), fill=colour, width=width)


def _power_button(d):
    x0, y0, x1, y1 = POWER_BTN
    d.rounded_rectangle(POWER_BTN, radius=10, outline=MUTED, width=2, fill=(30, 32, 38))
    _power_glyph(d, (x0 + x1) // 2, (y0 + y1) // 2 + 1, 11, TEXT)


# the big cat on the message screens: its top-left corner, and where it is to touch
BIG_CAT_X = 36
BIG_CAT_Y = HEADER_H + (H - HEADER_H - pet.CH * BIG_PET_SCALE) // 2 + 6


def big_cat_hit(pos, xcells=0):
    """A tap on the big NO LINK cat (where it is now, after a walk), with a margin."""
    x0 = BIG_CAT_X + xcells * BIG_PET_SCALE
    return pos is not None and (x0 - 12 <= pos[0] <= x0 + pet.CW * BIG_PET_SCALE + 12
                                and BIG_CAT_Y - 30 <= pos[1] <= BIG_CAT_Y + pet.CH * BIG_PET_SCALE + 12)


def _big_cat(img, d, mood, frame, pose, x, y, wear=(), zzz=True):
    """The big cat with its cell (0, 0) at (x, y), dressed when `wear` is given, and the
    z's when it is fast asleep (moved right when a hat is in the way)."""
    if wear:
        sp, dx, dy = pet.dressed_sprite(mood, frame, BIG_PET_SCALE, pose, wear)
        pet.paste(img, sp, x - dx, y - dy)
    else:
        pet.paste(img, pet.sprite(mood, frame, BIG_PET_SCALE, pose), x, y)
    if zzz and mood == "sleep" and pose in ("loaf", "loaf_in"):  # fast asleep: the z's
        shift = 34 if any(w in pet.HATS for w in wear) else 0
        for i, (dx, dy) in enumerate(((74, -8), (90, -28), (108, -50))):
            if i <= frame % 4:  # they appear one by one
                d.text((x + dx + shift, y + dy), "z", font=F_ZZZ, fill=(140, 150, 170), anchor="mm")


def _laser(d, pos):
    x, y = pos
    d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=(70, 14, 18))
    d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 40, 40))
    d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 200, 200))


def _shadow(d, xy, text, font, fill, anchor="mm"):
    """Text over a scene: a 2 px dark shadow keeps it readable on any background."""
    x, y = xy
    d.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0), anchor=anchor)
    d.text((x, y), text, font=font, fill=fill, anchor=anchor)


def render_message(title, detail, iface, mood=None, frame=0, pose="sit", xcells=0, button=None,
                   wear=(), laser=None, wifi=None, scene=None, power=False):
    """Full-screen message with the big cat on the left and the text to its right:
    mood "sleep" with a loaf pose for NO LINK (xcells shifts it when it walks),
    blink_on / blink_off for the port blink. `button` draws the Wi-Fi button, `power` the
    power button, `wear` dresses the cat (pet.WEAR), `laser` (x, y) draws the dot it chases,
    `wifi` ({ssid, address}) the network the Pi is on, in the top bar. scene "closet": NO LINK's place (scenes.py), the
    message on the panel on its wall and the cat on the long shelf."""
    img = scenes.closet(frame) if scene == "closet" else Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, HEADER_H), fill=(34, 38, 46))
    _title(d)
    d.text((W - 12, HEADER_H // 2), iface, font=F_LABEL, fill=TEXT, anchor="rm")
    _wifi_client(d, wifi, iface)
    left = BIG_CAT_X + pet.CW * BIG_PET_SCALE if mood else 0
    tx = left + (W - left) // 2
    room = W - left - 16
    say = _shadow if scene else (lambda d, xy, text, font, fill: d.text(xy, text, font=font, fill=fill, anchor="mm"))
    say(d, (tx, H // 2 - 14), _fit(d, title, F_MESSAGE, room), F_MESSAGE, TEXT)
    say(d, (tx, H // 2 + 26), _fit(d, detail, F_VALUE, room), F_VALUE, MUTED)
    if mood:  # over the text: a chase walks the cat across it
        _big_cat(img, d, mood, frame, pose, BIG_CAT_X + xcells * BIG_PET_SCALE, BIG_CAT_Y, wear)
    if button:
        _wifi_button(d, button)
    if power:
        _power_button(d)
    if laser:
        _laser(d, laser)
    return img


# --- power off: asked on NO LINK, or any clean shutdown -----------------------------------

POWER_RED = (226, 84, 72)
POWER_OFF_BTN = (BIG_CAT_X + pet.CW * BIG_PET_SCALE + 10, 170, W - 14, 236)
POWER_CANCEL_BTN = (14, 254, W - 14, H - 14)


def power_off_hit(pos):
    """A tap on "Power off" on the question screen; anything else cancels."""
    x0, y0, x1, y1 = POWER_OFF_BTN
    return pos is not None and x0 - 6 <= pos[0] <= x1 + 6 and y0 - 6 <= pos[1] <= y1 + 6


def _power_header(d, right):
    d.rectangle((0, 0, W, HEADER_H), fill=(34, 38, 46))
    _title(d)
    d.text((W - 12, HEADER_H // 2), right, font=F_LABEL, fill=TEXT, anchor="rm")


def render_power_ask(iface, seconds):
    """Turn off? The cat awake, a big red-edged Power off, Cancel the whole width under it
    (the easiest target); it cancels by itself after `seconds`."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _power_header(d, iface)
    _big_cat(img, d, "sleep", 0, "loaf_awake", BIG_CAT_X, BIG_CAT_Y - 30)
    x0, y0, x1, y1 = POWER_OFF_BTN
    tx = (x0 - 10 + W) // 2
    d.text((tx, HEADER_H + 46), "Turn off?", font=F_MESSAGE, fill=TEXT, anchor="mm")
    d.text((tx, HEADER_H + 84), f"cancels by itself in {seconds} s", font=F_VALUE, fill=MUTED,
           anchor="mm")
    d.rounded_rectangle(POWER_OFF_BTN, radius=12, outline=POWER_RED, width=3, fill=(52, 26, 24))
    _power_glyph(d, x0 + 34, (y0 + y1) // 2 + 1, 13, POWER_RED)
    d.text((x0 + 62, (y0 + y1) // 2), "Power off", font=F_MESSAGE, fill=TEXT, anchor="lm")
    c0, c1, c2, c3 = POWER_CANCEL_BTN
    d.rounded_rectangle(POWER_CANCEL_BTN, radius=10, outline=(70, 78, 92), width=2, fill=(34, 38, 46))
    d.text(((c0 + c2) // 2, (c1 + c3) // 2), "Cancel", font=F_MESSAGE, fill=TEXT, anchor="mm")
    return img


def render_power_off(iface, reboot=False):
    """On the rooftop: "Turning off…" (or "Restarting…") with the cat yawning while the
    system stops. The panel can't keep a picture once the Pi halts (its pins stop being
    driven: the controller resets, the backlight stays on), so gray is the "safe" signal."""
    img = scenes.rooftop(1)
    d = ImageDraw.Draw(img)
    _power_header(d, iface)
    title, detail = (("Restarting…", "back in a moment") if reboot
                     else ("Turning off…", "unplug when it goes gray"))
    _big_cat(img, d, "sleep", 0, "loaf_yawn", 30, 140)
    # under the z's, clear of the antenna: the title ends 26 px from the edge
    cx = W - 26 - d.textlength(title, font=F_MESSAGE) / 2
    _shadow(d, (cx, 146), title, F_MESSAGE, TEXT)
    _shadow(d, (cx, 186), detail, F_VALUE, (170, 178, 192))
    return img


def render_wifi(status, iface, frame=0, pose="ant_look", busy=False):
    """NO LINK while the hotspot is on (or starting): the router cat on the left, what a
    phone needs on the right, big enough to type from: network, password, address."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, HEADER_H), fill=(34, 38, 46))
    _title(d)
    d.text((W - 12, HEADER_H // 2), f"{iface} · NO LINK", font=F_LABEL, fill=TEXT, anchor="rm")
    sp = pet.sprite("good", frame, BIG_PET_SCALE, pose)
    pet.paste(img, sp, 14, HEADER_H + (H - HEADER_H - sp.height) // 2 - 10)
    x = 14 + sp.width + 14
    room = W - x - 12
    if status.get("on"):
        lines = (("Wi-Fi", F_LABEL, MUTED), (status.get("ssid") or "", F_MESSAGE, TEXT),
                 ("password", F_LABEL, MUTED), (status.get("password") or "", F_MESSAGE, FUR),
                 (status.get("url") or "", F_VALUE, TEXT))
        y = HEADER_H + 14
        for text, font, colour in lines:
            gap = 2 if font is F_LABEL else 8
            d.text((x, y), _fit(d, text, font, room), font=font, fill=colour, anchor="la")
            y += font.size + gap
        n = status.get("clients") or 0
        phones = "no phone yet" if not n else "1 phone connected" if n == 1 else f"{n} phones connected"
        d.text((x, y + 2), phones, font=F_SMALL, fill=MUTED, anchor="la")
        _wifi_button(d, "stopping…" if busy else "Hotspot off", active=not busy)
    else:
        joining = status.get("joining")
        d.text((x, HEADER_H + 60), "Wi-Fi", font=F_MESSAGE, fill=TEXT, anchor="la")
        d.text((x, HEADER_H + 104),
               _fit(d, f"joining {joining}…" if joining else "starting the hotspot…", F_VALUE, room),
               font=F_VALUE, fill=MUTED, anchor="la")
        _wifi_button(d, "joining…" if joining else "starting…", active=False)
    failed = status.get("failed")
    if failed and status.get("on"):  # the join failed and the hotspot came back
        reason = failed.rpartition(": ")[2]  # "SSID: reason"; the web page has it in full
        reason = {"no address from the network (DHCP)": "no DHCP address",
                  "no answer in time": "timed out"}.get(reason, reason)
        d.text((14, H - 14), _fit(d, f"join failed: {reason}", F_HINT, WIFI_BTN[0] - 24),
               font=F_HINT, fill=STATE_COLOR[FAIL], anchor="ls")
    else:
        d.text((14, H - 14), "plug in a cable to test", font=F_SMALL, fill=MUTED, anchor="ls")
    return img


# --- desk mode: NO LINK for a while, with the clock in sync ------------------------------------

F_CLOCK = _font("DejaVuSans-Bold.ttf", 60)
F_TEMP = _font("DejaVuSans-Bold.ttf", 40)
F_DATE = _font("DejaVuSansCondensed-Bold.ttf", 18)
BOARD_BOX = (214, 50, 468, 238)  # the weather window (scenes.WINDOW), the numbers on its glass
DESK_CAT = (24, 150)  # the presenter's cell (0, 0): its pointer reaches the window's edge


def _bubble(d, text, cat_head):
    """A thought bubble top left, two dots down to the cat's head."""
    w = int(d.textlength(text, font=F_LABEL)) + 24
    box = (12, HEADER_H + 8, 12 + w, HEADER_H + 40)
    hx, hy = cat_head
    for r, t in ((5, 0.45), (3, 0.75)):
        x = int(box[0] + 30 + (hx - box[0] - 30) * t)
        y = int(box[3] + (hy - box[3]) * t)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(40, 44, 54))
    d.rounded_rectangle(box, radius=14, fill=(40, 44, 54))
    d.text((box[0] + 12, (box[1] + box[3]) // 2), text, font=F_LABEL, fill=TEXT, anchor="lm")


def day_line(stats):
    """"today: 7 tests · 1 fail" from {"tests": n, "fails": n}."""
    n, f = stats.get("tests", 0), stats.get("fails", 0)
    if not n:
        return "today: no tests yet"
    return f"today: {n} test{'s' * (n != 1)}" + (f" · {f} fail{'s' * (f != 1)}" if f else "")


def render_desk(now, forecast, stats, detail, iface, frame=0, pose="sit", xcells=0, wear=(),
                mood="sleep", button=None, wifi=None, dim=False):
    """Desk mode. With a forecast: the presenter cat on the window sill, the weather outside
    the window (scenes.window) with the time, date and numbers on the glass. Without one:
    the cat asleep on the desk, the clock on the monitor (scenes.desk). The day's tests in a
    thought bubble; NO LINK in the top bar. `now` is a time.struct_time. dim: the night look
    (as render_night)."""
    if forecast:
        img = scenes.window(forecast["kind"], bool(forecast.get("night")) or dim, frame)
    else:
        img = scenes.desk(frame)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, HEADER_H), fill=(34, 38, 46))
    _title(d)
    d.text((W - 12, HEADER_H // 2), f"{iface} · NO LINK", font=F_LABEL, fill=TEXT, anchor="rm")
    _wifi_client(d, wifi, f"{iface} · NO LINK")
    clock, date = time.strftime("%H:%M", now), time.strftime("%a %-d %b", now)
    if forecast:
        cx, cy = DESK_CAT
        _bubble(d, day_line(stats), (cx + 60 + xcells * BIG_PET_SCALE, cy - 4))
        x0, y0, x1, y1 = BOARD_BOX
        _shadow(d, (x0 + 14, y0 + 12), clock, F_MESSAGE, TEXT, "la")
        _shadow(d, (x1 - 12, y0 + 18), date, F_LABEL, FUR, "ra")
        unit = forecast.get("unit", "")
        _shadow(d, (x1 - 12, y0 + 92), _fit(d, f"{forecast['temp']}°{unit}", F_TEMP, x1 - x0 - 118),
                F_TEMP, TEXT, "ra")
        _shadow(d, (x1 - 12, y0 + 138), f"↑{forecast['hi']}°  ↓{forecast['lo']}°", F_VALUE, TEXT, "ra")
        as_of = "as of " + (forecast.get("as_of")
                            or time.strftime("%H:%M", time.localtime(forecast["at"])))
        _shadow(d, (x1 - 10, y1 - 8), as_of, F_SMALL, (200, 206, 216), "rs")
        room = x1 - x0 - 28 - int(d.textlength(as_of, font=F_SMALL)) - 8
        _shadow(d, (x0 + 12, y1 - 8), _fit(d, forecast.get("summary", ""), F_SMALL, room), F_SMALL,
                (200, 206, 216), "ls")
        _big_cat(img, d, mood, frame, pose, cx + xcells * BIG_PET_SCALE, cy, wear)
        d.text((14, H - 14), _fit(d, f"NO LINK · {detail}", F_HINT, WIFI_BTN[0] - 24),
               font=F_HINT, fill=MUTED, anchor="ls")
    else:
        cx, cy = 30, 150
        _big_cat(img, d, mood, frame, pose, cx + xcells * BIG_PET_SCALE, cy, wear)
        sx = (scenes.SCREEN[0] + scenes.SCREEN[2] + 1) * scenes.K // 2   # the monitor's middle
        d.text((sx, 112), clock, font=F_CLOCK, fill=(220, 232, 250), anchor="mm")
        d.text((sx, 160), time.strftime("%A %-d %B", now), font=F_DATE, fill=FUR, anchor="mm")
        d.text((sx, 190), _fit(d, detail, F_VALUE, 210), font=F_VALUE, fill=(150, 160, 180), anchor="mm")
        d.text((14, H - 14), day_line(stats), font=F_HINT, fill=(150, 140, 130), anchor="ls")
    if button:
        _wifi_button(d, button)
    if dim:
        return Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.3)
    return img


def render_night(now, detail, iface, frame=0, pose="loaf", xcells=0, wifi=None):
    """Desk mode at night: the cat asleep in its nightcap on the rooftop ledge
    (scenes.rooftop), the moon, a quiet clock in the sky, all dimmed."""
    img = scenes.rooftop(frame)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, HEADER_H), fill=(34, 38, 46))
    _title(d)
    d.text((W - 12, HEADER_H // 2), f"{iface} · NO LINK", font=F_LABEL, fill=TEXT, anchor="rm")
    _wifi_client(d, wifi, f"{iface} · NO LINK")
    _big_cat(img, d, "sleep", frame, pose, 30 + xcells * BIG_PET_SCALE, 140, ["nightcap"])
    d.text((300, 112), time.strftime("%H:%M", now), font=F_CLOCK, fill=(150, 156, 175), anchor="mm")
    d.text((300, 158), _fit(d, detail, F_VALUE, 240), font=F_VALUE, fill=(100, 106, 125), anchor="mm")
    return Image.blend(img, Image.new("RGB", (W, H), (0, 0, 0)), 0.2)


# --- output -----------------------------------------------------------------

def find_panel():
    """The fbtft SPI panel registers as fb_<driver>; HDMI's fb0 is never picked here."""
    for fb in sorted(glob.glob("/sys/class/graphics/fb[0-9]*")):
        try:
            with open(os.path.join(fb, "name")) as f:
                if f.read().strip().startswith("fb_"):
                    return "/dev/" + os.path.basename(fb)
        except OSError:
            continue
    return None


def wait_for_panel(timeout):
    """The panel driver can still be probing when the service starts."""
    end = time.monotonic() + timeout
    while True:
        dev = find_panel()
        if dev or time.monotonic() >= end:
            return dev
        time.sleep(1)


def _to_rgb565(img):
    r, g, b = img.split()
    hi = ImageChops.add(r.point(lambda x: x & 0xF8), g.point(lambda x: x >> 5))
    lo = ImageChops.add(g.point(lambda x: (x << 3) & 0xE0), b.point(lambda x: x >> 3))
    return Image.merge("LA", (lo, hi)).tobytes()


KDSETMODE, KDGETMODE, KD_TEXT, KD_GRAPHICS = 0x4B3A, 0x4B3B, 0x00, 0x01


CURSOR_BLINK = "/sys/class/graphics/fbcon/cursor_blink"


class ConsoleOff:
    """Stops the text console drawing on the panel. Without an HDMI screen the panel is
    fb0, the console takes it and its login prompt lands on top of our screen. Graphics
    mode on the active virtual terminal keeps the console quiet until restore(); the
    cursor's blink timer keeps running in graphics mode, so the cursor is hidden and its
    blink switched off too."""

    def __init__(self, tty="/dev/tty0"):
        self.fd = None
        self.blink = None
        try:
            self.fd = os.open(tty, os.O_RDWR | os.O_NOCTTY)
            os.write(self.fd, b"\033[?25l")  # hide the cursor
            fcntl.ioctl(self.fd, KDSETMODE, KD_GRAPHICS)
        except OSError:
            self.restore()
            return
        try:
            with open(CURSOR_BLINK) as f:
                self.blink = f.read().strip()
            with open(CURSOR_BLINK, "w") as f:
                f.write("0")
        except OSError:
            self.blink = None

    def keep(self):
        """Puts the terminal back in graphics mode if something switched it to text (it
        then draws the login prompt over the panel); True when it had to."""
        if self.fd is None:
            return False
        try:
            mode = bytearray(4)
            fcntl.ioctl(self.fd, KDGETMODE, mode)
            if int.from_bytes(mode, "little") == KD_GRAPHICS:
                return False
            os.write(self.fd, b"\033[?25l")
            fcntl.ioctl(self.fd, KDSETMODE, KD_GRAPHICS)
        except OSError:
            return False
        print("console: had switched back to text mode, hidden again", flush=True)
        return True

    def release(self):
        """Lets go of the terminal and leaves it in graphics mode: the system is going down,
        and the console's shutdown messages must not land on our last screen."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.blink = None

    def restore(self):
        if self.blink is not None:
            try:
                with open(CURSOR_BLINK, "w") as f:
                    f.write(self.blink)
            except OSError:
                pass
            self.blink = None
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, KDSETMODE, KD_TEXT)
                os.write(self.fd, b"\033[?25h")  # show the cursor again
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


class Framebuffer:
    def __init__(self, dev, rotate=0):
        self.dev = dev
        sysfs = f"/sys/class/graphics/{os.path.basename(dev)}"
        with open(f"{sysfs}/virtual_size") as f:
            self.width, self.height = map(int, f.read().strip().split(","))
        with open(f"{sysfs}/bits_per_pixel") as f:
            self.bpp = int(f.read().strip())
        if rotate is None:
            rotate = 90 if self.height > self.width else 0
        self.rotate = rotate

    def show(self, img, band=None):
        """band=(y0, y1): write only those lines (the cat's corner), for cheap animation.
        Falls back to a full write when the panel is rotated or scaled."""
        if band and not self.rotate and img.size == (self.width, self.height):
            y0, y1 = band
            part = img.crop((0, y0, self.width, y1))
            data = _to_rgb565(part) if self.bpp == 16 else part.convert("RGBA").tobytes("raw", "BGRA")
            with open(self.dev, "r+b") as f:
                f.seek(y0 * self.width * self.bpp // 8)
                f.write(data)
            return
        if self.rotate:
            img = img.rotate(self.rotate, expand=True)
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        if self.bpp == 16:
            data = _to_rgb565(img)
        else:
            data = img.convert("RGBA").tobytes("raw", "BGRA")
        with open(self.dev, "wb") as f:
            f.write(data)


class PngOutput:
    def __init__(self, path):
        self.path = path

    def show(self, img, band=None):
        img.save(self.path)

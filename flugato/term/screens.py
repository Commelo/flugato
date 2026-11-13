"""The terminal app's screens, drawn into a Grid from the app's state (tui.State): the
dashboard (with or without root), the ports, the VLANs, the history, the help. Pure: no
curses, no network, so the tests draw them from fake state and read the text back."""

import time

from ..checks import FAIL, OK, PENDING, SKIP, WARN
from . import catframes, history, runner

STATE_ROLE = {OK: "ok", WARN: "warn", FAIL: "fail", SKIP: "skip", PENDING: "skip"}
BANNER_BG = {OK: "ok_bg", WARN: "warn_bg", FAIL: "fail_bg", PENDING: "wait_bg", SKIP: "wait_bg"}
CAT_W, CAT_H = catframes.W, catframes.H // 2         # 28 x 9 character cells
MIN_CAT_COLS = 90


def banner_state(word):
    return {"ALL GOOD": OK, "CHECK WARNINGS": WARN, "CHECK CABLE": WARN, "PROBLEM FOUND": FAIL,
            "NO DHCP": FAIL, "NO LINK": SKIP}.get(word, PENDING)


def title_bar(g, port, right):
    x = g.put(1, 0, "flu", bold=True)
    x = g.put(x, 0, "GATO", fg="accent", bold=True)
    if port:
        x = g.put(x + 2, 0, "▸ ", fg="muted")
        g.put(x, 0, port, bold=True)
    if g.w - 1 - len(right) > x + 2:
        g.right(g.w - 1, 0, right, fg="muted")
    g.rule(1)


def banner(g, y, state, word, detail=""):
    bg = BANNER_BG[state]
    g.fill(0, y, g.w, 3, bg)
    g.put(2, y + 1, word, fg="white", bg=bg, bold=True)
    if detail:
        g.right(g.w - 2, y + 1, detail[:g.w - 6 - len(word)], fg="white", bg=bg)


def footer(g, keys):
    g.rule(g.h - 2)
    x = 1
    for k, label in keys:
        if x + len(k) + len(label) + 2 > g.w:
            break
        x = g.put(x, g.h - 1, k, fg="accent", bold=True)
        x = g.put(x + 1, g.h - 1, label, fg="muted") + 3


def check_rows(g, y, report, width, notes=None):
    for i, (label, row) in enumerate(report.items()):
        yy = y + i
        g.put(2, yy, "●", fg=STATE_ROLE[row.state])
        g.put(4, yy, label, bold=True)
        value = row.value or "—"
        g.put(15, yy, value[:max(0, width - 17)], fg="text" if row.state not in (SKIP, PENDING) else "muted")
        if notes and label in notes and len(value) + 17 + len(notes[label]) + 2 < width:
            g.right(width - 2, yy, notes[label], fg="muted")


def clock(st):
    return time.strftime("%H:%M", time.localtime(st.now()))


def cat_box(g):
    """Where the cat goes, or None when the terminal is too narrow or too short."""
    if g.w < MIN_CAT_COLS or g.h < 24:
        return None
    return g.w - CAT_W - 2, g.h - 2 - CAT_H


def draw_cat(g, st, pose=None, mood=None):
    box = cat_box(g)
    if not box or not st.cat_on:
        return
    x, y = box
    g.cat(x, y, catframes.rows(mood or st.cat_mood, pose or st.cat_pose))


def dashboard(g, st):
    r = st.runner
    right = ("root · test in its own namespace" if st.root else "no root · read only") + " · " + clock(st)
    title_bar(g, st.iface, right)
    word = r.verdict if r else "NO LINK"
    state = banner_state(word)
    detail = f"plug a cable into {st.iface}" if word == "NO LINK" else st.banner_detail()
    if st.message:
        word, detail, state = st.message[0], st.message[1], PENDING
    banner(g, 2, state, word, detail)
    width = g.w if not cat_box(g) else cat_box(g)[0] - 1
    if r:
        notes = {}
        if not st.root and r.report["SWITCH"].state == SKIP and r.report["SWITCH"].value == runner.NEEDS_ROOT:
            notes["SWITCH"] = "press s"
        check_rows(g, 6, r.report, g.w, notes)
    y = 16
    if not st.root and r and r.done and r.link_up and r.report["SWITCH"].value == runner.NEEDS_ROOT and g.h >= 28:
        g.put(2, y, "Without root fluGATO only reads what this computer already knows.", fg="muted")
        x = g.put(2, y + 1, "s", fg="accent", bold=True)
        g.put(x + 1, y + 1, "re-runs with sudo: a fresh DHCP offer, the switch and port over LLDP/CDP, the VLANs.", fg="muted")
        y += 3
    g.put(2, y, "RECENT TESTS", fg="muted", bold=True)
    rows = max(0, min(len(st.history), g.h - 3 - (y + 1) - 1))
    for i, e in enumerate(st.history[:rows]):
        yy = y + 1 + i
        g.put(2, yy, history.when(e, st.now()), fg="muted")
        g.put(9, yy, e.get("port", "")[:11])
        sw = " ".join(x for x in (e.get("switch"), e.get("switch_port")) if x) or "no switch heard"
        g.put(21, yy, sw[:max(0, width - 21 - 22)])
        speed = e.get("speed")
        g.put(width - 20, yy, f"{speed // 1000}G" if speed and speed >= 1000 else (f"{speed}M" if speed else ""))
        v = e.get("verdict", "")
        g.put(width - 15, yy, "●", fg=STATE_ROLE[banner_state(v)])
        g.put(width - 13, yy, v[:13], fg=STATE_ROLE[banner_state(v)], bold=True)
    if not st.history:
        g.put(2, y + 1, "none yet", fg="muted")
    if g.h >= 28:
        g.put(2, y + 1 + max(rows, 1) + 1, f"Saved in {st.history_path}"[:width - 4], fg="muted")
    draw_cat(g, st)
    keys = [("r", "re-run"), ("v", "VLANs"), ("p", "ports"), ("b", "blink port"), ("h", "history"),
            ("?", "help"), ("q", "quit")]
    if not st.root:
        keys.insert(1, ("s", "sudo"))
        keys = [k for k in keys if k[0] != "b"]
    if st.cable_port:
        keys.insert(3, ("c", "cable test"))
    footer(g, keys)


def ports(g, st):
    title_bar(g, "", "pick a port · " + clock(st))
    r = st.runner
    word = r.verdict if r else "NO LINK"
    banner(g, 2, banner_state(word), word, st.banner_detail() if word != "NO LINK" else f"plug a cable into {st.iface}")
    g.put(2, 6, "PORTS", fg="muted", bold=True)
    g.put(4, 7, "port".ljust(12) + "hardware".ljust(22) + "state", fg="muted")
    for i, p in enumerate(st.ports):
        yy = 8 + i
        if yy >= g.h - 6:
            break
        sel = i == st.selected
        if sel:
            g.put(2, yy, "▸", fg="accent", bold=True)
        g.put(4, yy, p["name"][:11], bold=sel)
        g.put(16, yy, p["hardware"][:21])
        g.put(38, yy, "●", fg=p["role"])
        g.put(40, yy, p["state"][:17])
        g.put(58, yy, p["note"][:max(0, g.w - 60)], fg="muted")
    y = 8 + min(len(st.ports), g.h - 14) + 1
    if st.root:
        lines = ("The test ports run in their own network namespace: their DHCP address, routes and",
                 "DNS never reach the rest of this computer, so testing a cable cannot cut your",
                 "Wi-Fi, VPN or SSH session.")
    else:
        lines = ("Without root the port is only read, nothing is configured. A port that carries",
                 "this computer's default route is your own connection: it is never a test port.")
    for i, line in enumerate(lines):
        if y + i < g.h - 3:
            g.put(2, y + i, line[:g.w - 4], fg="muted")
    draw_cat(g, st, pose=st.cat_pose if st.cat_mood == "sleep" else None)
    footer(g, [("↑↓", "choose"), ("enter", "test it"), ("t", "make it the default"), ("esc", "back"), ("q", "quit")])


def vlans(g, st):
    r = st.runner
    title_bar(g, st.iface, ("root" if st.root else "no root") + " · VLANs · " + clock(st))
    native, tagged, offers = (r.native, r.tagged, r.offers) if r else (None, None, {})
    n = r.neighbour if r else None
    if tagged:
        word = f"TRUNK  " + (f"native {native} + " if native else "") + f"{len(tagged)} tagged"
    elif native:
        word = f"ACCESS  native {native}"
    else:
        word = "VLANS"
    via = f"{n['switch']} {n['port']} ({n['via']})" if n and n.get("switch") else ""
    banner(g, 2, OK if tagged or native else PENDING, word, via)
    g.put(4, 6, "VLAN".ljust(8) + "seen as".ljust(12) + "DHCP".ljust(10) + "address offered".ljust(22) + "router", fg="muted")
    g.rule(7, 2, min(70, g.w - 4))
    lines = []
    if native:
        lines.append((native, "native", "native"))
    for v in sorted(tagged or []):
        lines.append((v, "tagged", offers.get(v, "probing") if v in offers else "probing"))
    if not lines:
        msg = ("needs root: sudo re-runs with the VLAN listen and probes" if not st.root
               else ("no VLAN heard on this port" if r and r.done else "listening…"))
        g.put(4, 8, msg, fg="muted")
    for i, (v, how, off) in enumerate(lines):
        yy = 8 + i
        if yy >= g.h - 5:
            g.put(4, yy, f"… {len(lines) - i} more", fg="muted")
            break
        g.put(4, yy, str(v), bold=True)
        g.put(12, yy, how, fg="muted" if how == "tagged" else "text")
        if off == "native":
            if r and r.lease:
                g.put(24, yy, "●", fg="ok"); g.put(26, yy, "lease", fg="ok")
                g.put(34, yy, f"{r.lease['address']}/{r.lease['prefix']}")
                g.put(56, yy, r.lease.get("router") or "")
            elif r and r.report["IP"].state == OK:
                g.put(24, yy, "●", fg="ok"); g.put(26, yy, "this port", fg="ok")
                g.put(34, yy, r.report["IP"].value.split()[0])
        elif off == "probing":
            g.put(24, yy, "…", fg="skip"); g.put(34, yy, "probing", fg="muted")
        elif off is None:
            g.put(24, yy, "●", fg="warn"); g.put(26, yy, "no offer", fg="warn")
        else:
            g.put(24, yy, "●", fg="ok"); g.put(26, yy, "offer", fg="ok")
            g.put(34, yy, f"{off['address']}/{off['prefix']}")
            g.put(56, yy, off.get("router") or "")
    if g.h >= 26:
        g.put(2, g.h - 5, "One DHCP probe per tagged VLAN, from a throwaway MAC, inside the test", fg="muted")
        g.put(2, g.h - 4, "port's namespace. No lease is taken on them.", fg="muted")
    draw_cat(g, st)
    footer(g, [("esc", "back"), ("r", "re-run"), ("q", "quit")])


def history_screen(g, st):
    title_bar(g, st.iface, "history · " + clock(st))
    g.put(2, 2, "RECENT TESTS", fg="muted", bold=True)
    g.put(2, 3, f"{st.history_path}"[:g.w - 4], fg="muted")
    g.put(2, 5, "when".ljust(7) + "port".ljust(12) + "switch and port".ljust(30) + "link".ljust(6) + "verdict", fg="muted")
    rows = g.h - 9
    for i, e in enumerate(st.history_all[st.scroll:st.scroll + rows]):
        yy = 6 + i
        g.put(2, yy, history.when(e, st.now()), fg="muted")
        g.put(9, yy, e.get("port", "")[:11])
        sw = " ".join(x for x in (e.get("switch"), e.get("switch_port")) if x) or "no switch heard"
        g.put(21, yy, sw[:29])
        speed = e.get("speed")
        g.put(51, yy, f"{speed // 1000}G" if speed and speed >= 1000 else (f"{speed}M" if speed else ""))
        v = e.get("verdict", "")
        g.put(57, yy, "●", fg=STATE_ROLE[banner_state(v)])
        g.put(59, yy, v, fg=STATE_ROLE[banner_state(v)], bold=True)
    if not st.history_all:
        g.put(2, 6, "none yet", fg="muted")
    footer(g, [("↑↓", "scroll"), ("esc", "back"), ("q", "quit")])


HELP = (
    ("r", "run the checks again on the test port"),
    ("s", "re-run with sudo (the switch, the VLANs, a fresh DHCP offer)"),
    ("v", "the VLANs heard on the port and what DHCP offers on each"),
    ("p", "pick another port; t makes it the default"),
    ("c", "cable test: both ends of a patch cord in two ports"),
    ("b", "blink the switch port's LED (the link goes down and up three times)"),
    ("h", "the history of runs"),
    ("q", "quit"),
)


def help_screen(g, st):
    title_bar(g, st.iface, "help · " + clock(st))
    g.put(2, 2, f"fluGATO {st.version}", bold=True)
    g.put(2, 3, "Plug a cable in. The checks run by themselves when the link comes up.", fg="muted")
    for i, (k, text) in enumerate(HELP):
        g.put(2, 5 + i, k, fg="accent", bold=True)
        g.put(6, 5 + i, text[:g.w - 8])
    y = 5 + len(HELP) + 1
    lines = ("flugato --once     the rows on stdout; the exit code is the verdict:",
             "                   0 good, 1 warnings, 2 a problem, 3 no link",
             "flugato --json     the same as one JSON object;  --quiet  nothing, the code only",
             "flugato --watch    a line whenever the port changes",
             "flugato update     the latest release;  flugato uninstall  removes it",
             "settings: ~/.config/flugato/config (PORT, CHECK_HOST, LLDP_WAIT, PROBE_VLANS)")
    for i, line in enumerate(lines):
        if y + i < g.h - 3:
            g.put(2, y + i, line[:g.w - 4], fg="muted")
    draw_cat(g, st)
    footer(g, [("esc", "back"), ("q", "quit")])


def cable(g, st):
    title_bar(g, st.iface, ("cable test · " + clock(st)))
    word = st.cable_word or "CABLE TEST…"
    state = {"CABLE PASS": OK, "CABLE WARN": WARN, "CABLE FAIL": FAIL}.get(word, PENDING)
    banner(g, 2, state, word, f"{st.iface} to {st.cable_port}: both ends of the cord in the tester")
    if st.cable_report:
        check_rows(g, 6, st.cable_report, g.w)
    if g.h >= 24:
        g.put(2, 14, "Test frames go both ways between the two ports; lost frames, CRC errors or a link", fg="muted")
        g.put(2, 15, "slower than both ports can do point at the cable. No per-pair test (no TDR).", fg="muted")
    draw_cat(g, st, mood={OK: "good", WARN: "warn", FAIL: "fail"}.get(state, "testing"))
    footer(g, [("esc", "back"), ("q", "quit")])


SCREENS = {"dash": dashboard, "ports": ports, "vlans": vlans, "history": history_screen, "help": help_screen,
           "cable": cable}

"""The full-screen app (curses): the state the screens draw from, the worker threads (the
checks, the blink, the cable test, the carrier watch) and the key loop. The screens
themselves are in screens.py; nothing here decides what a row says."""

import curses
import os
import random
import threading
import time

from .. import cable, checks
from ..checks import FAIL, OK, PENDING, SKIP, WARN
from . import VERSION, catframes, cli, grid, history, ns, runner, screens

TICK = 0.5


# --- the ports -------------------------------------------------------------------------------

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def hardware(name):
    """A short name for what the port is: from the PCI ids file, a USB product string, or
    the driver's name."""
    dev = os.path.realpath(f"/sys/class/net/{name}/device")
    vendor, device = _read(f"{dev}/vendor"), _read(f"{dev}/device")
    if vendor and device:
        for ids in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
            found = pci_name(ids, vendor[2:].lower(), device[2:].lower())
            if found:
                return found
    for parent in (dev, os.path.dirname(dev)):
        product = _read(f"{parent}/product")
        if product:
            return product[:24]
    driver = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(f"{dev}/driver") else ""
    return driver or "?"


def pci_name(path, vendor, device):
    try:
        with open(path, errors="replace") as f:
            vname, in_vendor = None, False
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                if not line.startswith("\t"):
                    in_vendor = line[:4].lower() == vendor
                    vname = line[4:].strip().split(" ")[0].rstrip(",") if in_vendor else None
                elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == device:
                    dname = line[5:].strip()
                    for junk in ("Ethernet Connection", "Ethernet Controller", "Gigabit", "Network Connection",
                                 "PCI Express", "Family", "Controller"):
                        dname = dname.replace(junk, "")
                    dname = " ".join(dname.split())
                    return f"{vname} {dname}"[:24]
    except OSError:
        pass
    return None


def port_state(name):
    if not checks.carrier(name):
        return "no cable", "skip"
    addr = checks.parse_ip_addr(checks.run(["ip", "-j", "-4", "addr", "show", "dev", name]))
    if addr:
        return f"up  {addr[0]}", "ok"
    return "up, no address", "warn"


def list_ports(root, iface):
    out = []
    if root:
        mine = ns.ns_ports()
        for i, p in enumerate(mine):
            state, role = port_state(p)
            note = "test port" if p == iface else "second port: cable test" if len(mine) > 1 else ""
            out.append({"name": p, "hardware": hardware(p), "state": state, "role": role, "note": note, "testable": True})
        for h in ns.host_ports():
            if h["name"] in mine:
                continue
            note = "your connection: never touched" if h["own"] else ("Wi-Fi" if h["kind"] == "wifi" else "")
            out.append({"name": h["name"], "hardware": "Wi-Fi" if h["kind"] == "wifi" else "wired",
                        "state": "in use" if h["own"] else "", "role": "ok" if h["own"] else "skip",
                        "note": note, "testable": False})
    else:
        own = set(ns.default_route_devs())
        for p in ns.wired_ports():
            state, role = port_state(p)
            note = "your connection: never touched" if p in own else "test port" if p == iface else ""
            out.append({"name": p, "hardware": hardware(p), "state": state, "role": role, "note": note,
                        "testable": p not in own})
        for p in ns.wireless_ports():
            state, role = port_state(p)
            out.append({"name": p, "hardware": "Wi-Fi", "state": state, "role": role,
                        "note": "your connection: never touched" if p in own else "", "testable": False})
    return out


# --- the state ---------------------------------------------------------------------------------

class State:
    def __init__(self, iface, root, cat_on=True, now=time.time):
        self.iface, self.root, self.cat_on, self.now = iface, root, cat_on, now
        self.version = VERSION
        self.runner = None
        self.screen = "dash"
        self.message = None                     # (word, detail) shown in the banner meanwhile
        self.history, self.history_all = [], []
        self.history_path = history.path()
        self.ports, self.selected, self.scroll = [], 0, 0
        self.cable_port, self.cable_report, self.cable_word = None, None, ""
        self.cat_mood, self.cat_pose = "sleep", "loaf"
        self.hardware = ""

    def banner_detail(self):
        r = self.runner
        if not r:
            return ""
        bits = [self.iface]
        if self.hardware:
            bits.append(self.hardware)
        if r.link:
            bits.append(runner.link_text(r.link))
        if r.report["IP"].state == OK:
            bits.append(r.report["IP"].value.split()[0])
        elif r.done and r.link_up:
            bits.append("no address")
        return " · ".join(bits)


# --- colours -----------------------------------------------------------------------------------

def colour256(rgb):
    r, g, b = rgb
    if abs(r - g) < 10 and abs(g - b) < 10:
        grey = (r + g + b) // 3
        if grey < 8:
            return 16
        if grey > 238:
            return 231
        return 232 + round((grey - 8) / 10)
    q = lambda v: round(v / 255 * 5)
    return 16 + 36 * q(r) + 6 * q(g) + q(b)


class Palette:
    """Role -> curses colour, pairs made as they are first needed."""

    def __init__(self):
        self.pairs, self.ok = {}, False
        try:
            curses.start_color()
            curses.use_default_colors()
            self.ok = curses.has_colors()
        except curses.error:
            self.ok = False
        many = self.ok and curses.COLORS >= 256
        self.fg = {"text": -1, "muted": 245 if many else 8, "ok": 2, "warn": 3, "fail": 1,
                   "skip": 245 if many else 8, "accent": 208 if many else 3, "rule": 240 if many else 8,
                   "prompt": 4, "white": 231 if many else 7}
        self.bg = {"ok_bg": 28 if many else 2, "warn_bg": 94 if many else 3, "fail_bg": 88 if many else 1,
                   "wait_bg": 240 if many else 0}
        self.cat_ok = many

    def number(self, role, is_bg):
        if isinstance(role, tuple):
            return colour256(role)
        if is_bg:
            return self.bg.get(role, -1)
        return self.fg.get(role, -1)

    def attr(self, fg, bg, bold):
        a = curses.A_BOLD if bold else 0
        if not self.ok:
            return a
        key = (self.number(fg, False), self.number(bg, True) if bg is not None else -1)
        if key not in self.pairs:
            n = len(self.pairs) + 1
            if n >= curses.COLOR_PAIRS:
                return a
            try:
                curses.init_pair(n, key[0], key[1])
            except (curses.error, ValueError):
                return a
            self.pairs[key] = n
        return a | curses.color_pair(self.pairs[key])


# --- the cat's little life ----------------------------------------------------------------------

AWAKE_ACTS = (["blink"], ["blink"], ["look_l", "look_l", "look_l"], ["look_r", "look_r", "look_r"],
              ["look_up", "look_up"], ["yawn1", "yawn2", "yawn2", "yawn1"],
              ["lick1", "lick2", "lick1", "lick2", "lick1"], ["stretch", "stretch", "stretch", "stretch"],
              ["twitch", "sit", "twitch"])
SLEEP_ACTS = (["loaf_awake", "loaf_look_l", "loaf_look_l", "loaf_look_r", "loaf_look_r", "loaf_awake"],
              ["loaf_yawn", "loaf_yawn", "loaf_yawn"])


class Cat:
    def __init__(self, st):
        self.st, self.queue, self.next_act, self.breath = st, [], 0, 0

    def tick(self, now):
        st = self.st
        mood = st.runner.mood if st.runner else "sleep"
        if mood != st.cat_mood:
            st.cat_mood, self.queue = mood, []
            self.next_act = now + random.uniform(4, 10)
        if self.queue:
            st.cat_pose = self.queue.pop(0)
            return
        if mood == "sleep":
            self.breath += 1
            st.cat_pose = "loaf_in" if self.breath % 6 in (3, 4) else "loaf"
            if now >= self.next_act:
                self.queue = list(random.choice(SLEEP_ACTS))
                self.next_act = now + random.uniform(15, 40)
        elif mood == "testing":
            st.cat_pose = ("look_l", "look_l", "look_r", "look_r")[self.breath % 4]
            self.breath += 1
        else:
            st.cat_pose = "sit1" if self.breath % 8 in (3, 4) else "sit"
            self.breath += 1
            if now >= self.next_act:
                self.queue = list(random.choice(AWAKE_ACTS))
                self.next_act = now + random.uniform(6, 20)


# --- the app --------------------------------------------------------------------------------------

class App:
    def __init__(self, args, settings, iface, root):
        self.args, self.settings = args, settings
        self.st = State(iface, root, cat_on=not args.no_cat)
        self.dirty = threading.Event()
        self.thread, self.busy = None, False      # busy: blink or cable test running
        self.last_carrier, self.last_poll = None, 0
        self.cat = Cat(self.st)
        self.result = None

    # --- runs ---
    def mark(self):
        self.dirty.set()

    def start_run(self):
        st = self.st
        if st.runner and not st.runner.done:
            st.runner.stop.set()
        if self.args.demo:
            st.runner = cli.demo_runner(self.args.demo)
            st.hardware = "Intel I219-V"
            self.mark()
            return
        st.hardware = st.hardware or hardware(st.iface)
        r = st.runner = cli.make_runner(self.args, self.settings, st.iface, st.root)
        r.on_change = self.mark
        old = self.thread

        def work():
            if old and old.is_alive():
                old.join(3)
            r.run()
            if r.link_up and not r.interrupted():
                history.append(history.entry(st.iface, r.verdict, runner.EXIT.get(r.verdict, 2), r.report,
                                             r.neighbour, r.link))
                st.history = history.recent(6)
            self.mark()

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def blink(self):
        st = self.st
        if not st.root or self.busy or self.args.demo:
            st.message = ("NEEDS ROOT", "blinking the port takes the link down and up: sudo") if not st.root else None
            self.mark()
            return
        self.busy = True

        def work():
            checks.blink_port(st.iface, status=lambda t, d: (setattr(st, "message", (t, d)), self.mark()))
            st.message = None
            self.busy = False
            self.mark()

        threading.Thread(target=work, daemon=True).start()

    def cable_test(self):
        st = self.st
        if not st.cable_port or self.busy:
            return
        self.busy, st.screen, st.cable_word = True, "cable", "CABLE TEST…"
        r = st.cable_report = {label: checks.Row(label) for label in ("PORT A", "PORT B", "A TO B", "B TO A", "ERRORS", "RESULT")}
        a_if, b_if = st.iface, st.cable_port

        def work():
            a, b = cable.link(a_if), cable.link(b_if)
            for row, info, name in ((r["PORT A"], a, a_if), (r["PORT B"], b, b_if)):
                speed = checks.fmt_speed(info["speed"]) if info["speed"] else "speed unknown"
                row.set(" ".join(filter(None, (speed, info["duplex"]))) + f"  {name}", OK if info["speed"] else WARN)
            self.mark()
            before = cable.errors(a_if) + cable.errors(b_if)
            runs = {}
            for label, tx, rx in (("A TO B", a_if, b_if), ("B TO A", b_if, a_if)):
                r[label].set("sending test frames…", PENDING)
                self.mark()
                runs[label] = cable.frames_one_way(tx, rx)
                r[label].set(*cable.fmt_run(runs[label]))
                self.mark()
            new = cable.errors(a_if) + cable.errors(b_if) - before
            r["ERRORS"].set(f"{new} CRC / frame errors", OK if not new else FAIL)
            state, text = cable.verdict(a, b, runs["A TO B"], runs["B TO A"], new)
            r["RESULT"].set(text, state)
            st.cable_word = {OK: "CABLE PASS", WARN: "CABLE WARN", FAIL: "CABLE FAIL"}[state]
            self.busy = False
            self.mark()

        threading.Thread(target=work, daemon=True).start()

    def poll_carrier(self, now):
        if self.args.demo or self.busy or now - self.last_poll < TICK:
            return
        self.last_poll = now
        up = checks.carrier(self.st.iface)
        if self.last_carrier is None:
            self.last_carrier = up
            return
        if up != self.last_carrier:
            self.last_carrier = up
            if up:
                time.sleep(0.5)       # let the driver settle before ethtool is asked
            self.start_run()

    # --- keys ---
    def key(self, ch):
        st = self.st
        name = curses.keyname(ch).decode() if ch >= 0 else ""
        if name in ("q", "Q") and st.screen in ("dash",) or name == "^C":
            return "quit"
        if st.screen == "dash":
            if name == "r":
                self.start_run()
            elif name == "s" and not st.root:
                return "sudo"
            elif name == "v":
                st.screen = "vlans"
            elif name == "p":
                st.ports = list_ports(st.root, st.iface)
                st.selected = next((i for i, p in enumerate(st.ports) if p["name"] == st.iface), 0)
                st.screen = "ports"
            elif name == "b":
                self.blink()
            elif name == "c":
                self.cable_test()
            elif name == "h":
                st.history_all, st.scroll, st.screen = history.recent(500), 0, "history"
            elif name == "?":
                st.screen = "help"
        elif st.screen == "ports":
            if name == "KEY_UP":
                st.selected = max(0, st.selected - 1)
            elif name == "KEY_DOWN":
                st.selected = min(len(st.ports) - 1, st.selected + 1)
            elif name in ("^J", "^M", "KEY_ENTER") and st.ports:
                p = st.ports[st.selected]
                if p["testable"]:
                    st.iface, st.hardware, st.screen = p["name"], p["hardware"], "dash"
                    self.last_carrier = None
                    self.cable_port = None
                    st.cable_port = self.second_port()
                    self.start_run()
                else:
                    st.message = ("NOT A TEST PORT", f"{p['name']} is {p['note'] or 'not testable'}")
            elif name == "t" and st.ports:
                p = st.ports[st.selected]
                if p["testable"] and cli.save_setting("PORT", p["name"]):
                    p["note"] = "default test port (saved)"
            elif name in ("^[", "q"):
                st.screen = "dash"
        elif st.screen == "history":
            if name == "KEY_UP":
                st.scroll = max(0, st.scroll - 1)
            elif name == "KEY_DOWN":
                st.scroll = min(max(0, len(st.history_all) - 1), st.scroll + 1)
            elif name in ("^[", "q", "h"):
                st.screen = "dash"
        elif st.screen in ("vlans", "help", "cable"):
            if name == "r" and st.screen == "vlans":
                self.start_run()
            elif name in ("^[", "q", "v", "?", "c") or (st.screen == "cable" and not self.busy and name in ("^J", "^M")):
                st.screen = "dash"
        if name != "q" or st.screen == "dash":
            self.mark()
        return None

    def second_port(self):
        st = self.st
        if not st.root or self.args.demo:
            return None
        others = [p for p in ns.ns_ports() if p != st.iface]
        return others[0] if others else None

    # --- curses ---
    def draw(self, scr, pal):
        curses.update_lines_cols()
        h, w = scr.getmaxyx()
        g = grid.Grid(w, h)
        screens.SCREENS[self.st.screen](g, self.st)
        scr.erase()
        for y, row in enumerate(g.cells):
            for x, (ch, fg, bg, bold) in enumerate(row):
                if ch == " " and bg is None:
                    continue
                try:
                    scr.addstr(y, x, ch, pal.attr(fg, bg, bold))
                except curses.error:
                    pass      # the bottom-right cell: curses objects after writing it
        scr.refresh()

    def main(self, scr):
        curses.curs_set(0)
        scr.timeout(100)
        scr.keypad(True)
        try:
            curses.set_escdelay(25)
        except AttributeError:
            pass
        pal = Palette()
        if not pal.cat_ok:
            self.st.cat_on = False
        self.st.history = history.recent(6)
        self.st.cable_port = self.second_port()
        self.last_carrier = None if not self.args.demo else True
        self.start_run()
        last_tick = 0
        while True:
            ch = scr.getch()
            if ch == curses.KEY_RESIZE:
                self.mark()
            elif ch != -1:
                out = self.key(ch)
                if out:
                    return out
            now = time.monotonic()
            self.poll_carrier(now)
            if now - last_tick >= TICK:
                last_tick = now
                self.cat.tick(now)
                self.mark()
            if self.dirty.is_set():
                self.dirty.clear()
                self.draw(scr, pal)


def run(args, settings, iface, root):
    """-> None (quit), "sudo" (re-run with sudo) or an exit code."""
    app = App(args, settings, iface, root)
    try:
        out = curses.wrapper(app.main)
    except KeyboardInterrupt:
        out = None
    finally:
        r = app.st.runner
        if r and not args.demo:
            r.stop.set()
            r.release()
    return out

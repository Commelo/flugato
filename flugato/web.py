"""Web UI: status, settings and captures, for a phone on the fluGATO hotspot or a PC on the
Wi-Fi network fluGATO joined. Standard library only (http.server), plain HTML and CSS, no
scripts, nothing loaded from elsewhere.

Each server listens on one address only (the hotspot's, or the Pi's on the joined Wi-Fi
network), never on the network under test. On the hotspot there is no PIN (being on it means
operating fluGATO); on a joined network the pages need the PIN (WEB_PIN): a session cookie
after a right PIN, a growing lock-out after wrong ones. It runs in its own threads; start()
it after the network is up and stop() it before it goes down. The app keeps the status page current with
publish(); settings are written to the settings file (config.save) and handed to
on_saved, so the app can apply the cheap ones at once. The Wi-Fi page saves networks for
fluGATO to join (networks.py) and hands a Connect to the app through on_join.

Passwords typed into the Wi-Fi page are never shown again, written anywhere but the
networks file, or logged; requests are not logged at all."""

import hmac
import html
import http.cookies
import http.server
import io
import os
import random
import re
import secrets
import threading
import time
import urllib.parse

from . import checks, config, inline, networks, pet
from . import VERSION, update

PORT = 80
CAPTURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.pcap")
MAX_FORM = 16 << 10
RESTART_RELOAD = 60  # seconds before the restart page opens Settings again (the hotspot's)
RESTART_RELOAD_LAN = 20  # the same on a joined Wi-Fi network: only the app restarts
IMAGE_NAME = re.compile(r"[a-z0-9_-]{1,64}\.(png|gif)")
COOKIE = "flugato_session"
SESSION_AGE = 12 * 3600          # a right PIN is good for 12 hours on that browser
LOCK_AFTER, LOCK_FIRST, LOCK_MAX = 5, 60, 900  # 5 wrong PINs from one address: 60 s, doubling to 15 min
GLOBAL_TRIES, GLOBAL_WINDOW = 30, 600          # and at most 30 wrong PINs in 10 min from everyone
DEFAULT_PIN = "1234"

CSS = """
:root { --bg: #f6f1e8; --card: #fffdf9; --ink: #221d17; --muted: #6f665c; --line: #e6ddd0;
  --fur: #ec963c; --fur-ink: #3e2212; --ok: #2f8f46; --warn: #c58a00; --fail: #c8372d;
  --pending: #8a8a86; --tint: #f3e9dc; --tint-ok: #dff1e2; --tint-warn: #fbecc4;
  --tint-fail: #f8d9d4; --tint-sleep: #e3e6ee; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #15130f; --card: #211d18; --ink: #efe8dd; --muted: #a59b8e; --line: #36302a;
    --tint: #2d2419; --tint-ok: #19331f; --tint-warn: #3a2d08; --tint-fail: #3d1915;
    --tint-sleep: #1f2330; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
img.cat { image-rendering: pixelated; image-rendering: crisp-edges; display: block; }
header { background: var(--card); border-bottom: 3px solid var(--fur); padding: 10px 16px 12px; }
header div { max-width: 608px; margin: 0 auto; }
.brand { display: flex; align-items: center; gap: 10px; }
.brand b { font-size: 22px; letter-spacing: .5px; }
.brand img { width: 42px; height: 27px; }
nav { display: flex; gap: 4px; margin-top: 8px; }
nav a { flex: 1; text-align: center; padding: 8px 4px; border-radius: 8px; color: var(--ink);
  text-decoration: none; background: var(--bg); }
nav a.on { background: var(--fur); color: var(--fur-ink); font-weight: 600; }
main { padding: 16px; max-width: 640px; margin: 0 auto; }
.hero { display: flex; align-items: center; gap: 16px; padding: 14px 16px; border-radius: 14px;
  background: var(--tint); margin-bottom: 14px; }
.hero.ok { background: var(--tint-ok); } .hero.warn { background: var(--tint-warn); }
.hero.fail { background: var(--tint-fail); } .hero.sleep { background: var(--tint-sleep); }
.hero img { width: 112px; height: 72px; flex: 0 0 112px; }
.hero h1 { font-size: 22px; margin: 0 0 2px; line-height: 1.2; }
.hero .sub { margin: 0; }
.sub { color: var(--muted); font-size: 14px; margin-bottom: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 4px 14px; margin-bottom: 14px; }
.row { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--line);
  align-items: baseline; }
.row:last-child { border-bottom: 0; }
.row .k { flex: 0 0 34%; color: var(--muted); font-size: 14px; }
.row .v { flex: 1; word-break: break-word; white-space: pre-wrap; }  /* the screen's double spaces */
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px;
  background: var(--pending); }
.ok { background: var(--ok); } .warn { background: var(--warn); } .fail { background: var(--fail); }
.empty { display: flex; align-items: center; gap: 14px; padding: 12px 0; color: var(--muted); }
.empty img { width: 84px; height: 54px; flex: 0 0 84px; }
label { display: block; font-weight: 600; margin-top: 12px; }
.help { color: var(--muted); font-size: 13px; margin: 2px 0 4px; }
input[type=text], input[type=password] { width: 100%; font: inherit; padding: 9px 10px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); }
.bad input[type=text] { border-color: var(--fail); }
.err { color: var(--fail); font-size: 13px; }
button, .btn { font: inherit; padding: 10px 16px; border-radius: 8px; border: 0;
  background: var(--fur); color: var(--fur-ink); font-weight: 600; text-decoration: none;
  display: inline-block; }
button.quiet { background: transparent; color: var(--fail); border: 1px solid var(--line);
  padding: 6px 10px; font-weight: 400; }
button.danger { background: var(--fail); color: #fff; }
button.plain, .btn.plain { background: transparent; color: var(--ink); border: 1px solid var(--line);
  font-weight: 400; }
details.group { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 0 14px; margin-bottom: 10px; }
details.group[open] { padding-bottom: 14px; }
details.group > summary { list-style: none; cursor: pointer; padding: 12px 0; display: flex;
  align-items: center; justify-content: space-between; gap: 10px; }
details.group > summary::-webkit-details-marker { display: none; }
details.group > summary::after { content: "+"; color: var(--muted); font-size: 22px; line-height: 1; }
details.group[open] > summary::after { content: "\u2212"; }
details.group[open] > summary { border-bottom: 1px solid var(--line); }
summary .about { display: block; color: var(--muted); font-size: 13px; }
.note.pending { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  flex-wrap: wrap; border-left-color: var(--warn); }
.card.system { padding: 12px 14px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.actions form { margin: 0; }
.note { padding: 10px 14px; border-radius: 10px; margin-bottom: 14px; background: var(--card);
  border-left: 4px solid var(--fur); }
.file { padding: 10px 0; border-bottom: 1px solid var(--line); }
.file:last-child { border-bottom: 0; }
.file .name { word-break: break-all; font-family: ui-monospace, monospace; font-size: 13px; }
.file .meta { color: var(--muted); font-size: 13px; margin: 2px 0 6px; }
.file form { display: inline; margin-left: 8px; }
.net { display: flex; align-items: center; gap: 8px; padding: 10px 0;
  border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.net:last-child { border-bottom: 0; }
.net .name { flex: 1 1 140px; font-weight: 600; word-break: break-all; }
.net .meta { color: var(--muted); font-size: 13px; font-weight: 400; display: block; }
.net form { display: inline; margin: 0; }
.note a { color: inherit; }
.note.bad-note { border-left-color: var(--fail); } .note.good-note { border-left-color: var(--ok); }
@media (min-width: 700px) { .hero img { width: 140px; height: 90px; flex-basis: 140px; } }
"""


MODES = {"nolink": "No link", "network": "Network test", "cable": "Cable test",
         "inline": "Inline", "testing": "Testing"}

# --- the cat ------------------------------------------------------------------------------
# Every page carries the panel's cat, rendered from pet.py: a PNG for a still pose, a GIF
# for a short loop (an act from the sets). All of them are drawn once and kept in memory.

IMG_SCALE = 4                   # 112 x 72 px, shown at 112 CSS px: crisp on a phone


def _loops():
    """name -> (mood, sequence, ms per tick) for every GIF the pages can ask for."""
    loops = {}
    for mood in ("good", "testing"):
        for act, seq in pet.IDLE_ACTS.items():
            loops[f"{mood}-{act}"] = (mood, pet.IDLE_BASE[:4] + seq, 500)
    for mood in ("warn", "fail"):
        for act, seq in pet.IDLE_ACTS_SERIOUS.items():
            loops[f"{mood}-{act}"] = (mood, pet.IDLE_BASE[:4] + seq, 500)
    for act, seq in pet.INLINE_ACTS.items():
        loops[f"inline-{act}"] = ("good", pet.INLINE_BASE[:5] + seq, 500)
    for act, seq in pet.ROUTER_ACTS.items():
        loops[f"router-{act}"] = ("good", pet.ROUTER_BASE[:2] + seq, 500)
    for act in ("peek", "yawn", "stretch"):
        loops[f"sleep-{act}"] = ("sleep", pet.SLEEP_BASE * 2 + pet.SLEEP_ACTS[act], 250)
    for act, seq in pet.WEB_ACTS.items():
        loops[f"web-{act}"] = ("good", seq, 500)
    return loops


LOOPS = _loops()
# per situation: the GIF names to pick from and their weights
PICKS = {
    "good": ([f"good-{a}" for a in pet.IDLE_ACTS], [pet.IDLE_WEIGHTS.get(a, 1) for a in pet.IDLE_ACTS]),
    "testing": ([f"testing-{a}" for a in ("look_around", "ear_twitch")], [1, 1]),
    "warn": ([f"warn-{a}" for a in pet.IDLE_ACTS_SERIOUS], [1] * len(pet.IDLE_ACTS_SERIOUS)),
    "fail": ([f"fail-{a}" for a in pet.IDLE_ACTS_SERIOUS], [1] * len(pet.IDLE_ACTS_SERIOUS)),
    "inline": ([f"inline-{a}" for a in pet.INLINE_ACTS], [pet.INLINE_WEIGHTS.get(a, 1) for a in pet.INLINE_ACTS]),
    "router": ([f"router-{a}" for a in pet.ROUTER_ACTS], [pet.ROUTER_WEIGHTS.get(a, 1) for a in pet.ROUTER_ACTS]),
    "sleep": (["sleep-peek", "sleep-yawn", "sleep-stretch"], [3, 2, 1]),
}
TINT = {"good": "ok", "warn": "warn", "fail": "fail", "sleep": "sleep", "testing": "", "inline": "",
        "router": ""}


def pick_loop(where, rng=random):
    """A GIF name for the situation; a fresh pick on every page load, so each refresh of the
    status page shows another act."""
    names, weights = PICKS[where]
    return rng.choices(names, weights)[0]


def situation(status):
    """Which cat the status page shows: asleep before the first result, the router cat on
    NO LINK (the hotspot is what the tester is doing then), the laptop cat in inline mode,
    or the results mood."""
    if not status["time"]:
        return "sleep"
    if status["mode"] == "nolink":
        return "router"
    if status["mode"] == "inline":
        return "inline"
    states = {state for _, _, state in status["rows"]}
    if "fail" in states:
        return "fail"
    if "warn" in states:
        return "warn"
    if "pending" in states or (status["title"] or "").startswith("TESTING"):
        return "testing"
    return "good"


def render_image(name):
    """(bytes, content type) for a cat image name, or None when there is no such image.
    <pose>.png: a still at IMG_SCALE; mini-<pose>.png: at half that; icon.png: tiny;
    <loop>.gif: an animated loop from LOOPS."""
    if not IMAGE_NAME.fullmatch(name):
        return None
    base, kind = name.rsplit(".", 1)
    buf = io.BytesIO()
    if kind == "png":
        if base == "icon":
            sp = pet.sprite("good", 0, 1, "sit")
        elif base.startswith("mini-") and base[5:] in pet.POSES:
            sp = pet.sprite("good", 0, 2, base[5:])
        elif base in pet.POSES:
            sp = pet.sprite("good", 0, IMG_SCALE, base)
        else:
            return None
        sp.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    if base not in LOOPS:
        return None
    mood, seq, ms = LOOPS[base]
    frames = [pet.sprite(mood, 0, IMG_SCALE, pose) for pose, _ in pet.expand(seq)]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=ms,
                   loop=0, disposal=2)
    return buf.getvalue(), "image/gif"


def cat_img(src, alt, cls="cat"):
    return f'<img class="{cls}" src="/cat/{esc(src)}" alt="{esc(alt)}" width="112" height="72">'


def hero(src, title, sub, tint=""):
    """The top card of a page: the cat and the headline."""
    return (f'<div class="hero {tint}">{cat_img(src, "the fluGATO cat")}<div>'
            f"<h1>{title}</h1>" + (f'<p class="sub">{sub}</p>' if sub else "") + "</div></div>")



def esc(value):
    return html.escape("" if value is None else str(value))


def fmt_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def page(title, active, body, reload=None, nav=True):
    """reload: (seconds, url) for a page that sends itself on (no script: a meta refresh).
    nav=False: no tabs (the PIN page)."""
    tabs = "".join(f'<a href="{href}"{" class=on" if href == active else ""}>{name}</a>'
                   for href, name in (("/", "Status"), ("/settings", "Settings"),
                                      ("/wifi", "Wi-Fi"), ("/captures", "Captures"))) if nav else ""
    refresh = '<meta http-equiv="refresh" content="5">' if active == "/" else ""
    if reload:
        refresh = f'<meta http-equiv="refresh" content="{int(reload[0])};url={esc(reload[1])}">'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}'
            f'<link rel="icon" href="/cat/icon.png">'
            f"<title>{esc(title)} · fluGATO</title><style>{CSS}</style></head><body>"
            f'<header><div><div class="brand"><img class="cat" src="/cat/mini-sit.png" alt="" '
            f'width="56" height="36"><b>fluGATO</b></div>{"<nav>" + tabs + "</nav>" if tabs else ""}'
            f'</div></header>'
            f"<main>{body}</main></body></html>")


def not_found(title="Not found", detail="There is nothing at this address."):
    return page(title, "", hero("web-confused.gif", esc(title), esc(detail)))


def list_captures(directory):
    """[(name, size, mtime)] of the pcap files, newest first."""
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        if CAPTURE_NAME.fullmatch(name):
            try:
                st = os.stat(os.path.join(directory, name))
            except OSError:
                continue
            found.append((name, st.st_size, st.st_mtime))
    return sorted(found, key=lambda f: -f[2])


def capture_path(directory, name):
    """The file for a capture name from a URL, or None: plain names only, no paths."""
    if not CAPTURE_NAME.fullmatch(name or ""):
        return None
    full = os.path.join(directory, name)
    return full if os.path.isfile(full) else None


def parse_form(body):
    return {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                                       keep_blank_values=True).items()}


def same_origin(headers, host):
    """A POST must come from our own pages: Origin or Referer, when sent, name our host."""
    for name in ("Origin", "Referer"):
        value = headers.get(name)
        if value:
            return urllib.parse.urlsplit(value).netloc == host
    return True


class WebUI:
    """WebUI(...).start(address) / stop(); publish(mode, rows, title) from the app.

    captures: the capture directory (inline mode's). config_path: None for the settings
    file the app reads. hotspot: a hotspot.Hotspot, for the SSID and phone count on the
    status page. on_saved(cfg): called with the whole new settings after a save.
    live_keys: the keys the app applies at once; the others say "after a restart".
    on_join(ssid): a Connect on the Wi-Fi page, called a moment after the page answered
    (the app runs hotspot.join in a thread: this server stops on the way, and starts
    again only if the hotspot comes back; on a joined network there is no web UI).
    on_restart(): Restart (after a Save that needs it, or any time), called a moment after
    the page answered (the app restarts and brings the hotspot back, with any new name or
    password). pending: a set shared by the app's web UIs, the saved keys that wait for it.
    on_power(): "Power off" on the Settings page, asked once more on its own page, then
    called a moment after the last page answered (a clean shutdown of the Pi)."""

    def __init__(self, captures=inline.CAPTURE_DIR, config_path=None, hotspot=None,
                 on_saved=None, live_keys=(), proc_root="/proc", load=config.load,
                 save=config.save, on_join=None, networks_file=None, run=checks.run,
                 on_restart=None, pin=None, clock=time.monotonic, on_power=None, pending=None,
                 updater=None):
        self.captures, self.config_path, self.hotspot = captures, config_path, hotspot
        self.on_join, self.networks_file, self.run = on_join, networks_file, run
        self.on_saved, self.live_keys = on_saved, set(live_keys)
        self.proc_root, self.load, self.save = proc_root, load, save
        self._lock = threading.Lock()
        self._status = {"mode": None, "title": None, "rows": [], "time": None}
        self._images = {}        # name -> (bytes, content type), drawn once
        self._join_timer = None
        self.on_restart, self._restart_timer = on_restart, None
        self.on_power, self._power_timer = on_power, None
        self.updater = updater   # update.Updater: Check for updates, Install, Roll back
        # saved settings that wait for a restart, shared by the app's web UIs (the hotspot's
        # and the joined network's); the restart itself empties it (a new process)
        self.pending = pending if pending is not None else set()
        self.server, self.thread, self.address, self.port = None, None, None, None
        # pin: None (the hotspot: no PIN), a string, or a callable that returns the current one
        self._pin, self.clock = pin, clock
        self._sessions = {}   # token -> (expiry, the PIN it was opened with)
        self._fails = {}      # client address -> (wrong PINs in a row, locked until)
        self._recent = []     # times of wrong PINs, everyone

    # --- PIN ------------------------------------------------------------------------------

    def pin(self):
        """The PIN these pages need, or None when they need none."""
        pin = self._pin() if callable(self._pin) else self._pin
        return str(pin).strip() if pin and str(pin).strip().lower() != "off" else None

    def session_ok(self, cookie_header):
        if not self.pin():
            return True
        try:
            jar = http.cookies.SimpleCookie(cookie_header or "")
        except http.cookies.CookieError:
            return False
        token = jar[COOKIE].value if COOKIE in jar else None
        pin = self.pin()
        with self._lock:
            expiry, opened_with = self._sessions.get(token, (0, None))
            # a new PIN closes every session opened with the old one
            return expiry > self.clock() and hmac.compare_digest(str(opened_with), pin)

    def _locked_for(self, client, now):
        """Seconds until this address may try a PIN again (0: now). Hold self._lock."""
        self._recent = [t for t in self._recent if now - t < GLOBAL_WINDOW]
        mine = self._fails.get(client, (0, 0))[1] - now
        everyone = (self._recent[0] + GLOBAL_WINDOW - now) if len(self._recent) >= GLOBAL_TRIES else 0
        return max(0, int(mine + 0.999), int(everyone + 0.999))

    def locked_for(self, client):
        with self._lock:
            return self._locked_for(client, self.clock())

    def try_pin(self, client, given):
        """A new session token for the right PIN, else None (and the wrong one counted).
        The lock-out check, the comparison and the count are one step under the lock: a
        burst of requests at once can't all slip past the check before any is counted."""
        pin = self.pin() or ""
        with self._lock:
            now = self.clock()
            if self._locked_for(client, now):
                return None
            if pin and hmac.compare_digest(str(given or "").strip().encode(), pin.encode()):
                token = secrets.token_urlsafe(24)
                self._fails.pop(client, None)
                self._sessions = {t: v for t, v in self._sessions.items() if v[0] > now}
                self._sessions[token] = (now + SESSION_AGE, pin)
                return token
            count = self._fails.get(client, (0, 0))[0] + 1
            lock = 0 if count < LOCK_AFTER else min(LOCK_MAX, LOCK_FIRST * 2 ** (count - LOCK_AFTER))
            self._fails[client] = (count, now + lock)
            self._recent.append(now)
        return None

    def login_page(self, error=None, wait=0):
        if wait:
            body = hero("web-sad.gif", "Too many wrong PINs", f"try again in {wait} s")
        else:
            body = hero("web-box.gif", "PIN, please", "fluGATO's pages on this network need the PIN")
            if error:
                body += f'<div class="note bad-note">{error}</div>'
            body += ('<form method="post" action="/login" class="card" style="padding-bottom:14px">'
                     '<label for="pin">PIN</label>'
                     '<div class="help">WEB_PIN in the settings (on the hotspot there is no PIN).</div>'
                     '<input type="password" id="pin" name="pin" inputmode="numeric" maxlength="8"'
                     ' autocomplete="current-password" autofocus>'
                     '<p><button type="submit">Open</button></p></form>')
        return page("PIN", "", body, nav=False)

    def default_pin_note(self):
        """A warning on every page while the PIN is still the default."""
        if self.pin() != DEFAULT_PIN:
            return ""
        return ('<div class="note bad-note">The PIN is still 1234: anyone on this network can '
                'open these pages. Change WEB_PIN in <a href="/settings">Settings</a>.</div>')

    def publish(self, mode, rows=(), title=None):
        """The app's latest state for the status page. mode: nolink, network, cable,
        inline (or any short word); rows: (label, value, state) with state ok, warn,
        fail, skip or pending (e.g. [(r.label, r.value, r.state) for r in report.values()]);
        title: the banner, e.g. "ALL GOOD". Cheap; call it after every redraw if you like."""
        with self._lock:
            self._status = {"mode": mode, "title": title, "time": time.time(),
                            "rows": [tuple(r)[:3] for r in rows]}

    def start(self, address, port=PORT):
        """Listens on `address` only. Raises OSError when it can't (port taken)."""
        if self.server:
            return
        handler = type("Handler", (_Handler,), {"ui": self})
        server = _Server((address, port), handler)
        self.server, self.address, self.port = server, address, server.server_address[1]
        self.thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
        self.thread.start()

    def stop(self):
        server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()
            self.thread.join(3)

    def image(self, name):
        """A cat image by name, from the cache or drawn now; None when unknown."""
        with self._lock:
            if name in self._images:
                return self._images[name]
        made = render_image(name)
        if made:
            with self._lock:
                self._images[name] = made
        return made

    @property
    def url(self):
        if not self.server:
            return None
        return f"http://{self.address}/" if self.port == 80 else f"http://{self.address}:{self.port}/"

    # --- pages ----------------------------------------------------------------------------

    def status_page(self):
        with self._lock:
            s = dict(self._status)
        where = situation(s)
        if s["time"]:
            sub = f'{esc(s["title"] or "")} · updated {time.strftime("%H:%M:%S", time.localtime(s["time"]))}'
        else:
            sub = "waiting for the first result"
        body = hero(pick_loop(where) + ".gif", esc(MODES.get(s["mode"], s["mode"] or "Starting")),
                    sub, TINT[where])
        if s["rows"]:
            body += '<div class="card">' + "".join(
                f'<div class="row"><div class="k">{esc(label)}</div><div class="v">'
                f'<span class="dot {esc(state)}"></span>{esc(value)}</div></div>'
                for label, value, state in s["rows"]) + "</div>"
        items = []
        if self.updater:
            st = self.updater.state()
            items.append(("Version", VERSION + (f" · {st['available']} available" if st.get("available") else "")))
        if self.hotspot:
            hs = self.hotspot.status()
            items.append(("Wi-Fi", f"{hs['ssid']} · {hs['clients']} connected"))
        files = list_captures(self.captures)
        items.append(("Captures", f"{len(files)} files, {fmt_size(sum(f[1] for f in files))}"))
        body += '<div class="card">' + "".join(
            f'<div class="row"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>'
            for k, v in items) + "</div>"
        return page("Status", "/", body)

    def settings_page(self, values=None, errors=None, note=None):
        cfg = values or self.load(self.config_path)
        errors = errors or {}
        where = config.path(self.config_path)
        body = hero("web-confused.gif" if errors else "web-fiddle.gif", "Settings",
                    f"saved to {esc(where)}")
        if note:
            body += f'<div class="note">{note}</div>'
        if self.pending and self.on_restart:
            keys = ", ".join(sorted(self.pending))
            verb = "it applies" if len(self.pending) == 1 else "they apply"
            body += ('<form method="post" action="/restart" class="note pending">'
                     f'<span>Saved <b>{esc(keys)}</b>: {verb} after a restart.</span>'
                     '<button type="submit">Restart now</button></form>')
        # one form, a folded group per category (config.CATEGORIES): the page stays short,
        # a folded group's fields are still sent, and a group with a bad field opens
        body += '<form method="post" action="/settings">'
        for title, about, keys in config.CATEGORIES:
            opened = " open" if any(k in errors for k in keys) else ""
            body += (f'<details class="group"{opened}><summary><span><b>{esc(title)}</b>'
                     f'<span class="about">{esc(about)}</span></span></summary>')
            for key in keys:
                body += self._field(key, cfg, errors)
            body += "</details>"
        body += '<p><button type="submit">Save</button></p></form>'
        if self.on_restart or self.on_power:
            body += (f'<div class="card system"><b>System</b> <span class="about">fluGATO {esc(VERSION)}</span>'
                     '<div class="help">Restart applies every saved setting. Power off: unplug when '
                     'the screen goes gray.</div><div class="actions">')
            if self.on_restart:
                body += ('<form method="post" action="/restart"><button class="plain" '
                         'type="submit">Restart</button></form>')
            if self.on_power:
                body += ('<form method="post" action="/poweroff"><button class="plain" '
                         'type="submit">Power off…</button></form>')
            body += "</div></div>"
        body += self.update_card()
        return page("Settings", "/settings", body)

    def power_ask(self):
        """Power off, asked once more: a phone tap can land on the wrong button."""
        if not self.on_power:
            return not_found()
        body = hero("sleep-peek.gif", "Turn off fluGATO?", "the web UI goes with it", "sleep")
        body += ('<form method="post" action="/poweroff/now" class="card">'
                 '<div class="help">Plug the power back in to turn it on again.</div>'
                 '<p><button class="danger" type="submit">Power off</button> '
                 '<a class="btn plain" href="/settings">Cancel</a></p></form>')
        return page("Power off", "/settings", body)

    def power_off(self):
        """The last page; the power-off starts a moment later, once this page has gone out."""
        if not self.on_power:
            return not_found()
        if not (self._power_timer and self._power_timer.is_alive()):
            self._power_timer = threading.Timer(1.0, self.on_power)
            self._power_timer.daemon = True
            self._power_timer.start()
        body = hero("sleep-yawn.gif", "Turning off", "fluGATO is shutting down", "sleep")
        body += ('<div class="note">Unplug it when the screen goes <b>gray</b> (about 15 '
                 'seconds). Plug it back in to turn it on.</div>')
        return page("Turning off", "/settings", body)

    # --- updates from the site --------------------------------------------------------------
    def update_card(self):
        """The Update card on Settings: the reason nothing can run, or the last check's
        result with Check for updates / Install, and Roll back while a previous tree is kept."""
        up = self.updater
        if not up:
            return ""
        st = up.state()
        body = f'<div class="card update"><b>Update</b> <span class="about">from {esc(up.base)}</span>'
        if st["phase"] in update.RUNNING:
            body += (f'<div class="help">{esc(st["text"])}</div><div class="actions">'
                     '<a class="btn plain" href="/update">Progress</a></div></div>')
            return body
        why = up.blocked()
        if why:
            return body + f'<div class="help">{esc(why)}</div></div>'
        if st.get("error"):
            body += f'<div class="err">{esc(st["error"])}</div>'
        if st.get("available"):
            body += (f'<div class="help"><b>{esc(st["available"])}</b> is out; this is {esc(VERSION)}. '
                     'Install downloads it, tries it, swaps it in and restarts (about a minute).</div>')
        elif st.get("checked_at") and not st.get("error"):
            body += f'<div class="help">{esc(st["text"])}.</div>'
        else:
            body += f'<div class="help">This is fluGATO {esc(VERSION)}. Check asks the site for a newer one.</div>'
        body += '<div class="actions">'
        if st.get("available"):
            body += ('<form method="post" action="/update/install"><button type="submit">'
                     f'Install {esc(st["available"])}</button></form>')
        body += ('<form method="post" action="/update/check"><button class="plain" type="submit">'
                 'Check for updates</button></form>')
        prev = up.previous()
        if prev:
            body += ('<form method="post" action="/update/rollback"><button class="plain" type="submit">'
                     f'Roll back to {esc(prev)}</button></form>')
        return body + "</div></div>"

    def update_check(self):
        """Check for updates: asks the site now (a few seconds), then Settings with the answer."""
        if not self.updater:
            return not_found()
        self.updater.check()
        return self.settings_page()

    def update_install(self):
        """Install: starts the update in its thread and shows its progress page."""
        if not self.updater:
            return not_found()
        self.updater.install()
        return self.update_page()

    def update_page(self):
        """Progress while an update runs (the page reloads itself), the restart page once the
        new tree is in, or what went wrong."""
        up = self.updater
        if not up:
            return not_found()
        st = up.state()
        if st["phase"] == "restarting":
            body = hero(pick_loop("router") + ".gif", "Restarting", esc(st["text"]))
            again = " (the PIN again: a restart ends the session)" if self.pin() else ""
            body += ('<div class="note good-note">fluGATO is back in a few seconds; this page then '
                     f'opens Settings by itself{again}.</div>')
            return page("Updating", "/settings", body, reload=(RESTART_RELOAD_LAN, "/settings"))
        if st["phase"] in update.RUNNING:
            body = hero("web-fiddle.gif", "Updating", esc(st["text"]))
            body += '<div class="note">Keep the power on. This page follows the progress by itself.</div>'
            return page("Updating", "/settings", body, reload=(2, "/update"))
        if st.get("error"):
            body = hero("web-sad.gif", "Update failed", "nothing was changed")
            body += (f'<div class="note bad-note">{esc(st["error"])}</div>'
                     '<p><a class="btn plain" href="/settings">Back to Settings</a></p>')
            return page("Update failed", "/settings", body)
        return self._redirect_page("/settings")

    def rollback_ask(self):
        """Roll back, asked once more: it restarts fluGATO on the previous version."""
        up = self.updater
        if not up or not up.previous():
            return not_found()
        body = hero("web-fiddle.gif", f"Back to {esc(up.previous())}?",
                    f"this is {esc(VERSION)}; the previous version is still on the card")
        body += ('<form method="post" action="/update/rollback/now" class="card">'
                 '<div class="help">fluGATO restarts on the previous version; the current one stays '
                 'as the one to roll back to.</div>'
                 '<p><button class="danger" type="submit">Roll back</button> '
                 '<a class="btn plain" href="/settings">Cancel</a></p></form>')
        return page("Roll back", "/settings", body)

    def rollback_now(self):
        up = self.updater
        if not up or not up.previous():
            return not_found()
        if not up.rollback():
            return self.update_page()
        return self.update_page()

    def _redirect_page(self, url):
        return page("fluGATO", "/settings", "", reload=(0, url))

    def _field(self, key, cfg, errors):
        if key in config.AT_HOTSPOT_START:
            when = "applies at the next hotspot start"
        elif key in self.live_keys:
            when = "applies at once"
        else:
            when = "applies after a restart"
        bad = " bad" if key in errors else ""
        field = (f'<div class="{bad.strip()}"><label for="{key}">{key}</label>'
                 f'<div class="help">{esc(config.HELP.get(key, ""))} <i>({when})</i></div>'
                 f'<input type="text" id="{key}" name="{key}" value="{esc(cfg.get(key, ""))}"'
                 f' autocapitalize="off" autocorrect="off" spellcheck="false">')
        if key in errors:
            field += f'<div class="err">{esc(errors[key])}</div>'
        return field + "</div>"

    def hotspot_name(self):
        """The hotspot's network name (HOTSPOT_SSID), for the pages' instructions."""
        name = self.hotspot.status().get("ssid") if self.hotspot else None
        return name or config.DEFAULTS["HOTSPOT_SSID"]

    def restart(self):
        """Restart: the page, then the app restarts a moment later, once it has gone out."""
        if not self.on_restart:
            return not_found()
        if not (self._restart_timer and self._restart_timer.is_alive()):
            self._restart_timer = threading.Timer(1.0, self.on_restart)
            self._restart_timer.daemon = True
            self._restart_timer.start()
        body = hero(pick_loop("router") + ".gif", "Restarting",
                    "fluGATO is applying the settings")
        if self.pending:
            body += (f'<div class="note good-note">Applying {esc(", ".join(sorted(self.pending)))}.'
                     '</div>')
        if self.hotspot and self.hotspot.status().get("on"):
            ssid, _ = config.validate("HOTSPOT_SSID",  # the name it comes back with
                                      self.load(self.config_path).get("HOTSPOT_SSID", ""))
            body += ('<div class="note">The hotspot goes off now and comes back in about a '
                     'minute; this page then opens Settings by itself. If it does not, check '
                     'that this device is back on <b>' + esc(ssid or config.DEFAULTS["HOTSPOT_SSID"]) +
                     '</b> (some switch to a network with internet on their own), with the '
                     'password on the screen.</div>')
            wait = RESTART_RELOAD
        else:
            again = " (the PIN again: a restart ends the session)" if self.pin() else ""
            body += ('<div class="note">fluGATO is back in a few seconds; this page then opens '
                     f'Settings by itself{again}.</div>')
            wait = RESTART_RELOAD_LAN
        return page("Restarting", "/settings", body, reload=(wait, "/settings"))

    def save_settings(self, form):
        """Validates every key; saves only when all are good. Returns the page."""
        current = self.load(self.config_path)
        values, errors = {}, {}
        for key in config.DEFAULTS:
            clean, error = config.validate(key, form.get(key, current.get(key, "")))
            if error:
                errors[key] = error
            else:
                values[key] = clean
        if errors:
            shown = dict(current, **{k: form.get(k, "") for k in config.DEFAULTS})
            return self.settings_page(shown, errors, "Nothing saved: fix the fields in red.")
        same = lambda k, a, b: (a or config.DEFAULTS[k]) == (b or config.DEFAULTS[k])  # empty = default
        changed = {k: v for k, v in values.items() if not same(k, current.get(k, ""), v)}
        if not changed:
            return self.settings_page(note="No changes.")
        try:
            self.save(changed, self.config_path)
        except OSError as e:
            return self.settings_page(dict(current, **values), note=esc(
                f"Could not save: {e.strerror or e}"))
        new = dict(current, **values)
        if self.on_saved:
            try:
                self.on_saved(new)
            except Exception:
                pass
        # what waits for a restart joins the note on top with its Restart now button (a
        # hotspot key too while the hotspot is on: it comes back with the new name or
        # password); the rest applies at once, or at the next hotspot start
        later = set(changed) - self.live_keys - set(config.AT_HOTSPOT_START)
        hotspot_keys = set(changed) & set(config.AT_HOTSPOT_START)
        hotspot_on = bool(self.hotspot and self.hotspot.status().get("on"))
        wait = later | (hotspot_keys if hotspot_on else set())
        at_start = set() if hotspot_on else hotspot_keys
        said = lambda keys, when: f"Saved {esc(', '.join(sorted(keys)))}: {when}."
        notes = []
        if set(changed) - wait - at_start:
            notes.append(said(set(changed) - wait - at_start, "applied at once"))
        if at_start:
            notes.append(said(at_start, "at the next hotspot start"))
        if self.on_restart:
            self.pending.update(wait)  # the note on top says it, with Restart now
        elif wait:
            notes.append(said(wait, "after a restart of fluGATO"))
        return self.settings_page(note=" ".join(notes) or None)

    # --- Wi-Fi networks ---------------------------------------------------------------------

    def wifi_page(self, note=None, error=None, ssid=""):
        hs = self.hotspot.status() if self.hotspot else {}
        saved = networks.load(self.networks_file)
        if hs.get("failed"):
            top = hero("web-confused.gif", "Wi-Fi networks", "the last join did not work", "warn")
        else:
            top = hero(pick_loop("router") + ".gif", "Wi-Fi networks",
                       "networks fluGATO can join instead of being the hotspot")
        body = top
        if hs.get("failed"):
            came_back = {"same password": "The hotspot came back, with the same password.",
                         "new password": "The hotspot came back, with a new password: it is on the panel."
                         }.get(hs.get("fallback"), "")
            body += f'<div class="note bad-note">Could not join {esc(hs["failed"])}. {came_back}</div>'
        if note:
            body += f'<div class="note good-note">{note}</div>'
        if error:
            body += f'<div class="note bad-note">{error}</div>'
        body += '<div class="card">'
        if saved:
            for net in saved:
                name = esc(net["ssid"])
                kind = "password saved" if net["password"] else "open network"
                connect = ""
                if self.on_join:
                    connect = (f'<form method="post" action="/wifi/connect"><input type="hidden" '
                               f'name="ssid" value="{name}"><button type="submit">Connect</button></form>')
                body += (f'<div class="net"><div class="name">{name}<span class="meta">{kind}'
                         f'</span></div>{connect}<form method="post" action="/wifi/forget">'
                         f'<input type="hidden" name="ssid" value="{name}">'
                         f'<button class="quiet" type="submit">Forget</button></form></div>')
        else:
            body += ('<div class="empty">' + cat_img("confused.png", "", "cat") +
                     "<div>No networks saved yet.</div></div>")
        body += "</div>"
        if self.on_join and saved:
            body += ('<p class="sub">Connect turns the hotspot off, and this page with it, and '
                     "joins that network. There these pages are at the address on the panel, "
                     "behind the PIN (WEB_PIN), and SSH works too. If fluGATO can't join within "
                     "30 s, the hotspot comes back with the same password.</p>")
        bad = " bad" if error else ""
        body += ('<form method="post" action="/wifi/add" class="card" style="padding-bottom:14px">'
                 f'<div class="{bad.strip()}"><label for="ssid">Network name (SSID)</label>'
                 '<div class="help">Exactly as the router has it; hidden networks work too.</div>'
                 f'<input type="text" id="ssid" name="ssid" value="{esc(ssid)}" maxlength="32"'
                 ' autocapitalize="off" autocorrect="off" spellcheck="false" autocomplete="off">'
                 '<label for="password">Password</label>'
                 '<div class="help">8 to 63 characters; empty for an open network. Shown as you type, '
                 "so you can check it.</div>"
                 '<input type="text" id="password" name="password" maxlength="63" autocapitalize="off"'
                 ' autocorrect="off" spellcheck="false" autocomplete="off"></div>'
                 '<p><button type="submit">Save network</button></p>'
                 '<div class="help">Kept on the SD card\'s boot partition, the password too, in '
                 "plain text: anyone with the card can read it.</div></form>")
        return page("Wi-Fi", "/wifi", body)

    def add_network(self, form):
        ssid, password = form.get("ssid", ""), form.get("password", "")
        try:
            networks.add(ssid, password, self.run, self.networks_file)
        except ValueError as e:
            return self.wifi_page(error=f"Not saved: {esc(e)}. Type the password again.", ssid=ssid)
        except OSError as e:
            return self.wifi_page(error=esc(f"Could not save: {e.strerror or e}"), ssid=ssid)
        return self.wifi_page(note=f"Saved {esc(ssid)}.")

    def forget_network(self, form):
        ssid = form.get("ssid", "")
        try:
            networks.forget(ssid, self.run, self.networks_file)
        except ValueError as e:
            return self.wifi_page(error=f"Not forgotten: {esc(e)}.")
        except OSError as e:
            return self.wifi_page(error=esc(f"Could not save: {e.strerror or e}"))
        return self.wifi_page(note=f"Forgot {esc(ssid)}.")

    def connect_network(self, form):
        """Answers first, then hands the join to the app: the join stops this server."""
        ssid = form.get("ssid", "")
        if not self.on_join:
            return self.wifi_page(error="Joining is not available here.")
        if not networks.find(ssid, self.networks_file):
            return self.wifi_page(error=f"{esc(ssid)} is not saved.")
        with self._lock:
            busy = bool((self._join_timer and self._join_timer.is_alive()) or
                        (self.hotspot and self.hotspot.status().get("joining")))
            if not busy:
                self._join_timer = threading.Timer(1.0, self.on_join, args=(ssid,))
                self._join_timer.daemon = True
                self._join_timer.start()
        if busy:
            return self.wifi_page(error="Already joining a network: wait for it.")
        body = hero(pick_loop("router") + ".gif", f"Joining {esc(ssid)}…",
                    "the hotspot is going off now")
        body += ('<div class="card"><div class="row"><div class="v">This page stops answering '
                 "in a moment, with the hotspot. Once fluGATO is on the network, the panel shows "
                 "its address: open it there (the PIN is WEB_PIN), or SSH. If the join does not work within 30 s, "
                 "the fluGATO hotspot comes back with the same password and this page says "
                 "why.</div></div></div>")
        return page("Wi-Fi", "/wifi", body)

    def captures_page(self, note=None):
        files = list_captures(self.captures)
        in_ram = inline.root_in_ram(_read(os.path.join(self.proc_root, "mounts")))
        body = hero("web-fish.gif" if files else "web-box.gif", "Captures",
                    f"{len(files)} files, {fmt_size(sum(f[1] for f in files))} in {esc(self.captures)}")
        if in_ram:
            body += '<div class="note">The root is read-only: captures live in RAM and are gone after a reboot. Download what you need.</div>'
        if note:
            body += f'<div class="note">{esc(note)}</div>'
        if files:
            body += '<div class="card">'
            for name, size, mtime in files:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
                q = urllib.parse.quote(name)
                body += (f'<div class="file"><div class="name">{esc(name)}</div>'
                         f'<div class="meta">{fmt_size(size)} · {when}</div>'
                         f'<a class="btn" href="/captures/{q}" download>Download</a>'
                         f'<form method="post" action="/captures/delete">'
                         f'<input type="hidden" name="name" value="{esc(name)}">'
                         f'<button class="quiet" type="submit">Delete</button></form></div>')
            body += "</div>"
        else:
            body += ('<div class="card"><div class="empty">' + cat_img("confused.png", "", "cat") +
                     "<div>No captures yet: they are made in inline mode.</div></div></div>")
        return page("Captures", "/captures", body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A browser dropping its connection (a reload, a closed tab, the Pi restarting) is
        normal: no traceback. Anything else: one line, without the client's address (the
        requests are not logged)."""
        import sys
        e = sys.exc_info()[1]
        if e is not None and not isinstance(e, OSError):
            print(f"web: {type(e).__name__}: {e}", flush=True)


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


class _Handler(http.server.BaseHTTPRequestHandler):
    ui = None  # set per server
    timeout = 15  # a phone that stalls mid-request doesn't hold a thread forever
    server_version = "fluGATO"
    sys_version = ""

    def log_message(self, *args):
        pass  # quiet: nothing about the phones or the requests goes to the journal

    def _host_ok(self):
        host = self.headers.get("Host", "")
        ui = self.ui
        return host in (ui.address, f"{ui.address}:{ui.port}")

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=(), cache=False):
        if (isinstance(body, str) and ctype.startswith("text/html") and "<main>" in body
                and self.ui.pin() and self._signed_in()):  # never tell the PIN page the PIN
            body = body.replace("<main>", "<main>" + self.ui.default_pin_note(), 1)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                    "img-src 'self'; form-action 'self'; "
                                                    "frame-ancestors 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, where, extra=()):
        self.send_response(303)
        self.send_header("Location", where)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def _signed_in(self):
        return self.ui.session_ok(self.headers.get("Cookie"))

    def do_GET(self):
        if not self._host_ok():
            return self._send(400, "wrong host", "text/plain")
        url = urllib.parse.urlsplit(self.path).path
        if url == "/login":
            if not self.ui.pin():
                return self._redirect("/")
            return self._send(200, self.ui.login_page(wait=self.ui.locked_for(self.client_address[0])))
        if not url.startswith("/cat/") and not self._signed_in():
            return self._redirect("/login")  # the cats are no secret: the PIN page shows one
        if url == "/":
            return self._send(200, self.ui.status_page())
        if url == "/settings":
            return self._send(200, self.ui.settings_page())
        if url == "/captures":
            return self._send(200, self.ui.captures_page())
        if url == "/wifi":
            return self._send(200, self.ui.wifi_page())
        if url == "/update":
            return self._send(200, self.ui.update_page())
        if url.startswith("/captures/"):
            return self._download(urllib.parse.unquote(url[len("/captures/"):]))
        if url.startswith("/cat/"):
            made = self.ui.image(url[len("/cat/"):])
            if not made:
                return self._send(404, "no such cat", "text/plain")
            return self._send(200, made[0], made[1], cache=True)
        return self._send(404, not_found())

    def _download(self, name):
        full = capture_path(self.ui.captures, name)
        if not full:
            return self._send(404, not_found("No such capture", "It may have been deleted, or the name is wrong."))
        try:
            f = open(full, "rb")
        except OSError:
            return self._send(404, not_found("No such capture", "It may have been deleted, or the name is wrong."))
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.tcpdump.pcap")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command == "HEAD":
                return
            left = size  # a capture still being written: send the size we announced
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        if not self._host_ok():
            return self._send(400, "wrong host", "text/plain")
        if not same_origin(self.headers, self.headers.get("Host", "")):
            return self._send(403, "forbidden", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_FORM:
            return self._send(413, "too large", "text/plain")
        form = parse_form(self.rfile.read(length))
        url = urllib.parse.urlsplit(self.path).path
        if url == "/login" and self.ui.pin():
            client = self.client_address[0]
            token = self.ui.try_pin(client, form.get("pin"))
            if token:
                cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_AGE}"
                return self._redirect("/", extra=(("Set-Cookie", cookie),))
            wait = self.ui.locked_for(client)
            return self._send(200, self.ui.login_page(error="Wrong PIN.", wait=wait))
        if not self._signed_in():
            return self._redirect("/login")
        if url == "/settings":
            return self._send(200, self.ui.save_settings(form))
        if url == "/restart":
            return self._send(200, self.ui.restart())
        if url == "/poweroff":
            return self._send(200, self.ui.power_ask())
        if url == "/poweroff/now":
            return self._send(200, self.ui.power_off())
        if url == "/update/check":
            return self._send(200, self.ui.update_check())
        if url == "/update/install":
            return self._send(200, self.ui.update_install())
        if url == "/update/rollback":
            return self._send(200, self.ui.rollback_ask())
        if url == "/update/rollback/now":
            return self._send(200, self.ui.rollback_now())
        if url == "/wifi/add":
            return self._send(200, self.ui.add_network(form))
        if url == "/wifi/forget":
            return self._send(200, self.ui.forget_network(form))
        if url == "/wifi/connect":
            return self._send(200, self.ui.connect_network(form))
        if url == "/captures/delete":
            full = capture_path(self.ui.captures, form.get("name"))
            if full:
                try:
                    os.remove(full)
                except OSError:
                    pass
            return self._redirect("/captures")
        return self._send(404, not_found())

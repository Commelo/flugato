"""flugato main loop: wait for link, run the checks top to bottom,
redraw after every step, then wait for a tap, a hold or a link change."""

import argparse
import atexit
import datetime
import os
import queue
import signal
import sys
import threading
import time
import zoneinfo

from . import (cable, checks, config, dhcp_probe, display, hotspot, inline, networks, pet,
               update, weather, web)
from .checks import OK, PENDING, SKIP, WARN
from .touch import Touch, parse_cal


def as_int(value, default):
    try:
        return int(value)
    except ValueError:
        return default


# a Save on the web UI that restarts fluGATO: the next start brings the hotspot back (RAM only)
RESUME_HOTSPOT = "/run/flugato-resume-hotspot"
LIVE_KEYS = ("CHECK_HOST", "LLDP_WAIT", "PROBE_VLANS", "WEATHER_LAT", "WEATHER_LON",
             "WEATHER_UNITS", "WEB_PIN", "DESK_MINUTES", "SHOW_MINUTES", "NIGHT_HOURS",
             "DESK_TURN", "POWER_ASK")


class App:
    def __init__(self, cfg, out, iface):
        self.cfg, self.out, self.iface = cfg, out, iface
        self.check_host = cfg["CHECK_HOST"]
        self.lldp_wait = as_int(cfg["LLDP_WAIT"], 90)
        self.report = checks.new_report()
        self.cable_changed = False
        self.rerun = threading.Event()
        self.blink = threading.Event()
        self.tap = threading.Event()   # any touch, for the NO LINK poke
        self.actions = queue.Queue()   # touches for the screens, applied on the next draw
        self.wake = threading.Event()  # a touch wakes the idle loop at once
        self.dirty = False             # data changed off the main loop: redraw it all
        # VLANs to probe even if never heard tagged (spanning tree off, no traffic)
        self.extra_vlans = {int(v) for v in cfg.get("PROBE_VLANS", "").replace(" ", "").split(",")
                            if v.isdigit() and 1 <= int(v) <= 4094}
        # cable test: the other end of a patch cable goes into the second port (USB adapter)
        self.port_b = cable.second_port(iface, cfg.get("CABLE_IFACE", ""))
        self.b_link = False
        self.mode = "network"  # or "cable", "inline"
        self.session = None    # the inline bridge while in inline mode
        self.console = None    # display.ConsoleOff when the panel is the console's framebuffer
        self.keep_port_b()
        self.view, self.scroll, self.vlan = "results", 0, None  # results / list / vlan
        self.native, self.offers = None, {}
        self.frame = 0  # pet animation
        self.pose = "sit"
        self.pet = pet.Idle(pet.IDLE_BASE, pet.IDLE_ACTS, pet.IDLE_WEIGHTS, gap=(8, 24))
        # inline mode: the cat works on a laptop, glasses on, and keeps losing focus
        self.pet_inline = pet.Idle(pet.INLINE_BASE, pet.INLINE_ACTS, pet.INLINE_WEIGHTS,
                                   gap=pet.INLINE_GAP)
        self.sniffer, self.sniff_result = None, {}
        # Wi-Fi mode: the hotspot and the web UI, turned on and off from the NO LINK screen
        self.wifi = hotspot.Hotspot()
        if not self.wifi.available():
            self.wifi = None  # no Wi-Fi radio (the Pi 1): no button
        self.web = None
        self.wifi_busy = False  # starting or stopping in its thread
        self.wifi_note = None   # why the last start failed, shown on the Wi-Fi screen
        self.pet_router = pet.Idle(pet.ROUTER_BASE, pet.ROUTER_ACTS, pet.ROUTER_WEIGHTS,
                                   gap=pet.ROUTER_GAP)
        # desk mode: the forecast (started by main(), never in the tests) and the day's tests
        self.weather, self.weather_on = None, False
        self.client, self.client_at = None, -1e9  # the Wi-Fi network the Pi is on (NO LINK)
        self.web_lan = None  # the web UI on that network, behind WEB_PIN (keep_lan_web)
        self.lan_port = web.PORT
        self.restart_pending = set()  # saved settings waiting for a restart (both web UIs)
        self.updater = update.Updater(on_restart=self.restart)  # Check for updates, Install, Roll back
        self.day = {"date": None, "tests": 0, "fails": 0}
        self.last_failed = False  # the last test failed: a pass after it earns the cat a fish
        self.title = display.TitleWave()  # the title's wave on the waiting screens (pause)
        self.title_drawn = None           # the wave's frame on the panel now (None: at rest)

    # --- touch -------------------------------------------------------------------------
    # Called from the touch thread: only queue or flag, the main loop does the drawing.

    def on_tap(self, pos):
        self.tap.set()
        if pos is None and self.view != "results":
            return  # too short to read where it landed: don't guess on the lists
        self.actions.put(("tap", pos))
        self.wake.set()

    def on_double(self, pos):
        self.tap.set()
        hit = display.list_hit(pos, len(self.vlan_list), self.scroll) if self.view == "list" else None
        if hit in ("up", "down"):  # quick taps on a scroll button: two steps, not a re-run
            self.actions.put(("tap", pos))
            self.actions.put(("tap", pos))
            self.wake.set()
        else:
            self.rerun.set()

    def on_hold(self, pos):
        self.tap.set()
        # only a hold on the top bar blinks the port, so scrolling can't set it off
        if pos is None or pos[1] <= display.HEADER_H + 12:
            self.blink.set()

    @property
    def vlan_list(self):
        return sorted(self.tagged) if self.tagged else []

    def apply_touches(self):
        """Moves between the screens: results -> VLAN list -> one VLAN, and back."""
        changed = False
        while True:
            try:
                _, pos = self.actions.get_nowait()
            except queue.Empty:
                return changed
            if self.mode == "cable":
                continue  # the cable test has a single screen
            changed = True
            if self.mode == "inline":
                self.view = {"path": "list", "list": "traffic"}.get(self.view, "path")
                continue
            if self.view == "results":
                self.view, self.scroll = "list", 0
            elif self.view == "list":
                vlans = self.vlan_list
                hit = display.list_hit(pos, len(vlans), self.scroll)
                page = display.LIST_VLAN_ROWS  # the arrows turn a whole page
                if hit == "up":
                    self.scroll = max(0, self.scroll - page)
                elif hit == "down":
                    self.scroll = min(display.max_scroll(len(vlans)), self.scroll + page)
                elif hit:
                    self.view, self.vlan = "vlan", vlans[hit[1]]
                else:
                    self.view = "results"
            else:
                self.view = "list"

    def draw(self, banner=None, cat_only=False):
        if self.apply_touches() or self.dirty:
            cat_only = False
            self.dirty = False
        self.tap.clear()
        self.frame += 1
        display.title_frame = self.title_drawn  # a full redraw keeps the title as it is
        try:
            self._draw(banner, cat_only)
        finally:
            display.title_frame = None

    def _draw(self, banner, cat_only):
        if self.mode == "inline":
            snap = self.session.snapshot() if self.session else {}
            page = {"list": display.render_inline_list, "traffic": display.render_inline_traffic}.get(
                self.view, display.render_inline)
            self.out.show(page(snap, self.iface, self.frame, self.pose))  # live data: whole screen
            return
        if self.view == "list":
            img = display.render_details(self.report, self.iface, self.native, self.tagged,
                                         self.offers, self.frame, self.pose, self.scroll)
        elif self.view == "vlan":
            img = display.render_vlan(self.report, self.iface, self.vlan,
                                      self.offers.get(self.vlan), self.vlan in self.offers,
                                      self.frame, self.pose)
        else:
            footer = None
            if self.mode == "cable":
                banner = banner or self.banner
                footer = "2 taps: test the cable again"
            img = display.render(self.report, self.iface, banner, self.frame, self.pose, footer)
        self.out.show(img, band=display.CAT_BAND if cat_only else None)
        if not cat_only:
            if self.mode == "inline":
                n = len((self.session.snapshot() if self.session else {}).get("warnings") or [])
                title = "INLINE" + (f" · {n} warning{'s' * (n > 1)}" if n else "")
            else:
                title = banner or display.verdict(self.report)
            self.publish(self.mode, title)

    def idle(self, seconds, tick=0.5, done=False):
        """Waits while the cat lives in its corner; returns early when interrupted.
        Only the cat's corner rows are rewritten on each tick. done: the test is over, the
        title's wave may run on the results (never in inline mode, nor on the lists)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.interrupted():
            waves = done and self.mode != "inline" and self.view == "results"
            self.pause(tick, self.results_screen if waves else None, self.wake)
            self.wake.clear()
            if self.mode == "inline":
                self.pose, _ = self.pet_inline.tick()  # nonchalant whatever the results
            else:
                # no yawning at a problem: only the small acts while the results are bad
                serious = display.overall(self.report) in (checks.WARN, checks.FAIL)
                self.pet.acts = pet.IDLE_ACTS_SERIOUS if serious else pet.IDLE_ACTS
                self.pose, _ = self.pet.tick()
            self.keep_port_b()
            self.draw(cat_only=not self.keep_console())
        self.rest_title()

    def results_screen(self):
        """The results screen as it stands, for the title's wave (pause)."""
        footer = "2 taps: test the cable again" if self.mode == "cable" else None
        banner = self.banner if self.mode == "cable" else None
        return display.render(self.report, self.iface, banner, self.frame, self.pose, footer)

    # --- the title's wave -------------------------------------------------------------

    def pause(self, seconds, screen=None, wake=None):
        """Waits `seconds` (wake: an Event that ends it early). Meanwhile, with `screen` (the
        screen as it stands), the title's wave runs when it is due: the title bar only is
        redrawn, TITLE_FPS times a second. Without it the title rests."""
        end = time.monotonic() + seconds
        for _ in range(int(seconds * display.TITLE_FPS) + 1):  # never more steps than that
            frame = self.title.frame() if screen else None
            if frame != self.title_drawn:
                if screen:
                    self.show_title(screen, frame)
                else:  # a screen without the wave: the next full redraw draws it at rest
                    self.title.stop()
                    self.title_drawn = None
            left = end - time.monotonic()
            if left <= 0:
                return
            step = left if frame is None else min(left, 1 / display.TITLE_FPS)
            if wake is not None:
                if wake.wait(step):
                    return
            else:
                time.sleep(step)

    def show_title(self, screen, frame):
        display.title_frame = frame
        try:
            img = screen()
        finally:
            display.title_frame = None
        self.out.show(img, band=(0, display.HEADER_H))
        self.title_drawn = frame

    def rest_title(self):
        """Leaving a waiting screen: no wave until the next gap; the next screen draws the
        title at rest."""
        self.title.stop()
        self.title_drawn = None

    def keep_port_b(self):
        """The USB adapter can be plugged in at any time and comes back down after every
        replug; NetworkManager leaves it alone, so the app brings it up (no address)."""
        if not self.port_b:
            self.port_b = cable.second_port(self.iface, self.cfg.get("CABLE_IFACE", ""))
        if self.port_b and checks.admin_down(self.port_b):
            checks.run(["ip", "link", "set", self.port_b, "up"])

    # --- Wi-Fi mode ------------------------------------------------------------------------

    def wifi_status(self):
        """The hotspot's state for the screens; also keeps the top bar's Wi-Fi mark right."""
        status = self.wifi.status() if self.wifi else {"on": False}
        display.wifi_mark = bool(status.get("on"))
        return status

    def toggle_wifi(self):
        if not self.wifi or self.wifi_busy or self.wifi.status().get("joining"):
            return  # a join switches the radio itself and brings the hotspot back on failure
        self.wifi_busy = True
        on = self.wifi.status().get("on")
        threading.Thread(target=self._wifi_off if on else self._wifi_on, daemon=True).start()

    def _wifi_on(self):
        self.wifi_note = None
        try:
            info = self.wifi.start()  # up to ~40 s; wlan0 leaves the home network
            self.start_web(info["address"])
            print(f"wifi: hotspot up, web UI at {info['url']}", flush=True)
        except hotspot.HotspotError as e:  # wlan0 is already back on its network
            self.wifi_note = f"could not start: {e}"
            print(f"wifi: {self.wifi_note}", flush=True)
        except Exception as e:  # the web UI failed: take the hotspot down with it
            self.wifi_note = f"could not start: {e}"
            print(f"wifi: {self.wifi_note}", flush=True)
            self._wifi_off()
        finally:
            self.wifi_busy = False
            self.wake.set()

    def start_web(self, address):
        """The web UI on the hotspot's address, no PIN (being on the hotspot means operating
        fluGATO); keep_lan_web runs the one on a joined network, behind WEB_PIN."""
        self.web = web.WebUI(hotspot=self.wifi, on_saved=self.settings_saved,
                             live_keys=LIVE_KEYS, on_join=self.on_join, on_restart=self.restart,
                             on_power=self.power_off_now, pending=self.restart_pending,
                             updater=self.updater)
        self.web.start(address)

    def restart(self):
        """From the web UI's thread: restart through systemd (Restart=always), so every
        setting applies and every cleanup runs; the next start brings the hotspot back."""
        try:
            if self.wifi and self.wifi.status().get("on"):
                open(RESUME_HOTSPOT, "w").close()
        except OSError:
            pass
        print("restart: from the web UI", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)  # the main thread exits through atexit

    # --- power off ------------------------------------------------------------------------

    def ask_power_off(self):
        """The power button on NO LINK: "Turn off?" until a tap; Power off turns fluGATO off,
        any other tap (Cancel is the whole width) or POWER_ASK seconds with none, or a cable,
        cancels. True when the power is going off."""
        end = time.monotonic() + config.setting(self.cfg, "POWER_ASK", 3, 60)
        shown = None
        self.tap.clear()
        while not checks.carrier(self.iface):
            left = int(end - time.monotonic() + 0.999)
            if left <= 0:
                break
            if self.tap.is_set() or self.rerun.is_set() or self.blink.is_set():
                self.tap.clear()
                self.rerun.clear()
                self.blink.clear()
                pos = None
                while not self.actions.empty():
                    _, pos = self.actions.get_nowait()
                return display.power_off_hit(pos) and self.power_off()
            if left != shown:
                self.out.show(display.render_power_ask(self.iface, left))
                self.publish("nolink", "Turn off?")
                shown = left
            self.wake.wait(0.25)
            self.wake.clear()
        return False

    def power_off(self):
        """Turning off… on the panel, then a clean power-off; leaving() draws it again on
        the way out. When the Pi halts the panel goes gray: safe to unplug."""
        self.out.show(display.render_power_off(self.iface))
        print("power: off, from the screen", flush=True)
        if not self.power_off_now():
            self.out.show(display.render_message("NO POWER OFF", "systemctl poweroff failed",
                                                 self.iface, mood="fail"))
            time.sleep(3)
            return False
        time.sleep(60)  # systemd stops us in a moment (SIGTERM)
        return True

    def power_off_now(self):
        """Also "Power off" on the web UI (its thread): systemd takes the system down and
        stops this app on the way, which then shows Turning off… (leaving)."""
        ok = checks.run(["systemctl", "poweroff"]) is not None
        if not ok:
            print("power: systemctl poweroff failed", flush=True)
        return ok

    def leaving(self):
        """At exit (atexit, before the console's restore): on a power-off or a reboot, the
        rooftop screen for it, and the console stays quiet so its messages don't cover it."""
        going = checks.shutting_down()
        if not going:
            return
        try:
            self.out.show(display.render_power_off(self.iface, reboot=going == "reboot"))
        except OSError:
            pass
        if self.console:
            self.console.release()

    def stop_web(self):
        if self.web:
            self.web.stop()
            self.web = None

    def on_join(self, ssid):
        """Connect on the web UI's Wi-Fi page: leave the hotspot for a saved network. The
        hotspot (and the web UI) come back by themselves if the join fails."""
        def run():
            ok = self.wifi.join(ssid, stop_web=self.stop_web, start_web=self.start_web)
            st = self.wifi.status()
            print("wifi: joined" if ok else f"wifi: join failed: {st.get('failed')}, "
                  f"back on {st.get('fallback')}", flush=True)  # never the password
            self.wake.set()
        threading.Thread(target=run, daemon=True).start()

    def _wifi_off(self):
        try:
            self.stop_web()      # before the hotspot goes
            self.wifi.stop()     # rejoins the home network
            print("wifi: hotspot off", flush=True)
        finally:
            self.wifi_busy = False
            self.wake.set()

    def shutdown_wifi(self):
        self.stop_lan_web()
        if self.wifi and (self.web or self.wifi.status().get("on")):
            self._wifi_off()

    # --- the web UI on the joined Wi-Fi network -------------------------------------------

    def keep_lan_web(self):
        """The web UI on the Wi-Fi network the Pi is on as a client, at its address there,
        behind WEB_PIN; none with the hotspot on or joining, off any network, or with
        WEB_PIN=off. Called every 5 s (lan_web_keeper); moves when the address changes."""
        pin = str(self.cfg.get("WEB_PIN", web.DEFAULT_PIN)).strip()
        st = self.wifi.status() if self.wifi else {}
        client = None
        if self.wifi and pin.lower() != "off" and not st.get("on") and not st.get("joining"):
            client = hotspot.client()
            self.client, self.client_at = client, time.monotonic()  # NO LINK's top bar too
        want = client["address"] if client else None
        if self.web_lan and self.web_lan.address != want:
            self.stop_lan_web()
        if want and not self.web_lan:
            ui = web.WebUI(hotspot=self.wifi, on_saved=self.settings_saved, live_keys=LIVE_KEYS,
                           on_restart=self.restart, on_power=self.power_off_now,
                           pending=self.restart_pending, updater=self.updater,
                           pin=lambda: self.cfg.get("WEB_PIN", web.DEFAULT_PIN))
            try:
                ui.start(want, self.lan_port)
            except OSError as e:
                print(f"web: could not start on the Wi-Fi network: {e}", flush=True)
                return
            self.web_lan = ui
            print(f"web: on the Wi-Fi network at {ui.url} (PIN)", flush=True)

    def stop_lan_web(self):
        ui, self.web_lan = self.web_lan, None
        if ui:
            ui.stop()
            print("web: off the Wi-Fi network", flush=True)

    def lan_web_keeper(self):
        """main() runs this in a thread (the tests don't)."""
        while True:
            try:
                self.keep_lan_web()
            except Exception as e:  # a bad moment of NetworkManager must not end the thread
                print(f"web: {e}", flush=True)
            time.sleep(5)

    def settings_saved(self, cfg):
        """From the web UI's thread: the keys read at every check apply at once."""
        self.cfg = cfg
        self.check_host = cfg["CHECK_HOST"]
        self.lldp_wait = as_int(cfg["LLDP_WAIT"], 90)
        self.extra_vlans = {int(v) for v in cfg.get("PROBE_VLANS", "").replace(" ", "").split(",")
                            if v.isdigit() and 1 <= int(v) <= 4094}
        self.start_weather()

    # --- desk mode ---------------------------------------------------------------------------

    def start_weather(self):
        """Keeps the forecast fresh for WEATHER_LAT / WEATHER_LON, or for where the IP
        address says the Pi is when they are empty; "off" stops it. Called again when the
        settings are saved."""
        if not self.weather_on:
            return
        if weather.disabled(self.cfg):
            if self.weather:
                self.weather.stop()
                self.weather = None
            return
        place = weather.place(self.cfg)  # None: from the IP address
        if not self.weather:
            self.weather = weather.Weather(*(place or ()))
            self.weather.start()
        elif place is None and not self.weather.auto:
            self.weather.set_place()
        elif place is not None and (self.weather.auto or self.weather.place != place):
            self.weather.set_place(*place)

    def zone(self):
        """The time zone of the place the forecast is for (WEATHER_LAT / WEATHER_LON, or
        where the IP address says the Pi is): the clock follows the place, whatever the
        Pi's own zone (Pi OS ships with Europe/London). None: the Pi's local time."""
        f = self.forecast()
        if not f or not f.get("tz"):
            return None
        try:
            return zoneinfo.ZoneInfo(f["tz"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return None

    def local(self, when=None):
        """time.struct_time for the clock: now, or the epoch `when`."""
        tz = self.zone()
        if tz is None:
            return time.localtime(when)
        when = time.time() if when is None else when
        return datetime.datetime.fromtimestamp(when, tz).timetuple()

    def wifi_client(self):
        """{ssid, address} of the Wi-Fi network the Pi is on as a client, read every 5 s;
        None with no radio, with the hotspot on or off any network."""
        if not self.wifi:
            return None
        now = time.monotonic()
        if now - self.client_at >= 5:
            self.client, self.client_at = hotspot.client(), now
        return self.client

    def forecast(self):
        return self.weather.current() if self.weather else None

    def count_test(self, failed):
        """The day's tests for the desk screen; a pass right after a failure: a fish."""
        today = time.strftime("%Y-%m-%d")
        if self.day["date"] != today:
            self.day = {"date": today, "tests": 0, "fails": 0}
        self.day["tests"] += 1
        self.day["fails"] += bool(failed)
        if not failed and self.last_failed:
            self.pet.play(pet.WEB_ACTS["fish"] * 3, "treat")
        self.last_failed = failed

    def day_stats(self):
        today = time.strftime("%Y-%m-%d")
        return self.day if self.day["date"] == today else {"tests": 0, "fails": 0}

    def desk_mode(self, quiet):
        """None, "weather", "clock", "night" or "night_weather": what NO LINK shows after
        `quiet` seconds with no touch. Only with the clock set from the network. With a
        forecast the weather comes and goes: DESK_TURN s of it, DESK_TURN s of the clock
        (by day the big clock, in the NIGHT_HOURS the night screen; at night the weather
        board dims and the cat sleeps through it)."""
        minutes = as_int(self.cfg.get("DESK_MINUTES", "3"), 3)
        if not minutes or quiet < minutes * 60 or not checks.time_synced():
            return None
        night = config.is_night(config.night_hours(self.cfg), self.local().tm_hour)
        turn = config.setting(self.cfg, "DESK_TURN", 10, 3600)
        weather_turn = (quiet - minutes * 60) % (2 * turn) < turn
        if self.forecast() and weather_turn:
            return "night_weather" if night else "weather"
        return "night" if night else "clock"

    def publish(self, mode, title):
        """What the screen shows, for the web UIs' status page."""
        uis = [ui for ui in (self.web, self.web_lan) if ui]
        if uis:
            rows = [] if mode in ("nolink", "inline") else \
                [(r.label, r.value, r.state) for r in self.report.values()]
            for ui in uis:
                ui.publish(mode, rows, title)

    def keep_console(self):
        """True when the console had taken the panel back: the caller redraws it all."""
        return bool(self.console and self.console.keep())

    def interrupted(self):
        return (self.rerun.is_set() or self.blink.is_set() or not checks.carrier(self.iface)
                or self.b_changed() or self.inline_ended())

    def inline_ended(self):
        """The bridge took itself down (port A unplugged, a loop, a crash in its thread)."""
        return (self.mode == "inline" and self.session is not None
                and self.session.snapshot().get("state") in ("stopped", "error"))

    def b_changed(self):
        """The second port was plugged or unplugged: cable test <-> network checks."""
        return bool(self.port_b) and checks.carrier(self.port_b) != self.b_link

    def run_checks(self):
        r = self.report = checks.new_report()
        self.view, self.scroll, self.native, self.offers = "results", 0, None, {}
        self.tap.clear()
        while not self.actions.empty():
            self.actions.get_nowait()
        self.draw()
        if self.cable_changed:
            # the cable may now be in another port: drop what the last one told us
            self.cable_changed = False
            checks.renew_dhcp(self.iface)
            checks.forget_neighbours()
        # listen for tagged frames (trunk ports) while the other checks run; a listener
        # left over from an interrupted run is stopped first, and each run has its own
        # stop flag and result, so an old thread can't write into the new report
        self.stop_sniffer()
        self.sniff_stop = threading.Event()
        self.sniff_result = {}
        self.sniffer = threading.Thread(target=self.sniff,
                                        args=(self.sniff_stop, self.sniff_result), daemon=True)
        self.sniffer.start()
        steps = (
            lambda: checks.check_link(self.iface, r["LINK"]),
            self.addressing,
            lambda: checks.check_gateway(self.gateway, self.iface, r["GATEWAY"]),
            self.dns_step,
            lambda: checks.check_internet(self.check_host, self.resolved, self.iface, r["INTERNET"]),
        )
        self.gateway, self.dns, self.resolved = None, [], None
        for step in steps:
            if self.interrupted():
                return
            step()
            self.draw()
        self.listen_for_switch()
        if not self.interrupted():
            self.count_test(display.overall(r) == checks.FAIL)

    def addressing(self):
        r = self.report
        self.gateway, self.dns = checks.check_ip(
            self.iface, r["IP"], r["DHCP"], lambda: not self.interrupted())

    def dns_step(self):
        self.resolved = checks.check_dns(self.dns, self.check_host, self.iface, self.report["DNS"])

    def sniff(self, stop, result):
        heard = checks.sniff_vlans(self.iface, 25, stop=lambda: stop.is_set() or self.interrupted())
        if heard is None:
            result["tagged"] = set(self.extra_vlans) or None
        else:
            result["tagged"] = heard | self.extra_vlans

    def stop_sniffer(self):
        if self.sniffer and self.sniffer.is_alive():
            self.sniff_stop.set()
            self.sniffer.join(2)

    @property
    def tagged(self):
        return self.sniff_result.get("tagged")

    def finish_vlans(self, native):
        """Waits for the tagged-frame listen, then merges it into the VLAN row."""
        r = self.report
        self.native = native
        while self.sniffer.is_alive() and not self.interrupted():
            if r["VLAN"].state == PENDING or native:
                r["VLAN"].set((f"{native} native  " if native else "") + "checking for trunk…",
                              PENDING)
            self.draw()
            self.idle(2)
        if self.interrupted():
            return
        r["VLAN"].set(*checks.vlan_summary(native, self.tagged))
        self.draw()
        if self.tagged:
            self.probe_vlans(native)

    def probe_vlans(self, native, limit=32):
        """Trunk: asks for a DHCP offer on every tagged VLAN at once (never takes a lease).
        The VLAN row shows the count, the VLAN list fills in as offers arrive, and each
        answer goes to the log (journalctl -u flugato)."""
        r = self.report
        vlans = sorted(self.tagged)[:limit]
        r["VLAN"].set((f"{native} native  " if native else "") +
                      f"asking DHCP on {len(vlans)} VLANs…", PENDING)
        self.draw()

        def arrived(vlan, offer):
            self.offers[vlan] = offer
            self.dirty = True
            self.wake.set()  # the VLAN list redraws with the new row

        done = threading.Event()
        result = {}
        threading.Thread(target=lambda: (result.update(dhcp_probe.dhcp_offers(
            self.iface, vlans, on_offer=arrived)), done.set()), daemon=True).start()
        while not done.is_set() and not self.interrupted():
            self.idle(0.5)
        if self.interrupted():
            return
        for vlan in vlans:
            offer = self.offers.setdefault(vlan, result.get(vlan))
            if offer:
                print(f"VLAN {vlan}: offer {offer['address']}/{offer['prefix']} from "
                      f"{offer['server']}, router {offer['router']}, dns {offer['dns']}, "
                      f"lease {offer['lease']}s", flush=True)
            else:
                print(f"VLAN {vlan}: no DHCP offer", flush=True)
        r["VLAN"].set(*checks.vlan_summary(native, self.tagged, self.offers))
        self.draw()

    def listen_for_switch(self):
        r = self.report
        end = time.monotonic() + self.lldp_wait
        while not self.interrupted():
            n = checks.lldp_neighbor(self.iface)
            if n:
                native = checks.fill_switch_rows(n, r["SWITCH"], r["PORT"], r["VLAN"])
                self.draw()
                self.finish_vlans(native)
                return
            left = int(end - time.monotonic())
            if left <= 0:
                r["SWITCH"].set("no LLDP/CDP heard (unmanaged switch?)", WARN)
                r["PORT"].set("", SKIP)
                r["VLAN"].set("", SKIP)
                self.draw()
                self.finish_vlans(None)
                return
            r["SWITCH"].set(f"listening for LLDP/CDP… {left} s", PENDING)
            self.draw()
            self.idle(3)

    def no_link(self):
        """NO LINK: the fat cat sleeps, breathes and now and then wakes up for an act
        (peek, yawn, stretch, lick, walk, turn). A touch on it pokes it; a touch elsewhere
        is a laser dot it chases; the Wi-Fi button turns the hotspot on, and then the router
        cat shows what a phone needs. With no touch for a while: desk mode (the clock, the
        weather presenter, the night) and now and then the variety show. Returns on link."""
        sleeper = pet.Idle(pet.SLEEP_BASE, pet.SLEEP_ACTS, pet.SLEEP_WEIGHTS, gap=pet.SLEEP_GAP)
        presenter = pet.Idle(pet.DESK_BASE, pet.DESK_ACTS, pet.DESK_WEIGHTS, gap=pet.DESK_GAP)
        shown, tick, laser = None, 0, None
        quiet = last_show = time.monotonic()
        while not checks.carrier(self.iface):
            now = time.monotonic()
            status = self.wifi_status()
            wifi_screen = status.get("on") or self.wifi_busy or status.get("joining")
            desk = None if wifi_screen else self.desk_mode(now - quiet)
            if self.rerun.is_set() or self.blink.is_set() or self.tap.is_set():
                self.rerun.clear()
                self.blink.clear()
                self.tap.clear()
                button, power, pos = False, False, None
                while not self.actions.empty():
                    _, pos = self.actions.get_nowait()
                    button = button or (self.wifi is not None and display.wifi_hit(pos))
                    power = power or display.power_hit(pos)
                quiet = last_show = now
                if button and desk not in ("night", "night_weather"):  # no button at night: wake first
                    self.toggle_wifi()
                elif power and not desk and not wifi_screen:  # only on NO LINK itself
                    self.rest_title()
                    self.ask_power_off()
                    quiet = last_show = time.monotonic()
                    shown, laser = None, None
                    continue
                elif desk or wifi_screen:
                    pass  # a touch only ends desk mode
                elif pos is None or display.big_cat_hit(pos, sleeper.x):
                    sleeper.play(pet.POKE)
                    laser = None
                else:
                    laser = self.laser(sleeper, pos)
                desk = None
            show = as_int(self.cfg.get("SHOW_MINUTES", "10"), 10)
            sched = presenter if desk == "weather" else sleeper
            if (show and not wifi_screen and desk not in ("night", "night_weather")
                    and now - last_show >= show * 60
                    and sched.current is None):
                sched.play(pet.variety(start="sit" if sched is presenter else "loaf"), "show")
                last_show, laser = now, None
            if wifi_screen:
                pose, x = self.pet_router.tick()
            else:
                pose, x = sched.tick()
            if laser:
                laser[2] -= 1
                if laser[2] <= 0 or sleeper.current != "chase":
                    laser = None  # caught (or the chase was cut short)
            frame = tick // 4  # the z's step once a second
            if tick % 2 == 0:
                self.keep_port_b()
                if self.keep_console():
                    shown = None
            joined = status.get("joined")
            wifi = None if wifi_screen else self.wifi_client()  # the top bar shows it
            detail = self.wifi_note or (
                f"Wi-Fi: {status['failed']}" if status.get("failed") else "plug in a cable")
            if self.port_b and checks.carrier(self.port_b):
                detail = "cable test: other end into the main port"
            clock = time.strftime("%H:%M", self.local()) if desk else None
            forecast = self.forecast() if desk in ("weather", "night_weather") else None
            if forecast:
                forecast = weather.in_units(forecast, self.cfg.get("WEATHER_UNITS", "F"))
                forecast["as_of"] = time.strftime("%H:%M", self.local(forecast["at"]))
            state = (pose, x, frame, status.get("on"), self.wifi_busy, status.get("clients"),
                     self.wifi_note, status.get("joining"), str(joined), status.get("failed"),
                     desk, clock, forecast and (forecast["at"], forecast["unit"]),
                     tuple(laser[:2]) if laser else None,
                     detail, tuple(self.day_stats().values()), str(wifi))
            screen = lambda: self.no_link_screen(status, desk, detail, frame, pose, x, laser,
                                                 forecast, sched, wifi)
            if state != shown:  # only redraw what changed (the title as it is on the panel)
                display.title_frame = self.title_drawn
                try:
                    img = screen()
                finally:
                    display.title_frame = None
                self.out.show(img)
                self.publish("nolink", "NO LINK")
                shown = state
            tick += 1
            # the title's wave while waiting, but not at night: the cat sleeps
            self.pause(0.25, None if desk in ("night", "night_weather") else screen)
        self.rest_title()

    def laser(self, sleeper, pos):
        """A touch away from the cat: a red dot there, and the cat goes for it. Returns
        [x, y, ticks until it is caught]."""
        centre = display.BIG_CAT_X + pet.CW * display.BIG_PET_SCALE // 2
        target = round((pos[0] - centre) / display.BIG_PET_SCALE)
        target = max(-6, min(58, target))  # the whole cat stays on the screen
        seq, catch = pet.chase(target - sleeper.x)
        sleeper.play(seq, "chase")
        return [pos[0], pos[1], catch]

    def no_link_screen(self, status, desk, detail, frame, pose, x, laser, forecast, sched, wifi=None):
        if status.get("on") or self.wifi_busy or status.get("joining"):
            return display.render_wifi(status, self.iface, frame, pose, busy=self.wifi_busy)
        button = "Wi-Fi Hotspot" if self.wifi else None
        now = self.local()
        hat = pet.season(now.tm_mon, now.tm_mday)
        if desk == "night":
            return display.render_night(now, detail, self.iface, frame, pose, x, wifi)
        if desk == "night_weather":  # the board, dimmed; the cat asleep in its nightcap
            return display.render_desk(now, forecast, self.day_stats(), detail, self.iface, frame,
                                       pose, x, ["nightcap"], "sleep", None, wifi, dim=True)
        if desk:
            wear, mood = [hat] if hat else [], "sleep"
            if forecast and sched.current != "show":
                wear, mood = pet.outfit(forecast["kind"], forecast.get("night"), hat)
            return display.render_desk(now, forecast, self.day_stats(), detail, self.iface,
                                       frame, pose, x, wear, mood, button, wifi)
        return display.render_message("NO LINK", detail, self.iface, mood="sleep", frame=frame,
                                      pose=pose, xcells=x, button=button,
                                      wear=[hat] if hat else (),
                                      laser=tuple(laser[:2]) if laser else None, wifi=wifi,
                                      scene="closet", power=True)

    # --- cable test ----------------------------------------------------------------------

    def port_b_kind(self):
        """Both ports are up: "cable" (one cable between them), "device" (the tester sits
        between a switch and a device: inline) or "switch" (two switch ports)."""
        time.sleep(2)  # the adapter's link comes up a moment after it reports carrier
        ours = {cable.mac_of(self.iface), cable.mac_of(self.port_b)}
        return inline.listen_port_b(self.port_b, ours, 3.0, probe_from=self.iface)

    def run_inline(self):
        """Bridges port A (switch) and port B (device) and watches what crosses."""
        self.mode, self.view = "inline", "path"
        self.session = inline.InlineSession(self.iface, self.port_b)
        try:
            self.session.start()
        except inline.InlineError as e:
            self.end_inline()
            self.out.show(display.render_message("NO INLINE", str(e), self.iface, mood="fail"))
            print(f"inline: could not start: {e}", flush=True)
            time.sleep(5)
            self.mode = "network"
            self.run_checks()
            return
        print("inline: bridged", flush=True)
        self.pose, _ = self.pet_inline.tick()  # the laptop comes out at once
        self.draw()

    def end_inline(self):
        self.pose = "sit"  # the laptop goes away with the bridge
        if self.session:
            snap = self.session.snapshot()
            self.session.stop()
            self.session = None
            reason = snap.get("error") or snap.get("ended")
            if reason:
                print(f"inline: stopped: {reason}", flush=True)
                self.out.show(display.render_message("INLINE STOPPED", reason, self.iface,
                                                     mood="fail" if snap.get("error") else "blink_off"))
                time.sleep(3)

    def run_cable_test(self):
        self.mode, self.view, self.banner = "cable", "results", "CABLE TEST…"
        r = self.report = {label: checks.Row(label) for label in
                           ("PORT A", "PORT B", "A TO B", "B TO A", "ERRORS", "RESULT")}
        self.draw(banner="CABLE TEST…")
        a, b = cable.link(self.iface), cable.link(self.port_b)
        for row, info, name in ((r["PORT A"], a, f"main ({self.iface})"),
                                (r["PORT B"], b, f"USB ({self.port_b})")):
            speed = checks.fmt_speed(info["speed"]) if info["speed"] else "speed unknown"
            row.set(" ".join(filter(None, (speed, info["duplex"]))) + f"  {name}",
                    OK if info["speed"] else WARN)
        self.draw(banner="CABLE TEST…")
        errors_before = cable.errors(self.iface) + cable.errors(self.port_b)
        runs = {}
        for label, tx, rx in (("A TO B", self.iface, self.port_b), ("B TO A", self.port_b, self.iface)):
            if self.interrupted():
                return
            r[label].set("sending test frames…", PENDING)
            self.draw(banner="CABLE TEST…")
            runs[label] = cable.frames_one_way(tx, rx)
            r[label].set(*cable.fmt_run(runs[label]))
            self.draw(banner="CABLE TEST…")
        new_errors = cable.errors(self.iface) + cable.errors(self.port_b) - errors_before
        r["ERRORS"].set(f"{new_errors} CRC / frame errors", OK if not new_errors else checks.FAIL)
        state, text = cable.verdict(a, b, runs["A TO B"], runs["B TO A"], new_errors)
        r["RESULT"].set(text, state)
        banner = {OK: "CABLE PASS", WARN: "CABLE WARN", checks.FAIL: "CABLE FAIL"}[state]
        self.banner = banner
        self.draw(banner=banner)
        self.count_test(state == checks.FAIL)
        print(f"cable test: {banner}: {text}; A: {r['PORT A'].value}; B: {r['PORT B'].value}; "
              f"{r['A TO B'].value}; {r['B TO A'].value}; {r['ERRORS'].value}", flush=True)

    def do_blink(self):
        checks.blink_port(self.iface, status=lambda title, detail: self.out.show(
            display.render_message(title, detail, self.iface,
                                   mood="blink_on" if title.endswith("ON") else "blink_off")))

    def loop(self):
        while True:
            if self.blink.is_set():
                self.blink.clear()
                if checks.carrier(self.iface) and self.mode == "network":
                    self.do_blink()
                self.rerun.set()
            if not checks.carrier(self.iface):
                self.no_link()
                self.cable_changed = True
                time.sleep(1)  # let autonegotiation settle before reading speed
            self.rerun.clear()
            self.keep_port_b()
            if self.port_b and not checks.carrier(self.port_b):
                # one cable between both ports: the adapter reports the same link a moment
                # later than the main port, and deciding before it would run the network checks
                for _ in range(8):
                    time.sleep(0.25)
                    if checks.carrier(self.port_b):
                        break
            self.b_link = bool(self.port_b) and checks.carrier(self.port_b)
            kind = self.port_b_kind() if self.b_link else None
            if kind == "cable":
                self.run_cable_test()
            elif kind == "device":
                self.run_inline()
            else:
                self.mode = "network"
                self.run_checks()
            # results stay on screen until something happens
            try:
                while not self.interrupted():
                    self.idle(60, done=True)
            finally:
                self.end_inline()  # the bridge never outlives inline mode


def preview(path, scenario):
    from .demo import SCENARIOS
    report, iface = SCENARIOS[scenario]()
    if scenario == "blink":
        img = display.render_message("PORT LED ON", "blink 2 of 3, watch the switch port",
                                     iface, mood="blink_on")
    elif scenario == "wifi":
        display.wifi_mark = True
        img = display.render_wifi({"on": True, "ssid": "fluGATO", "password": "kepvazum",
                                   "url": "http://192.0.2.1", "clients": 1}, iface, 1, "ant_look")
    elif scenario in ("desk", "desk_rain", "desk_snow", "desk_storm", "desk_clock", "night",
                      "night_weather"):
        from .demo import forecast
        now = time.strptime("2026-10-10 14:32", "%Y-%m-%d %H:%M")
        stats = {"tests": 7, "fails": 1}
        wifi = {"ssid": "lab-wifi", "address": "192.0.2.10"}
        cast = {"desk": forecast(temp=21, hi=23, lo=14),
                "desk_rain": forecast("rain", 12, 14, 9, 80, "rain · 80%"),
                "desk_snow": forecast("snow", -2, 0, -5, 60, "snow · 60%"),
                "desk_storm": forecast("storm", 17, 19, 12, 70, "thunderstorms · 70%")}.get(scenario)
        if cast:
            cast = weather.in_units(cast, "F")
        if scenario == "night_weather":
            cast = weather.in_units(forecast("sun", 18, 23, 14, 5, "clear · rain 5%", night=True), "F")
            img = display.render_desk(time.strptime("2026-10-10 23:40", "%Y-%m-%d %H:%M"), cast, stats,
                                      "plug in a cable", iface, 3, "loaf", 0, ["nightcap"], "sleep",
                                      None, wifi, dim=True)
        elif scenario == "night":
            img = display.render_night(time.strptime("2026-10-10 02:17", "%Y-%m-%d %H:%M"),
                                       "plug in a cable", iface, 3, "loaf", wifi=wifi)
        elif cast:
            wear, mood = pet.outfit(cast["kind"], cast["night"], pet.season(now.tm_mon, now.tm_mday))
            img = display.render_desk(now, cast, stats, "plug in a cable", iface, 0, "sit", 0,
                                      wear, mood, "Wi-Fi Hotspot", wifi)
        else:
            img = display.render_desk(now, None, stats, "plug in a cable", iface, 3, "loaf", 0,
                                      ["witch"], "sleep", "Wi-Fi Hotspot", wifi)
    elif scenario == "laser":
        img = display.render_message("NO LINK", "plug in a cable", iface, mood="sleep", frame=1,
                                     pose="walk2", xcells=20, button="Wi-Fi Hotspot",
                                     wear=["witch"], laser=(410, 214), scene="closet", power=True)
    elif scenario == "show":
        img = display.render_message("NO LINK", "plug in a cable", iface, mood="sleep", frame=1,
                                     pose="bow_down", button="Wi-Fi Hotspot", wear=["witch"],
                                     scene="closet", power=True)
    elif scenario == "power_ask":
        img = display.render_power_ask(iface, 7)
    elif scenario in ("power_off", "power_reboot"):
        img = display.render_power_off(iface, reboot=scenario == "power_reboot")
    elif report is None:
        img = display.render_message("NO LINK", "plug in a cable", iface, mood="sleep", frame=3,
                                     pose="loaf", button="Wi-Fi Hotspot", scene="closet",
                                     wifi={"ssid": "lab-wifi", "address": "192.0.2.10"}, power=True)
    elif scenario == "details":
        from .demo import DETAILS
        img = display.render_details(report, iface, *DETAILS, scroll=2)
    elif scenario.startswith("inline"):
        from .demo import inline_snapshot
        render_page = {"inline": display.render_inline, "inline_list": display.render_inline_list,
                       "inline_traffic": display.render_inline_traffic}[scenario]
        img = render_page(inline_snapshot(), iface, 1, "work")  # the inline cat at its laptop
    elif scenario == "cable":
        from .demo import cable_report
        img = display.render(cable_report(), iface, "CABLE PASS", footer="2 taps: test the cable again")
    elif scenario == "vlan":
        from .demo import DETAILS
        offers = DETAILS[2]
        img = display.render_vlan(report, iface, 210, offers[210], True)
    else:
        img = display.render(report, iface)
    img.save(path)


def main():
    ap = argparse.ArgumentParser(prog="flugato")
    ap.add_argument("--config", default=None, help="settings file (default: the boot partition's)")
    ap.add_argument("--png", help="draw to this PNG instead of the framebuffer")
    ap.add_argument("--preview", metavar="PNG", help="render a demo screen and exit")
    ap.add_argument("--scenario", default="good", help="demo scenario for --preview")
    ap.add_argument("--calibrate", action="store_true",
                    help="touch calibration on the panel (stop the service first)")
    ap.add_argument("--write", action="store_true", help="with --calibrate: save to --config")
    args = ap.parse_args()

    if args.calibrate:
        from . import calibrate
        dev = config.load(args.config)["FB"] or display.wait_for_panel(5)
        calibrate.run(dev, config.path(args.config) if args.write else None)
        return

    if args.preview:
        preview(args.preview, args.scenario)
        return

    cfg = config.load(args.config)
    iface = cfg["IFACE"] or checks.detect_iface()
    console = None
    if args.png:
        out = display.PngOutput(args.png)
    else:
        dev = cfg["FB"] or display.wait_for_panel(30)
        if not dev:
            # exit non-zero so systemd restarts us once the panel driver has loaded
            sys.exit("SPI panel framebuffer (fb_*) not found; set FB= to use another one")
        out = display.Framebuffer(dev, as_int(cfg["ROTATE"], None) if cfg["ROTATE"] else None)
        if dev == "/dev/fb0":  # the console's framebuffer: keep it from drawing over us
            console = display.ConsoleOff()
            atexit.register(console.restore)

    # SIGTERM -> sys.exit, so atexit handlers run: the console restore and the bridge teardown
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = App(cfg, out, iface)
    app.console = console
    if app.updater.boot_guard():  # a fresh update that keeps dying: the old tree is back
        sys.exit(1)               # systemd starts it
    atexit.register(app.leaving)  # runs before the console's restore (last in, first out)
    app.weather_on = True  # the tests build an App without it: no network from them
    app.start_weather()
    if app.port_b:
        inline.cleanup(iface, app.port_b)  # a crash may have left eth0 bridged and unmanaged
    if app.wifi:
        hotspot.cleanup()  # and the Pi stuck as a hotspot, away from its home network
        networks.sync()    # the networks saved on the web UI's Wi-Fi page, into NetworkManager
        if os.path.exists(RESUME_HOTSPOT):  # a Save on the web UI that restarted us
            os.remove(RESUME_HOTSPOT)
            app.toggle_wifi()
        atexit.register(app.shutdown_wifi)
        threading.Thread(target=app.lan_web_keeper, name="lan-web", daemon=True).start()
    Touch(on_tap=app.on_tap, on_double=app.on_double, on_hold=app.on_hold,
          cal=parse_cal(cfg["TOUCH_CAL"], cfg.get("PANEL", "")))
    app.loop()


if __name__ == "__main__":
    main()

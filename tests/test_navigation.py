"""Screen navigation driven through the touch callbacks, without a panel."""

import unittest

from flugato import display
from flugato.__main__ import App


class Screen:
    def __init__(self):
        self.shown = 0

    def show(self, img, band=None):
        self.shown += 1


def centre(box):
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


class Navigation(unittest.TestCase):
    def setUp(self):
        cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
               "TOUCH_CAL": ""}
        self.app = App(cfg, Screen(), "lo")
        self.app.sniff_result = {"tagged": set(range(10, 21))}  # 11 tagged VLANs
        self.app.offers = {10: None}

    def tap(self, pos):
        self.app.on_tap(pos)
        self.app.draw()

    def test_results_list_vlan_and_back(self):
        self.tap((240, 160))
        self.assertEqual(self.app.view, "list")
        last_page = display.max_scroll(11)  # 11 VLANs, 7 a page: the second page shows 5-11
        self.tap(centre(display.BTN_DOWN))
        self.assertEqual(self.app.scroll, last_page)
        self.tap(centre(display.BTN_DOWN))  # already on the last page
        self.assertEqual(self.app.scroll, last_page)
        self.tap(centre(display.BTN_UP))
        self.assertEqual(self.app.scroll, 0)
        self.tap(centre(display.BTN_DOWN))
        first_vlan_row = display.LIST_TOP + (display.LIST_FIXED + 0.5) * display.LIST_ROW_H
        self.tap((150, int(first_vlan_row)))
        self.assertEqual((self.app.view, self.app.vlan), ("vlan", 10 + last_page))
        self.tap((240, 160))
        self.assertEqual(self.app.view, "list")
        self.tap((150, 300))  # the footer: nothing there, back to the results
        self.assertEqual(self.app.view, "results")

    def test_scroll_stops_at_the_ends(self):
        self.tap((240, 160))
        for _ in range(20):
            self.tap(centre(display.BTN_DOWN))
        self.assertEqual(self.app.scroll, display.max_scroll(11))
        for _ in range(20):
            self.tap(centre(display.BTN_UP))
        self.assertEqual(self.app.scroll, 0)

    def test_double_tap_on_a_button_pages_not_reruns(self):
        self.tap((240, 160))
        self.app.on_double(centre(display.BTN_DOWN))
        self.app.draw()
        self.assertEqual(self.app.scroll, display.max_scroll(11))
        self.assertFalse(self.app.rerun.is_set())
        self.app.on_double((150, 100))
        self.assertTrue(self.app.rerun.is_set())

    def test_blink_only_from_the_top_bar(self):
        self.app.on_hold((240, 200))
        self.assertFalse(self.app.blink.is_set())
        self.app.on_hold((240, 15))
        self.assertTrue(self.app.blink.is_set())


class CableMode(unittest.TestCase):
    def test_cable_test_runs_end_to_end_with_fake_ports(self):
        from flugato import cable, checks
        saved = (cable.link, cable.errors, cable.frames_one_way, checks.carrier)
        cable.link = lambda iface: {"speed": 1000, "duplex": "Full", "supported": [], "partner": []}
        cable.errors = lambda iface: 0
        cable.frames_one_way = lambda tx, rx: (100, 100, 0.1)
        checks.carrier = lambda iface: True
        try:
            cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
                   "TOUCH_CAL": "", "CABLE_IFACE": "lo"}
            app = App(cfg, Screen(), "lo")
            app.b_link = True
            app.run_cable_test()
            self.assertEqual(app.report["RESULT"].state, checks.OK)
            self.assertEqual(app.banner, "CABLE PASS")
            app.draw()  # idle redraw keeps the cable screen
        finally:
            cable.link, cable.errors, cable.frames_one_way, checks.carrier = saved


class InlineMode(unittest.TestCase):
    def test_inline_pages_and_teardown_with_a_fake_bridge(self):
        from flugato import checks, demo, inline
        events = []

        class FakeSession:
            def __init__(self, a, b):
                self.snap = demo.inline_snapshot()

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

            def snapshot(self):
                return self.snap

        saved = (inline.InlineSession, checks.carrier)
        inline.InlineSession = FakeSession
        checks.carrier = lambda iface: True
        try:
            cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
                   "TOUCH_CAL": "", "CABLE_IFACE": "lo"}
            app = App(cfg, Screen(), "lo")
            app.b_link = True
            app.run_inline()
            self.assertEqual((app.mode, app.view), ("inline", "path"))
            from flugato import pet
            inline_poses = {step[0] for step in pet.INLINE_BASE}
            inline_poses |= {step[0] for act in pet.INLINE_ACTS.values() for step in act}
            self.assertIn(app.pose, inline_poses)  # the cat brought its laptop
            for expected in ("list", "traffic", "path"):  # a tap turns the page
                app.on_tap((240, 160))
                app.draw()
                self.assertEqual(app.view, expected)
            self.assertFalse(app.interrupted())
            app.session.snap = dict(app.session.snap, state="stopped", ended="port A unplugged")
            self.assertTrue(app.interrupted())  # the bridge ended itself
            import flugato.__main__ as m
            saved_sleep, m.time.sleep = m.time.sleep, lambda s: None
            try:
                app.end_inline()
            finally:
                m.time.sleep = saved_sleep
            self.assertEqual(events, ["start", "stop"])
            self.assertIsNone(app.session)
            self.assertEqual(app.pose, "sit")  # and put it away again
        finally:
            inline.InlineSession, checks.carrier = saved


class Upkeep(unittest.TestCase):
    """What the loop keeps right while it waits: the second port up, the console hidden."""

    def setUp(self):
        cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
               "TOUCH_CAL": ""}
        self.app = App(cfg, Screen(), "lo")

    def test_adapter_plugged_in_later_is_brought_up(self):
        from flugato import cable, checks
        runs = []
        saved = (cable.second_port, checks.admin_down, checks.run)
        cable.second_port = lambda main, configured="": "eth1"
        checks.admin_down = lambda iface: True  # every replug brings the adapter back down
        checks.run = lambda cmd, timeout=10: runs.append(cmd)
        try:
            self.app.port_b = None  # no adapter when the app started
            self.app.keep_port_b()
        finally:
            cable.second_port, checks.admin_down, checks.run = saved
        self.assertEqual(self.app.port_b, "eth1")
        self.assertEqual(runs, [["ip", "link", "set", "eth1", "up"]])

    def test_console_taking_the_panel_back_redraws_it_all(self):
        bands = []

        class Recorder(Screen):
            def show(self, img, band=None):
                bands.append(band)

        class Console:
            calls = 0

            def keep(self):
                Console.calls += 1
                return Console.calls == 2  # the prompt came back once

        self.app.out, self.app.console, self.app.port_b = Recorder(), Console(), None
        self.app.idle(0.05, tick=0.01)
        self.assertIn(None, bands)  # the whole screen, not only the cat's corner
        self.assertEqual(bands[0], display.CAT_BAND)
        self.assertEqual(bands[1], None)

    def test_console_keep_puts_graphics_mode_back(self):
        import os
        calls, written = [], []
        con = display.ConsoleOff.__new__(display.ConsoleOff)
        con.fd, con.blink = 99, None

        def ioctl(fd, req, arg):
            calls.append((req, arg if isinstance(arg, int) else None))
            if req == display.KDGETMODE:
                arg[:] = state["mode"].to_bytes(4, "little")

        state = {"mode": display.KD_TEXT}
        saved = (display.fcntl.ioctl, os.write)
        display.fcntl.ioctl, os.write = ioctl, lambda fd, data: written.append(data)
        try:
            self.assertTrue(con.keep())
            self.assertIn((display.KDSETMODE, display.KD_GRAPHICS), calls)
            self.assertEqual(written, [b"\033[?25l"])
            state["mode"] = display.KD_GRAPHICS
            calls.clear()
            self.assertFalse(con.keep())
            self.assertEqual([c[0] for c in calls], [display.KDGETMODE])
        finally:
            display.fcntl.ioctl, os.write = saved



class WifiMode(unittest.TestCase):
    """The NO LINK button, the hotspot and the web UI, with both faked."""

    def setUp(self):
        from flugato import web
        events = self.events = []

        class FakeHotspot:
            on = False

            def available(self):
                return True

            def start(self):
                events.append("hotspot start")
                FakeHotspot.on = True
                return {"ssid": "fluGATO", "password": "kepvazum", "address": "192.0.2.1",
                        "url": "http://192.0.2.1"}

            def stop(self):
                events.append("hotspot stop")
                FakeHotspot.on = False

            def join(self, ssid, stop_web=None, start_web=None):
                events.append(f"join {ssid}")
                stop_web()
                FakeHotspot.on = False
                return True

            def status(self):
                return {"on": FakeHotspot.on, "ssid": "fluGATO", "password": "kepvazum",
                        "url": "http://192.0.2.1", "clients": 0}

        class FakeWeb:
            def __init__(self, hotspot, on_saved, live_keys, on_join=None, on_restart=None,
                         on_power=None, pending=None, updater=None):
                self.on_saved, self.on_join, self.on_restart = on_saved, on_join, on_restart
                self.on_power = on_power

            def start(self, address):
                events.append(f"web start {address}")

            def stop(self):
                events.append("web stop")

            def publish(self, mode, rows=(), title=None):
                events.append(f"publish {mode} {title}")

        self.saved_web = web.WebUI
        web.WebUI = FakeWeb
        cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
               "TOUCH_CAL": ""}
        self.app = App(cfg, Screen(), "lo")
        self.app.wifi, self.app.port_b = FakeHotspot(), None

    def tearDown(self):
        from flugato import display, web
        web.WebUI = self.saved_web
        display.wifi_mark = False

    def wait(self):
        import time
        for _ in range(200):
            if not self.app.wifi_busy:
                return
            time.sleep(0.01)

    def test_button_turns_the_hotspot_and_web_ui_on_then_off(self):
        self.app.toggle_wifi()
        self.wait()
        self.assertEqual(self.events, ["hotspot start", "web start 192.0.2.1"])
        self.assertTrue(self.app.wifi_status()["on"])
        from flugato import display
        self.assertTrue(display.wifi_mark)  # the top bar shows it on every screen
        self.events.clear()
        self.app.toggle_wifi()
        self.wait()
        self.assertEqual(self.events, ["web stop", "hotspot stop"])  # the web UI goes first
        self.app.wifi_status()
        self.assertFalse(display.wifi_mark)

    def test_tap_on_the_button_on_no_link_toggles_and_shows_the_wifi_screen(self):
        from flugato import checks, display
        import flugato.__main__ as m
        calls = {"n": 0}

        def carrier(iface):
            calls["n"] += 1
            return calls["n"] > 12 and not self.app.wifi_busy

        x0, y0, x1, y1 = display.WIFI_BTN
        self.app.on_tap(((x0 + x1) // 2, (y0 + y1) // 2))
        saved = (checks.carrier, m.time.sleep)
        checks.carrier, m.time.sleep = carrier, lambda s: self.wait()
        try:
            self.app.no_link()
        finally:
            checks.carrier, m.time.sleep = saved
        self.assertIn("hotspot start", self.events)
        self.assertIn("publish nolink NO LINK", self.events)

    def test_connect_on_the_web_ui_joins_and_stops_the_web_ui(self):
        self.app.toggle_wifi()
        self.wait()
        self.events.clear()
        self.app.web.on_join("lab-wifi.example")  # what the Wi-Fi page calls on Connect
        import time
        for _ in range(200):
            if "web stop" in self.events:
                break
            time.sleep(0.01)
        self.assertEqual(self.events[:2], ["join lab-wifi.example", "web stop"])
        self.assertIsNone(self.app.web)  # hotspot only: no web UI on the joined network

    def test_a_save_that_restarts_brings_the_hotspot_back(self):
        import os
        import signal
        import tempfile
        import flugato.__main__ as m
        self.app.toggle_wifi()
        self.wait()
        flag = os.path.join(tempfile.mkdtemp(), "resume")
        kills = []
        saved = (m.RESUME_HOTSPOT, m.os.kill)
        m.RESUME_HOTSPOT, m.os.kill = flag, lambda pid, sig: kills.append(sig)
        try:
            self.app.web.on_restart()  # what a Save that needs a restart calls
        finally:
            m.RESUME_HOTSPOT, m.os.kill = saved
        self.assertEqual(kills, [signal.SIGTERM])  # systemd restarts the app, cleanups run
        self.assertTrue(os.path.exists(flag))      # and the next start resumes the hotspot

    def test_taps_elsewhere_only_poke_the_cat(self):
        from flugato import display
        self.assertFalse(display.wifi_hit((60, 160)))  # the big cat
        self.assertFalse(display.wifi_hit(None))
        x0, y0, x1, y1 = display.WIFI_BTN
        self.assertTrue(display.wifi_hit((x0 - 10, y1)))  # a little off the edge still counts

    def test_saved_settings_apply_at_once(self):
        cfg = dict(self.app.cfg, CHECK_HOST="host.example", LLDP_WAIT="45", PROBE_VLANS="70, 300")
        self.app.settings_saved(cfg)
        self.assertEqual((self.app.check_host, self.app.lldp_wait, self.app.extra_vlans),
                         ("host.example", 45, {70, 300}))

if __name__ == "__main__":
    unittest.main()

"""Power off: the button on NO LINK, the question, the clean shutdown, the screens on the way
out, and Power off on the web UI (asked twice, behind the PIN on a joined network)."""

import http.client
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse

from flugato import checks, display, web
from flugato.__main__ import App


class Screen:
    def __init__(self):
        self.shown = []

    def show(self, img, band=None):
        self.shown.append(img)


CFG = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo", "TOUCH_CAL": ""}


def centre(box):
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


class Targets(unittest.TestCase):
    def test_the_power_button_is_its_own_target(self):
        self.assertTrue(display.power_hit(centre(display.POWER_BTN)))
        self.assertFalse(display.power_hit(centre(display.WIFI_BTN)))
        self.assertFalse(display.wifi_hit(centre(display.POWER_BTN)))
        cat = (display.BIG_CAT_X + 40, display.BIG_CAT_Y + 40)
        self.assertTrue(display.big_cat_hit(cat))
        self.assertFalse(display.power_hit(cat))
        self.assertFalse(display.power_hit(None))

    def test_only_power_off_turns_it_off(self):
        self.assertTrue(display.power_off_hit(centre(display.POWER_OFF_BTN)))
        self.assertFalse(display.power_off_hit(centre(display.POWER_CANCEL_BTN)))
        self.assertFalse(display.power_off_hit((60, 120)))  # on the cat
        self.assertFalse(display.power_off_hit(None))

    def test_the_screens_draw(self):
        for img in (display.render_power_ask("eth0", 10), display.render_power_off("eth0"),
                    display.render_power_off("eth0", reboot=True),
                    display.render_message("NO LINK", "plug in a cable", "eth0", mood="sleep",
                                           pose="loaf", scene="closet", power=True)):
            self.assertEqual(img.size, (display.W, display.H))


class Shutdown(unittest.TestCase):
    def test_parse_shutdown(self):
        self.assertIsNone(checks.parse_shutdown(""))
        self.assertIsNone(checks.parse_shutdown(None))
        self.assertIsNone(checks.parse_shutdown("88 flugato.service restart running\n"))
        jobs = ("1201 poweroff.target start waiting\n"
                "1210 flugato.service stop running\n")
        self.assertEqual(checks.parse_shutdown(jobs), "poweroff")
        self.assertEqual(checks.parse_shutdown("7 reboot.target start waiting\n"), "reboot")
        self.assertEqual(checks.parse_shutdown("7 halt.target start waiting\n"), "poweroff")


class NoLinkPower(unittest.TestCase):
    """The NO LINK loop with the carrier and the commands faked."""

    def setUp(self):
        import flugato.__main__ as m
        self.m = m
        self.screen = Screen()
        self.app = App(dict(CFG), self.screen, "lo")
        self.app.wifi, self.app.port_b = None, None
        self.app.cfg["DESK_MINUTES"] = "0"
        self.ran = []
        self.saved = (checks.carrier, checks.run, m.time.sleep, display.render_power_ask,
                      display.render_power_off)
        self.asked, self.off = [], []
        real_ask, real_off = display.render_power_ask, display.render_power_off

        def ask(*a, **k):
            self.asked.append(a)
            return real_ask(*a, **k)

        def off(*a, **k):
            self.off.append(k)
            return real_off(*a, **k)

        display.render_power_ask, display.render_power_off = ask, off
        checks.run = lambda cmd, timeout=10: self.ran.append(cmd) or ""
        m.time.sleep = lambda s: None

    def tearDown(self):
        (checks.carrier, checks.run, self.m.time.sleep, display.render_power_ask,
         display.render_power_off) = self.saved

    def run_ticks(self, n, before=None):
        count = {"n": 0}

        def carrier(iface):
            count["n"] += 1
            if before:
                before(count["n"])
            return count["n"] > n

        checks.carrier = carrier
        self.app.no_link()

    def test_power_off_asks_then_turns_off(self):
        self.app.on_tap(centre(display.POWER_BTN))

        def answer(n):
            if n == 4:  # the question is up: tap Power off
                self.app.on_tap(centre(display.POWER_OFF_BTN))
        self.run_ticks(8, answer)
        self.assertTrue(self.asked)
        self.assertIn(["systemctl", "poweroff"], self.ran)
        self.assertEqual(self.off, [{}])  # Turning off… on the panel first

    def test_any_other_tap_cancels(self):
        self.app.on_tap(centre(display.POWER_BTN))

        def answer(n):
            if n == 4:
                self.app.on_tap(centre(display.POWER_CANCEL_BTN))
        self.run_ticks(8, answer)
        self.assertTrue(self.asked)
        self.assertNotIn(["systemctl", "poweroff"], self.ran)
        self.assertEqual(self.off, [])

    def test_the_question_cancels_itself(self):
        clock = {"t": 1000.0}
        saved = self.m.time.monotonic
        self.m.time.monotonic = lambda: clock["t"]
        self.app.wake.wait = lambda t=None: clock.__setitem__("t", clock["t"] + 1)
        try:
            self.app.on_tap(centre(display.POWER_BTN))
            self.run_ticks(40)
        finally:
            self.m.time.monotonic = saved
        self.assertEqual([a[1] for a in self.asked][:3], [10, 9, 8])  # the countdown
        self.assertNotIn(["systemctl", "poweroff"], self.ran)

    def test_a_cable_cancels_the_question(self):
        self.app.on_tap(centre(display.POWER_BTN))
        self.run_ticks(3)  # the link comes up while it asks
        self.assertNotIn(["systemctl", "poweroff"], self.ran)

    def test_no_power_button_in_desk_mode(self):
        self.app.cfg["DESK_MINUTES"] = "3"
        self.app.desk_mode = lambda quiet: "clock"
        self.app.on_tap(centre(display.POWER_BTN))
        self.run_ticks(6)
        self.assertEqual(self.asked, [])  # the touch only ends desk mode

    def test_a_failed_poweroff_comes_back(self):
        checks.run = lambda cmd, timeout=10: self.ran.append(cmd) and None
        self.app.on_tap(centre(display.POWER_BTN))

        def answer(n):
            if n == 4:
                self.app.on_tap(centre(display.POWER_OFF_BTN))
        self.run_ticks(8, answer)  # returns: NO LINK goes on
        self.assertIn(["systemctl", "poweroff"], self.ran)


class Leaving(unittest.TestCase):
    def setUp(self):
        self.saved = checks.shutting_down
        self.screen = Screen()
        self.app = App(dict(CFG), self.screen, "lo")

        class Console:
            released = False

            def release(self):
                self.released = True
        self.app.console = Console()

    def tearDown(self):
        checks.shutting_down = self.saved

    def test_a_power_off_shows_turning_off_and_keeps_the_console_quiet(self):
        checks.shutting_down = lambda: "poweroff"
        self.app.leaving()
        self.assertEqual(len(self.screen.shown), 1)
        self.assertTrue(self.app.console.released)

    def test_a_plain_stop_leaves_the_screen_alone(self):
        checks.shutting_down = lambda: None
        self.app.leaving()
        self.assertEqual(self.screen.shown, [])
        self.assertFalse(self.app.console.released)


class WebPower(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("CHECK_HOST=old.example\n")
        self.called = threading.Event()
        self.ui = web.WebUI(self.tmp, self.conf, on_power=self.called.set, pin=lambda: "4321")
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"

    def tearDown(self):
        self.ui.stop()
        shutil.rmtree(self.tmp)

    def request(self, method, path, form=None, cookie=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Host": self.host}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = origin or f"http://{self.host}"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode(errors="replace")
        conn.close()
        return res, data

    def test_power_off_needs_the_pin_and_a_second_yes(self):
        res, _ = self.request("POST", "/poweroff/now", {})
        self.assertEqual(res.status, 303)  # no session: to the PIN page
        self.assertFalse(self.called.wait(1.5))
        res, _ = self.request("POST", "/login", {"pin": "4321"})
        cookie = res.getheader("Set-Cookie").split(";")[0]
        _, page = self.request("GET", "/settings", cookie=cookie)
        self.assertIn('action="/poweroff"', page)
        _, page = self.request("POST", "/poweroff", {}, cookie)
        self.assertIn("Turn off fluGATO?", page)
        self.assertIn('action="/poweroff/now"', page)
        self.assertFalse(self.called.is_set())  # the question alone turns nothing off
        res, _ = self.request("POST", "/poweroff/now", {}, cookie, origin="http://evil.example")
        self.assertEqual(res.status, 403)
        _, page = self.request("POST", "/poweroff/now", {}, cookie)
        self.assertIn("screen goes <b>gray</b>", page)  # the page goes out first
        self.assertTrue(self.called.wait(3))   # then the power-off

    def test_no_callback_no_button(self):
        self.ui.on_power = None
        res, _ = self.request("POST", "/login", {"pin": "4321"})
        cookie = res.getheader("Set-Cookie").split(";")[0]
        _, page = self.request("GET", "/settings", cookie=cookie)
        self.assertNotIn('action="/poweroff"', page)
        _, page = self.request("POST", "/poweroff/now", {}, cookie)
        self.assertNotIn("Turning off", page)

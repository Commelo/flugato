"""The settings page in groups, and the settings that were constants: the hotspot's name,
desk mode's night hours and turn, and how long "Turn off?" waits."""

import http.client
import io
import os
import shutil
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

from flugato import checks, config, display, hotspot, web
from flugato.__main__ import LIVE_KEYS, App
from tests.test_wifi import FakeNM


class Groups(unittest.TestCase):
    def test_every_setting_in_exactly_one_group(self):
        keys = [k for _, _, ks in config.CATEGORIES for k in ks]
        self.assertEqual(sorted(keys), sorted(config.DEFAULTS))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k in config.HELP for k in keys))

    def test_the_new_ones_apply_when_said(self):
        for key in ("NIGHT_HOURS", "DESK_TURN", "POWER_ASK"):
            self.assertIn(key, LIVE_KEYS)
        self.assertIn("HOTSPOT_SSID", config.AT_HOTSPOT_START)


class Validate(unittest.TestCase):
    def test_hotspot_ssid(self):
        self.assertEqual(config.validate("HOTSPOT_SSID", " Lab cat "), ("Lab cat", None))
        self.assertIsNotNone(config.validate("HOTSPOT_SSID", "x" * 33)[1])
        self.assertIsNotNone(config.validate("HOTSPOT_SSID", "café")[1])
        self.assertIsNotNone(config.validate("HOTSPOT_SSID", 'say "hi"')[1])

    def test_panel(self):
        self.assertEqual(config.validate("PANEL", " ILI9486 "), ("ili9486", None))
        self.assertEqual(config.validate("PANEL", "mz61581"), ("mz61581", None))
        self.assertEqual(config.validate("PANEL", ""), ("", None))
        self.assertIsNotNone(config.validate("PANEL", "waveshare35a")[1])

    def test_night_hours(self):
        self.assertEqual(config.validate("NIGHT_HOURS", "22 - 07"), ("22-7", None))
        self.assertEqual(config.validate("NIGHT_HOURS", "OFF"), ("off", None))
        for bad in ("23-23", "24-1", "22", "night"):
            self.assertIsNotNone(config.validate("NIGHT_HOURS", bad)[1], bad)
        self.assertEqual(config.night_hours({"NIGHT_HOURS": "1-5"}), (1, 5))
        self.assertIsNone(config.night_hours({"NIGHT_HOURS": "off"}))
        self.assertEqual(config.night_hours({"NIGHT_HOURS": "bad"}), (22, 7))  # a hand edit
        self.assertEqual([config.is_night((22, 7), h) for h in (21, 22, 0, 6, 7)],
                         [False, True, True, True, False])
        self.assertEqual([config.is_night((1, 5), h) for h in (0, 1, 4, 5, 23)],
                         [False, True, True, False, False])
        self.assertFalse(config.is_night(None, 3))

    def test_seconds(self):
        self.assertEqual(config.validate("DESK_TURN", "090"), ("90", None))
        self.assertIsNotNone(config.validate("DESK_TURN", "5")[1])
        self.assertEqual(config.validate("POWER_ASK", "30"), ("30", None))
        self.assertIsNotNone(config.validate("POWER_ASK", "61")[1])
        self.assertEqual(config.setting({"POWER_ASK": "x"}, "POWER_ASK", 3, 60), 10)
        self.assertEqual(config.setting({"POWER_ASK": "500"}, "POWER_ASK", 3, 60), 60)


class HotspotName(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "class", "net", "wlan0", "wireless"))
        self.name = "Lab cat"
        self.nm = FakeNM()
        self.hs = hotspot.Hotspot(
            run=self.nm, sys_root=self.tmp, leases=os.path.join(self.tmp, "leases-{iface}"),
            load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0",
                                     HOTSPOT_SSID=self.name),
            state_file=os.path.join(self.tmp, "hs.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_the_hotspot_takes_its_name_at_start(self):
        self.assertEqual(self.hs.status()["ssid"], "Lab cat")  # off: the name it will have
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            info = self.hs.start()
        self.assertEqual(info["ssid"], "Lab cat")
        add = next(c for c in self.nm.cmds if c[:3] == ["nmcli", "connection", "add"])
        self.assertEqual(add[add.index("ssid") + 1], "Lab cat")

    def test_a_bad_name_falls_back_to_fluGATO(self):
        self.name = "x" * 40  # a hand edit
        self.assertEqual(self.hs.ssid(), "fluGATO")


class Page(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("CHECK_HOST=old.example\n")
        self.ui = web.WebUI(self.tmp, self.conf, live_keys=LIVE_KEYS)
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"

    def tearDown(self):
        self.ui.stop()
        shutil.rmtree(self.tmp)

    def request(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Host": self.host}
        if body is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded",
                            "Origin": f"http://{self.host}"})
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode()
        conn.close()
        return res, data

    def test_folded_groups_one_save(self):
        _, page = self.request("GET", "/settings")
        self.assertEqual(page.count('<details class="group">'), len(config.CATEGORIES))  # all folded
        self.assertEqual(page.count('type="submit">Save</button>'), 1)
        for key in config.DEFAULTS:
            self.assertEqual(page.count(f'name="{key}"'), 1, key)
        for title, _, _ in config.CATEGORIES:
            self.assertIn(f"<b>{title}</b>", page)

    def test_a_bad_field_opens_its_group_and_saves_the_new_ones(self):
        _, page = self.request("POST", "/settings", {"NIGHT_HOURS": "late"})
        self.assertEqual(page.count('<details class="group" open>'), 1)
        self.assertIn("from-to hours", page)
        _, page = self.request("POST", "/settings", {"NIGHT_HOURS": "23-6", "DESK_TURN": "30",
                                                     "POWER_ASK": "20", "HOTSPOT_SSID": "Lab cat"})
        cfg = config.load(self.conf)
        self.assertEqual((cfg["NIGHT_HOURS"], cfg["DESK_TURN"], cfg["POWER_ASK"], cfg["HOTSPOT_SSID"]),
                         ("23-6", "30", "20", "Lab cat"))
        self.assertIn("HOTSPOT_SSID: at the next hotspot start", page)


class PowerAsk(unittest.TestCase):
    def test_the_question_waits_power_ask_seconds(self):
        import flugato.__main__ as m
        screen = type("Screen", (), {"show": lambda self, img, band=None: None})()
        app = App(dict(config.DEFAULTS, IFACE="lo", POWER_ASK="4"), screen, "lo")
        app.wifi, app.port_b = None, None
        asked, clock = [], {"t": 1000.0}
        saved = checks.carrier, m.time.monotonic, display.render_power_ask
        checks.carrier = lambda iface: False
        m.time.monotonic = lambda: clock["t"]
        display.render_power_ask = lambda iface, left: asked.append(left)
        app.wake.wait = lambda t=None: clock.__setitem__("t", clock["t"] + 1)
        try:
            self.assertFalse(app.ask_power_off())
        finally:
            checks.carrier, m.time.monotonic, display.render_power_ask = saved
        self.assertEqual(asked, [4, 3, 2, 1])


class RestartOnTheWifiNetwork(unittest.TestCase):
    """Restart from the web UI on a joined network (no hotspot)."""

    def test_back_in_seconds_with_the_pin_again(self):
        import threading
        tmp = tempfile.mkdtemp()
        conf = os.path.join(tmp, "flugato.conf")
        open(conf, "w").close()
        called = threading.Event()
        pending = set()
        ui = web.WebUI(tmp, conf, live_keys=LIVE_KEYS, on_restart=called.set, pin="4321",
                       pending=pending)
        try:
            page = ui.save_settings({"IFACE": "eth0"})
            self.assertIn("Saved <b>IFACE</b>: it applies after a restart.", page)
            self.assertEqual(pending, {"IFACE"})  # shared with the app's other web UI
            self.assertFalse(called.wait(1.5))
            page = ui.restart()
            self.assertIn(f'content="{web.RESTART_RELOAD_LAN};url=/settings"', page)
            self.assertIn("the PIN again", page)
            self.assertNotIn("hotspot goes off", page)
            self.assertTrue(called.wait(3))
        finally:
            shutil.rmtree(tmp)

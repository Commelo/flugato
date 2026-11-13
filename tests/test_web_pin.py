"""The web UI on a joined Wi-Fi network: the PIN, the session, the lock-out; and the app
running it there (the hotspot's needs no PIN)."""

import http.client
import os
import shutil
import tempfile
import unittest
import urllib.parse

from flugato import web


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Pin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("CHECK_HOST=old.example\n")
        os.makedirs(os.path.join(self.tmp, "proc"))
        with open(os.path.join(self.tmp, "proc", "mounts"), "w") as f:
            f.write("overlayroot / overlay rw 0 0\n")
        self.pin = "4321"
        self.clock = Clock()
        self.ui = web.WebUI(self.tmp, self.conf, proc_root=os.path.join(self.tmp, "proc"),
                            pin=lambda: self.pin, clock=self.clock)
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"

    def tearDown(self):
        self.ui.stop()
        shutil.rmtree(self.tmp)

    def request(self, method, path, form=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Host": self.host}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = f"http://{self.host}"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode(errors="replace")
        conn.close()
        return res, data

    def sign_in(self, pin=None):
        res, _ = self.request("POST", "/login", {"pin": pin or self.pin})
        self.assertEqual(res.status, 303)
        return res.getheader("Set-Cookie").split(";")[0]

    def test_every_page_asks_for_the_pin_first(self):
        for path in ("/", "/settings", "/wifi", "/captures", "/captures/x.pcap"):
            res, _ = self.request("GET", path)
            self.assertEqual((res.status, res.getheader("Location")), (303, "/login"), path)
        self.pin = web.DEFAULT_PIN
        res, page = self.request("GET", "/login")
        self.assertIn('name="pin"', page)
        self.assertNotIn("1234", page)  # the default-PIN warning only after sign-in
        self.assertNotIn("<nav>", page)
        res, _ = self.request("GET", "/cat/icon.png")
        self.assertEqual(res.status, 200)  # the cats are no secret

    def test_a_right_pin_opens_the_pages_for_that_browser(self):
        res, page = self.request("POST", "/login", {"pin": "0000"})
        self.assertIn("Wrong PIN", page)
        cookie = self.sign_in()
        self.assertIn("HttpOnly", self.request("POST", "/login", {"pin": self.pin})[0].getheader("Set-Cookie"))
        res, page = self.request("GET", "/", cookie=cookie)
        self.assertEqual(res.status, 200)
        self.assertNotIn("still 1234", page)
        res, _ = self.request("GET", "/", cookie="flugato_session=made-up")
        self.assertEqual(res.status, 303)
        self.clock.now += web.SESSION_AGE + 1
        res, _ = self.request("GET", "/", cookie=cookie)
        self.assertEqual(res.status, 303)  # 12 hours later: the PIN again

    def test_forms_need_a_session_too(self):
        res, _ = self.request("POST", "/settings", {"CHECK_HOST": "evil.example"})
        self.assertEqual((res.status, res.getheader("Location")), (303, "/login"))
        with open(self.conf) as f:
            self.assertIn("old.example", f.read())

    def test_wrong_pins_lock_the_address_out(self):
        for _ in range(web.LOCK_AFTER):
            self.request("POST", "/login", {"pin": "0000"})
        res, page = self.request("POST", "/login", {"pin": self.pin})
        self.assertIn("Too many wrong PINs", page)  # even the right one, while locked
        self.assertIsNone(res.getheader("Set-Cookie"))
        self.clock.now += web.LOCK_FIRST + 1
        self.sign_in()
        for _ in range(web.LOCK_AFTER + 1):  # locked again: twice as long this time
            self.clock.now += 1000
            self.ui.try_pin("192.0.2.9", "0000")
        self.assertGreater(self.ui.locked_for("192.0.2.9"), web.LOCK_FIRST)

    def test_a_burst_at_once_gets_no_more_tries(self):
        import threading
        results = []
        barrier = threading.Barrier(40)

        def guess(i):
            barrier.wait()
            results.append(self.ui.try_pin("192.0.2.50", f"{i:04d}" if i else "0000"))

        threads = [threading.Thread(target=guess, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.ui._fails["192.0.2.50"][0], web.LOCK_AFTER)  # 5 counted, then locked

    def test_a_new_pin_closes_the_old_sessions(self):
        cookie = self.sign_in()
        self.pin = "8642"
        res, _ = self.request("GET", "/", cookie=cookie)
        self.assertEqual((res.status, res.getheader("Location")), (303, "/login"))
        cookie = self.sign_in()
        self.assertEqual(self.request("GET", "/", cookie=cookie)[0].status, 200)

    def test_many_addresses_together_are_capped(self):
        for i in range(web.GLOBAL_TRIES):
            self.ui.try_pin(f"192.0.2.{i}", "0000")
        self.assertGreater(self.ui.locked_for("198.51.100.1"), 0)
        self.assertIsNone(self.ui.try_pin("198.51.100.1", self.pin))
        self.clock.now += web.GLOBAL_WINDOW + 1
        self.assertIsNotNone(self.ui.try_pin("198.51.100.1", self.pin))

    def test_the_default_pin_is_flagged_and_a_new_one_applies_at_once(self):
        self.pin = web.DEFAULT_PIN
        cookie = self.sign_in()
        _, page = self.request("GET", "/", cookie=cookie)
        self.assertIn("The PIN is still 1234", page)
        self.pin = "98765"
        res, _ = self.request("POST", "/login", {"pin": web.DEFAULT_PIN})
        self.assertIsNone(res.getheader("Set-Cookie"))

    def test_a_dropped_connection_is_quiet(self):
        import contextlib
        import io
        import socket
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            sock = socket.create_connection(("127.0.0.1", self.ui.port))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            sock.close()  # a reset, before any request line
            self.assertEqual(self.request("GET", "/login")[0].status, 200)
            self.ui.server.handle_error(None, ("192.0.2.7", 1))  # no exception active: quiet too
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("127.0.0.1", out.getvalue())

    def test_the_hotspot_ui_needs_no_pin(self):
        ui = web.WebUI(self.tmp, self.conf, proc_root=os.path.join(self.tmp, "proc"))
        self.assertIsNone(ui.pin())
        self.assertTrue(ui.session_ok(None))
        self.assertEqual(ui.default_pin_note(), "")
        ui = web.WebUI(self.tmp, self.conf, pin="off")
        self.assertIsNone(ui.pin())


class AppKeepsIt(unittest.TestCase):
    def test_started_on_the_joined_network_moved_and_stopped(self):
        from flugato import hotspot
        from flugato.__main__ import App

        class Screen:
            def show(self, img, band=None):
                pass

        class Radio:
            on = False

            def available(self):
                return True

            def status(self):
                return {"on": Radio.on}

        cfg = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo",
               "TOUCH_CAL": ""}
        app = App(cfg, Screen(), "lo")
        app.wifi, app.lan_port = Radio(), 0
        seen = {"client": {"ssid": "lab-wifi", "address": "127.0.0.1"}}
        saved = hotspot.client
        hotspot.client = lambda: seen["client"]
        try:
            app.keep_lan_web()
            self.assertEqual((app.web_lan.address, app.web_lan.pin()), ("127.0.0.1", "1234"))
            app.cfg["WEB_PIN"] = "2468"
            self.assertEqual(app.web_lan.pin(), "2468")  # read live from the settings
            Radio.on = True  # the hotspot: the joined-network UI goes
            app.keep_lan_web()
            self.assertIsNone(app.web_lan)
            Radio.on = False
            app.cfg["WEB_PIN"] = "off"
            app.keep_lan_web()
            self.assertIsNone(app.web_lan)
            app.cfg["WEB_PIN"] = "1234"
            seen["client"] = None  # off the network
            app.keep_lan_web()
            self.assertIsNone(app.web_lan)
        finally:
            hotspot.client = saved
            app.stop_lan_web()


if __name__ == "__main__":
    unittest.main()

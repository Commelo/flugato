"""Saved Wi-Fi networks and joining one from the hotspot, against a fake NetworkManager;
the Wi-Fi page of the web UI on loopback. Documentation addresses and .example names."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
import io

from flugato import config, hotspot, networks, web

HOME = "preconfigured"


def write(path, text, mode=0o644):
    with open(path, "w") as f:
        f.write(text)


class FakeNM:
    """nmcli and ip for the hotspot and the saved networks. `joinable` maps an SSID to the
    nmcli error it fails with (None: joins)."""

    def __init__(self):
        self.cmds, self.profiles, self.active = [], [HOME], HOME
        self.joinable, self.hotspot_fails, self.state = {}, False, "100 (connected)"
        self.psk = {}

    def __call__(self, cmd, timeout=10):
        self.cmds.append(list(cmd))
        if cmd[:5] == ["nmcli", "-t", "-f", "NAME", "connection"]:
            return "\n".join(self.profiles) + "\n"
        if cmd[:3] == ["nmcli", "-g", "GENERAL.CON-UUID"]:
            return ("uuid-home" if self.active == HOME else "") + "\n"
        if cmd[:3] == ["nmcli", "-g", "GENERAL.CONNECTION"]:
            return self.active + "\n"
        if cmd[:3] == ["nmcli", "-g", "GENERAL.STATE"]:
            return self.state + "\n"
        if cmd[:3] == ["nmcli", "connection", "add"]:
            name = cmd[cmd.index("con-name") + 1]
            if name not in self.profiles:
                self.profiles.append(name)
            if "wifi-sec.psk" in cmd:
                self.psk[name] = cmd[cmd.index("wifi-sec.psk") + 1]
            return ""
        if cmd[:3] == ["nmcli", "connection", "delete"]:
            if cmd[3] not in self.profiles:
                return None
            self.profiles.remove(cmd[3])
            if self.active == cmd[3]:
                self.active = ""
            return ""
        if cmd[1:3] == ["--wait", "30"] and cmd[-1] == hotspot.PROFILE:
            if self.hotspot_fails:
                return None
            self.active = hotspot.PROFILE
            return ""
        if cmd[-3:] == ["up", "uuid", "uuid-home"] or cmd[-2:] == ["connect", "wlan0"]:
            self.active = HOME
            return ""
        if cmd[:3] == ["ip", "-j", "-4"]:
            addr = {hotspot.PROFILE: "192.0.2.1", HOME: "198.51.100.20"}.get(self.active)
            if self.active.startswith(networks.PREFIX):
                addr = "203.0.113.45"
            return json.dumps([{"addr_info": [{"family": "inet", "local": addr, "prefixlen": 24}]}]
                              if addr else [])
        return ""

    def run_err(self, cmd, timeout=10):
        self.cmds.append(list(cmd))
        name = cmd[-1]
        ssid = next((s for s in self.joinable if networks.profile(s) == name), None)
        if name not in self.profiles or ssid is None:
            return False, "Error: Connection activation failed: The Wi-Fi network could not be found"
        if self.joinable[ssid]:
            return False, self.joinable[ssid]
        self.active = name
        return True, ""


class Saved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.file = os.path.join(self.tmp, "flugato-wifi.json")
        self.nm = FakeNM()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save(self, ssid, password):
        return networks.add(ssid, password, self.nm, self.file, write)

    def test_validate(self):
        self.assertIsNone(networks.validate("office.example", "eightchr"))
        self.assertIsNone(networks.validate("café 5G", ""))                 # open network
        self.assertIsNone(networks.validate("x" * 32, "p" * 63))
        for ssid, password in (("", "eightchr"), ("x" * 33, ""), ("é" * 17, ""), ("a\nb", ""),
                               ("ok", "short"), ("ok", "p" * 64), ("ok", "pässwörd"), ("ok", "tab\there!")):
            self.assertTrue(networks.validate(ssid, password), (ssid, password))

    def test_add_replace_forget(self):
        self.save("office.example", "first-pass")
        self.save("guest.example", "")
        self.save("office.example", "second-pass")                  # replaces, no duplicate
        saved = networks.load(self.file)
        self.assertEqual([n["ssid"] for n in saved], ["guest.example", "office.example"])
        self.assertEqual(networks.find("office.example", self.file)["password"], "second-pass")
        self.assertEqual(self.nm.psk[networks.profile("office.example")], "second-pass")
        add = [c for c in self.nm.cmds if c[:3] == ["nmcli", "connection", "add"]][-1]
        pairs = dict(zip(add[3::2], add[4::2]))
        self.assertEqual((pairs["save"], pairs["802-11-wireless.hidden"], pairs["ssid"],
                          pairs["autoconnect"]), ("no", "yes", "office.example", "no"))
        open_add = next(c for c in self.nm.cmds if "guest.example" in c)
        self.assertNotIn("wifi-sec.psk", open_add)
        networks.forget("office.example", self.nm, self.file, write)
        self.assertEqual([n["ssid"] for n in networks.load(self.file)], ["guest.example"])
        self.assertNotIn(networks.profile("office.example"), self.nm.profiles)
        self.assertIn(HOME, self.nm.profiles)                           # never touched

    def test_the_network_the_pi_is_on_cannot_be_forgotten_or_replaced(self):
        self.save("office.example", "first-pass")
        self.nm.active = networks.profile("office.example")
        with self.assertRaisesRegex(ValueError, "on this network now"):
            networks.forget("office.example", self.nm, self.file, write)
        with self.assertRaisesRegex(ValueError, "on this network now"):
            self.save("office.example", "second-pass")
        self.assertIn(networks.profile("office.example"), self.nm.profiles)
        self.assertEqual(networks.find("office.example", self.file)["password"], "first-pass")

    def test_a_file_edited_on_a_pc(self):
        with open(self.file, "w", encoding="utf-8-sig") as f:          # with a BOM
            f.write(json.dumps([{"ssid": "office.example", "password": "first-pass"}]))
        self.assertEqual(len(networks.load(self.file)), 1)
        self.save("guest.example", "")
        self.assertEqual(len(networks.load(self.file)), 2)              # nothing lost

    def test_a_save_never_drops_what_it_could_not_read(self):
        text = json.dumps([{"ssid": "ok.example", "password": ""}, {"ssid": 5}])
        write(self.file, text)
        with self.assertRaisesRegex(ValueError, "can't read"):
            self.save("guest.example", "")
        with self.assertRaisesRegex(ValueError, "can't read"):
            networks.forget("ok.example", self.nm, self.file, write)
        with open(self.file) as f:
            self.assertEqual(f.read(), text)
        write(self.file, '[{"ssid": "\\ud83d", "password": ""}]')   # a lone surrogate
        self.assertEqual(networks.load(self.file), [])

    def test_bad_input_and_a_full_list(self):
        with self.assertRaises(ValueError):
            self.save("office.example", "short")
        for i in range(networks.MAX_SAVED):
            self.save(f"net{i}.example", "")
        with self.assertRaisesRegex(ValueError, "at most"):
            self.save("one-more.example", "")

    def test_a_damaged_file_loads_what_it_can(self):
        write(self.file, json.dumps([{"ssid": "ok.example", "password": ""}, {"ssid": 5},
                                     {"ssid": "x", "password": "short"}, "junk"]))
        self.assertEqual(networks.load(self.file), [{"ssid": "ok.example", "password": ""}])
        write(self.file, "not json")
        self.assertEqual(networks.load(self.file), [])

    def test_sync_at_start(self):
        self.save("office.example", "first-pass")
        self.save("guest.example", "")
        self.nm.profiles.append(networks.PREFIX + "deadbeef")         # forgotten since
        self.nm.active = networks.profile("office.example")           # the Pi is on it now
        self.nm.cmds.clear()
        self.assertEqual(networks.sync(self.nm, self.file), ["office.example", "guest.example"])
        self.assertNotIn(networks.PREFIX + "deadbeef", self.nm.profiles)
        # the network the Pi is on is left alone, so the link doesn't drop
        self.assertNotIn(["nmcli", "connection", "delete", networks.profile("office.example")], self.nm.cmds)
        self.assertIn(HOME, self.nm.profiles)

    def test_file_sits_next_to_the_settings(self):
        boot = os.path.join(self.tmp, "boot", "flugato.conf")
        os.makedirs(os.path.dirname(boot))
        write(boot, "")
        old = config.BOOT_PATHS
        config.BOOT_PATHS = (boot,)
        try:
            self.assertEqual(networks.path(), os.path.join(self.tmp, "boot", "flugato-wifi.json"))
        finally:
            config.BOOT_PATHS = old


class Join(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "class", "net", "wlan0", "wireless"))
        self.file = os.path.join(self.tmp, "flugato-wifi.json")
        self.nm = FakeNM()
        self.hs = hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=os.path.join(self.tmp, "l"),
                                  load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0"),
                                  state_file=os.path.join(self.tmp, "hs.json"),
                                  run_err=self.nm.run_err)
        networks.add("office.example", "office-pass", self.nm, self.file, write)
        self.web = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def join(self, ssid="office.example"):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            ok = self.hs.join(ssid, stop_web=lambda: self.web.append("stop"),
                              start_web=lambda address: self.web.append(address),
                              networks_file=self.file)
        self.assertNotIn("office-pass", out.getvalue())
        return ok

    def test_joins_and_the_web_ui_stays_on_the_hotspot_only(self):
        info = self.hs.start()
        self.nm.joinable["office.example"] = None
        self.assertTrue(self.join())
        st = self.hs.status()
        self.assertEqual((st["on"], st["joining"], st["failed"]), (False, None, None))
        self.assertEqual(st["joined"], {"ssid": "office.example", "address": "203.0.113.45"})
        # hotspot only for the web UI: stopped for the join, never started on the
        # joined network's address; the Pi is reachable there by SSH only
        self.assertEqual(self.web, ["stop"])
        self.assertNotIn(hotspot.PROFILE, self.nm.profiles)
        # the hotspot did not hand wlan0 back to the home network on the way
        self.assertNotIn(["nmcli", "--wait", "0", "connection", "up", "uuid", "uuid-home"], self.nm.cmds)
        self.assertTrue(info["password"])
        self.hs.start()                                                # a hand start clears it
        self.assertIsNone(self.hs.status()["joined"])

    def test_wrong_password_brings_the_hotspot_back_with_the_same_password(self):
        info = self.hs.start()
        self.nm.joinable["office.example"] = ("Error: Connection activation failed: Secrets were "
                                              "required, but not provided")
        self.assertFalse(self.join())
        st = self.hs.status()
        self.assertEqual((st["on"], st["password"], st["address"]), (True, info["password"], "192.0.2.1"))
        self.assertEqual(st["failed"], "office.example: wrong password")
        self.assertEqual(st["fallback"], "same password")
        self.assertEqual(self.web, ["stop", "192.0.2.1"])                 # back with the hotspot
        self.hs.stop()  # and a later stop goes home, not to the network that just failed
        self.assertIn(["nmcli", "--wait", "0", "connection", "up", "uuid", "uuid-home"], self.nm.cmds[-2:])
        self.assertEqual(self.nm.active, HOME)

    def test_join_without_a_hotspot_falls_back_with_a_new_password(self):
        self.nm.joinable["office.example"] = "Error: Timeout expired (30 seconds)"
        self.assertFalse(self.join())
        st = self.hs.status()
        self.assertEqual((st["on"], st["fallback"], st["failed"]),
                         (True, "new password", "office.example: no answer in time"))

    def test_hand_toggle_during_a_join_waits(self):
        self.hs.start()
        gate, started = threading.Event(), threading.Event()
        real = self.nm.run_err

        def slow(cmd, timeout=10):
            started.set()
            gate.wait(5)
            return real(cmd, timeout)
        self.hs.run_err = slow
        self.nm.joinable["office.example"] = None
        t = threading.Thread(target=self.join)
        t.start()
        self.assertTrue(started.wait(5))
        with self.assertRaisesRegex(hotspot.HotspotError, "joining"):
            self.hs.start()
        n = len(self.nm.cmds)
        self.hs.stop()
        self.assertEqual(len(self.nm.cmds), n)                          # did nothing
        gate.set()
        t.join(5)
        st = self.hs.status()
        self.assertEqual((st["on"], st["joined"]["ssid"]), (False, "office.example"))

    def test_network_not_there(self):
        self.hs.start()
        self.assertFalse(self.join())
        self.assertEqual(self.hs.status()["failed"], "office.example: network not found")

    def test_hotspot_failing_too_goes_back_home(self):
        self.hs.start()
        self.nm.hotspot_fails = True
        self.assertFalse(self.join())
        st = self.hs.status()
        self.assertFalse(st["on"])
        self.assertEqual(self.nm.active, HOME)                        # never on nothing
        self.assertEqual(st["fallback"], "home")

    def test_a_bug_in_the_join_still_falls_back(self):
        self.hs.start()

        def broken(cmd, timeout=10):
            raise RuntimeError("boom")
        self.hs.run_err = broken
        self.assertFalse(self.join())
        self.assertTrue(self.hs.status()["on"])
        self.assertIn("error: RuntimeError", self.hs.status()["failed"])

    def test_unsaved_network_changes_nothing(self):
        self.hs.start()
        self.assertFalse(self.join("nowhere.example"))
        self.assertTrue(self.hs.status()["on"])
        self.assertEqual(self.web, [])

    def test_status_while_joining(self):
        self.hs.start()
        gate, started = threading.Event(), threading.Event()
        real = self.nm.run_err

        def slow(cmd, timeout=10):
            started.set()
            gate.wait(5)
            return real(cmd, timeout)
        self.hs.run_err = slow
        self.nm.joinable["office.example"] = None
        t = threading.Thread(target=self.join)
        t.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(self.hs.status()["joining"], "office.example")
        self.assertFalse(self.hs.join("office.example", networks_file=self.file))  # one at a time
        gate.set()
        t.join(5)
        self.assertEqual(self.hs.status()["joined"]["ssid"], "office.example")

    def test_crash_mid_join_cleanup_reconnects(self):
        # the app died after the hotspot went and before the join finished: wlan0 on nothing
        self.nm.active, self.nm.state = "", "30 (disconnected)"
        os_path = hotspot.os.path.isdir
        hotspot.os.path.isdir = lambda p: True
        try:
            self.assertEqual(hotspot.cleanup(run=self.nm), ["reconnected wlan0"])
            self.assertEqual(self.nm.active, HOME)
            self.nm.state = "100 (connected)"
            self.assertEqual(hotspot.cleanup(run=self.nm), [])         # healthy: nothing to do
        finally:
            hotspot.os.path.isdir = os_path

    def test_join_reasons(self):
        cases = {"Error: Connection activation failed: Secrets were required, but not provided": "wrong password",
                 "Error: Connection activation failed: The Wi-Fi network could not be found": "network not found",
                 "Error: Connection activation failed: IP configuration could not be reserved": "no address from the network (DHCP)",
                 "Error: Timeout expired (30 seconds)": "no answer in time",
                 "": "could not connect"}
        for err, want in cases.items():
            self.assertEqual(hotspot.join_reason(err), want)


class FakeHotspot:
    def __init__(self):
        self.st = {"on": True, "ssid": "fluGATO", "clients": 1, "joining": None, "joined": None,
                   "failed": None, "fallback": None}

    def status(self):
        return dict(self.st)


class WifiPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.file = os.path.join(self.tmp, "flugato-wifi.json")
        self.nm = FakeNM()
        self.joins = []
        self.hs = FakeHotspot()
        self.ui = web.WebUI(os.path.join(self.tmp, "caps"), os.path.join(self.tmp, "f.conf"), self.hs,
                            on_join=self.joins.append, networks_file=self.file, run=self.nm,
                            proc_root=self.tmp)
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"

    def tearDown(self):
        self.ui.stop()
        shutil.rmtree(self.tmp)

    def request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        h = {"Host": self.host}
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read().decode()
        conn.close()
        return res, data

    def test_save_list_forget(self):
        _, page = self.request("GET", "/wifi")
        self.assertIn("No networks saved yet", page)
        self.assertIn('href="/wifi" class=on', page)
        _, page = self.request("POST", "/wifi/add", {"ssid": "<b>office</b>", "password": "office-pass"})
        self.assertIn("Saved &lt;b&gt;office&lt;/b&gt;", page)
        self.assertIn("password saved", page)
        self.assertNotIn("office-pass", page)                         # never shown again
        self.assertEqual(networks.find("<b>office</b>", self.file)["password"], "office-pass")
        _, page = self.request("POST", "/wifi/add", {"ssid": "guest.example", "password": "short"})
        self.assertIn("Not saved", page)
        self.assertNotIn("short", page.split("Not saved")[1].split("</div>")[0].replace("Type the password again", ""))
        self.assertIn('value="guest.example"', page)                  # the name stays typed
        _, page = self.request("POST", "/wifi/forget", {"ssid": "<b>office</b>"})
        self.assertIn("Forgot", page)
        self.assertEqual(networks.load(self.file), [])

    def test_connect_answers_then_hands_over(self):
        self.request("POST", "/wifi/add", {"ssid": "office.example", "password": ""})
        res, page = self.request("POST", "/wifi/connect", {"ssid": "office.example"})
        self.assertEqual(res.status, 200)
        self.assertIn("Joining office.example", page)
        self.assertEqual(self.joins, [])                              # not before the answer
        end = time.monotonic() + 3
        while not self.joins and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(self.joins, ["office.example"])
        _, page = self.request("POST", "/wifi/connect", {"ssid": "nowhere.example"})
        self.assertIn("is not saved", page)

    def test_failed_notes_say_what_came_back(self):
        self.hs.st.update(failed="office.example: wrong password", fallback="same password")
        _, page = self.request("GET", "/wifi")
        self.assertIn("Could not join office.example: wrong password. The hotspot came back, "
                      "with the same password.", page)
        self.hs.st["fallback"] = "new password"
        _, page = self.request("GET", "/wifi")
        self.assertIn("with a new password: it is on the panel", page)

    def test_pages_say_where_the_web_ui_is_after_a_join(self):
        self.request("POST", "/wifi/add", {"ssid": "office.example", "password": ""})
        _, page = self.request("GET", "/wifi")
        self.assertIn("behind the PIN (WEB_PIN)", page)
        _, page = self.request("POST", "/wifi/connect", {"ssid": "office.example"})
        self.assertIn("the PIN is WEB_PIN", page)

    def test_two_connects_start_one_join(self):
        self.request("POST", "/wifi/add", {"ssid": "office.example", "password": ""})
        self.request("POST", "/wifi/connect", {"ssid": "office.example"})
        _, page = self.request("POST", "/wifi/connect", {"ssid": "office.example"})
        self.assertIn("Already joining", page)
        time.sleep(1.5)
        self.assertEqual(self.joins, ["office.example"])

    def test_safety_rules_hold(self):
        res, _ = self.request("POST", "/wifi/add", {"ssid": "x.example", "password": ""},
                              {"Origin": "http://evil.example"})
        self.assertEqual(res.status, 403)
        res, _ = self.request("POST", "/wifi/connect", {"ssid": "x.example"}, {"Host": "evil.example"})
        self.assertEqual(res.status, 400)
        res, page = self.request("GET", "/wifi")
        self.assertNotIn("<script", page)
        self.assertIn("default-src 'none'", res.getheader("Content-Security-Policy"))
        self.assertEqual(networks.load(self.file), [])

    def test_no_connect_button_without_the_app(self):
        self.ui.on_join = None
        self.request("POST", "/wifi/add", {"ssid": "x.example", "password": ""})
        _, page = self.request("GET", "/wifi")
        self.assertNotIn("/wifi/connect", page)


if __name__ == "__main__":
    unittest.main()

"""Wi-Fi mode without a radio: the settings file on the boot partition, the hotspot against
a fake NetworkManager, and the web UI served on loopback. Documentation addresses only."""

import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from flugato import config, hotspot, web


# --- settings file ------------------------------------------------------------------------

class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.boot = os.path.join(self.tmp, "boot", "firmware", "flugato.conf")
        self.etc = os.path.join(self.tmp, "etc", "flugato.conf")
        os.makedirs(os.path.dirname(self.boot))
        os.makedirs(os.path.dirname(self.etc))
        patches = [mock.patch.object(config, "BOOT_PATHS", (self.boot,)),
                   mock.patch.object(config, "ETC_PATH", self.etc)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def test_boot_partition_first_then_etc(self):
        self.assertEqual(config.path(), self.boot)          # nothing yet: made on boot
        self.write(self.etc, "CHECK_HOST=old.example\n")
        self.assertEqual(config.load()["CHECK_HOST"], "old.example")
        self.write(self.boot, "CHECK_HOST=new.example\n")
        self.assertEqual(config.load()["CHECK_HOST"], "new.example")
        # callers that still pass the /etc path get the active file
        self.assertEqual(config.load(self.etc)["CHECK_HOST"], "new.example")
        other = os.path.join(self.tmp, "other.conf")
        self.write(other, "CHECK_HOST=other.example\n")
        self.assertEqual(config.load(other)["CHECK_HOST"], "other.example")
        self.assertEqual(config.load(os.path.join(self.tmp, "none"))["LLDP_WAIT"], "90")

    def test_update_keeps_comments_and_order(self):
        text = "# top\nIFACE=\n# probe\nPROBE_VLANS=60  # mine\nEXTRA=1\n"
        new = config.update_text(text, {"PROBE_VLANS": "60,61", "CHECK_HOST": "a.example"})
        self.assertEqual(new, "# top\nIFACE=\n# probe\nPROBE_VLANS=60,61\nEXTRA=1\nCHECK_HOST=a.example\n")
        self.assertEqual(config.update_text("# IFACE=eth9\n", {"IFACE": "eth0"}),
                         "# IFACE=eth9\nIFACE=eth0\n")

    def test_save_and_read_only_boot(self):
        self.write(self.boot, "# comment\nLLDP_WAIT=90\n")
        self.assertEqual(config.save({"LLDP_WAIT": "60"}), self.boot)
        self.assertEqual(config.load()["LLDP_WAIT"], "60")
        calls, real = [], config._replace

        def read_only_once(target, text, mode=0o644):
            if not calls:
                raise OSError(30, "Read-only file system")
            real(target, text, mode)
        with mock.patch.object(config, "_replace", read_only_once):
            config.save({"LLDP_WAIT": "45"}, remount=lambda mount, mode: calls.append(mode))
        self.assertEqual(calls, ["rw", "ro"])
        self.assertEqual(config.load()["LLDP_WAIT"], "45")
        with open(self.boot) as f:
            self.assertTrue(f.read().startswith("# comment\n"))

    def test_a_key_set_twice_keeps_one_line(self):
        new = config.update_text("CHECK_HOST=a.example\n# x\nCHECK_HOST=b.example\n", {"CHECK_HOST": "c.example"})
        self.assertEqual(new, "CHECK_HOST=c.example\n# x\n")
        self.assertEqual(config.parse(new)["CHECK_HOST"], "c.example")

    def test_unicode_digits_are_refused_not_a_crash(self):
        for key, value in (("LLDP_WAIT", "\u00b2"), ("PROBE_VLANS", "6\u00b2"), ("FB", "/dev/fb\u0663"),
                           ("TOUCH_CAL", "\u0661,2,3,4,5,6")):
            self.assertIsNone(config.validate(key, value)[0], key)

    def test_validate(self):
        good = {"IFACE": "eth0", "CHECK_HOST": "www.example", "LLDP_WAIT": "060", "FB": "/dev/fb1",
                "ROTATE": "90", "TOUCH_CAL": "1, 0.5,-3,1e-3,2,0", "PROBE_VLANS": "60, 61",
                "CABLE_IFACE": "", "UNKNOWN": "x"}
        want = {"LLDP_WAIT": "60", "TOUCH_CAL": "1,0.5,-3,1e-3,2,0", "PROBE_VLANS": "60,61"}
        for key, value in good.items():
            clean, err = config.validate(key, value)
            self.assertIsNone(err, key)
            self.assertEqual(clean, want.get(key, value.strip()))
        bad = {"IFACE": "eth 0", "CHECK_HOST": "bad_host!", "LLDP_WAIT": "2", "FB": "/etc/passwd",
               "ROTATE": "45", "TOUCH_CAL": "1,2,3", "PROBE_VLANS": "0,5000",
               "CABLE_IFACE": "eth1\nIFACE=x", "PROBE_VLANS ": "60#"}
        for key, value in bad.items():
            clean, err = config.validate(key.strip(), value)
            self.assertIsNone(clean, key)
            self.assertTrue(err)


# --- hotspot ------------------------------------------------------------------------------

class FakeNM:
    """nmcli and ip as far as the hotspot uses them."""

    def __init__(self):
        self.cmds, self.profiles, self.active = [], ["preconfigured", "flugato-test"], "preconfigured"
        self.fail_up = False

    def __call__(self, cmd, timeout=10):
        self.cmds.append(list(cmd))
        if cmd[:5] == ["nmcli", "-t", "-f", "NAME", "connection"]:
            return "\n".join(self.profiles) + "\n"
        if cmd[:3] == ["nmcli", "-g", "GENERAL.CON-UUID"]:
            return {"preconfigured": "uuid-home"}.get(self.active, "") + "\n"
        if cmd[:3] == ["nmcli", "connection", "add"]:
            self.profiles.append(cmd[cmd.index("con-name") + 1])
            return ""
        if cmd[1:4] == ["--wait", "30", "connection"] and cmd[-1] == hotspot.PROFILE:
            if self.fail_up:
                return None
            self.active = hotspot.PROFILE
            return ""
        if cmd[:3] == ["nmcli", "connection", "delete"]:
            self.profiles.remove(cmd[3])
            if self.active == cmd[3]:
                self.active = ""
            return ""
        if cmd[-3:] == ["up", "uuid", "uuid-home"] or cmd[-2:] == ["connect", "wlan0"]:
            self.active = "preconfigured"
            return ""
        if cmd[:3] == ["ip", "-j", "-4"]:
            if self.active != hotspot.PROFILE:
                return "[]"
            return json.dumps([{"addr_info": [{"family": "inet", "local": "192.0.2.1", "prefixlen": 24}]}])
        return ""


class Hotspot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "class", "net", "wlan0", "wireless"))
        self.leases = os.path.join(self.tmp, "leases-{iface}")
        self.nm = FakeNM()
        self.hs = hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=self.leases, load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0"), state_file=os.path.join(self.tmp, "hs.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_password(self):
        seen = set()
        for _ in range(200):
            pw = hotspot.new_password()
            self.assertEqual(len(pw), 8)
            self.assertTrue(set(pw) <= set(hotspot.LETTERS))
            seen.add(pw)
        self.assertGreater(len(seen), 195)
        self.assertFalse(set("ilo01IO") & set(hotspot.LETTERS))

    def test_start_status_stop(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            info = self.hs.start()
        self.assertEqual((info["ssid"], info["address"], info["url"]),
                         ("fluGATO", "192.0.2.1", "http://192.0.2.1/"))
        self.assertNotIn(info["password"], out.getvalue() + err.getvalue())  # never logged
        add = next(c for c in self.nm.cmds if c[:3] == ["nmcli", "connection", "add"])
        pairs = dict(zip(add[3::2], add[4::2]))
        self.assertEqual((pairs["save"], pairs["autoconnect"], pairs["802-11-wireless.mode"],
                          pairs["ipv4.method"], pairs["wifi-sec.key-mgmt"], pairs["wifi-sec.psk"],
                          pairs["ssid"]),
                         ("no", "no", "ap", "shared", "wpa-psk", info["password"], "fluGATO"))
        with open(self.leases.format(iface="wlan0"), "w") as f:
            f.write("1760000000 02:00:00:00:00:01 192.0.2.10 phone *\n"
                    "1760000000 02:00:00:00:00:02 192.0.2.11 * *\n")
        st = self.hs.status()
        self.assertEqual((st["on"], st["password"], st["clients"]), (True, info["password"], 2))
        self.assertEqual(self.hs.start(), info)                     # already on: same password
        self.hs.stop()
        self.assertNotIn(hotspot.PROFILE, self.nm.profiles)
        self.assertEqual(self.nm.active, "preconfigured")
        self.assertIn(["nmcli", "--wait", "0", "connection", "up", "uuid", "uuid-home"], self.nm.cmds)
        self.assertEqual(self.hs.status()["on"], False)
        self.assertIsNone(self.hs.status()["password"])
        n = len(self.nm.cmds)
        self.hs.stop()                                               # twice: nothing to undo
        self.assertFalse(any(c[:3] == ["nmcli", "connection", "delete"] for c in self.nm.cmds[n:]))
        again = self.hs.start()
        self.assertNotEqual(again["password"], info["password"])    # a new one each time
        self.hs.stop()

    def test_pmf_off_for_the_pi_chip(self):
        add = hotspot.add_command("wlan0", "abcdefgh")
        self.assertEqual(add[add.index("wifi-sec.pmf") + 1], "disable")

    def test_status_answers_while_start_is_busy(self):
        import threading
        gate, started = threading.Event(), threading.Event()
        real = self.nm

        def slow(cmd, timeout=10):
            if cmd[:3] == ["nmcli", "--wait", "30"]:
                started.set()
                gate.wait(5)
            return real(cmd, timeout)
        self.hs.run = slow
        t = threading.Thread(target=self.hs.start)
        t.start()
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        self.assertFalse(self.hs.status()["on"])
        self.assertLess(time.monotonic() - t0, 0.5)
        gate.set()
        t.join(5)
        self.assertTrue(self.hs.status()["on"])
        self.hs.stop()

    def test_stop_when_nm_cannot_list_still_takes_the_hotspot_down(self):
        self.hs.start()
        real = self.nm
        self.hs.run = lambda cmd, timeout=10: None if cmd[:3] == ["nmcli", "-t", "-f"] else real(cmd, timeout)
        self.hs.stop()
        self.assertNotIn(hotspot.PROFILE, self.nm.profiles)
        self.assertEqual(self.nm.active, "preconfigured")
        self.assertFalse(self.hs.status()["on"])

    def test_failure_goes_back_to_the_client_network(self):
        self.nm.fail_up = True
        with self.assertRaises(hotspot.HotspotError):
            self.hs.start()
        self.assertNotIn(hotspot.PROFILE, self.nm.profiles)
        self.assertEqual(self.nm.active, "preconfigured")
        self.assertFalse(self.hs.status()["on"])

    def test_no_wifi(self):
        hs = hotspot.Hotspot(iface="wlan9", run=self.nm, sys_root=self.tmp, load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0"), state_file=os.path.join(self.tmp, "hs.json"))
        self.assertFalse(hs.available())
        with self.assertRaisesRegex(hotspot.HotspotError, "no Wi-Fi"):
            hs.start()
        self.assertEqual(self.nm.cmds, [])

    def test_cleanup_after_a_crash(self):
        self.hs.start()
        self.nm.cmds.clear()
        self.assertEqual(hotspot.cleanup(run=self.nm), ["deleted flugato-hotspot", "reconnected wlan0"])
        self.assertEqual(self.nm.active, "preconfigured")
        self.assertEqual(hotspot.cleanup(run=self.nm), [])          # nothing left
        crashed = hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=self.leases, load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0"), state_file=os.path.join(self.tmp, "hs.json"))
        crashed.start()
        fresh = hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=self.leases, load_config=lambda: dict(config.DEFAULTS, HOTSPOT_PASSWORD_DAYS="0"), state_file=os.path.join(self.tmp, "hs.json"))
        fresh.start()                                                # over the leftover
        self.assertEqual(self.nm.profiles.count(hotspot.PROFILE), 1)
        fresh.stop()

    def test_leases(self):
        self.assertEqual(hotspot.parse_leases("1 aa 192.0.2.5 phone id\nbroken\n"),
                         [{"mac": "aa", "ip": "192.0.2.5", "name": "phone"}])
        self.assertEqual(hotspot.parse_leases(None), [])


# --- web UI -------------------------------------------------------------------------------

class FakeHotspot:
    def status(self):
        return {"on": True, "ssid": "fluGATO", "clients": 1}


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.caps = os.path.join(self.tmp, "captures")
        os.makedirs(self.caps)
        os.makedirs(os.path.join(self.tmp, "proc"))
        with open(os.path.join(self.tmp, "proc", "mounts"), "w") as f:
            f.write("overlayroot / overlay rw 0 0\n")
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("# settings\nCHECK_HOST=old.example\nLLDP_WAIT=90\n")
        with open(os.path.join(self.caps, "inline-20261010-142501-00005e005301-1.pcap"), "wb") as f:
            f.write(b"\xd4\xc3\xb2\xa1" + bytes(2000))
        self.saved = []
        self.ui = web.WebUI(self.caps, self.conf, FakeHotspot(), on_saved=self.saved.append,
                            live_keys=("CHECK_HOST",), proc_root=os.path.join(self.tmp, "proc"))
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
        data = res.read()
        conn.close()
        return res, data

    def test_status_page(self):
        res, data = self.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn(b"waiting for the first result", data)
        self.ui.publish("network", [("LINK", "1G Full", "ok"), ("DNS", "no <answer>", "fail")], "PROBLEM")
        _, data = self.request("GET", "/")
        page = data.decode()
        self.assertIn("Network test", page)
        self.assertIn("no &lt;answer&gt;", page)            # escaped
        self.assertIn('class="dot fail"', page)
        self.assertIn("1 connected", page)
        self.assertNotIn("http://", page.split("</head>")[1])   # nothing loaded from elsewhere
        self.assertNotIn("<script", page)

    def test_save_then_restart_when_you_choose(self):
        _, data = self.request("GET", "/settings")
        self.assertNotIn(b'action="/restart"', data)  # no restart callback: no Restart
        self.assertEqual(data.count(b'type="submit">Save</button>'), 1)
        calls = threading.Event()
        self.ui.on_restart = calls.set
        _, data = self.request("GET", "/settings")
        self.assertIn(b'class="card system"', data)
        self.assertIn(b'type="submit">Restart</button>', data)  # any time, from System
        _, data = self.request("POST", "/settings", {"CHECK_HOST": "live.example"})
        self.assertIn(b"Saved CHECK_HOST: applied at once.", data)  # nothing waits
        self.assertNotIn(b"Restart now", data)
        _, data = self.request("POST", "/settings", {"ROTATE": "180"})
        self.assertIn(b"Saved <b>ROTATE</b>: it applies after a restart.", data)
        self.assertIn(b"Restart now", data)
        _, data = self.request("POST", "/settings", {"HOTSPOT_SSID": "Lab cat"})  # the hotspot is on
        self.assertIn(b"Saved <b>HOTSPOT_SSID, ROTATE</b>: they apply after a restart.", data)
        _, data = self.request("GET", "/settings")
        self.assertIn(b"Restart now", data)       # it stays until the restart
        self.assertFalse(calls.wait(1.5))         # Save never restarts on its own
        _, data = self.request("POST", "/restart", {}, {"Origin": f"http://{self.host}"})
        self.assertIn(b"Restarting", data)        # the page goes out first
        self.assertIn(b"Applying HOTSPOT_SSID, ROTATE.", data)
        self.assertIn(b"back on <b>Lab cat</b>", data)  # the hotspot comes back with its new name
        self.assertIn(b'http-equiv="refresh" content="60;url=/settings"', data)
        self.assertTrue(calls.wait(3))            # then the app restarts
        res, _ = self.request("POST", "/restart", {}, {"Origin": "http://evil.example"})
        self.assertEqual(res.status, 403)         # same-origin rule as every other POST

    def test_settings_save_valid_and_invalid(self):
        res, data = self.request("GET", "/settings")
        self.assertIn(b'value="old.example"', data)
        form = {k: "" for k in config.DEFAULTS}
        form.update(CHECK_HOST="new.example", LLDP_WAIT="2")
        _, data = self.request("POST", "/settings", form)
        self.assertIn(b"Nothing saved", data)
        self.assertEqual(config.load(self.conf)["CHECK_HOST"], "old.example")
        form["LLDP_WAIT"] = "60"
        _, data = self.request("POST", "/settings", form)
        self.assertIn(b"Saved CHECK_HOST: applied at once.", data)
        self.assertIn(b"Saved LLDP_WAIT: after a restart of fluGATO.", data)
        cfg = config.load(self.conf)
        self.assertEqual((cfg["CHECK_HOST"], cfg["LLDP_WAIT"]), ("new.example", "60"))
        with open(self.conf) as f:
            self.assertTrue(f.read().startswith("# settings\n"))
        self.assertEqual(self.saved[-1]["CHECK_HOST"], "new.example")

    def test_captures_list_download_delete(self):
        name = "inline-20261010-142501-00005e005301-1.pcap"
        _, data = self.request("GET", "/captures")
        self.assertIn(name.encode(), data)
        self.assertIn(b"RAM", data)
        res, data = self.request("GET", f"/captures/{name}")
        self.assertEqual(res.status, 200)
        self.assertEqual(len(data), 2004)
        self.assertIn("attachment", res.getheader("Content-Disposition"))
        for bad in ("../flugato.conf", "%2e%2e%2fflugato.conf", "flugato.conf", "x.pcap"):
            res, _ = self.request("GET", f"/captures/{bad}")
            self.assertEqual(res.status, 404, bad)
        res, _ = self.request("POST", "/captures/delete", {"name": "../flugato.conf"})
        self.assertTrue(os.path.exists(self.conf))
        res, _ = self.request("POST", "/captures/delete", {"name": name})
        self.assertEqual(res.status, 303)
        self.assertEqual(os.listdir(self.caps), [])

    def test_foreign_pages_and_hosts_are_refused(self):
        form = {"name": "inline-20261010-142501-00005e005301-1.pcap"}
        res, _ = self.request("POST", "/captures/delete", form, {"Origin": "http://evil.example"})
        self.assertEqual(res.status, 403)
        res, _ = self.request("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(res.status, 400)
        self.assertEqual(len(os.listdir(self.caps)), 1)

    def test_listens_on_the_given_address_only(self):
        self.assertEqual(self.ui.server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()

"""The terminal app's pieces with their I/O faked: the LLDP/CDP parsers, the DHCP client's
frames, the history file, the settings file, the --once output, the --watch lines, update and
uninstall against a local web server. Addresses are RFC 5737, names made up."""

import io
import json
import os
import socket
import struct
import tempfile
import unittest
import zipfile
from unittest import mock

from flugato import dhcp_probe
from flugato.checks import FAIL, OK, SKIP, WARN
from flugato.term import VERSION, cli, dhcp_client, history, neighbours, release, runner
from .site_fake import make_release, serve

MAC = bytes.fromhex("001122334455")


def lldp_tlv(t, v):
    return struct.pack("!H", (t << 9) | len(v)) + v


def lldp_frame(pvid=70, names=((10, b"VLAN10"),)):
    body = (lldp_tlv(1, b"\x04" + bytes.fromhex("aabbccddeeff")) + lldp_tlv(2, b"\x05Gi1/0/14")
            + lldp_tlv(3, b"\x00\x78") + lldp_tlv(4, b"desk 3B") + lldp_tlv(5, b"access-sw-02")
            + lldp_tlv(8, b"\x05\x01" + socket.inet_aton("192.0.2.250") + b"\x02\x00\x00\x00\x00\x00\x00"))
    if pvid:
        body += lldp_tlv(127, b"\x00\x80\xc2\x01" + struct.pack("!H", pvid))
    for vid, name in names:
        body += lldp_tlv(127, b"\x00\x80\xc2\x03" + struct.pack("!HB", vid, len(name)) + name)
    return neighbours.LLDP_DST + MAC + b"\x88\xcc" + body + lldp_tlv(0, b"")


def cdp_tlv(t, v):
    return struct.pack("!HH", t, len(v) + 4) + v


def cdp_frame():
    addr = b"\x00\x00\x00\x01" + b"\x01\x01\xcc\x00\x04" + socket.inet_aton("192.0.2.250")
    body = (cdp_tlv(1, b"access-sw-02") + cdp_tlv(2, addr) + cdp_tlv(3, b"GigabitEthernet1/0/14")
            + cdp_tlv(6, b"cisco WS-C2960X-48") + cdp_tlv(0x0A, b"\x00\x46") + cdp_tlv(0x0E, b"\x01\x00\x6e"))
    return neighbours.CDP_DST + MAC + struct.pack("!H", len(body) + 12) + neighbours.CDP_SNAP + b"\x02\xb4\x00\x00" + body


class Neighbours(unittest.TestCase):
    def test_lldp(self):
        n = neighbours.parse_lldp_frame(lldp_frame())
        self.assertEqual((n["via"], n["switch"], n["mgmt"], n["port"], n["descr"], n["pvid"], n["vlans"]),
                         ("LLDP", "access-sw-02", "192.0.2.250", "Gi1/0/14", "desk 3B", 70, [10]))

    def test_lldp_without_name_falls_back_to_the_chassis_mac(self):
        f = lldp_frame()
        f = f.replace(lldp_tlv(5, b"access-sw-02"), b"")
        self.assertEqual(neighbours.parse_lldp_frame(f)["switch"], "aa:bb:cc:dd:ee:ff")

    def test_cdp(self):
        n = neighbours.parse_cdp_frame(cdp_frame())
        self.assertEqual((n["via"], n["switch"], n["mgmt"], n["port"], n["pvid"], n["voice_vlan"], n["platform"]),
                         ("CDPv2", "access-sw-02", "192.0.2.250", "Gi1/0/14", 70, 110, "cisco WS-C2960X-48"))

    def test_other_frames_are_ignored(self):
        self.assertIsNone(neighbours.parse_frame(b"\xff" * 6 + MAC + b"\x08\x00" + b"\x45" + bytes(60)))
        self.assertIsNone(neighbours.parse_frame(b""))

    def test_merge_prefers_lldp_and_keeps_cdps_native_vlan(self):
        lldp = neighbours.parse_lldp_frame(lldp_frame(pvid=0, names=()))
        cdp = neighbours.parse_cdp_frame(cdp_frame())
        m = neighbours.merge(cdp, lldp)
        self.assertEqual((m["via"], m["descr"], m["pvid"]), ("LLDP + CDPv2", "desk 3B", 70))
        m = neighbours.merge(lldp, cdp)
        self.assertEqual((m["via"], m["pvid"]), ("LLDP + CDPv2", 70))

    def test_malformed_frames_and_control_characters(self):
        f = lldp_frame()
        cut = neighbours.parse_frame(f[:40])                                 # cut mid-TLV: what fit, no error
        self.assertEqual((cut["via"], cut["descr"]), ("LLDP", None))
        bad = f.replace(b"access-sw-02", b"\x1b]0;x\x07sw-02")                  # an escape in the name
        name = neighbours.parse_frame(bad)["switch"]
        self.assertEqual(name, "]0;xsw-02")                                   # the printable rest stays
        self.assertNotIn("\x1b", name)
        self.assertNotIn("\x07", name)
        c = cdp_frame()
        self.assertIsNone(neighbours.parse_frame(c[:30] + b"\xff" * 4))
        for junk in (b"\x00" * 14, bytes(range(256)), neighbours.LLDP_DST + b"\x00" * 8 + b"\x88\xcc" + b"\xff" * 50):
            neighbours.parse_frame(junk)                                       # never raises
        self.assertEqual(cli.clean("a\x1b[31mb\x07"), "a?[31mb?")

    def test_short_port_names(self):
        self.assertEqual(neighbours.short_port("TenGigabitEthernet1/1/2"), "Te1/1/2")
        self.assertEqual(neighbours.short_port("eth0"), "eth0")


def bootp_reply(xid, kind, address="192.0.2.57", server="192.0.2.1"):
    """A server's OFFER/ACK/NAK frame to our xid."""
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 2, 1, 6, 0, xid, 0, 0, bytes(4),
                        socket.inet_aton(address), bytes(4), bytes(4), MAC + bytes(10), bytes(64), bytes(128))
    opts = (b"\x63\x82\x53\x63" + bytes((53, 1, kind)) + b"\x36\x04" + socket.inet_aton(server)
            + b"\x01\x04\xff\xff\xff\x00" + b"\x03\x04" + socket.inet_aton("192.0.2.254")
            + b"\x06\x04" + socket.inet_aton("192.0.2.53") + b"\x33\x04\x00\x00\xa8\xc0" + b"\xff")
    payload = bootp + opts
    udp = struct.pack("!HHHH", 67, 68, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                     socket.inet_aton(server), bytes((255, 255, 255, 255)))
    return MAC + bytes.fromhex("aabbccddeeff") + b"\x08\x00" + ip + udp


class DhcpClient(unittest.TestCase):
    def test_request_frame_carries_the_offer(self):
        f = dhcp_client.build_request(MAC, 0x1234, "192.0.2.57", "192.0.2.1")
        opts = f[14 + 20 + 8 + 240:]
        self.assertTrue(opts.startswith(b"\x35\x01\x03\x32\x04" + socket.inet_aton("192.0.2.57") + b"\x36\x04"))
        self.assertEqual(f[:6], b"\xff" * 6)

    def test_release_goes_to_the_server(self):
        f = dhcp_client.build_release(MAC, 1, "192.0.2.57", "192.0.2.1", bytes.fromhex("aabbccddeeff"))
        self.assertEqual(f[:6], bytes.fromhex("aabbccddeeff"))
        self.assertEqual(socket.inet_ntoa(f[26:30]), "192.0.2.57")
        self.assertEqual(socket.inet_ntoa(f[30:34]), "192.0.2.1")
        self.assertEqual(f[14 + 20 + 8 + 12:14 + 20 + 8 + 16], socket.inet_aton("192.0.2.57"))  # ciaddr

    def test_ack_and_nak_parse_with_their_kind(self):
        ack = dhcp_probe.parse_offer(bootp_reply(7, 5), 7, None, None, kinds=(5, 6))
        self.assertEqual((ack["kind"], ack["address"], ack["prefix"], ack["lease"]), (5, "192.0.2.57", 24, 43200))
        nak = dhcp_probe.parse_offer(bootp_reply(7, 6), 7, None, None, kinds=(5, 6))
        self.assertEqual(nak["kind"], 6)
        self.assertIsNone(dhcp_probe.parse_offer(bootp_reply(7, 5), 7))      # an offer parser ignores an ack

    def test_describe(self):
        text = dhcp_client.describe({"server": "192.0.2.1", "lease": 43200, "took": 0.042})
        self.assertEqual(text, "from 192.0.2.1  lease 12h  offer in 42 ms")
        self.assertTrue(dhcp_client.describe({"server": "192.0.2.1", "took": 2.4}).endswith("offer in 2.4 s"))


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_append_and_recent(self):
        r = runner.Runner("eth0")
        r.report["LINK"].set("1G Full", OK)
        e = history.entry("eth0", "ALL GOOD", 0, r.report, {"switch": "sw", "port": "Gi1/0/1"}, {"speed": 1000})
        self.assertTrue(history.append(e))
        self.assertTrue(history.append(history.entry("eth0", "NO DHCP", 2, r.report)))
        got = history.recent(5)
        self.assertEqual([x["verdict"] for x in got], ["NO DHCP", "ALL GOOD"])
        self.assertEqual((got[1]["switch"], got[1]["switch_port"], got[1]["speed"]), ("sw", "Gi1/0/1", 1000))
        self.assertTrue(history.path().startswith(self.tmp.name))

    def test_when(self):
        import time
        now = time.mktime(time.strptime("2026-10-14 15:00:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(history.when({"at": "2026-10-14T14:02:00"}, now), "14:02")
        self.assertEqual(history.when({"at": "2026-10-13T23:50:00"}, now), "yest.")
        self.assertEqual(history.when({"at": "2026-10-01T09:00:00"}, now), "10-01")
        self.assertEqual(history.when({}, now), "?")


class Settings(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(cli.load_settings(), {})
            self.assertTrue(cli.save_setting("PORT", "enp3s0"))
            self.assertTrue(cli.save_setting("CHECK_HOST", "host.example"))
            self.assertTrue(cli.save_setting("PORT", "enp4s0"))
            self.assertEqual(cli.load_settings(), {"PORT": "enp4s0", "CHECK_HOST": "host.example"})
            with open(cli.config_path()) as f:
                self.assertEqual(f.read().count("PORT="), 1)


class Once(unittest.TestCase):
    def test_report_text_and_exit_codes(self):
        for name, verdict, code in (("good", "ALL GOOD", 0), ("cable", "CHECK CABLE", 1),
                                    ("nodhcp", "NO DHCP", 2), ("nolink", "NO LINK", 3)):
            r = cli.demo_runner(name)
            out = io.StringIO()
            self.assertEqual(cli.print_report(r, out, colour=False), code, name)
            text = out.getvalue()
            self.assertIn(f"fluGATO {VERSION} · enp3s0", text)
            self.assertIn("● LINK      ", text)
            self.assertIn(verdict, text.splitlines()[-1])
            self.assertEqual(runner.EXIT[r.verdict], code)

    def test_json_shape(self):
        d = cli.demo_runner("good").to_dict(VERSION)
        self.assertEqual((d["verdict"], d["exit"], d["rows"]["LINK"]["value"], d["switch"]["port"]),
                         ("ALL GOOD", 0, "1G Full", "Gi1/0/14"))
        self.assertEqual(d["vlans"]["native"], 40)
        json.dumps(d)

    def test_verdicts(self):
        r = runner.Runner("eth0")
        for row in r.report.values():
            row.set("x", OK)
        self.assertEqual(r.verdict, "ALL GOOD")
        r.report["LINK"].set("100M Full  both do 1G: check cable", WARN)
        self.assertEqual(r.verdict, "CHECK CABLE")
        r.report["LINK"].set("1G Full", OK)
        r.report["SWITCH"].set("no LLDP/CDP heard", WARN)
        self.assertEqual(r.verdict, "CHECK WARNINGS")
        r.report["DHCP"].set("no offer", FAIL)
        self.assertEqual(r.verdict, "NO DHCP")
        r.report["LINK"].set("down", FAIL)
        self.assertEqual(r.verdict, "PROBLEM FOUND")
        r.link_up = False
        self.assertEqual((r.verdict, r.mood), ("NO LINK", "sleep"))

    def test_passive_hint_names_the_command(self):
        r = cli.demo_runner("good")
        r.root = False
        r.report["SWITCH"].set(runner.NEEDS_ROOT, SKIP)
        out = io.StringIO()
        cli.print_report(r, out, colour=False)
        self.assertIn("need root: sudo", out.getvalue())


class Watch(unittest.TestCase):
    def test_lines_on_change(self):
        row = runner.checks.Row("LINK")
        row.set("1G Full", OK)
        down = {"up": False, "addr": None, "gw": None}
        up = {"up": True, "addr": ("192.0.2.57", 24), "gw": "192.0.2.1"}
        self.assertEqual(cli.watch_lines(None, down, "eth0"), [(FAIL, "link down")])
        self.assertEqual(cli.watch_lines(down, up, "eth0", link_row=row),
                         [(OK, "link up 1G Full"), (OK, "address 192.0.2.57/24"), (OK, "gateway 192.0.2.1")])
        self.assertEqual(cli.watch_lines(up, up, "eth0", link_row=row), [])
        ll = {"up": True, "addr": ("169.254.3.4", 16), "gw": None}
        self.assertEqual(cli.watch_lines(up, ll, "eth0", link_row=row), [(FAIL, "link-local only 169.254.3.4/16")])
        self.assertEqual(cli.watch_lines(up, down, "eth0"), [(FAIL, "link down"), (WARN, "no address")])


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.site = os.path.join(self.tmp.name, "site")
        os.makedirs(self.site)
        self.srv, self.base = serve(self.site)
        self.target = os.path.join(self.tmp.name, "flugato")
        with zipfile.ZipFile(self.target, "w") as z:
            z.writestr("__main__.py", "print('old')")

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_update_installs_a_newer_release(self):
        make_release(self.site, "9.9.9", b"PK\x05\x06" + bytes(18) + b"new")
        lines = []
        self.assertEqual(release.update(lines.append, target=self.target, base=self.base), 0)
        with open(self.target, "rb") as f:
            self.assertTrue(f.read().endswith(b"new"))
        self.assertTrue(os.access(self.target, os.X_OK))
        self.assertIn("installed flugato 9.9.9", lines[-1])

    def test_update_refuses_a_bad_checksum(self):
        make_release(self.site, "9.9.9")
        with open(os.path.join(self.site, "releases", "9.9.9", "SHA256SUMS"), "w") as f:
            f.write("0" * 64 + "  flugato.pyz\n")
        lines = []
        self.assertEqual(release.update(lines.append, target=self.target, base=self.base), 4)
        self.assertIn("sha256 mismatch", lines[-1])
        with zipfile.ZipFile(self.target) as z:
            self.assertEqual(z.namelist(), ["__main__.py"])          # untouched

    def test_update_knows_when_it_is_current(self):
        make_release(self.site, VERSION)
        lines = []
        self.assertEqual(release.update(lines.append, target=self.target, base=self.base), 0)
        self.assertIn("is the latest", lines[-1])

    def test_update_without_a_site(self):
        self.srv.shutdown()
        lines = []
        self.assertEqual(release.update(lines.append, target=self.target, base="http://127.0.0.1:1"), 4)
        self.assertIn("cannot reach", lines[-1])

    def test_uninstall(self):
        state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(state, "flugato"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state, "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "none")}):
            lines = []
            self.assertEqual(release.uninstall(lines.append, ask=lambda q: "y", target=self.target), 0)
        self.assertFalse(os.path.exists(self.target))
        self.assertFalse(os.path.exists(os.path.join(state, "flugato")))

    def test_sum_for(self):
        self.assertEqual(release.sum_for("abc  flugato.pyz\ndef *other\n", "other"), "def")
        self.assertIsNone(release.sum_for("abc  x\n", "flugato.pyz"))


if __name__ == "__main__":
    unittest.main()

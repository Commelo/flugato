"""Parser tests against captured-style tool output. Addresses are from the
RFC 5737 documentation range; names are fictional."""

import os
import socket
import struct
import tempfile
import threading
import unittest

from flugato import checks, config, display
from flugato.checks import OK, WARN, Row

ETHTOOL = """Settings for eth0:
	Supported ports: [ TP MII ]
	Supported link modes:   10baseT/Half 10baseT/Full
	                        100baseT/Half 100baseT/Full
	Advertised link modes:  10baseT/Half 10baseT/Full
	                        100baseT/Half 100baseT/Full
	Link partner advertised link modes:  10baseT/Half 10baseT/Full
	                                     100baseT/Half 100baseT/Full
	                                     1000baseT/Full
	Speed: 100Mb/s
	Duplex: Full
	Auto-negotiation: on
	Link detected: yes
"""

IP_ADDR = """[{"ifindex":2,"ifname":"eth0","addr_info":[{"family":"inet","local":"192.0.2.57",
"prefixlen":24,"scope":"global"}]}]"""
IP_ROUTE = """[{"dst":"default","gateway":"192.0.2.1","dev":"eth0","protocol":"dhcp"}]"""

NMCLI = """DHCP4.OPTION[1]:dhcp_lease_time = 43200
DHCP4.OPTION[2]:dhcp_server_identifier = 192.0.2.1
DHCP4.OPTION[3]:domain_name_servers = 192.0.2.1 192.0.2.2
DHCP4.OPTION[4]:ip_address = 192.0.2.57
"""

PING_OK = """3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 0.812/0.934/1.101/0.120 ms
"""
PING_LOSS = "3 packets transmitted, 0 received, 100% packet loss, time 2040ms\n"

LLDP = """lldp.eth0.via=LLDP
lldp.eth0.rid=1
lldp.eth0.age=0 day, 00:00:12
lldp.eth0.chassis.mac=00:00:5e:00:53:01
lldp.eth0.chassis.name=access-sw-02
lldp.eth0.chassis.mgmt-ip=192.0.2.250
lldp.eth0.port.ifname=Gi1/0/14
lldp.eth0.port.descr=desk 3B
lldp.eth0.vlan.vlan-id=20
lldp.eth0.vlan.pvid=yes
lldp.eth0.vlan=VLAN0020
"""

# one neighbour seen twice (LLDP and CDP), two VLANs where the first is the native one
LLDP_TWO = """lldp.eth0.via=CDPv2
lldp.eth0.chassis.name=access-sw-02.example
lldp.eth0.port.ifname=GigabitEthernet1/0/14
lldp.eth0.via=LLDP
lldp.eth0.chassis.name=access-sw-02
lldp.eth0.port.ifname=Gi1/0/14
lldp.eth0.vlan.vlan-id=10
lldp.eth0.vlan.pvid=yes
lldp.eth0.vlan=DATA
lldp.eth0.vlan.vlan-id=20
lldp.eth0.vlan=VOICE
"""


def fake_reply(query, addrs, rcode=0):
    """A DNS server's answer to `query`, with compression pointers like real servers."""
    qid, = struct.unpack("!H", query[:2])
    head = struct.pack("!HHHHHH", qid, 0x8180 | rcode, 1, len(addrs), 0, 0)
    body = query[12:]
    for a in addrs:
        body += struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(a)
    return head + body


class Parsers(unittest.TestCase):
    def test_ethtool(self):
        info = checks.parse_ethtool(ETHTOOL)
        self.assertEqual(info["speed"], 100)
        self.assertEqual(info["duplex"], "Full")
        self.assertTrue(info["autoneg"])
        self.assertEqual(checks.max_speed(info["partner"]), 1000)
        self.assertEqual(checks.max_speed(info["supported"]), 100)

    def test_link_judgement(self):
        # Pi 1 (10/100) on a gigabit port: fine, just informative
        row = Row("LINK")
        checks.run, saved = (lambda cmd, timeout=10: ETHTOOL), checks.run
        try:
            checks.check_link("eth0", row)
            self.assertEqual((row.value, row.state), ("100M Full  (port does 1G)", OK))
            # Pi 5 (gigabit) stuck at 100 on a gigabit port: flag the cable
            gig = ETHTOOL.replace("100baseT/Half 100baseT/Full\n\tAdvertised",
                                  "100baseT/Half 100baseT/Full\n\t                        1000baseT/Full\n\tAdvertised")
            checks.run = lambda cmd, timeout=10: gig
            checks.check_link("eth0", row)
            self.assertEqual((row.value, row.state), ("100M Full  both do 1G: check cable", WARN))
            checks.run = lambda cmd, timeout=10: gig.replace("Speed: 100Mb/s", "Speed: 1000Mb/s")
            checks.check_link("eth0", row)
            self.assertEqual((row.value, row.state), ("1G Full", OK))
        finally:
            checks.run = saved

    def test_ethtool_unknown_speed(self):
        info = checks.parse_ethtool("Speed: Unknown!\nDuplex: Unknown! (255)\n")
        self.assertIsNone(info["speed"])
        self.assertIsNone(info["duplex"])

    def test_ip(self):
        self.assertEqual(checks.parse_ip_addr(IP_ADDR), ("192.0.2.57", 24))
        self.assertEqual(checks.parse_ip_route(IP_ROUTE), "192.0.2.1")
        self.assertIsNone(checks.parse_ip_addr("[]"))
        self.assertIsNone(checks.parse_ip_route(None))

    def test_nmcli(self):
        opts = checks.parse_nmcli_dhcp(NMCLI)
        self.assertEqual(opts["dhcp_server_identifier"], "192.0.2.1")
        self.assertEqual(checks.fmt_lease(opts["dhcp_lease_time"]), "12h")

    def test_ping(self):
        self.assertEqual(checks.parse_ping(PING_OK), (0.0, 0.934))
        self.assertEqual(checks.parse_ping(PING_LOSS), (100.0, None))

    def test_lldp(self):
        n = checks.parse_lldp(LLDP, "eth0")
        self.assertEqual(n["switch"], "access-sw-02")
        self.assertEqual(n["port"], "Gi1/0/14")
        self.assertEqual(n["pvid"], 20)
        self.assertIsNone(checks.parse_lldp("", "eth0"))
        rows = Row("SWITCH"), Row("PORT"), Row("VLAN")
        checks.fill_switch_rows(n, *rows)
        self.assertEqual(rows[1].value, "Gi1/0/14  desk 3B")
        self.assertEqual(rows[2].value, "20 native")

    def test_lldp_repeated_keys(self):
        n = checks.parse_lldp(LLDP_TWO, "eth0")
        self.assertEqual(n["via"], "LLDP")
        self.assertEqual(n["port"], "Gi1/0/14")
        self.assertEqual(n["vlans"], [10, 20])
        self.assertEqual(n["pvid"], 10)

    def test_config_inline_comment(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("LLDP_WAIT=60   # shorter\nCHECK_HOST='host.example'\n")
        try:
            cfg = config.load(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["LLDP_WAIT"], "60")
        self.assertEqual(cfg["CHECK_HOST"], "host.example")

    def test_fit_bisection(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        text = "Gi1/0/14  " + "very long port description " * 6
        out = display._fit(d, text, display.F_VALUE, 200)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(d.textlength(out, font=display.F_VALUE), 200)
        longer = text[:len(out)] + "…"  # one more character must not fit
        self.assertGreater(d.textlength(longer, font=display.F_VALUE), 200)

    def test_nmcli_dns(self):
        out = "IP4.DNS[1]:192.0.2.1\nIP4.DNS[2]:192.0.2.2\n"
        self.assertEqual(checks.parse_nmcli_dns(out), ["192.0.2.1", "192.0.2.2"])

    def test_dns_wire_format(self):
        q = checks.build_dns_query("host.example", 0x1234)
        self.assertEqual(q[:12], struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0))
        self.assertIn(b"\x04host\x07example\x00", q)
        reply = fake_reply(q, ["192.0.2.80"])
        self.assertEqual(checks.parse_dns_response(reply, 0x1234), (0, ["192.0.2.80"]))
        self.assertEqual(checks.parse_dns_response(fake_reply(q, [], rcode=3), 0x1234), (3, []))
        with self.assertRaises(ValueError):
            checks.parse_dns_response(reply, 0x9999)

    def test_dns_lookup_over_loopback(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]

        def answer():
            data, peer = srv.recvfrom(512)
            srv.sendto(fake_reply(data, ["192.0.2.80"]), peer)
        threading.Thread(target=answer, daemon=True).start()
        try:
            # the real code talks to port 53; point it at the fake server instead
            orig = checks.dns_lookup.__globals__["socket"].socket

            class Redirect(orig):
                def sendto(self, data, addr):
                    return super().sendto(data, (addr[0], port))
            checks.dns_lookup.__globals__["socket"].socket = Redirect
            addrs, err = checks.dns_lookup("127.0.0.1", "host.example", "lo")
        finally:
            checks.dns_lookup.__globals__["socket"].socket = orig
            srv.close()
        if err and err.startswith("cannot use"):
            self.skipTest("binding to an interface needs root here: " + err)
        self.assertEqual((addrs, err), (["192.0.2.80"], None))

    def test_cat_band_write(self):
        fb = display.Framebuffer.__new__(display.Framebuffer)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            fb.dev = f.name
        fb.width, fb.height, fb.bpp, fb.rotate = display.W, display.H, 16, 0
        try:
            img = display.render(checks.new_report(), "eth0", frame=1)
            fb.show(img)
            full = open(fb.dev, "rb").read()
            fb.show(display.render(checks.new_report(), "eth0", frame=2, pose="look_l"),
                    band=display.CAT_BAND)
            after = open(fb.dev, "rb").read()
            y0 = display.CAT_BAND[0] * display.W * 2
            self.assertEqual(len(after), len(full))     # written in place
            self.assertEqual(after[:y0], full[:y0])     # rows above the cat untouched
            self.assertNotEqual(after[y0:], full[y0:])  # the cat moved
        finally:
            os.unlink(fb.dev)

    def test_text_clears_the_cat(self):
        top = display.HEADER_H + 8 * display.ROW_H        # VLAN row, beside the cat
        self.assertEqual(display.text_limit(display.HEADER_H, display.ROW_H), display.W - 8)
        self.assertGreaterEqual(display.text_limit(top, display.ROW_H, OK), 409)
        self.assertLess(display.text_limit(top, display.ROW_H, checks.FAIL),
                        display.text_limit(top, display.ROW_H, OK))  # scared ears stick out

    def test_frame_vlan(self):
        dst_src = bytes(12)
        tagged = dst_src + b"\x81\x00" + (0x2000 | 20).to_bytes(2, "big") + b"\x08\x00"
        self.assertEqual(checks.frame_vlan(tagged), 20)
        self.assertIsNone(checks.frame_vlan(dst_src + b"\x08\x00" + bytes(4)))
        # tag stripped by the kernel and handed over in the auxdata
        aux = checks.AUXDATA.pack(checks.TP_STATUS_VLAN_VALID, 60, 60, 0, 14, 70, 0x8100)
        self.assertEqual(checks.frame_vlan(dst_src + b"\x08\x00" + bytes(4), aux), 70)
        aux0 = checks.AUXDATA.pack(checks.TP_STATUS_VLAN_VALID, 60, 60, 0, 14, 0, 0x8100)
        self.assertIsNone(checks.frame_vlan(dst_src + b"\x08\x00", aux0))  # priority tag

    def test_vlan_summary(self):
        self.assertEqual(checks.vlan_summary(70, {10, 20}), ("70 native  trunk 2 VLANs: 10, 20", OK))
        self.assertEqual(checks.vlan_summary(70, set()), ("70 native", OK))
        self.assertEqual(checks.vlan_summary(None, set()), ("untagged only", OK))
        self.assertEqual(checks.vlan_summary(None, None), ("not advertised", checks.SKIP))
        self.assertEqual(checks.vlan_summary(70, {10, 20}, {10: {"address": "x"}, 20: None}),
                         ("70 native  trunk 2 VLANs  DHCP 1/2", OK))
        many = checks.vlan_summary(1, set(range(2, 14)))[0]
        self.assertTrue(many.endswith("+8"))

    def test_overall_state(self):
        r = checks.new_report()
        for row in r.values():
            row.set("x", OK)
        self.assertEqual(display.overall(r), OK)
        r["LINK"].set("10M Half", WARN)
        self.assertEqual(display.overall(r), WARN)

    def test_long_values_are_cut(self):
        r = checks.new_report()
        r["PORT"].set("x" * 300, OK)
        display.render(r, "eth0")  # must not raise or overflow


if __name__ == "__main__":
    unittest.main()

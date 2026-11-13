"""Inline mode without the ports: parsers on hand-built frames, the observer fed frame by
frame, the pcap writer into a temporary directory, and a whole session against a fake
host (commands recorded, /sys and /proc in a temporary tree). Documentation addresses
only."""

import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

from flugato import inline

DEV = bytes.fromhex("00005e005301")      # the device behind port B
DEV2 = bytes.fromhex("00005e005302")     # a PC behind a phone
SW = bytes.fromhex("00005e0053fe")       # the switch / router side
A_MAC, B_MAC = bytes.fromhex("88a29e005301"), bytes.fromhex("9c69d3005302")
OURS = {A_MAC, B_MAC}
BCAST = b"\xff" * 6
IP = socket.inet_aton


# --- frame builders -------------------------------------------------------------------------

def eth(dst, src, etype, payload, vlan=None):
    tag = b"\x81\x00" + struct.pack("!H", vlan) if vlan else b""
    return dst + src + tag + struct.pack("!H", etype) + payload


def ipv4(src, dst, proto, payload, frag=0):
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, frag, 64, proto, 0,
                       IP(src), IP(dst)) + payload


def udp(sport, dport, payload):
    return struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload


def tcp(sport, dport, payload=b""):
    return struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, 0x18, 0, 0, 0) + payload


def option(code, value):
    return bytes((code, len(value))) + value


def dhcp(op, kind, mac=DEV, xid=0x1234, yiaddr="0.0.0.0", options=b"", giaddr="0.0.0.0"):
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", op, 1, 6, 0, xid, 0, 0x8000,
                        bytes(4), IP(yiaddr), bytes(4), IP(giaddr), mac + bytes(10),
                        bytes(64), bytes(128))
    return bootp + inline.DHCP_MAGIC + option(53, bytes((kind,))) + options + b"\xff"


def client_frame(kind, mac=DEV, vlan=None, options=b""):
    payload = dhcp(1, kind, mac, options=options)
    return eth(BCAST, mac, 0x0800, ipv4("0.0.0.0", "255.255.255.255", 17, udp(68, 67, payload)), vlan)


def server_frame(kind, mac=DEV, yiaddr="192.0.2.57", server="192.0.2.1", options=None):
    if options is None:
        options = (option(54, IP(server)) + option(1, IP("255.255.255.0")) +
                   option(3, IP("192.0.2.254")) + option(6, IP("192.0.2.53") + IP("192.0.2.54")) +
                   option(51, struct.pack("!I", 86400)))
    payload = dhcp(2, kind, mac, yiaddr=yiaddr, options=options)
    return eth(mac, SW, 0x0800, ipv4(server, yiaddr, 17, udp(67, 68, payload)))


def arp(sender_mac, sender_ip, target_ip, op=1):
    body = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, sender_mac, IP(sender_ip),
                       bytes(6), IP(target_ip))
    return eth(BCAST, sender_mac, 0x0806, body)


def tlv(kind, value):
    return struct.pack("!H", kind << 9 | len(value)) + value


def lldp_frame(src=SW, name=b"sw1.example", caps=0x0014, extra=b""):
    body = (tlv(1, b"\x04" + src) + tlv(2, b"\x05Gi1/0/7") + tlv(3, b"\x00\x78") +
            tlv(4, b"office desk 7") + tlv(5, name) + tlv(6, b"Example OS 1.0\nmore") +
            tlv(7, struct.pack("!HH", caps, caps)) +
            tlv(8, b"\x05\x01" + IP("192.0.2.2") + b"\x02\x00\x00\x00\x01\x00") +
            tlv(127, b"\x00\x80\xc2\x01\x00\x46") +                      # PVID 70
            tlv(127, b"\x00\x80\xc2\x03\x00\x46\x04data") +              # VLAN 70 named
            tlv(127, b"\x00\x12\xbb\x02\x01" + (0x400000 | 60 << 9 | 5 << 6 | 46).to_bytes(3, "big")) +
            extra + tlv(0, b""))
    return eth(bytes.fromhex("0180c200000e"), src, 0x88CC, body)


def cdp_tlv(kind, value):
    return struct.pack("!HH", kind, 4 + len(value)) + value


def cdp_frame(src=SW, caps=0x28, name=b"sw1.example", pid=inline.SNAP_CDP):
    addr = struct.pack("!I", 1) + b"\x01\x01\xcc" + struct.pack("!H", 4) + IP("192.0.2.2")
    body = (b"\x02\xb4\x00\x00" + cdp_tlv(1, name) + cdp_tlv(2, addr) +
            cdp_tlv(3, b"GigabitEthernet0/7") + cdp_tlv(4, struct.pack("!I", caps)) +
            cdp_tlv(5, b"Example IOS 15.2\nline 2") + cdp_tlv(6, b"cisco WS-C2960-24") +
            cdp_tlv(0x0A, b"\x00\x46") + cdp_tlv(0x0E, b"\x01\x00\x3c"))
    llc = b"\xaa\xaa\x03\x00\x00\x0c" + struct.pack("!H", pid) + body
    return bytes.fromhex("01000ccccccc") + src + struct.pack("!H", len(llc)) + llc


def stp_frame(src=SW):
    llc = b"\x42\x42\x03" + bytes(35)
    return inline.STP_DST + src + struct.pack("!H", len(llc)) + llc


def dns_query(name, qid=7):
    q = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes((len(label),)) + label.encode()
    return q + b"\0" + struct.pack("!HH", 1, 1)


# --- parsers -------------------------------------------------------------------------------

class Dhcp(unittest.TestCase):
    def test_client_discover_with_name_vendor_and_wish(self):
        opts = option(12, b"printer-7") + option(60, b"MSFT 5.0") + option(50, IP("192.0.2.80")) + \
            option(55, b"\x01\x03\x06")
        d = inline.parse_dhcp(client_frame(1, options=opts))
        self.assertEqual((d["op"], d["type"], d["mac"]), ("client", "DISCOVER", inline.mac_str(DEV)))
        self.assertEqual((d["hostname"], d["vendor_class"], d["requested_ip"]),
                         ("printer-7", "MSFT 5.0", "192.0.2.80"))
        self.assertEqual(d["params"], [1, 3, 6])
        self.assertIsNone(d["yiaddr"])

    def test_server_ack_and_nak(self):
        d = inline.parse_dhcp(server_frame(5))
        self.assertEqual((d["op"], d["type"], d["yiaddr"], d["server"], d["router"]),
                         ("server", "ACK", "192.0.2.57", "192.0.2.1", "192.0.2.254"))
        self.assertEqual((d["dns"], d["lease"], d["prefix"]), (["192.0.2.53", "192.0.2.54"], 86400, 24))
        nak = inline.parse_dhcp(server_frame(6, yiaddr="0.0.0.0",
                                             options=option(54, IP("192.0.2.1")) + option(56, b"wrong network")))
        self.assertEqual((nak["type"], nak["message"], nak["yiaddr"]), ("NAK", "wrong network", None))

    def test_tagged_frame_is_parsed(self):
        self.assertEqual(inline.parse_dhcp(client_frame(3, vlan=60))["type"], "REQUEST")
        self.assertEqual(inline.l2(client_frame(3, vlan=60))[4], 60)

    def test_not_dhcp(self):
        dns = eth(SW, DEV, 0x0800, ipv4("192.0.2.57", "192.0.2.53", 17, udp(5000, 53, b"x" * 20)))
        self.assertIsNone(inline.parse_dhcp(dns))
        self.assertIsNone(inline.parse_dhcp(client_frame(1)[:200]))  # cut short
        frag = eth(BCAST, DEV, 0x0800, ipv4("0.0.0.0", "255.255.255.255", 17, b"x" * 300, frag=0x10))
        self.assertIsNone(inline.parse_dhcp(frag))


class Arp(unittest.TestCase):
    def test_request_reply_and_probe(self):
        a = inline.parse_arp(arp(DEV, "192.0.2.57", "192.0.2.254"))
        self.assertEqual((a["op"], a["mac"], a["ip"], a["target_ip"]),
                         ("request", inline.mac_str(DEV), "192.0.2.57", "192.0.2.254"))
        self.assertEqual(inline.parse_arp(arp(DEV, "192.0.2.57", "192.0.2.254", op=2))["op"], "reply")
        self.assertIsNone(inline.parse_arp(arp(DEV, "0.0.0.0", "192.0.2.57"))["ip"])
        self.assertIsNone(inline.parse_arp(client_frame(1)))


class Neighbours(unittest.TestCase):
    def test_lldp(self):
        n = inline.parse_lldp_frame(lldp_frame())
        self.assertEqual((n["via"], n["switch"], n["port"], n["descr"], n["mgmt"]),
                         ("LLDP", "sw1.example", "Gi1/0/7", "office desk 7", "192.0.2.2"))
        self.assertEqual((n["pvid"], n["vlans"], n["voice_vlan"]), (70, [70], 60))
        self.assertEqual(n["caps"], ["bridge", "router"])
        self.assertEqual(n["system"], "Example OS 1.0\nmore")

    def test_lldp_without_name_uses_the_chassis_mac(self):
        body = tlv(1, b"\x04" + SW) + tlv(2, b"\x03" + SW) + tlv(3, b"\x00\x78") + tlv(0, b"")
        n = inline.parse_lldp_frame(eth(bytes.fromhex("0180c200000e"), SW, 0x88CC, body))
        self.assertEqual((n["switch"], n["port"]), (inline.mac_str(SW), inline.mac_str(SW)))

    def test_cdp(self):
        n = inline.parse_cdp_frame(cdp_frame())
        self.assertEqual((n["via"], n["switch"], n["port"], n["mgmt"]),
                         ("CDPv2", "sw1.example", "GigabitEthernet0/7", "192.0.2.2"))
        self.assertEqual((n["pvid"], n["vlans"], n["voice_vlan"]), (70, [70], 60))
        self.assertEqual((n["platform"], n["system"]), ("cisco WS-C2960-24", "Example IOS 15.2"))
        self.assertEqual(n["caps"], ["switch", "igmp"])

    def test_garbage_is_not_a_neighbour(self):
        self.assertIsNone(inline.neighbour(client_frame(1)))
        self.assertIsNone(inline.neighbour(cdp_frame()[:22]))


class Dns(unittest.TestCase):
    def test_query_name(self):
        self.assertEqual(inline.parse_dns_query(dns_query("Updates.Example")), "updates.example")

    def test_answers_and_garbage(self):
        answer = bytearray(dns_query("a.example"))
        answer[2] |= 0x80
        self.assertIsNone(inline.parse_dns_query(bytes(answer)))
        self.assertIsNone(inline.parse_dns_query(b"\x00" * 5))
        self.assertIsNone(inline.parse_dns_query(dns_query("a.example")[:16]))


class Protocols(unittest.TestCase):
    def frame(self, proto, payload, src="192.0.2.57", dst="198.51.100.9"):
        return eth(SW, DEV, 0x0800, ipv4(src, dst, proto, payload))

    def test_buckets(self):
        cases = [
            (self.frame(6, tcp(50000, 443)), "HTTPS"),
            (self.frame(17, udp(50000, 443, b"q")), "HTTPS"),
            (self.frame(6, tcp(50000, 80)), "HTTP"),
            (self.frame(6, tcp(50000, 22)), "TCP"),
            (self.frame(17, udp(50000, 53, dns_query("a.example"))), "DNS"),
            (self.frame(17, udp(5353, 5353, b"m"), dst="224.0.0.251"), "chatter"),
            (self.frame(17, udp(50000, 1900, b"s"), dst="239.255.255.250"), "chatter"),
            (self.frame(17, udp(50000, 9999, b"u")), "UDP"),
            (self.frame(1, b"\x08\x00" + bytes(6)), "ICMP"),
            (client_frame(1), "DHCP"),
            (arp(DEV, "192.0.2.57", "192.0.2.254"), "ARP"),
            (lldp_frame(), "other"),
        ]
        for frame, want in cases:
            self.assertEqual(inline.protocol(frame), want, want)

    def test_ipv6(self):
        def v6(nh, payload):
            head = struct.pack("!IHBB16s16s", 6 << 28, len(payload), nh, 64,
                               socket.inet_pton(socket.AF_INET6, "2001:db8::57"),
                               socket.inet_pton(socket.AF_INET6, "2001:db8::1"))
            return eth(SW, DEV, 0x86DD, head + payload)
        self.assertEqual(inline.protocol(v6(17, udp(50000, 53, b"x"))), "DNS")
        self.assertEqual(inline.protocol(v6(6, tcp(50000, 443))), "HTTPS")
        self.assertEqual(inline.protocol(v6(58, b"\x87\x00" + bytes(22))), "IPv6")


# --- what is on port B ---------------------------------------------------------------------

class Classify(unittest.TestCase):
    def test_cable_device_switch(self):
        self.assertEqual(inline.classify([], OURS), "cable")
        self.assertEqual(inline.classify([eth(BCAST, A_MAC, 0x0800, bytes(40))], OURS), "cable")
        self.assertEqual(inline.classify([client_frame(1)], OURS), "device")
        self.assertEqual(inline.classify([client_frame(1), stp_frame()], OURS), "switch")

    def test_switch_frames(self):
        self.assertTrue(inline.switch_frame(stp_frame()))
        pvst = bytes(inline.PVST_DST) + SW + bytes(40)
        self.assertTrue(inline.switch_frame(pvst))
        self.assertTrue(inline.switch_frame(cdp_frame(pid=inline.SNAP_DTP)))
        self.assertTrue(inline.switch_frame(cdp_frame()))          # caps: switch
        self.assertTrue(inline.switch_frame(lldp_frame()))         # caps: bridge, router

    def test_phones_and_access_points_are_devices(self):
        phone_cdp = cdp_frame(src=DEV, caps=0x490, name=b"SEP00005E005301")  # host, phone, relay
        phone_lldp = lldp_frame(src=DEV, caps=0x0024)                        # bridge, telephone
        ap_lldp = lldp_frame(src=DEV, caps=0x000C)                           # bridge, wlan-ap
        for frame in (phone_cdp, phone_lldp, ap_lldp):
            self.assertFalse(inline.switch_frame(frame))
            self.assertEqual(inline.classify([frame], OURS), "device")

    def test_a_probe_that_did_not_come_back_means_a_quiet_device(self):
        self.assertEqual(inline.classify([], OURS, probe_heard=False), "device")
        self.assertEqual(inline.classify([], OURS, probe_heard=True), "cable")


class Oui(unittest.TestCase):
    IEEE = ["OUI/MA-L                                                    Organization\n",
            "00-00-5E   (hex)\t\tICANN, IANA Department\n",
            "00005E     (base 16)\t\tICANN, IANA Department\n"]
    ARPSCAN = ["# comment\n", "00005E\tICANN, IANA Department\n"]

    def test_both_formats(self):
        mac = inline.mac_str(DEV)
        self.assertEqual(inline.oui_lookup(mac, self.IEEE), "ICANN, IANA Department")
        self.assertEqual(inline.oui_lookup(mac, self.IEEE[2:]), "ICANN, IANA Department")
        self.assertEqual(inline.oui_lookup(mac, self.ARPSCAN), "ICANN, IANA Department")
        self.assertIsNone(inline.oui_lookup("00:11:22:33:44:55", self.IEEE))

    def test_file_missing_or_random_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "oui.txt")
            with open(path, "w") as f:
                f.writelines(self.IEEE)
            inline._makers.clear()
            self.assertEqual(inline.manufacturer("00:00:5e:00:53:99", (path,)), "ICANN, IANA Department")
            inline._makers.clear()
            self.assertIsNone(inline.manufacturer("00:00:5e:00:53:99", (os.path.join(tmp, "none"),)))
        self.assertTrue(inline.random_mac("02:00:5e:00:53:01"))
        self.assertIsNone(inline.manufacturer("da:a1:19:00:00:01"))


class SystemParsers(unittest.TestCase):
    def test_brforward(self):
        data = (inline.FDB_ENTRY.pack(DEV, 2, 0, 100, 0, 0, 0) +
                inline.FDB_ENTRY.pack(B_MAC, 2, 1, 0, 0, 0, 0) +
                inline.FDB_ENTRY.pack(SW, 1, 0, 5, 0, 0, 0))
        self.assertEqual(inline.parse_brforward(data),
                         [(DEV, 2, False), (B_MAC, 2, True), (SW, 1, False)])

    def test_addresses_and_routes(self):
        addr = json.dumps([{"ifname": "eth1", "addr_info": [
            {"family": "inet6", "local": "fe80::1", "scope": "link"}]}])
        self.assertFalse(inline.has_global_address(addr))
        addr = json.dumps([{"ifname": "eth1", "addr_info": [
            {"family": "inet", "local": "192.0.2.9", "scope": "global"}]}])
        self.assertTrue(inline.has_global_address(addr))
        routes = json.dumps([{"dst": "default", "gateway": "198.51.100.1", "dev": "wlan0"},
                             {"dst": "198.51.100.0/24", "dev": "wlan0"}])
        self.assertEqual(inline.routes_via(routes, ("eth0", "eth1")), [])
        self.assertEqual(inline.routes_via(json.dumps([{"dev": "eth0"}]), ("eth0", "eth1")), ["eth0"])
        self.assertEqual(inline.routes_via(None, ("eth0",)), [])

    def test_root_in_ram(self):
        plain = "/dev/mmcblk0p2 / ext4 rw 0 0\nproc /proc proc rw 0 0\n"
        overlay = plain + "overlayroot / overlay rw 0 0\n"
        self.assertFalse(inline.root_in_ram(plain))
        self.assertTrue(inline.root_in_ram(overlay))

    def test_restore_vlan(self):
        frame = eth(BCAST, DEV, 0x0800, bytes(30))
        aux = inline.AUXDATA.pack(inline.TP_STATUS_VLAN_VALID, len(frame), len(frame), 0, 0,
                                  (5 << 13) | 60, 0x8100)
        tagged = inline.restore_vlan(frame, aux)
        self.assertEqual(tagged[12:16], b"\x81\x00" + struct.pack("!H", (5 << 13) | 60))
        self.assertEqual(inline.l2(tagged)[4], 60)
        self.assertEqual(inline.restore_vlan(frame, None), frame)


@unittest.skipUnless(sys.platform.startswith("linux"), "socket filters are Linux")
class NeighbourFilter(unittest.TestCase):
    """The BPF program runs in the real kernel: a Unix datagram socket applies socket
    filters to what it receives, from offset 0, the same as a packet socket."""

    def passes(self, frame):
        rx, tx = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with rx, tx:
            inline.attach_filter(rx, inline.neighbour_filter())
            tx.send(frame)
            rx.setblocking(False)
            try:
                return rx.recv(4096) == frame
            except BlockingIOError:
                return False

    def test_only_lldp_and_cdp_pass(self):
        self.assertTrue(self.passes(lldp_frame()))
        self.assertTrue(self.passes(cdp_frame()))
        self.assertFalse(self.passes(stp_frame()))
        self.assertFalse(self.passes(client_frame(1)))
        self.assertFalse(self.passes(arp(DEV, "192.0.2.57", "192.0.2.254")))


# --- observer ------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.obs = inline.Observer(OURS, lookup=lambda mac: "Example Corp", clock=self.clock)

    def test_dhcp_exchange_as_it_passes(self):
        o = self.obs
        o.feed(client_frame(1, options=option(12, b"printer-7") + option(60, b"vendor-x")), "b")
        o.feed(server_frame(2, mac=DEV2), "a")             # an offer to someone else: ignored
        self.clock.t += 0.5
        o.feed(server_frame(2), "a")
        o.feed(client_frame(3, options=option(50, IP("192.0.2.57"))), "b")
        self.clock.t += 1.0
        o.feed(server_frame(5), "a")
        o.lookup_vendors()
        s = o.snapshot()
        d = s["device"]
        self.assertEqual((d["mac"], d["vendor"], d["hostname"], d["vendor_class"]),
                         (inline.mac_str(DEV), "Example Corp", "printer-7", "vendor-x"))
        self.assertEqual((d["ip"], d["ip_from"], d["prefix"]), ("192.0.2.57", "dhcp", 24))
        self.assertEqual(d["dhcp"]["state"], "ack")
        self.assertEqual((d["dhcp"]["server"], d["dhcp"]["router"], d["dhcp"]["lease"], d["dhcp"]["took"]),
                         ("192.0.2.1", "192.0.2.254", 86400, 1.5))
        self.assertEqual(d["dhcp"]["dns"], ["192.0.2.53", "192.0.2.54"])
        self.assertEqual(s["warnings"], [])
        self.assertEqual([h["mac"] for h in s["hosts"]], [inline.mac_str(DEV)])

    def test_unanswered_nak_and_two_servers(self):
        o = self.obs
        o.feed(client_frame(1), "b")
        self.assertEqual(o.snapshot()["device"]["dhcp"]["state"], "pending")
        self.clock.t += 11
        self.assertIn("fail", [w[0] for w in o.snapshot()["warnings"]])
        o.feed(server_frame(2, server="192.0.2.1"), "a")
        o.feed(server_frame(2, server="192.0.2.99", yiaddr="192.0.2.200"), "a")
        texts = [w[1] for w in o.snapshot()["warnings"]]
        self.assertTrue(any("2 DHCP servers" in t for t in texts))
        self.assertFalse(any("not answered" in t for t in texts))
        o.feed(server_frame(6, yiaddr="0.0.0.0", options=option(54, IP("192.0.2.1")) + option(56, b"no")), "a")
        s = o.snapshot()
        self.assertEqual(s["device"]["dhcp"]["state"], "nak")
        self.assertIn(("fail", "DHCP refused (NAK): no"), s["warnings"])

    def test_server_on_the_device_side(self):
        self.obs.feed(server_frame(2), "b")
        self.assertTrue(any("device side" in w[1] for w in self.obs.snapshot()["warnings"]))

    def test_silent_device_and_none_state(self):
        o = self.obs
        o.tick(fdb_b=[DEV])                                  # known only from the bridge table
        self.clock.t += 31
        s = o.snapshot()
        self.assertEqual(s["device"]["mac"], inline.mac_str(DEV))
        self.assertEqual(s["device"]["dhcp"], {"state": "none"})
        self.assertTrue(any("silent" in w[1] for w in s["warnings"]))
        o.feed(arp(DEV, "192.0.2.57", "192.0.2.254"), "b")
        s = o.snapshot()
        self.assertFalse(any("silent" in w[1] for w in s["warnings"]))
        self.assertEqual((s["device"]["ip"], s["device"]["ip_from"]), ("192.0.2.57", "arp"))

    def test_switch_neighbour_vlans_and_loop(self):
        o = self.obs
        o.feed(lldp_frame(), "a")
        o.feed(cdp_frame(), "a")
        o.feed(client_frame(1, vlan=60), "b")
        o.feed(eth(BCAST, SW, 0x0806, bytes(28), vlan=70), "a")
        s = o.snapshot()
        sw = s["switch"]
        self.assertEqual((sw["name"], sw["port"], sw["via"], sw["native_vlan"], sw["voice_vlan"]),
                         ("sw1.example", "Gi1/0/7", "LLDP", 70, 60))
        self.assertEqual(sw["platform"], "cisco WS-C2960-24")     # filled in from CDP
        self.assertEqual((s["device"]["vlans"], sw["tagged"]), ([60], [70]))
        self.assertFalse(o.loop)
        o.feed(lldp_frame(src=DEV, caps=0x0024), "b")             # a phone speaking LLDP
        self.assertFalse(o.loop)
        self.assertEqual(o.snapshot()["device"]["neighbour"]["caps"], ["bridge", "telephone"])
        o.feed(stp_frame(), "b")
        self.assertTrue(o.loop)

    def test_traffic(self):
        o = self.obs
        o.feed(eth(SW, DEV, 0x0800, ipv4("192.0.2.57", "198.51.100.9", 6, tcp(50000, 443, bytes(1000)))), "b")
        o.feed(eth(DEV, SW, 0x0800, ipv4("198.51.100.9", "192.0.2.57", 6, tcp(443, 50000, bytes(400)))), "a")
        o.feed(eth(SW, DEV, 0x0800, ipv4("192.0.2.57", "203.0.113.5", 6, tcp(50000, 80))), "b")
        for name in ("a.example", "b.example", "a.example"):
            o.feed(eth(SW, DEV, 0x0800, ipv4("192.0.2.57", "192.0.2.53", 17, udp(5000, 53, dns_query(name)))), "b")
        o.feed(arp(DEV, "192.0.2.57", "192.0.2.254"), "b")
        o.tick(now=1000.0, counters={"up": (100, 10000), "down": (200, 50000)})
        o.tick(now=1001.0, counters={"up": (150, 135000), "down": (300, 175000)})
        o.tick(now=1002.0, counters={"up": (150, 135000), "down": (300, 175000)})
        t = o.snapshot()["traffic"]
        self.assertEqual(t["history"], [(1000000, 1000000), (0, 0)])
        self.assertEqual((t["up_bps"], t["down_fps"], t["bcast_ps"]), (0, 0, 0))
        self.assertEqual(t["dns"], ["a.example", "b.example"])
        self.assertEqual(t["top"][0], ("198.51.100.9", 1054 + 454))
        self.assertIn(("203.0.113.5", 54), t["top"])
        self.assertTrue(t["protocols"]["HTTPS"] > t["protocols"]["HTTP"] > 0)
        self.assertIn("ARP", t["protocols"])
        self.assertNotIn("192.0.2.255", [a for a, _ in t["top"]])

    def test_storm_and_link_warnings(self):
        o = self.obs
        o.tick(now=1000.0, counters={"up": (0, 0), "down": (0, 0)},
               links={"a": {"speed": 1000, "duplex": "Full"}, "b": {"speed": 100, "duplex": "Half"}})
        for _ in range(600):
            o.feed(arp(DEV, "192.0.2.57", "192.0.2.254"), "b")
        o.tick(now=1001.0, counters={"up": (600, 36000), "down": (0, 0)})
        w = o.snapshot()["warnings"]
        self.assertIn(("warn", "speed differs: switch side 1G, device side 100M"), w)
        self.assertIn(("warn", "duplex differs: switch side Full, device side Half"), w)
        self.assertIn(("warn", "half duplex on the device side"), w)
        self.assertIn(("fail", "broadcast storm: 600 broadcasts/s"), w)

    def test_unplug_forgets_the_device(self):
        self.obs.feed(client_frame(1), "b")
        self.obs.reset_device()
        self.assertIsNone(self.obs.snapshot()["device"]["mac"])

    def test_snapshot_is_a_copy(self):
        self.obs.feed(client_frame(1), "b")
        s = self.obs.snapshot()
        s["device"]["dhcp"]["dns"].append("x")
        s["traffic"]["dns"].append("x")
        self.assertEqual(self.obs.snapshot()["device"]["dhcp"]["dns"], [])


# --- pcap ----------------------------------------------------------------------------------

class Pcap(unittest.TestCase):
    def test_format(self):
        head = inline.pcap_header()
        self.assertEqual(struct.unpack("<IHHiIII", head), (0xA1B2C3D4, 2, 4, 0, 0, 262144, 1))
        rec = inline.pcap_record(1700000000.25, b"abc", 70)
        self.assertEqual(struct.unpack("<IIII", rec[:16]), (1700000000, 250000, 3, 70))
        self.assertEqual(rec[16:], b"abc")

    def test_names_have_no_addresses(self):
        name = inline.capture_name(time.mktime((2026, 10, 10, 14, 25, 1, 0, 0, -1)), "00:00:5e:00:53:01", 2)
        self.assertEqual(name, "inline-20261010-142501-00005e005301-2.pcap")
        self.assertIn("unknown", inline.capture_name(0, None, 1))

    def test_oldest_first(self):
        files = [("c", 10, 3), ("a", 10, 1), ("b", 10, 2)]
        self.assertEqual(inline.oldest_first(files, 15), ["a", "b"])
        self.assertEqual(inline.oldest_first(files, 30), [])

    def test_writer_rolls_and_keeps_under_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "inline-old-unknown-1.pcap"), "wb") as f:
                f.write(bytes(70000))
            os.utime(os.path.join(tmp, "inline-old-unknown-1.pcap"), (1, 1))
            mac = {"v": None}
            w = inline.PcapWriter(tmp, time.time(), lambda: mac["v"], cap=4 << 16, parts=4)
            w.start()
            frame = eth(SW, DEV, 0x0800, bytes(1400))
            for i in range(20):
                w.put(1700000000 + i, frame)
            time.sleep(1.3)  # the writer names the file once the device is known
            mac["v"] = inline.mac_str(DEV)
            for i in range(300):
                w.put(1700000100 + i, frame)
                if i % 50 == 0:
                    time.sleep(0.05)
            w.stop()
            stats = w.stats()
            self.assertEqual(stats["frames"] + stats["dropped"], 320)
            self.assertIsNone(stats["error"])
            names = sorted(os.listdir(tmp))
            self.assertNotIn("inline-old-unknown-1.pcap", names)   # oldest went first
            self.assertLessEqual(w.kept(), 4 << 16)
            self.assertTrue(any("00005e005301" in n for n in names))
            with open(stats["file"], "rb") as f:
                self.assertEqual(f.read(24), inline.pcap_header())

    def test_full_queue_drops_instead_of_blocking(self):
        w = inline.PcapWriter("/nonexistent", 0, depth=100, queue_bytes=64 << 10)
        hold = threading.Event()
        w._thread = threading.Thread(target=hold.wait, daemon=True)  # alive, never drains
        w._thread.start()
        for _ in range(5):
            w.put(0, bytes(30000))               # GRO-sized frames: the bytes bound bites first
        self.assertEqual(w.stats()["dropped"], 3)
        hold.set()

    def test_a_dead_writer_queues_nothing(self):
        w = inline.PcapWriter("/nonexistent", 0)
        w.put(0, b"x" * 60)
        self.assertEqual((w.queue.qsize(), w.stats()["dropped"]), (0, 0))


# --- the session against a fake host -------------------------------------------------------

class FakeHost:
    """Records commands and plays their effect on a /sys and /proc tree in a temp dir."""

    def __init__(self, root):
        self.sys, self.proc = os.path.join(root, "sys"), os.path.join(root, "proc")
        self.cmds, self.fail = [], []
        self.addrs, self.routes, self.nm = {}, [], "flugato-test"
        for name, mac in (("eth0", A_MAC), ("eth1", B_MAC), ("wlan0", bytes(6))):
            self.iface(name, mac)
        os.makedirs(self.net("wlan0", "wireless"))
        with open(os.path.join(self.proc, "mounts"), "w") as f:
            f.write("overlayroot / overlay rw 0 0\n")

    def net(self, *parts):
        return os.path.join(self.sys, "class", "net", *parts)

    def v6(self, iface):
        return os.path.join(self.proc, "sys", "net", "ipv6", "conf", iface, "disable_ipv6")

    def write(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(value)

    def iface(self, name, mac):
        self.write(self.net(name, "address"), inline.mac_str(mac))
        self.write(self.net(name, "carrier"), "1")
        for stat in ("rx_packets", "rx_bytes", "tx_packets", "tx_bytes"):
            self.write(self.net(name, "statistics", stat), "0")
        self.write(self.v6(name), "0")

    def __call__(self, cmd, timeout=10):
        self.cmds.append(list(cmd))
        if any(cmd[:len(f)] == f for f in self.fail):
            return None
        br = inline.BRIDGE
        if cmd[:3] == ["ip", "link", "add"]:
            self.write(self.net(br, "bridge", "group_fwd_mask"),
                       hex(int(cmd[cmd.index("group_fwd_mask") + 1], 0)))
            self.write(self.v6(br), "0")
        elif cmd[:3] == ["ip", "link", "set"] and cmd[4:5] == ["master"]:
            os.symlink(self.net(br), self.net(cmd[3], "master"))
            self.write(self.net(cmd[3], "brport", "port_no"), "0x1" if cmd[3] == "eth0" else "0x2")
        elif cmd[:3] == ["ip", "link", "set"] and cmd[4:5] == ["nomaster"]:
            os.unlink(self.net(cmd[3], "master"))
        elif cmd[:3] == ["ip", "link", "del"]:
            shutil.rmtree(self.net(cmd[3]))
            for p in ("eth0", "eth1"):
                if os.path.islink(self.net(p, "master")):
                    os.unlink(self.net(p, "master"))
        elif cmd[:2] == ["ip", "-j"] and "addr" in cmd:
            return json.dumps([{"ifname": cmd[-1], "addr_info": self.addrs.get(cmd[-1], [])}])
        elif cmd[:2] == ["ip", "-j"] and "route" in cmd:
            return json.dumps(self.routes)
        elif cmd[:2] == ["nmcli", "-g"]:
            return self.nm + "\n"
        elif cmd[:3] == ["nmcli", "device", "disconnect"]:
            self.nm = ""
        elif cmd[-3:] == ["connection", "up", "flugato-test"]:
            self.nm = "flugato-test"
        return ""


class Session(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = FakeHost(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def session(self, **kw):
        kw.setdefault("listen", False)
        return inline.InlineSession("eth0", "eth1", run=self.host, sys_root=self.host.sys,
                                    proc_root=self.host.proc, lookup=lambda mac: None,
                                    link_info=lambda iface: {"speed": 1000, "duplex": "Full"},
                                    capture_dir=os.path.join(self.tmp, "cap"), tick=0.05, **kw)

    def wait(self, cond, seconds=3.0):
        end = time.monotonic() + seconds
        while not cond() and time.monotonic() < end:
            time.sleep(0.02)
        return cond()

    def assert_safe(self):
        """Nothing ever names the Wi-Fi port or changes a route."""
        for cmd in self.host.cmds:
            self.assertNotIn("wlan0", cmd)
            if "route" in cmd:
                self.assertIn("show", cmd)

    def test_start_snapshot_stop(self):
        s = self.session()
        s.start()
        self.assertEqual(s.state, "bridged")
        add = next(c for c in self.host.cmds if c[:3] == ["ip", "link", "add"])
        self.assertEqual(add[add.index("group_fwd_mask") + 1], "0x4008")
        self.assertEqual(add[add.index("stp_state") + 1], "0")
        self.assertIn(["nmcli", "device", "set", "eth0", "managed", "no"], self.host.cmds)
        self.assertIn(["lldpcli", "configure", "ports", "eth0,eth1", "lldp", "status", "rx-only"],
                      self.host.cmds)
        for p in ("eth0", "eth1", inline.BRIDGE):
            with open(self.host.v6(p)) as f:
                self.assertEqual(f.read(), "1")
        self.assertFalse(any("addr" in c and "add" in c for c in self.host.cmds))  # no address
        self.assertTrue(self.wait(lambda: s.snapshot()["switch"]["link"]["speed"] == 1000))
        snap = s.snapshot()
        self.assertEqual(snap["state"], "bridged")
        self.assertEqual(snap["capture"]["in_ram"], True)
        self.assertEqual(set(snap) >= {"state", "error", "switch", "device", "traffic",
                                       "capture", "warnings"}, True)
        s.stop()
        self.assertEqual(s.state, "stopped")
        self.assertFalse(os.path.exists(self.host.net(inline.BRIDGE)))
        self.assertIn(["nmcli", "device", "set", "eth0", "managed", "yes"], self.host.cmds)
        self.assertIn(["nmcli", "--wait", "0", "connection", "up", "flugato-test"], self.host.cmds)
        self.assertIn(["lldpcli", "configure", "ports", "eth0,eth1", "lldp", "status", "rx-and-tx"],
                      self.host.cmds)
        for p in ("eth0", "eth1"):
            with open(self.host.v6(p)) as f:
                self.assertEqual(f.read(), "0")
            self.assertFalse(os.path.islink(self.host.net(p, "master")))
        self.assertEqual(self.host.cmds[-1], ["ip", "link", "del", inline.BRIDGE])
        self.assert_safe()

    def test_start_and_stop_twice(self):
        s = self.session()
        s.start()
        s.start()
        self.assertEqual(sum(c[:3] == ["ip", "link", "add"] for c in self.host.cmds), 1)
        s.stop()
        n = len(self.host.cmds)
        s.stop()
        self.assertEqual(len(self.host.cmds), n)  # nothing left to undo
        self.session().stop()                     # a stop without a start
        self.assertEqual(len(self.host.cmds), n)

    def test_failure_half_way_undoes_everything(self):
        self.host.fail = [["ip", "link", "set", "eth1", "master"]]
        s = self.session()
        with self.assertRaises(inline.InlineError):
            s.start()
        self.assertEqual(s.state, "error")
        self.assertIn("failed", s.error)
        self.assertFalse(os.path.exists(self.host.net(inline.BRIDGE)))
        self.assertFalse(os.path.islink(self.host.net("eth0", "master")))
        self.assertIn(["nmcli", "device", "set", "eth0", "managed", "yes"], self.host.cmds)
        self.assert_safe()

    def test_interrupted_start_undoes_everything(self):
        host = self.host

        def interrupted(cmd, timeout=10):
            if cmd[:4] == ["ip", "link", "set", "eth1"] and "master" in cmd:
                raise KeyboardInterrupt
            return host(cmd, timeout)
        s = self.session()
        s.run = interrupted
        with self.assertRaises(KeyboardInterrupt):
            s.start()
        self.assertEqual(s.state, "error")
        self.assertFalse(os.path.exists(host.net(inline.BRIDGE)))
        self.assertIn(["nmcli", "device", "set", "eth0", "managed", "yes"], host.cmds)

    def test_failed_bridge_add_touches_nothing_else(self):
        self.host.fail = [["ip", "link", "add"]]
        with self.assertRaises(inline.InlineError):
            self.session().start()
        self.assertFalse(any(c[0] in ("nmcli", "lldpcli") and "set" in c or "configure" in c
                             for c in self.host.cmds))

    def test_leftover_bridge_from_a_crash(self):
        crashed = self.session()
        crashed.start()
        crashed._stop.set()  # as if the process died: no teardown
        crashed._thread.join(2)
        self.host.cmds.clear()
        self.assertEqual(inline.cleanup("eth0", "eth1", run=self.host, sys_root=self.host.sys,
                                        proc_root=self.host.proc)[-1], f"ip link del {inline.BRIDGE}")
        self.assertEqual(inline.cleanup("eth0", "eth1", run=self.host, sys_root=self.host.sys,
                                        proc_root=self.host.proc), [])
        crashed._unhook()
        crashed = self.session()  # crash again, then a new session starts over the leftover
        crashed.start()
        crashed._stop.set()
        crashed._thread.join(2)
        self.host.cmds.clear()
        s = self.session()
        s.start()
        dels = [i for i, c in enumerate(self.host.cmds) if c[:3] == ["ip", "link", "del"]]
        adds = [i for i, c in enumerate(self.host.cmds) if c[:3] == ["ip", "link", "add"]]
        self.assertTrue(dels and adds and dels[0] < adds[0])
        s.stop()
        crashed._unhook()

    def test_refuses_unsafe_ports(self):
        cases = [
            (("eth0", "wlan0"), "not a wired port"),
            (("eth0", "eth0"), "two different ports"),
            (("eth0", "eth9"), "not found"),
        ]
        for (a, b), text in cases:
            s = inline.InlineSession(a, b, run=self.host, sys_root=self.host.sys,
                                     proc_root=self.host.proc, listen=False)
            with self.assertRaisesRegex(inline.InlineError, text):
                s.start()
        self.host.addrs["eth1"] = [{"family": "inet", "local": "192.0.2.9", "scope": "global"}]
        with self.assertRaisesRegex(inline.InlineError, "management"):
            self.session().start()
        self.host.addrs.clear()
        self.host.routes = [{"dst": "default", "gateway": "192.0.2.1", "dev": "eth0"}]
        with self.assertRaisesRegex(inline.InlineError, "main-table"):
            self.session().start()
        self.assertFalse(any(c[:3] == ["ip", "link", "add"] for c in self.host.cmds))
        self.assert_safe()

    def test_cable_pulled_on_port_a_ends_the_session(self):
        s = self.session(grace_a=0.1)
        s.start()
        self.host.write(self.host.net("eth0", "carrier"), "0")
        self.assertTrue(self.wait(lambda: s.state == "stopped"))
        self.assertEqual(s.ended, "port A unplugged")
        self.assertFalse(os.path.exists(self.host.net(inline.BRIDGE)))
        s.stop()
        self.assert_safe()

    def test_device_unplugged_is_forgotten_but_bridge_stays(self):
        s = self.session(grace_b=60)
        s.start()
        s.observer.feed(client_frame(1), "b")
        self.host.write(self.host.net("eth1", "carrier"), "0")
        self.assertFalse(self.wait(lambda: s.snapshot()["device"]["mac"] is None, 1.0))
        s.stop()  # 3 s before forgetting is longer than this test waits; the bridge stayed
        self.assertEqual(s.state, "stopped")

    def test_switch_on_port_b_stops_at_once(self):
        s = self.session()
        s.start()
        s.observer.feed(stp_frame(), "b")
        self.assertTrue(self.wait(lambda: s.state == "error"))
        snap = s.snapshot()
        self.assertIn("loop", snap["error"])
        self.assertEqual(snap["warnings"][0][0], "fail")
        self.assertFalse(os.path.exists(self.host.net(inline.BRIDGE)))
        s.stop()

    def test_a_bug_in_the_watch_thread_takes_the_bridge_down(self):
        s = self.session()
        s.link_info = lambda iface: 1 / 0
        s.start()
        self.assertTrue(self.wait(lambda: s.state == "error"))
        self.assertIn("ZeroDivisionError", s.error)
        self.assertFalse(os.path.exists(self.host.net(inline.BRIDGE)))
        s.stop()

    def test_traffic_counters_from_port_b(self):
        s = self.session()
        s.start()
        self.wait(lambda: len(s.snapshot()["traffic"]["history"]) >= 1)
        for stat, value in (("rx_bytes", "1250000"), ("tx_bytes", "2500000")):
            self.host.write(self.host.net("eth1", "statistics", stat), value)
        self.assertTrue(self.wait(lambda: any(up for up, _ in s.snapshot()["traffic"]["history"])))
        s.stop()



class LinkColours(unittest.TestCase):
    def test_unknown_duplex_is_not_a_mismatch(self):
        from flugato import display
        full, nodup = {"speed": 1000, "duplex": "Full"}, {"speed": 1000, "duplex": None}
        self.assertFalse(display.links_differ(full, nodup))  # the USB adapter reports no duplex
        self.assertTrue(display.links_differ(full, {"speed": 100, "duplex": None}))
        self.assertTrue(display.links_differ(full, {"speed": 1000, "duplex": "Half"}))
        self.assertFalse(display.links_differ(full, {}))
        self.assertEqual(display.links_differ(full, nodup), bool(inline.link_warnings(full, nodup)))

if __name__ == "__main__":
    unittest.main()

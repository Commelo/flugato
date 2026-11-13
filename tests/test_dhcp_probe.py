"""DHCP probe wire-format tests, using documentation-only addresses."""

import socket
import struct
import unittest

from flugato import checks, dhcp_probe


MAC = bytes.fromhex("020000000001")
XID = 0x12345678


def option(code, value):
    return bytes((code, len(value))) + value


def offer_frame(vlan=None, xid=XID, message_type=2):
    options = b"".join((
        option(53, bytes((message_type,))),
        b"\x00",
        option(54, socket.inet_aton("192.0.2.1")),
        option(1, socket.inet_aton("255.255.255.0")),
        option(3, socket.inet_aton("192.0.2.254")),
        option(6, socket.inet_aton("192.0.2.53") + socket.inet_aton("192.0.2.54")),
        option(51, struct.pack("!I", 43200)),
        b"\xff",
    ))
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 2, 1, 6, 0, xid, 0, 0,
                        bytes(4), socket.inet_aton("192.0.2.57"), bytes(4), bytes(4),
                        MAC + bytes(10), bytes(64), bytes(128))
    payload = bootp + b"\x63\x82\x53\x63" + options
    udp = struct.pack("!HHHH", 67, 68, len(payload) + 8, 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, 0, 0, 64, 17, 0,
                     socket.inet_aton("192.0.2.1"), bytes((255, 255, 255, 255)))
    ethernet = bytes((255,) * 6) + MAC
    if vlan:
        ethernet += b"\x81\x00" + struct.pack("!H", vlan) + b"\x08\x00"
    else:
        ethernet += b"\x08\x00"
    return ethernet + ip + udp


class DhcpProbe(unittest.TestCase):
    def test_build_tagged_discover(self):
        frame = dhcp_probe.build_discover(MAC, XID, 70)
        self.assertEqual(frame[:6], bytes((255,) * 6))
        self.assertEqual(frame[6:12], MAC)
        self.assertEqual(frame[12:18], b"\x81\x00\x00\x46\x08\x00")
        ip = frame[18:38]
        self.assertEqual(dhcp_probe._checksum(ip), 0)
        self.assertEqual(ip[12:20], bytes(4) + bytes((255, 255, 255, 255)))
        self.assertEqual(struct.unpack("!HH", frame[38:42]), (68, 67))
        bootp = 46
        self.assertEqual(frame[bootp + 4:bootp + 8], struct.pack("!I", XID))
        self.assertEqual(frame[bootp + 10:bootp + 12], b"\x80\x00")
        self.assertEqual(frame[bootp + 28:bootp + 34], MAC)
        self.assertIn(b"\x35\x01\x01\x37\x05\x01\x03\x06\x33\x36\xff",
                      frame[bootp + 236:])

    def test_build_untagged_discover(self):
        self.assertEqual(dhcp_probe.build_discover(MAC, XID, None)[12:14], b"\x08\x00")
        self.assertEqual(dhcp_probe.build_discover(MAC, XID, 0)[12:14], b"\x08\x00")

    def test_parse_tagged_offer_and_options(self):
        result = dhcp_probe.parse_offer(offer_frame(70), XID, 70)
        self.assertEqual(result, {
            "kind": 2,
            "server": "192.0.2.1",
            "address": "192.0.2.57",
            "prefix": 24,
            "router": "192.0.2.254",
            "dns": ["192.0.2.53", "192.0.2.54"],
            "lease": 43200,
        })

    def test_parse_untagged_offer(self):
        self.assertEqual(dhcp_probe.parse_offer(offer_frame(), XID, 0)["address"],
                         "192.0.2.57")

    def test_parse_stripped_tag_from_auxdata(self):
        frame = offer_frame()
        aux = checks.AUXDATA.pack(checks.TP_STATUS_VLAN_VALID, len(frame), len(frame),
                                  0, 14, 70, 0x8100)
        self.assertEqual(dhcp_probe.parse_offer(frame, XID, 70, aux)["server"],
                         "192.0.2.1")

    def test_ignore_wrong_transaction_vlan_or_message(self):
        self.assertIsNone(dhcp_probe.parse_offer(offer_frame(70), XID + 1, 70))
        self.assertIsNone(dhcp_probe.parse_offer(offer_frame(70), XID, 71))
        self.assertIsNone(dhcp_probe.parse_offer(offer_frame(70, message_type=5), XID, 70))



class ProbeMac(unittest.TestCase):
    def test_probe_mac_is_local_unique_and_stable(self):
        from flugato.dhcp_probe import probe_mac
        port = "88:a2:9e:00:53:01"
        a, b = probe_mac(port, 100), probe_mac(port, 200)
        self.assertNotEqual(a, b)
        self.assertEqual(a, probe_mac(port, 100))
        self.assertEqual(a[0] & 0x03, 0x02)  # locally administered, unicast
        self.assertEqual(a[4:], (100).to_bytes(2, "big"))


if __name__ == "__main__":
    unittest.main()

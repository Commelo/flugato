"""Inline mode: the tester sits between a device and the switch, main port (A, eth0) to the
switch and second port (B, the USB adapter) to the device. Both ports go into a Linux
bridge with no address of its own, so the device keeps working while the tester watches
what passes: the device's MAC and maker, its DHCP exchange, its address, its DNS lookups,
the switch's LLDP/CDP, VLAN tags, link speeds, traffic, and a pcap of all of it.

What the bridge passes: every ordinary frame, plus LLDP and 802.1X (group_fwd_mask), so
the device still hears the switch's LLDP and can authenticate on an 802.1X port. CDP,
PVST+, DTP and VTP go to Cisco multicast addresses and are forwarded like any multicast.
802.1D spanning tree is forwarded too: the kernel passes BPDUs on whenever the bridge runs
no STP of its own, so loop protection on the switch (BPDU guard) sees the device as if
plugged in directly. What can't pass: LACP and pause frames (the kernel refuses those
bits), so a device that wants an 802.3ad bundle won't form one through the tester. And the
switch never sees the device's link go down: its port stays up on the tester.

Everything that crosses the bridge goes in or out of port B, so that is where the capture
listens: frames arriving on B went up (device -> switch), frames leaving B came down.
Port A has a second socket that only takes LLDP/CDP, so the switch shows even if the
bridge holds them back.

The parsers are pure functions over raw frames. Observer keeps what was seen, fed from the
capture thread; PcapWriter writes the files from a thread of its own; InlineSession builds
and tears down the bridge. Nothing here touches wlan0 or the main routing table: the
bridge never gets an address, and eth0's NetworkManager profile (flugato-test, table 100)
is released while bridged and brought back after."""

import atexit
import collections
import ctypes
import json
import os
import queue
import select
import signal
import socket
import struct
import sys
import threading
import time

from . import cable, checks
from .checks import (AUXDATA, ETH_P_ALL, PACKET_AUXDATA, PACKET_OUTGOING, SOL_PACKET,
                     TP_STATUS_VLAN_VALID, VLAN_TPIDS)
from .dhcp_probe import _address, _options, _prefix

BRIDGE = "flugato-br"
NM_PROFILE = "flugato-test"
# 802.1D link-local groups the bridge forwards, one bit per last address byte: 0x0E LLDP,
# 0x03 802.1X. Bits 0-2 (spanning tree, pause, LACP) are refused by the kernel.
GROUP_FWD_MASK = (1 << 0x0E) | (1 << 0x03)
CAPTURE_DIR = "/var/lib/flugato/captures"

BROADCAST = b"\xff" * 6
STP_DST = bytes.fromhex("0180c2000000")
PVST_DST = bytes.fromhex("01000ccccccd")
ETH_P_IP, ETH_P_ARP, ETH_P_IPV6, ETH_P_LLDP = 0x0800, 0x0806, 0x86DD, 0x88CC
SNAP_CDP, SNAP_VTP, SNAP_DTP = 0x2000, 0x2003, 0x2004
DHCP_MAGIC = b"\x63\x82\x53\x63"
DHCP_TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK",
              7: "RELEASE", 8: "INFORM"}
LLDP_CAPS = {0x01: "other", 0x02: "repeater", 0x04: "bridge", 0x08: "wlan-ap", 0x10: "router",
             0x20: "telephone", 0x40: "docsis", 0x80: "station"}
CDP_CAPS = {0x01: "router", 0x02: "trans-bridge", 0x04: "sr-bridge", 0x08: "switch",
            0x10: "host", 0x20: "igmp", 0x40: "repeater", 0x80: "phone", 0x100: "remote",
            0x200: "cvta", 0x400: "mac-relay"}
OUI_FILES = ("/usr/share/ieee-data/oui.txt", "/usr/share/arp-scan/ieee-oui.txt")
PROTOCOLS = ("ARP", "TCP", "UDP", "ICMP", "IPv6", "DNS", "DHCP", "HTTPS", "HTTP", "chatter",
             "other")
CHATTER_PORTS = {5353, 1900, 5355, 137, 138, 3702}  # mDNS, SSDP, LLMNR, NetBIOS, WS-Discovery


class InlineError(Exception):
    """Inline mode could not start; the text says why, short enough for the screen."""


# --- frames -------------------------------------------------------------------------------

def mac_str(mac):
    return ":".join(f"{b:02x}" for b in mac)


def l2(frame):
    """(dst, src, ethertype, payload offset, VLAN id) past any VLAN tag in the frame. An
    802.3 frame gives its length as the ethertype (below 0x600). None if too short."""
    if len(frame) < 14:
        return None
    off, vid = 12, None
    etype = int.from_bytes(frame[12:14], "big")
    while etype in VLAN_TPIDS and len(frame) >= off + 8:
        if vid is None:
            vid = (int.from_bytes(frame[off + 2:off + 4], "big") & 0x0FFF) or None
        off += 4
        etype = int.from_bytes(frame[off:off + 2], "big")
    return frame[:6], frame[6:12], etype, off + 2, vid


def restore_vlan(frame, aux):
    """Puts back the VLAN tag the NIC took off (handed over in the packet auxdata), so the
    pcap and the parsers see the frame as it was on the wire."""
    if aux and len(aux) >= AUXDATA.size and len(frame) >= 12:
        status, *_, tci, tpid = AUXDATA.unpack(aux[:AUXDATA.size])
        if status & TP_STATUS_VLAN_VALID:
            return frame[:12] + struct.pack("!HH", tpid or 0x8100, tci) + frame[12:]
    return frame


def snap_pid(frame, off):
    """Protocol id of a Cisco LLC/SNAP payload at `off` (CDP, VTP, DTP, PVST+), else None."""
    if frame[off:off + 6] == b"\xaa\xaa\x03\x00\x00\x0c" and len(frame) >= off + 8:
        return int.from_bytes(frame[off + 6:off + 8], "big")
    return None


def l3(frame, etype, off):
    """(version, protocol, src, dst, sport, dport, l4 offset) of an IPv4 or IPv6 packet at
    `off`; addresses as raw bytes, ports None when there are none. None if not IP."""
    if etype == ETH_P_IP and len(frame) >= off + 20 and frame[off] >> 4 == 4:
        ihl = (frame[off] & 0x0F) * 4
        proto, src, dst = frame[off + 9], frame[off + 12:off + 16], frame[off + 16:off + 20]
        l4 = off + ihl
        if int.from_bytes(frame[off + 6:off + 8], "big") & 0x1FFF:
            proto = None  # a later fragment: no transport header
        version = 4
    elif etype == ETH_P_IPV6 and len(frame) >= off + 40 and frame[off] >> 4 == 6:
        proto, src, dst, l4, version = frame[off + 6], frame[off + 8:off + 24], frame[off + 24:off + 40], off + 40, 6
    else:
        return None
    sport = dport = None
    if proto in (6, 17) and len(frame) >= l4 + 4:
        sport, dport = struct.unpack("!HH", frame[l4:l4 + 4])
    return version, proto, src, dst, sport, dport, l4


def protocol(frame):
    """The traffic-mix bucket of a frame (one of PROTOCOLS): the application when the port
    says it (DNS, DHCP, HTTPS, HTTP, discovery chatter), else the transport for IPv4
    (TCP, UDP, ICMP), IPv6 for the rest of IPv6, ARP, or other."""
    info = l2(frame)
    if not info:
        return "other"
    return _bucket(info[2], l3(frame, info[2], info[3]))


def _bucket(etype, ip):
    if etype == ETH_P_ARP:
        return "ARP"
    if not ip:
        return "other"
    version, proto, _, _, sport, dport, _ = ip
    ports = {sport, dport}
    if proto == 17 and ports & {67, 68, 546, 547}:
        return "DHCP"
    if proto in (6, 17) and 53 in ports:
        return "DNS"
    if proto == 17 and ports & CHATTER_PORTS:
        return "chatter"
    if proto in (6, 17) and 443 in ports:
        return "HTTPS"
    if proto == 6 and 80 in ports:
        return "HTTP"
    if version == 6:
        return "IPv6"
    return {6: "TCP", 17: "UDP", 1: "ICMP"}.get(proto, "other")


def _text(data):
    return data.decode("utf-8", "replace").strip("\x00 \r\n") or None


def _ip(data):
    return None if len(data) != 4 or data == bytes(4) else socket.inet_ntoa(data)


def ip_str(addr):
    return socket.inet_ntop(socket.AF_INET if len(addr) == 4 else socket.AF_INET6, addr)


def _caps(bits, names):
    return [name for bit, name in names.items() if bits & bit]


# --- LLDP / CDP ---------------------------------------------------------------------------

def _lldp_id(value, mac_subtype, addr_subtype):
    kind, body = value[0], value[1:]
    if kind == mac_subtype and len(body) == 6:
        return mac_str(body)
    if kind == addr_subtype and len(body) == 5 and body[0] == 1:
        return socket.inet_ntoa(body[1:])
    return _text(body)


def parse_lldp_frame(frame):
    """An LLDP frame -> the neighbour, shaped like checks.parse_lldp (via, switch, mgmt,
    port, descr, vlans, pvid) plus caps (enabled capabilities), system (description),
    platform and voice_vlan (LLDP-MED network policy). None if it isn't LLDP."""
    info = l2(frame)
    if not info or info[2] != ETH_P_LLDP:
        return None
    n = {"via": "LLDP", "switch": None, "mgmt": None, "port": None, "descr": None,
         "vlans": [], "pvid": None, "caps": [], "system": None, "platform": None,
         "voice_vlan": None}
    chassis, vlans, i = None, set(), info[3]
    while i + 2 <= len(frame):
        head = int.from_bytes(frame[i:i + 2], "big")
        kind, size = head >> 9, head & 0x1FF
        value = frame[i + 2:i + 2 + size]
        i += 2 + size
        if kind == 0 or len(value) < size:
            break
        if kind == 1 and value:
            chassis = _lldp_id(value, 4, 5)
        elif kind == 2 and value:
            n["port"] = _lldp_id(value, 3, 4)
        elif kind == 4:
            n["descr"] = _text(value)
        elif kind == 5:
            n["switch"] = _text(value)
        elif kind == 6:
            n["system"] = _text(value)
        elif kind == 7 and size >= 4:
            n["caps"] = _caps(int.from_bytes(value[2:4], "big"), LLDP_CAPS)
        elif kind == 8 and size >= 2 and not n["mgmt"]:
            addr = value[2:1 + value[0]]
            if value[1] == 1 and len(addr) == 4:
                n["mgmt"] = socket.inet_ntoa(addr)
        elif kind == 127 and size >= 6:
            oui, sub, body = value[:3], value[3], value[4:]
            if oui == b"\x00\x80\xc2" and sub == 1:      # 802.1 port VLAN id
                n["pvid"] = int.from_bytes(body[:2], "big") or None
            elif oui == b"\x00\x80\xc2" and sub == 3:    # 802.1 VLAN name
                vlans.add(int.from_bytes(body[:2], "big"))
            elif oui == b"\x00\x12\xbb" and sub == 2 and len(body) >= 4 and body[0] == 1:
                policy = int.from_bytes(body[1:4], "big")  # MED policy for voice: U T X vid
                if policy & 0x400000 and not n["voice_vlan"]:
                    n["voice_vlan"] = (policy >> 9) & 0x0FFF or None
    n["switch"] = n["switch"] or chassis
    if n["pvid"]:
        vlans.add(n["pvid"])
    n["vlans"] = sorted(v for v in vlans if v)
    return n


def _cdp_address(data):
    """First IPv4 address of a CDP address list."""
    if len(data) < 4:
        return None
    i = 4
    for _ in range(int.from_bytes(data[:4], "big")):
        if i + 2 > len(data):
            break
        kind, size = data[i], data[i + 1]
        proto = data[i + 2:i + 2 + size]
        i += 2 + size
        if i + 2 > len(data):
            break
        length = int.from_bytes(data[i:i + 2], "big")
        addr = data[i + 2:i + 2 + length]
        i += 2 + length
        if kind == 1 and proto == b"\xcc" and len(addr) == 4:
            return socket.inet_ntoa(addr)
    return None


def parse_cdp_frame(frame):
    """A Cisco CDP frame -> the neighbour, same shape as parse_lldp_frame. The native VLAN
    stands in for the PVID, and voice_vlan is the VoIP VLAN the switch offers phones."""
    info = l2(frame)
    if not info or info[2] >= 0x600 or snap_pid(frame, info[3]) != SNAP_CDP:
        return None
    i, end = info[3] + 8, min(len(frame), info[3] + info[2])
    if i + 4 > end:
        return None
    version = frame[i]
    i += 4
    fields = {}
    while i + 4 <= end:
        kind, size = struct.unpack("!HH", frame[i:i + 4])
        if size < 4:
            break
        fields.setdefault(kind, frame[i + 4:i + size])
        i += size
    caps = fields.get(0x04, b"")
    native = fields.get(0x0A, b"")
    native = int.from_bytes(native[:2], "big") if len(native) >= 2 else None
    voice = fields.get(0x0E, b"")
    software = _text(fields.get(0x05, b""))
    return {
        "via": f"CDPv{version}",
        "switch": _text(fields.get(0x01, b"")),
        "mgmt": _cdp_address(fields.get(0x16, b"")) or _cdp_address(fields.get(0x02, b"")),
        "port": _text(fields.get(0x03, b"")),
        "descr": None,
        "vlans": [native] if native else [],
        "pvid": native or None,
        "caps": _caps(int.from_bytes(caps[:4], "big"), CDP_CAPS) if len(caps) >= 4 else [],
        "system": software.splitlines()[0] if software else None,
        "platform": _text(fields.get(0x06, b"")),
        "voice_vlan": (int.from_bytes(voice[1:3], "big") or None) if len(voice) >= 3 else None,
    }


def neighbour(frame):
    return parse_lldp_frame(frame) or parse_cdp_frame(frame)


# --- DHCP / ARP / DNS ---------------------------------------------------------------------

def parse_dhcp(frame):
    """A DHCP message from a client or a server -> dict, or None. Client side: hostname
    (option 12), vendor_class (60), requested_ip (50), params (55). Server side: yiaddr,
    server (54), router, dns, lease, prefix, domain, and message (56, a NAK's reason)."""
    info = l2(frame)
    if not info or info[2] != ETH_P_IP:
        return None
    net = info[3]
    if len(frame) < net + 20 or frame[net] >> 4 != 4:
        return None
    ihl = (frame[net] & 0x0F) * 4
    if ihl < 20 or frame[net + 9] != 17 or int.from_bytes(frame[net + 6:net + 8], "big") & 0x1FFF:
        return None
    udp = net + ihl
    if len(frame) < udp + 8:
        return None
    sport, dport, length = struct.unpack("!HHH", frame[udp:udp + 6])
    if 67 not in (sport, dport) or not {sport, dport} <= {67, 68}:
        return None
    b, end = udp + 8, min(len(frame), udp + length)
    if end < b + 240 or frame[b] not in (1, 2) or frame[b + 236:b + 240] != DHCP_MAGIC:
        return None
    options = _options(frame[b + 240:end])
    if options is None:
        return None
    kind, dns, lease = options.get(53, b""), options.get(6, b""), options.get(51, b"")
    return {
        "op": "client" if frame[b] == 1 else "server",
        "type": (DHCP_TYPES.get(kind[0], f"type {kind[0]}") if len(kind) == 1 else "BOOTP"),
        "xid": int.from_bytes(frame[b + 4:b + 8], "big"),
        "mac": mac_str(frame[b + 28:b + 34]),
        "ciaddr": _ip(frame[b + 12:b + 16]),
        "yiaddr": _ip(frame[b + 16:b + 20]),
        "giaddr": _ip(frame[b + 24:b + 28]),
        "src_ip": _ip(frame[net + 12:net + 16]),
        "dst_ip": _ip(frame[net + 16:net + 20]),
        "hostname": _text(options.get(12, b"")),
        "vendor_class": _text(options.get(60, b"")),
        "requested_ip": _ip(options.get(50, b"")),
        "params": list(options.get(55, b"")),
        "server": _address(options.get(54, b"")),
        "router": _address(options.get(3, b"")[:4]),
        "dns": [_address(dns[i:i + 4]) for i in range(0, len(dns) - 3, 4)],
        "lease": int.from_bytes(lease, "big") if len(lease) == 4 else None,
        "prefix": _prefix(options.get(1, b"")),
        "domain": _text(options.get(15, b"")),
        "message": _text(options.get(56, b"")),
    }


def parse_arp(frame):
    """An Ethernet/IPv4 ARP frame -> {op, mac, ip, target_mac, target_ip}. ip is None for
    an ARP probe (sender 0.0.0.0, a device checking an address before taking it)."""
    info = l2(frame)
    if not info or info[2] != ETH_P_ARP or len(frame) < info[3] + 28:
        return None
    a = info[3]
    htype, ptype, hlen, plen, op = struct.unpack("!HHBBH", frame[a:a + 8])
    if htype != 1 or ptype != ETH_P_IP or hlen != 6 or plen != 4:
        return None
    return {"op": {1: "request", 2: "reply"}.get(op, str(op)),
            "mac": mac_str(frame[a + 8:a + 14]), "ip": _ip(frame[a + 14:a + 18]),
            "target_mac": mac_str(frame[a + 18:a + 24]), "target_ip": _ip(frame[a + 24:a + 28])}


def parse_dns_query(msg):
    """The name asked in a DNS query (a UDP payload), or None for an answer or garbage."""
    if len(msg) < 12 or msg[2] & 0x80 or int.from_bytes(msg[4:6], "big") < 1:
        return None
    labels, i = [], 12
    while i < len(msg):
        size = msg[i]
        if size == 0:
            name = ".".join(labels)
            return name.lower() if name else None
        if size & 0xC0 or i + 1 + size > len(msg) or len(labels) > 127:
            return None
        labels.append(msg[i + 1:i + 1 + size].decode("ascii", "replace"))
        i += 1 + size
    return None


# --- what is on port B --------------------------------------------------------------------

def switch_frame(frame):
    """True for frames only a switch sends: spanning tree (802.1D/RSTP/MST and Cisco
    PVST+), DTP, VTP, and LLDP/CDP from a box that calls itself a switch or bridge. Phones
    and access points speak LLDP/CDP too (a phone even calls itself a bridge), so those
    count as devices."""
    if frame[:6] in (STP_DST, PVST_DST):
        return True
    info = l2(frame)
    if not info:
        return False
    if info[2] < 0x600 and snap_pid(frame, info[3]) in (SNAP_VTP, SNAP_DTP):
        return True
    n = neighbour(frame)
    if not n:
        return False
    caps = set(n["caps"])
    if caps & {"phone", "telephone", "wlan-ap", "station", "host"}:
        return False
    return bool(caps & {"switch", "bridge"})


def classify(frames, ours, probe_heard=None):
    """What is on port B, from the frames that arrived there: "switch" when a switch speaks
    for itself (switch_frame), "device" when anyone else talks, "cable" when only our own
    ports do. Silence is ambiguous: it counts as "cable" unless a probe was sent from
    port A and did not come back on B (probe_heard False), which means a quiet device.
    Inline mode is port A linked to a switch and port B classified "device"."""
    foreign = False
    for frame in frames:
        if len(frame) < 14:
            continue
        if switch_frame(frame):
            return "switch"
        src = frame[6:12]
        if src not in ours and not src[0] & 1:
            foreign = True
    if foreign or (probe_heard is False):
        return "device"
    return "cable"


def listen_port_b(port_b, ours, seconds=3.0, probe_from=None):
    """Listens on port B and returns classify() of what arrived, or None when the port
    can't be opened. With probe_from (port A) three test frames go out of A while
    listening: hearing them on B means one cable between the ports, not hearing them
    means a quiet device rather than a cable."""
    try:
        # protocol 0 receives nothing until bound, so no Wi-Fi frame can look like a device
        rx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    except OSError:
        return None
    frames, probe_heard, tx, probe = [], None, None, None
    with rx:
        try:
            rx.bind((port_b, ETH_P_ALL))
            mreq = struct.pack("iHH8s", socket.if_nametoindex(port_b), checks.PACKET_MR_PROMISC, 0, b"")
            rx.setsockopt(SOL_PACKET, checks.PACKET_ADD_MEMBERSHIP, mreq)
        except OSError:
            return None
        if probe_from:
            try:
                tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(cable.ETH_P_TEST))
                tx.bind((probe_from, cable.ETH_P_TEST))
                probe = cable.test_frame(cable.mac_of(port_b), cable.mac_of(probe_from), 0, 60)
                probe_heard = False
            except OSError:
                if tx:
                    tx.close()
                tx = None
        try:
            start = time.monotonic()
            end, sends = start + seconds, [start, start + 0.5, start + 1.0]
            while time.monotonic() < end:
                if tx and sends and time.monotonic() >= sends[0]:
                    sends.pop(0)
                    try:
                        tx.send(probe)
                    except OSError:
                        pass
                rx.settimeout(max(0.05, min(0.25, end - time.monotonic())))
                try:
                    frame, addr = rx.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                if addr[2] == PACKET_OUTGOING or len(frame) < 14:
                    continue
                if probe and frame[:len(probe)] == probe:
                    probe_heard = True
                elif len(frames) < 500:
                    frames.append(frame)
        finally:
            if tx:
                tx.close()
    return classify(frames, ours, probe_heard)


# --- manufacturer -------------------------------------------------------------------------

def random_mac(mac):
    """Locally administered: phones and laptops make these up per network (private
    address), so no manufacturer can be looked up."""
    return bool(int(mac.replace(":", "")[:2], 16) & 0x02)


def oui_lookup(mac, lines):
    """Manufacturer of `mac` from IEEE oui.txt lines ("00-00-0C   (hex)\t\tCisco Systems,
    Inc", Debian ieee-data) or arp-scan's ieee-oui.txt ("00000C\tCisco Systems, Inc")."""
    prefix = mac.replace(":", "").replace("-", "")[:6].upper()
    dashed = "-".join(prefix[i:i + 2] for i in (0, 2, 4))
    for line in lines:
        if line.startswith(dashed) and "(hex)" in line:
            return line.split("(hex)", 1)[1].strip() or None
        if line[:6].upper() == prefix and line[6:7] in ("\t", " "):
            name = line[6:].strip()
            if name.startswith("(base 16)"):
                name = name[len("(base 16)"):].strip()
            return name or None
    return None


_makers = {}


def manufacturer(mac, paths=OUI_FILES):
    """oui_lookup() over the first OUI file present; None when there is none (the
    ieee-data package is optional) or the address is a random one."""
    if random_mac(mac):
        return None
    key = mac[:8].lower()
    if key not in _makers:
        _makers[key] = None
        for path in paths:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    _makers[key] = oui_lookup(mac, f)
                break
            except OSError:
                continue
    return _makers[key]


# --- system state parsers -----------------------------------------------------------------

FDB_ENTRY = struct.Struct("=6sBBIBBH")  # struct __fdb_entry in linux/if_bridge.h


def parse_brforward(data):
    """/sys/class/net/BRIDGE/brforward -> [(mac bytes, port number, is_local)]: every
    address the bridge has learned, and on which port."""
    entries = []
    for off in range(0, len(data) - FDB_ENTRY.size + 1, FDB_ENTRY.size):
        mac, port_no, local, _age, port_hi, _, _ = FDB_ENTRY.unpack_from(data, off)
        entries.append((mac, port_hi << 8 | port_no, bool(local)))
    return entries


def has_global_address(text):
    """`ip -j addr show dev X` -> True when X has an address beyond link-local."""
    try:
        links = json.loads(text or "[]")
    except ValueError:
        return False
    return any(a.get("scope") == "global" for link in links for a in link.get("addr_info", []))


def routes_via(text, ports):
    """`ip -j -4 route show table main` -> the ports among `ports` that carry routes there."""
    try:
        routes = json.loads(text or "[]")
    except ValueError:
        return []
    return sorted({r.get("dev") for r in routes} & set(ports))


def root_in_ram(mounts):
    """/proc/mounts -> True when / is an overlay (the read-only root of pi/readonly.sh):
    what is written then lives in RAM and is gone at reboot."""
    roots = [p[2] for p in (line.split() for line in mounts.splitlines())
             if len(p) >= 3 and p[1] == "/"]
    return bool(roots) and roots[-1] == "overlay"  # the last mount on / is the one in use


# --- capture filter for port A ------------------------------------------------------------
# Port A's socket only needs LLDP and CDP: a classic BPF filter drops the rest in the
# kernel. Port B takes everything (the capture).

_LDW, _LDH, _JEQ, _RET = 0x20, 0x28, 0x15, 0x06


def _assemble(rows):
    """[(code, jt, jf, k)] from rows of (code, k[, jt label, jf label]) and label strings."""
    labels, code = {}, []
    for row in rows:
        if isinstance(row, str):
            labels[row] = len(code)
        else:
            code.append(row)
    program = []
    for i, (op, k, *jumps) in enumerate(code):
        jt, jf = (labels[j] - i - 1 if j else 0 for j in (jumps or [None, None]))
        program.append((op, jt, jf, k))
    return program


def neighbour_filter():
    """Accepts frames to the LLDP (01:80:c2:00:00:0e) or Cisco (01:00:0c:cc:cc:cc) group."""
    return _assemble([(_LDW, 0), (_JEQ, 0x0180C200, "lldp", None), (_JEQ, 0x01000CCC, "cisco", "drop"),
                      "lldp", (_LDH, 4), (_JEQ, 0x000E, "accept", "drop"),
                      "cisco", (_LDH, 4), (_JEQ, 0xCCCC, "accept", "drop"),
                      "accept", (_RET, 0xFFFF), "drop", (_RET, 0)])


SO_ATTACH_FILTER, SO_RCVBUFFORCE, PACKET_STATISTICS = 26, 33, 6


def attach_filter(sock, program):
    blob = b"".join(struct.pack("HBBI", *ins) for ins in program)
    buf = ctypes.create_string_buffer(blob, len(blob))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER,
                    struct.pack("HL", len(program), ctypes.addressof(buf)))


# --- pcap ---------------------------------------------------------------------------------

PCAP_SNAPLEN = 262144


def pcap_header():
    """libpcap file header: microsecond timestamps, linktype 1 (Ethernet), full snaplen."""
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, PCAP_SNAPLEN, 1)


def pcap_record(ts, frame, length=None):
    sec = int(ts)
    return struct.pack("<IIII", sec, int((ts - sec) * 1e6), len(frame), length or len(frame)) + frame


def capture_name(started, mac, part):
    """inline-20261010-142501-0050569a1b2c-1.pcap: the session's start, the device's MAC
    (unknown until one is seen) and the part number. No addresses or names."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
    return f"inline-{stamp}-{mac.replace(':', '') if mac else 'unknown'}-{part}.pcap"


def oldest_first(files, budget):
    """[(path, size, mtime)] -> the paths to delete, oldest first, so the rest fit `budget`."""
    total = sum(size for _, size, _ in files)
    doomed = []
    for path, size, _ in sorted(files, key=lambda f: (f[2], f[0])):
        if total <= budget:
            break
        doomed.append(path)
        total -= size
    return doomed


class PcapWriter:
    """Writes frames to rolling pcap files from a thread of its own. put() never blocks:
    when the queue is full the frame is counted as dropped, so a slow SD card or a busy
    CPU can never hold the capture thread (the bridge itself runs in the kernel and
    doesn't wait for any of this). All pcap files in the directory together stay under
    `cap` bytes: before each new part the oldest files go, whichever session made them."""

    def __init__(self, directory, started, device_mac=lambda: None, cap=64 << 20, parts=4,
                 depth=4096, queue_bytes=16 << 20):
        self.dir, self.started, self.device_mac = directory, started, device_mac
        self.cap, self.part_size = cap, max(1 << 16, cap // parts)
        self.queue = queue.Queue(depth)
        self.queue_bytes, self._queued = queue_bytes, 0  # GRO hands over frames up to 64 KB
        self.lock = threading.Lock()
        self.file, self.bytes, self.frames, self.dropped, self.error = None, 0, 0, 0, None
        self._f, self._part, self._size, self._mac = None, 0, 0, None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="inline-pcap", daemon=True)
        self._thread.start()

    def put(self, ts, frame, length=None):
        if self.error or not (self._thread and self._thread.is_alive()):
            return  # the writer gave up; its error is the warning, not a drop count
        with self.lock:
            if self._queued + len(frame) > self.queue_bytes:
                self.dropped += 1
                return
            self._queued += len(frame)
        try:
            self.queue.put_nowait((ts, frame, length))
        except queue.Full:
            with self.lock:
                self._queued -= len(frame)
                self.dropped += 1

    def stop(self, wait=3.0):
        if self._thread and self._thread.is_alive():
            try:
                self.queue.put(None, timeout=wait)
            except queue.Full:
                pass
            self._thread.join(wait)

    def stats(self):
        with self.lock:
            return {"file": self.file, "bytes": self.bytes, "frames": self.frames,
                    "dropped": self.dropped, "error": self.error}

    def kept(self):
        """Bytes of pcap files in the directory now, from every session."""
        return sum(size for _, size, _ in self._files())

    def _files(self):
        found = []
        try:
            for name in os.listdir(self.dir):
                if name.endswith(".pcap"):
                    st = os.stat(os.path.join(self.dir, name))
                    found.append((os.path.join(self.dir, name), st.st_size, st.st_mtime))
        except OSError:
            pass
        return found

    def _open(self):
        self._close()
        os.makedirs(self.dir, mode=0o750, exist_ok=True)
        for path in oldest_first(self._files(), self.cap - self.part_size):
            os.remove(path)
        self._part += 1
        self._mac = self.device_mac()
        path = os.path.join(self.dir, capture_name(self.started, self._mac, self._part))
        self._f = open(path, "wb")
        self._f.write(pcap_header())
        self._size = 24
        with self.lock:
            self.file = path

    def _close(self):
        if self._f:
            self._f.close()
            self._f = None

    def _name_device(self):
        """The first part opens before the device has spoken: give it the MAC when known."""
        mac = self.device_mac()
        if self._f and not self._mac and mac:
            path = os.path.join(self.dir, capture_name(self.started, mac, self._part))
            os.rename(self.file, path)
            self._mac = mac
            with self.lock:
                self.file = path

    def _run(self):
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    item = self.queue.get(timeout=1.0)
                except queue.Empty:
                    item = False
                if item is None:
                    break
                if item:
                    ts, frame, length = item
                    with self.lock:
                        self._queued -= len(frame)
                    record = pcap_record(ts, frame, length)
                    if not self._f or self._size + len(record) > self.part_size:
                        self._open()
                    self._f.write(record)
                    self._size += len(record)
                    with self.lock:
                        self.bytes += len(record)
                        self.frames += 1
                if self._f and time.monotonic() - last_flush >= 1.0:
                    self._name_device()
                    self._f.flush()  # so a copy taken now holds what was seen so far
                    last_flush = time.monotonic()
        except OSError as e:
            with self.lock:
                self.error = e.strerror or str(e)
        finally:
            try:
                self._close()
            except OSError:
                pass


# --- observer -----------------------------------------------------------------------------

def _merge(found):
    """One neighbour from what LLDP and CDP said: LLDP first, CDP fills the gaps (the
    native and voice VLAN often come only from CDP)."""
    first, other = found.get("LLDP"), found.get("CDP")
    if not first:
        first, other = other, None
    if not first:
        return None
    n = dict(first)
    for k, v in (other or {}).items():
        if v and not n.get(k):
            n[k] = v
    return n


DHCP_STATES = {"DISCOVER": "pending", "REQUEST": "pending", "INFORM": "pending",
               "DECLINE": "pending", "OFFER": "offer", "ACK": "ack", "NAK": "nak",
               "RELEASE": "none"}


def link_warnings(a, b):
    """(level, text) for the two links: speeds or duplex that differ, half duplex."""
    out = []
    sa, sb = a.get("speed"), b.get("speed")
    if sa and sb and sa != sb:
        out.append(("warn", f"speed differs: switch side {checks.fmt_speed(sa)}, "
                            f"device side {checks.fmt_speed(sb)}"))
    da, db = a.get("duplex"), b.get("duplex")
    if da and db and da != db:
        out.append(("warn", f"duplex differs: switch side {da}, device side {db}"))
    for side, duplex in (("switch", da), ("device", db)):
        if duplex == "Half":
            out.append(("warn", f"half duplex on the {side} side"))
    return out


class Observer:
    """What passed through the bridge. The capture thread calls feed() per frame and tick()
    once a second; snapshot() can be called from any thread. Side "b" is what came from
    the device (up), side "a" what came from the switch (down). No I/O of its own."""
    MAX_HOSTS, MAX_EVENTS, MAX_TALKERS = 32, 12, 4096

    def __init__(self, ours=(), lookup=lambda mac: None, clock=time.monotonic,
                 dhcp_patience=10.0, silent_after=30.0, storm=500):
        self._lock = threading.Lock()
        self.ours = {bytes(m) for m in ours}
        self.lookup, self.clock = lookup, clock
        self.dhcp_patience, self.silent_after, self.storm = dhcp_patience, silent_after, storm
        self.start = clock()
        self.links = {"a": {}, "b": {}}
        self.counts = {"broadcast": 0, "multicast": 0}
        self.traffic = {"up_bps": None, "down_bps": None, "up_fps": None, "down_fps": None,
                        "bcast_ps": None, "mcast_ps": None}
        self.history = collections.deque(maxlen=60)
        self.protocols = dict.fromkeys(PROTOCOLS, 0)
        self.talkers = {}
        self.dns = []
        self._last = None
        self.switch, self.switch_vlans = {}, set()
        self.loop = False
        self.warnings = {}
        self.events = []
        self.reset_device()

    def reset_device(self):
        """Forgets the device side (port B unplugged): the next one starts afresh."""
        with self._lock:
            self.hosts = {}       # mac -> {first, ip, ip_from, vendor}; the first is the device
            self.clients = {}     # mac -> DHCP state
            self.device_n = {}    # the device's own LLDP/CDP (phones, access points)
            self.device_vlans = set()
            self.device_since = self.clock()
            self.spoke = False    # any DHCP or ARP from the device
            for key in ("servers", "server-b"):
                self.warnings.pop(key, None)

    def warn(self, key, level, text):
        with self._lock:
            self.warnings.setdefault(key, (level, text))

    def note(self, text):
        with self._lock:
            self._event(text)

    def device_mac(self):
        with self._lock:
            return next(iter(self.hosts), None)

    def _event(self, text):
        self.events.append((round(self.clock() - self.start, 1), text))
        del self.events[:-self.MAX_EVENTS]

    def _host(self, mac, now):
        if mac not in self.hosts and len(self.hosts) < self.MAX_HOSTS:
            self.hosts[mac] = {"first": now, "ip": None, "ip_from": None}
            if len(self.hosts) == 1:
                self._event(f"device {mac}")
        return self.hosts.get(mac)

    def _set_ip(self, mac, ip, source, weak=False):
        host = self._host(mac, self.clock())
        if host and not (weak and host["ip"]):
            host["ip"], host["ip_from"] = ip, source

    def feed_neighbour(self, frame):
        """LLDP/CDP heard on port A itself (its own filtered socket)."""
        n = neighbour(frame)
        if n:
            with self._lock:
                self.switch["LLDP" if n["via"] == "LLDP" else "CDP"] = n

    def feed(self, frame, side, length=None):
        """One frame seen on port B: side "b" arrived from the device, "a" was sent to it."""
        info = l2(frame)
        if not info:
            return
        dst, src, etype, off, vid = info
        ip = l3(frame, etype, off) if etype in (ETH_P_IP, ETH_P_IPV6) else None
        bucket = _bucket(etype, ip)
        now = self.clock()
        with self._lock:
            self.protocols[bucket] += length or len(frame)
            if dst == BROADCAST:
                self.counts["broadcast"] += 1
            elif dst[0] & 1:
                self.counts["multicast"] += 1
                if side == "b" and switch_frame(frame):
                    self.loop = True
            if vid:
                (self.device_vlans if side == "b" else self.switch_vlans).add(vid)
            if side == "b" and src not in self.ours and not src[0] & 1:
                self._host(mac_str(src), now)
            if ip and not dst[0] & 1:
                remote = ip[3] if side == "b" else ip[2]
                if remote in self.talkers or len(self.talkers) < self.MAX_TALKERS:
                    self.talkers[remote] = self.talkers.get(remote, 0) + (length or len(frame))
            if etype == ETH_P_ARP:
                arp = parse_arp(frame)
                if side == "b" and arp and arp["mac"] in self.hosts:
                    self.spoke = True
                    if arp["ip"]:
                        self._set_ip(arp["mac"], arp["ip"], "arp")
            elif bucket == "DHCP" and ip[0] == 4:
                dhcp = parse_dhcp(frame)
                if dhcp:
                    self._dhcp(dhcp, side, vid, now)
            elif bucket == "DNS" and side == "b" and ip[1] == 17 and ip[5] == 53:
                name = parse_dns_query(frame[ip[6] + 8:])
                if name:
                    if name in self.dns:
                        self.dns.remove(name)
                    self.dns.insert(0, name)
                    del self.dns[20:]
            elif etype == ETH_P_LLDP or etype < 0x600:
                n = neighbour(frame)
                if n:
                    found = self.switch if side == "a" else self.device_n
                    found["LLDP" if n["via"] == "LLDP" else "CDP"] = n
            if side == "b" and ip and ip[0] == 4 and mac_str(src) in self.hosts:
                source = _ip(ip[2])
                if source:
                    self._set_ip(mac_str(src), source, "ip", weak=True)

    def _dhcp(self, d, side, vid, now):
        mac = d["mac"]
        if d["op"] == "client":
            if side != "b":
                return
            self._host(mac, now)
            self.spoke = True
            c = self._client(mac)
            for key in ("hostname", "vendor_class", "requested_ip"):
                if d[key]:
                    c[key] = d[key]
            if d["type"] in ("DISCOVER", "REQUEST"):
                c["asks"] += 1
                c["waiting_since"] = c["waiting_since"] or now
                c["round"] = c["round"] or now
            c["vlan"] = vid
            self._event(f"{d['type']} {d['requested_ip'] or ''}".strip())
        else:
            if side == "b":
                self.warnings.setdefault("server-b", (
                    "fail", f"DHCP server on the device side: {d['server'] or d['src_ip']}"))
                return
            if mac not in self.clients and mac not in self.hosts:
                return  # an answer to someone else on the switch side
            c = self._client(mac)
            c["waiting_since"] = None
            server = d["server"] or d["src_ip"]
            if d["type"] == "OFFER":
                c["offers"][server] = d["yiaddr"]
                if len(c["offers"]) > 1:
                    self.warnings["servers"] = ("fail", f"{len(c['offers'])} DHCP servers answered: "
                                                + ", ".join(map(str, c["offers"])))
            if d["type"] in ("OFFER", "ACK", "NAK"):
                c["reply"] = {k: d[k] for k in ("type", "yiaddr", "server", "router", "dns",
                                                "lease", "prefix", "domain", "message", "giaddr")}
            if d["type"] in ("ACK", "NAK"):
                c["took"] = round(now - c["round"], 1) if c["round"] else None
                c["round"] = None
            if d["type"] == "ACK" and d["yiaddr"]:
                self._set_ip(mac, d["yiaddr"], "dhcp")
            self._event(" ".join(filter(None, (d["type"], d["yiaddr"], "from", server))))
        c["state"] = d["type"]

    def _client(self, mac):
        return self.clients.setdefault(mac, {
            "state": None, "hostname": None, "vendor_class": None, "requested_ip": None,
            "vlan": None, "asks": 0, "waiting_since": None, "round": None, "took": None,
            "offers": {}, "reply": None})

    def tick(self, now=None, carrier=None, counters=None, fdb_b=(), links=None):
        """Once a second from the capture thread: carrier {side: bool}; port B's interface
        counters {"up": (frames, bytes), "down": (frames, bytes)} (exact, unlike the
        captured frames, which can drop under load); MACs the bridge learned on port B;
        and every few seconds link info {side: cable.link()}."""
        now = self.clock() if now is None else now
        with self._lock:
            for mac in fdb_b:
                if bytes(mac) not in self.ours:
                    self._host(mac_str(mac), now)
            for side, up in (carrier or {}).items():
                self.links[side]["carrier"] = up
            for side, info in (links or {}).items():
                self.links[side].update(speed=info.get("speed"), duplex=info.get("duplex"))
            if counters:
                sample = (now, counters["up"], counters["down"], self.counts["broadcast"],
                          self.counts["multicast"])
                if self._last and now > self._last[0]:
                    dt = now - self._last[0]
                    per_s = lambda new, old: max(0, round((new - old) / dt))
                    t = self.traffic
                    t["up_fps"] = per_s(sample[1][0], self._last[1][0])
                    t["up_bps"] = per_s(sample[1][1] * 8, self._last[1][1] * 8)
                    t["down_fps"] = per_s(sample[2][0], self._last[2][0])
                    t["down_bps"] = per_s(sample[2][1] * 8, self._last[2][1] * 8)
                    t["bcast_ps"] = per_s(sample[3], self._last[3])
                    t["mcast_ps"] = per_s(sample[4], self._last[4])
                    self.history.append((t["up_bps"], t["down_bps"]))
                self._last = sample

    def lookup_vendors(self):
        """Manufacturer names for new hosts, read outside the lock (a file scan)."""
        with self._lock:
            todo = [m for m, h in self.hosts.items() if "vendor" not in h]
        found = {m: self.lookup(m) for m in todo}
        with self._lock:
            for m, vendor in found.items():
                if m in self.hosts:
                    self.hosts[m]["vendor"] = vendor

    def snapshot(self):
        """switch, device, traffic, warnings and a few extras (hosts, events, seconds)."""
        with self._lock:
            now = self.clock()
            mac = next(iter(self.hosts), None)
            n = _merge(self.switch)
            switch = {"name": None, "mgmt": None, "port": None, "descr": None, "via": None,
                      "native_vlan": None, "voice_vlan": None, "platform": None,
                      "tagged": sorted(self.switch_vlans),
                      "link": {k: self.links["a"].get(k) for k in ("speed", "duplex")}}
            if n:
                switch.update(name=n["switch"], mgmt=n["mgmt"], port=n["port"],
                              descr=n["descr"], via=n["via"], native_vlan=n["pvid"],
                              voice_vlan=n["voice_vlan"], platform=n["platform"])
            device = {"mac": mac, "vendor": None, "hostname": None, "vendor_class": None,
                      "ip": None, "prefix": None, "dhcp": None,
                      "link": {k: self.links["b"].get(k) for k in ("speed", "duplex")},
                      "vlans": sorted(self.device_vlans), "random_mac": False, "ip_from": None,
                      "neighbour": _merge(self.device_n), "hosts": len(self.hosts)}
            warnings = []
            silent = now - self.device_since >= self.silent_after
            if mac:
                host, c = self.hosts[mac], self.clients.get(mac)
                device.update(vendor=host.get("vendor"), ip=host["ip"], ip_from=host["ip_from"],
                              random_mac=random_mac(mac))
                if c:
                    reply = c["reply"] or {}
                    device.update(hostname=c["hostname"], vendor_class=c["vendor_class"])
                    if reply.get("type") == "ACK":
                        device["prefix"] = reply.get("prefix")
                    device["dhcp"] = {
                        "state": DHCP_STATES.get(c["state"], "pending"),
                        "server": reply.get("server"), "router": reply.get("router"),
                        "dns": list(reply.get("dns") or []), "lease": reply.get("lease"),
                        "took": c["took"], "offered": reply.get("yiaddr"),
                        "requested_ip": c["requested_ip"], "relay": reply.get("giaddr"),
                        "message": reply.get("message"), "asks": c["asks"],
                        "servers": list(c["offers"]), "vlan": c["vlan"]}
                    waited = now - c["waiting_since"] if c["waiting_since"] else 0
                    if waited >= self.dhcp_patience:
                        warnings.append(("fail", f"DHCP not answered: {c['asks']} requests "
                                                  f"in {round(waited)} s"))
                    if c["state"] == "NAK":
                        warnings.append(("fail", "DHCP refused (NAK)" + (
                            f": {reply['message']}" if reply.get("message") else "")))
                elif silent:
                    device["dhcp"] = {"state": "none"}
                if host["ip"] and host["ip"].startswith("169.254."):
                    warnings.append(("warn", "device has a link-local address: no DHCP lease"))
            if silent and not self.spoke:
                warnings.append(("warn", f"device silent: no DHCP or ARP in "
                                         f"{round(now - self.device_since)} s"))
            warnings = link_warnings(self.links["a"], self.links["b"]) + warnings
            bcast = self.traffic["bcast_ps"]
            if bcast is not None and bcast >= self.storm:
                warnings.append(("fail", f"broadcast storm: {bcast} broadcasts/s"))
            warnings += list(self.warnings.values())
            top = sorted(self.talkers.items(), key=lambda kv: -kv[1])[:5]
            return {
                "seconds": round(now - self.start),
                "switch": switch,
                "device": device,
                "traffic": dict(self.traffic, history=list(self.history),
                                protocols={k: v for k, v in self.protocols.items() if v},
                                top=[(ip_str(a), b) for a, b in top], dns=list(self.dns)),
                "warnings": warnings,
                "hosts": [{"mac": m, "vendor": h.get("vendor"), "ip": h["ip"]}
                          for m, h in self.hosts.items()],
                "events": list(self.events),
            }


# --- bridge -------------------------------------------------------------------------------

def _paths(sys_root, proc_root):
    net = lambda iface, *parts: os.path.join(sys_root, "class", "net", iface, *parts)
    v6 = lambda iface: os.path.join(proc_root, "sys", "net", "ipv6", "conf", iface, "disable_ipv6")
    return net, v6


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


def cleanup(port_a, port_b, run=checks.run, sys_root="/sys", proc_root="/proc", saved=None):
    """Takes inline mode apart, after a stop or a crash: ports out of the bridge, IPv6 back,
    lldpd talking again, eth0 back on its NetworkManager profile, and the bridge deleted
    last, so a crash half-way leaves it behind as the sign to run this again. Does nothing
    when no session ran (saved None) and no bridge is left. Returns what it did."""
    net, v6 = _paths(sys_root, proc_root)
    left = os.path.exists(net(BRIDGE))
    if saved is None and not left:
        return []
    done = []

    def do(cmd):
        done.append(" ".join(cmd))
        return run(cmd)

    for p in (port_a, port_b):
        if os.path.basename(os.path.realpath(net(p, "master"))) == BRIDGE:
            do(["ip", "link", "set", p, "nomaster"])
        value = (saved or {}).get(p) or "0"
        if _read(v6(p)) not in (None, value) and _write(v6(p), value):
            done.append(f"{p} disable_ipv6={value}")
    do(["ip", "link", "set", port_b, "up"])  # the app keeps the second port up, no address
    do(["lldpcli", "configure", "ports", f"{port_a},{port_b}", "lldp", "status", "rx-and-tx"])
    do(["nmcli", "device", "set", port_a, "managed", "yes"])
    do(["nmcli", "device", "set", port_a, "autoconnect", "yes"])  # undoes the disconnect
    current = (run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", port_a]) or "").strip()
    if current != NM_PROFILE and _read(net(port_a, "carrier")) == "1":
        do(["nmcli", "--wait", "0", "connection", "up", NM_PROFILE])
    if os.path.exists(net(BRIDGE)):
        do(["ip", "link", "del", BRIDGE])
    return done


class InlineSession:
    """Bridges port A (to the switch) and port B (to the device), watches the traffic and
    writes it to pcap files.

        s = InlineSession("eth0", "eth1")
        s.start()          # raises InlineError with a short reason
        s.snapshot()       # any thread, any time
        s.stop()           # always safe, also twice or without a start

    The session tears itself down at once when a switch shows up on port B (a loop:
    state "error"), and when port A has no link for grace_a seconds or port B for
    grace_b (state "stopped", reason in "ended"); a device that reboots drops its link
    for a while, hence the longer grace on B. start() registers stop() with atexit and,
    from the main thread, turns a SIGTERM into a clean exit if nothing else handles it.
    A crash that skips both leaves the bridge behind; the next start(), or cleanup(),
    removes it."""

    def __init__(self, port_a, port_b, run=checks.run, sys_root="/sys", proc_root="/proc",
                 link_info=cable.link, lookup=manufacturer, listen=True,
                 capture_dir=CAPTURE_DIR, capture_cap=64 << 20, capture_parts=4,
                 grace_a=5.0, grace_b=60.0, tick=1.0, storm=500):
        self.port_a, self.port_b, self.run = port_a, port_b, run
        self.sys_root, self.proc_root = sys_root, proc_root
        self._net, self._v6 = _paths(sys_root, proc_root)
        self.link_info, self.lookup, self.listen = link_info, lookup, listen
        self.capture_dir, self.capture_cap, self.capture_parts = capture_dir, capture_cap, capture_parts
        self.grace_a, self.grace_b, self.tick_s, self.storm = grace_a, grace_b, tick, storm
        self.state, self.error, self.ended = "stopped", None, None
        self.observer = Observer()
        self.writer = None
        self.kernel_dropped = 0
        self.in_ram = None
        self._ctl = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._saved = None
        self._sigterm = False
        self._kicking = False

    # --- control ------------------------------------------------------------------------

    def start(self):
        with self._ctl:
            if self.state == "bridged":
                return
            self._check_ports()
            if os.path.exists(self._net(BRIDGE)):  # left behind by a crash
                self._cleanup(None)
            a, b = self.port_a, self.port_b
            ours = [bytes.fromhex(m.replace(":", "")) for m in
                    (_read(self._net(p, "address")) for p in (a, b)) if m]
            self._saved = {p: _read(self._v6(p)) for p in (a, b)}
            self._stop = threading.Event()  # each run its own: an old thread stays stopped
            self.state, self.error, self.ended = "starting", None, None
            atexit.register(self.stop)  # before the first change, so an exit mid-way undoes it
            self._catch_sigterm()
            try:
                self._bridge_up()
            except BaseException as e:  # Ctrl-C or SIGTERM half-way too
                if not os.path.exists(self._net(BRIDGE)):
                    self._saved = None  # nothing was changed: nothing to undo
                self._cleanup(self._saved)
                self._saved = None
                self.state, self.error = "error", str(e) or e.__class__.__name__
                self._unhook()
                if not isinstance(e, Exception) or isinstance(e, InlineError):
                    raise
                raise InlineError(str(e)) from e
            self.observer = Observer(ours, self.lookup, storm=self.storm)
            self.kernel_dropped = 0
            self.in_ram = root_in_ram(_read(os.path.join(self.proc_root, "mounts")) or "")
            self.writer = None
            if self.listen and self.capture_dir:
                self.writer = PcapWriter(self.capture_dir, time.time(), self.observer.device_mac,
                                         self.capture_cap, self.capture_parts)
                self.writer.start()
            self._thread = threading.Thread(target=self._watch, name="inline", daemon=True,
                                            args=(self._stop, self.observer))
            self._thread.start()
            self.state = "bridged"

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(3)
        with self._ctl:
            self._cleanup(self._saved)
            self._saved = None
            if self.writer:
                self.writer.stop()
            if self.state in ("bridged", "starting"):
                self.state = "stopped"
            self._unhook()

    def snapshot(self):
        """The screen's view; see the module docs of the keys in the report. Every value
        may be None or empty until known."""
        snap = self.observer.snapshot()
        capture = {"file": None, "bytes": 0, "frames": 0, "dropped": self.kernel_dropped,
                   "dir": self.capture_dir, "kept": None, "cap": self.capture_cap,
                   "in_ram": self.in_ram, "error": None}
        writer = self.writer
        if writer:
            stats = writer.stats()
            capture.update(stats, dropped=stats["dropped"] + self.kernel_dropped,
                           kept=writer.kept())
        if capture["dropped"]:
            snap["warnings"].append(("warn", f"capture missed {capture['dropped']} frames"))
        if capture["error"]:
            snap["warnings"].append(("warn", f"capture stopped: {capture['error']}"))
        if self.observer.loop:
            snap["warnings"].insert(0, ("fail", "a switch answered on the device port: "
                                                "bridge stopped (loop risk)"))
        snap.update(state=self.state, error=self.error, ended=self.ended, capture=capture,
                    ports={"a": self.port_a, "b": self.port_b})
        return snap

    def kick_device(self, down=3.0, wait=10.0):
        """Takes port B's link down and up so the device starts over: most devices ask DHCP
        again on link up, so the exchange shows in the snapshot. Blocks for `down` seconds
        plus the time the link takes to come back (at most `wait`)."""
        with self._ctl:
            if self.state != "bridged":
                return False
            self._kicking = True
        try:
            self.observer.note("port B link down by hand")
            self.run(["ip", "link", "set", self.port_b, "down"])
            time.sleep(down)
            self.run(["ip", "link", "set", self.port_b, "up"])
            end = time.monotonic() + wait
            while _read(self._net(self.port_b, "carrier")) != "1" and time.monotonic() < end:
                time.sleep(0.2)
        finally:
            self._kicking = False
        return True

    # --- bridge -------------------------------------------------------------------------

    def _check_ports(self):
        a, b = self.port_a, self.port_b
        if not a or not b or a == b:
            raise InlineError("needs two different ports")
        for p in (a, b):
            if not os.path.exists(self._net(p)):
                raise InlineError(f"{p} not found")
            if (p in ("lo", BRIDGE) or os.path.exists(self._net(p, "wireless"))
                    or os.path.exists(self._net(p, "phy80211"))):
                raise InlineError(f"{p} is not a wired port")
            master = os.path.basename(os.path.realpath(self._net(p, "master")))
            if os.path.islink(self._net(p, "master")) and master != BRIDGE:
                raise InlineError(f"{p} already belongs to {master}")
        if has_global_address(self.run(["ip", "-j", "addr", "show", "dev", b])):
            raise InlineError(f"{b} has an address: management port?")
        used = routes_via(self.run(["ip", "-j", "-4", "route", "show", "table", "main"]), (a, b))
        if used:
            raise InlineError(f"{', '.join(used)} carries main-table routes")

    def _must(self, cmd):
        if self.run(cmd) is None:
            raise InlineError(" ".join(cmd[:5]) + " failed")

    def _bridge_up(self):
        """The bridge comes first: from here on its presence marks a session to undo."""
        a, b, run = self.port_a, self.port_b, self.run
        self._must(["ip", "link", "add", "name", BRIDGE, "type", "bridge", "stp_state", "0",
                    "mcast_snooping", "0", "group_fwd_mask", hex(GROUP_FWD_MASK)])
        _write(self._v6(BRIDGE), "1")  # no link-local address, no router solicitations
        run(["nmcli", "device", "set", BRIDGE, "managed", "no"])
        # lldpd would announce the tester to both sides: listen only while inline
        run(["lldpcli", "configure", "ports", f"{a},{b}", "lldp", "status", "rx-only"])
        run(["nmcli", "device", "disconnect", a])
        if (run(["nmcli", "device", "set", a, "managed", "no"]) is None
                and run(["nmcli", "--version"]) is not None):
            raise InlineError(f"NetworkManager won't let go of {a}")
        run(["ip", "addr", "flush", "dev", a])
        for p in (a, b):
            _write(self._v6(p), "1")
        for p in (a, b):
            self._must(["ip", "link", "set", p, "master", BRIDGE])
        for p in (a, b, BRIDGE):
            self._must(["ip", "link", "set", p, "up"])
        mask = _read(self._net(BRIDGE, "bridge", "group_fwd_mask"))
        if mask and int(mask, 0) != GROUP_FWD_MASK:
            raise InlineError(f"bridge group_fwd_mask is {mask}")

    def _cleanup(self, saved):
        cleanup(self.port_a, self.port_b, self.run, self.sys_root, self.proc_root, saved)

    def _catch_sigterm(self):
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
                signal.signal(signal.SIGTERM, _exit_on_term)
                self._sigterm = True
        except (ValueError, OSError):
            pass

    def _unhook(self):
        atexit.unregister(self.stop)
        if self._sigterm and threading.current_thread() is threading.main_thread():
            try:
                if signal.getsignal(signal.SIGTERM) is _exit_on_term:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
            self._sigterm = False

    def _end(self, reason, error=False, stop=None):
        """Teardown from the capture thread: a cable pulled, or a loop."""
        with self._ctl:
            if stop is not self._stop or stop.is_set() or self.state != "bridged":
                return  # stopped already, or a thread left over from an earlier run
            self._stop.set()
            self._cleanup(self._saved)
            self._saved = None
            if self.writer:
                self.writer.stop()
            self.state, self.ended = ("error" if error else "stopped"), reason
            if error:
                self.error = reason
            atexit.unregister(self.stop)

    # --- capture ------------------------------------------------------------------------

    def _open_socket(self, iface, program=None):
        sock = None
        try:
            # protocol 0 receives nothing until bind: no unfiltered frame slips in
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
            if program:
                attach_filter(sock, program)
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, 4 << 20)
            except OSError:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            sock.bind((iface, ETH_P_ALL))
            sock.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
            sock.setblocking(False)
            return sock
        except OSError as e:
            if sock:
                sock.close()
            self.observer.warn(f"capture-{iface}", "warn", f"cannot watch {iface}: {e.strerror or e}")
            return None

    def _drain(self, sock, port, buf, obs):
        view = memoryview(buf)
        for _ in range(512):
            try:
                size, anc, flags, addr = sock.recvmsg_into([buf], socket.CMSG_SPACE(AUXDATA.size))
            except OSError:
                return
            aux = next((d for lvl, typ, d in anc if lvl == SOL_PACKET and typ == PACKET_AUXDATA), None)
            frame = restore_vlan(bytes(view[:size]), aux)
            if port == "a":
                if addr[2] != PACKET_OUTGOING:  # leaving A is the device's own LLDP/CDP
                    obs.feed_neighbour(frame)
                continue
            length = AUXDATA.unpack(aux[:AUXDATA.size])[1] if aux and len(aux) >= AUXDATA.size else None
            length = max(length or 0, len(frame))
            if self.writer:
                self.writer.put(time.time(), frame, length)
            # leaving port B = came down from the switch, arriving = came up from the device
            obs.feed(frame, "a" if addr[2] == PACKET_OUTGOING else "b", length)

    def _kernel_drops(self, sock):
        try:
            _, drops = struct.unpack("II", sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
            self.kernel_dropped += drops  # reading resets the kernel's counters
        except OSError:
            pass

    def _counters(self, iface):
        names = ("rx_packets", "rx_bytes", "tx_packets", "tx_bytes")
        v = [_read(self._net(iface, "statistics", n)) for n in names]
        v = [int(x) if x and x.isdigit() else 0 for x in v]
        return {"up": (v[0], v[1]), "down": (v[2], v[3])}

    def _fdb_b(self):
        try:
            with open(self._net(BRIDGE, "brforward"), "rb") as f:
                data = f.read()
            port = int(_read(self._net(self.port_b, "brport", "port_no")) or "", 16)
        except (OSError, ValueError):
            return []
        return [mac for mac, p, local in parse_brforward(data) if p == port and not local]

    def _watch(self, stop, obs):
        try:
            self._watch_loop(stop, obs)
        except Exception as e:  # a bug here must not leave a bridge up with nobody watching
            self._end(f"inline watch failed: {e.__class__.__name__}: {e}", True, stop)

    def _watch_loop(self, stop, obs):
        a, b = self.port_a, self.port_b
        socks = {}
        if self.listen:
            for port, iface, program in (("b", b, None), ("a", a, neighbour_filter())):
                sock = self._open_socket(iface, program)
                if sock:
                    socks[sock] = port
        buf = bytearray(65536)
        lost, cleared, next_tick, next_link = {"a": None, "b": None}, False, 0.0, 0.0
        try:
            while not stop.is_set():
                if socks:
                    for sock in select.select(list(socks), [], [], self.tick_s / 2)[0]:
                        self._drain(sock, socks[sock], buf, obs)
                else:
                    stop.wait(self.tick_s / 2)
                if obs.loop:
                    self._end("a switch answered on the device port (loop risk)", True, stop)
                    return
                now = time.monotonic()
                if now < next_tick:
                    continue
                next_tick = now + self.tick_s
                for sock, port in socks.items():
                    if port == "b" and stop is self._stop:
                        self._kernel_drops(sock)
                carrier = {"a": _read(self._net(a, "carrier")) == "1",
                           "b": _read(self._net(b, "carrier")) == "1"}
                links = None
                if now >= next_link:
                    links, next_link = {"a": self.link_info(a), "b": self.link_info(b)}, now + 5
                obs.tick(now, carrier, self._counters(b), self._fdb_b(), links)
                obs.lookup_vendors()
                for side in "ab":
                    if carrier[side] or (side == "b" and self._kicking):
                        lost[side] = None
                    elif lost[side] is None:
                        lost[side] = now
                if carrier["b"]:
                    cleared = False
                elif lost["b"] and now - lost["b"] >= 3 and not cleared:
                    obs.reset_device()  # another device may come next
                    obs.note("device unplugged")
                    cleared = True
                for side, grace in (("a", self.grace_a), ("b", self.grace_b)):
                    if lost[side] is not None and now - lost[side] >= grace:
                        self._end(f"port {side.upper()} unplugged", False, stop)
                        return
        finally:
            for sock in socks:
                sock.close()


def _exit_on_term(*_):
    sys.exit(0)  # atexit runs, and with it InlineSession.stop


# --- by hand ------------------------------------------------------------------------------

def main(argv=None):
    import argparse
    from . import config
    ap = argparse.ArgumentParser(prog="python3 -m flugato.inline",
                                 description="Inline mode by hand; stop the flugato service first.")
    ap.add_argument("action", choices=("classify", "watch", "cleanup"))
    ap.add_argument("--config", default=None, help="settings file (default: config.path())")
    ap.add_argument("--seconds", type=float, default=0, help="watch: stop after this long")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    a = cfg["IFACE"] or checks.detect_iface()
    b = cable.second_port(a, cfg.get("CABLE_IFACE", ""))
    if not b:
        sys.exit("no second port")
    if args.action == "cleanup":
        print("\n".join(cleanup(a, b)) or "nothing to clean up")
        return
    if args.action == "classify":
        print(listen_port_b(b, {cable.mac_of(a), cable.mac_of(b)}, probe_from=a))
        return
    session = InlineSession(a, b)
    try:
        session.start()
    except InlineError as e:
        sys.exit(f"inline mode: {e}")
    end = time.monotonic() + args.seconds if args.seconds else None
    try:
        while session.state == "bridged" and (end is None or time.monotonic() < end):
            print(json.dumps(session.snapshot(), default=str), flush=True)
            time.sleep(2)
        print(json.dumps(session.snapshot(), default=str), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()


if __name__ == "__main__":
    main()

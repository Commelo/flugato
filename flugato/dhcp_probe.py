"""Probe DHCP on an Ethernet VLAN without configuring an interface or taking a lease."""

import os
import socket
import struct
import time

from .checks import (AUXDATA, ETH_P_ALL, PACKET_ADD_MEMBERSHIP, PACKET_AUXDATA,
                     PACKET_MR_PROMISC, PACKET_OUTGOING, SOL_PACKET, TP_STATUS_VLAN_VALID,
                     frame_vlan)


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _mac_bytes(mac):
    if isinstance(mac, str):
        try:
            mac = bytes.fromhex(mac.replace(":", ""))
        except ValueError as e:
            raise ValueError("bad MAC address") from e
    if len(mac) != 6:
        raise ValueError("bad MAC address")
    return bytes(mac)


def _vlan_id(vlan):
    if vlan is None or vlan == 0:
        return None
    if not isinstance(vlan, int) or not 1 <= vlan <= 4094:
        raise ValueError("VLAN must be 0 through 4094")
    return vlan


BROADCAST_MAC = bytes((255,) * 6)
DISCOVER_OPTIONS = b"\x35\x01\x01\x37\x05\x01\x03\x06\x33\x36\xff"


def build_message(mac, xid, options, vlan=None, ciaddr=bytes(4), dst_mac=BROADCAST_MAC,
                  src_ip=bytes(4), dst_ip=bytes((255, 255, 255, 255)), flags=0x8000):
    """An Ethernet/IP/UDP BOOTP request frame from `mac` with the DHCP `options` (the magic
    cookie is added here), broadcast unless told otherwise; the probe's DISCOVER and the
    terminal app's REQUEST and RELEASE are all made here."""
    mac = _mac_bytes(mac)
    vlan = _vlan_id(vlan)
    if not isinstance(xid, int) or not 0 <= xid <= 0xFFFFFFFF:
        raise ValueError("xid must be a 32-bit integer")

    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0,
                        flags, ciaddr, bytes(4), bytes(4), bytes(4),
                        mac + bytes(10), bytes(64), bytes(128))
    payload = (bootp + b"\x63\x82\x53\x63" + options).ljust(300, b"\0")
    udp = struct.pack("!HHHH", 68, 67, 8 + len(payload), 0) + payload

    total = 20 + len(udp)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 17, 0, src_ip, dst_ip)
    ip = ip[:10] + struct.pack("!H", _checksum(ip)) + ip[12:]
    ethernet = dst_mac + mac
    if vlan is None:
        ethernet += b"\x08\x00"
    else:
        ethernet += b"\x81\x00" + struct.pack("!H", vlan) + b"\x08\x00"
    return ethernet + ip + udp


def build_discover(mac, xid, vlan=None):
    """Build an Ethernet DHCPDISCOVER frame for `mac`, transaction `xid`, and VLAN."""
    return build_message(mac, xid, DISCOVER_OPTIONS, vlan)


def _options(data):
    options = {}
    i = 0
    while i < len(data):
        code = data[i]
        i += 1
        if code == 0:
            continue
        if code == 255:
            break
        if i >= len(data):
            return None
        size = data[i]
        i += 1
        if i + size > len(data):
            return None
        options.setdefault(code, data[i:i + size])
        i += size
    return options


def _address(data):
    return socket.inet_ntoa(data) if len(data) == 4 else None


def _prefix(mask):
    if len(mask) != 4:
        return None
    value = int.from_bytes(mask, "big")
    inverse = value ^ 0xFFFFFFFF
    if inverse & (inverse + 1):
        return None
    return value.bit_count()


def parse_offer(frame, xid, vlan=None, aux=None, kinds=(2,)):
    """Parse a matching DHCPOFFER frame, returning its useful lease details or None.
    `kinds`: the DHCP message types accepted (2 = offer; the terminal app's client also takes
    5 = ack and 6 = nak); the type found is returned as "kind"."""
    vlan = _vlan_id(vlan)
    if frame_vlan(frame, aux) != vlan or len(frame) < 14:
        return None
    tagged = int.from_bytes(frame[12:14], "big") in (0x8100, 0x88A8)
    network = 18 if tagged else 14
    if len(frame) < network + 20 or frame[network] >> 4 != 4:
        return None
    ihl = (frame[network] & 0x0F) * 4
    if ihl < 20 or len(frame) < network + ihl + 8 or frame[network + 9] != 17:
        return None
    udp = network + ihl
    source, dest, length, _ = struct.unpack("!HHHH", frame[udp:udp + 8])
    if source != 67 or dest != 68 or length < 8 + 240:
        return None
    bootp = udp + 8
    if len(frame) < bootp + length - 8 or frame[bootp] != 2:
        return None
    if int.from_bytes(frame[bootp + 4:bootp + 8], "big") != xid:
        return None
    if frame[bootp + 236:bootp + 240] != b"\x63\x82\x53\x63":
        return None
    options = _options(frame[bootp + 240:bootp + length - 8])
    if options is None or options.get(53) not in {bytes((k,)) for k in kinds}:
        return None

    dns = options.get(6, b"")
    return {
        "kind": options[53][0],
        "server": _address(options.get(54, b"")),
        "address": _address(frame[bootp + 16:bootp + 20]),
        "prefix": _prefix(options.get(1, b"")),
        "router": _address(options.get(3, b"")[:4]),
        "dns": [_address(dns[i:i + 4]) for i in range(0, len(dns) - 3, 4)],
        "lease": int.from_bytes(options[51], "big") if len(options.get(51, b"")) == 4 else None,
    }


def dhcp_offer(iface, vlan, timeout=3.0):
    """Send DHCPDISCOVER on `iface` and return the first matching offer, without requesting it."""
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            mac = _mac_bytes(f.read().strip())
        xid = int.from_bytes(os.urandom(4), "big")
        discover = build_discover(mac, xid, vlan)
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)  # bound below, to iface only
    except OSError:
        return None
    with sock:
        try:
            sock.bind((iface, ETH_P_ALL))
            sock.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
            sock.send(discover)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                sock.settimeout(max(0.01, end - time.monotonic()))
                frame, anc, _, addr = sock.recvmsg(4096, socket.CMSG_SPACE(AUXDATA.size))
                if addr[2] == PACKET_OUTGOING:
                    continue
                aux = next((data for level, kind, data in anc
                            if level == SOL_PACKET and kind == PACKET_AUXDATA), None)
                offer = parse_offer(frame, xid, vlan, aux)
                if offer:
                    return offer
        except (OSError, socket.timeout):
            pass
    return None


def probe_mac(mac, vlan):
    """A client address of its own for each probed VLAN: locally administered, built from
    the port's MAC and the VLAN, so it is unique per tester and stable between runs (no
    new reservations every time). DHCP servers track clients by this address: one MAC
    asking on several VLANs at once gets answered on only some of them, and using the
    port's own MAC elsewhere could make a server think the tester moved."""
    mac = _mac_bytes(mac)
    vid = vlan or 0
    return bytes(((mac[0] | 0x02) & 0xFE, mac[3], mac[4], mac[5], vid >> 8, vid & 0xFF))


def dhcp_offers(iface, vlans, quiet=8.0, longest=30.0, resend=(4.0, 12.0),
                on_offer=lambda vlan, offer: None):
    """Asks every VLAN at once: one DHCPDISCOVER per VLAN (each with its own xid and probe
    MAC), resent at `resend` seconds for the VLANs still silent, and collects the offers
    on one socket until all answered, or nothing new came for `quiet` seconds, or after
    `longest`. A server that ping-checks an address before offering it takes 1-4 s per
    request, and one serving several VLANs works through them one by one (measured: one
    every 4 s), so the window has to stretch while offers keep coming.
    Returns {vlan: offer or None}; on_offer(vlan, offer) is called as each one arrives."""
    vlans = list(vlans)
    result = dict.fromkeys(vlans)
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            mac = _mac_bytes(f.read().strip())
        xids = {int.from_bytes(os.urandom(4), "big"): vlan for vlan in vlans}
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)  # bound below, to iface only
    except OSError:
        return result
    with sock:
        try:
            sock.bind((iface, ETH_P_ALL))
            sock.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
            # replies may be unicast to the probe MACs: promiscuous for this socket only
            mreq = struct.pack("iHH8s", socket.if_nametoindex(iface), PACKET_MR_PROMISC, 0, b"")
            sock.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)

            def ask(silent_only):
                for xid, vlan in xids.items():
                    if not silent_only or result[vlan] is None:
                        sock.send(build_discover(probe_mac(mac, vlan), xid, vlan))
                        time.sleep(0.005)  # don't burst a relay with a dozen requests at once

            start = time.monotonic()
            last_new = start
            resends = [start + t for t in resend]
            ask(False)
            while any(v is None for v in result.values()):
                end = min(start + longest, last_new + quiet)
                if time.monotonic() >= end:
                    break
                if resends and time.monotonic() >= resends[0]:
                    resends.pop(0)
                    ask(True)
                wait_until = min([end] + resends[:1])
                sock.settimeout(max(0.01, wait_until - time.monotonic()))
                try:
                    frame, anc, _, addr = sock.recvmsg(4096, socket.CMSG_SPACE(AUXDATA.size))
                except socket.timeout:
                    continue
                if addr[2] == PACKET_OUTGOING:
                    continue
                aux = next((data for level, kind, data in anc
                            if level == SOL_PACKET and kind == PACKET_AUXDATA), None)
                if len(frame) < 60:
                    continue
                for xid, vlan in xids.items():
                    if result[vlan] is None:
                        offer = parse_offer(frame, xid, vlan, aux)
                        if offer:
                            result[vlan] = offer
                            last_new = time.monotonic()
                            on_offer(vlan, offer)
                            break
        except (OSError, socket.timeout):
            pass
    return result

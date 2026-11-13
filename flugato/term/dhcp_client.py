"""A small DHCP client for the test port inside its namespace: discover, offer, request,
acknowledge, then the address and default route on the port with `ip`; release on the way
out. Only what a tester needs: no renewals (a run lasts seconds), no hostname, no NAK retry
loop. Built on the probe's frame builder and parser (dhcp_probe)."""

import os
import socket
import struct
import time

from ..checks import ETH_P_ALL, PACKET_AUXDATA, PACKET_OUTGOING, SOL_PACKET, AUXDATA, run
from ..dhcp_probe import _mac_bytes, build_message, parse_offer

DISCOVER, OFFER, REQUEST, ACK, NAK, RELEASE = 1, 2, 3, 5, 6, 7
PARAMS = b"\x37\x05\x01\x03\x06\x33\x36"          # subnet, router, dns, lease, server id


def port_mac(iface):
    with open(f"/sys/class/net/{iface}/address") as f:
        return _mac_bytes(f.read().strip())


def build_request(mac, xid, address, server):
    opts = (b"\x35\x01\x03" + b"\x32\x04" + socket.inet_aton(address)
            + b"\x36\x04" + socket.inet_aton(server) + PARAMS + b"\xff")
    return build_message(mac, xid, opts)


def build_release(mac, xid, address, server, server_mac):
    opts = b"\x35\x01\x07" + b"\x36\x04" + socket.inet_aton(server) + b"\xff"
    return build_message(mac, xid, opts, ciaddr=socket.inet_aton(address), dst_mac=server_mac,
                         src_ip=socket.inet_aton(address), dst_ip=socket.inet_aton(server), flags=0)


def _recv(sock, xid, kinds, end):
    """The first frame from the server for our xid of one of `kinds`; (reply, server_mac)."""
    while time.monotonic() < end:
        sock.settimeout(max(0.01, end - time.monotonic()))
        try:
            frame, anc, _, addr = sock.recvmsg(4096, socket.CMSG_SPACE(AUXDATA.size))
        except socket.timeout:
            return None, None
        if addr[2] == PACKET_OUTGOING:
            continue
        aux = next((d for lvl, typ, d in anc if lvl == SOL_PACKET and typ == PACKET_AUXDATA), None)
        reply = parse_offer(frame, xid, None, aux, kinds=kinds)
        if reply:
            return reply, frame[6:12]
    return None, None


def obtain(iface, timeout=8.0, tries=2):
    """-> lease dict (address, prefix, router, dns, server, lease, took, server_mac, xid) or
    None. The port must be up with no address of its own yet."""
    try:
        mac = port_mac(iface)
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    except OSError:
        return None
    with sock:
        try:
            sock.bind((iface, ETH_P_ALL))
            sock.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
        except OSError:
            return None
        t0 = time.monotonic()
        for attempt in range(tries):
            xid = int.from_bytes(os.urandom(4), "big")
            try:
                sock.send(build_message(mac, xid, b"\x35\x01\x01" + PARAMS + b"\xff"))
            except OSError:
                return None
            offer, server_mac = _recv(sock, xid, (OFFER,), time.monotonic() + timeout / tries)
            if not offer or not offer["address"] or not offer["server"]:
                continue
            try:
                sock.send(build_request(mac, xid, offer["address"], offer["server"]))
            except OSError:
                return None
            ack, _ = _recv(sock, xid, (ACK, NAK), time.monotonic() + 4.0)
            if not ack or ack.get("kind") == NAK:
                continue
            lease = dict(ack)
            lease.update(address=ack["address"] or offer["address"], prefix=ack["prefix"] or offer["prefix"],
                         router=ack["router"] or offer["router"], dns=ack["dns"] or offer["dns"],
                         took=time.monotonic() - t0, server_mac=server_mac, xid=xid, mac=mac)
            return lease
    return None


def apply(iface, lease):
    """Puts the lease on the port: the address and, if offered, the default route."""
    if not lease or not lease.get("address") or not lease.get("prefix"):
        return False
    run(["ip", "-4", "addr", "flush", "dev", iface])
    ok = run(["ip", "addr", "add", f"{lease['address']}/{lease['prefix']}", "dev", iface]) is not None
    if ok and lease.get("router"):
        run(["ip", "route", "replace", "default", "via", lease["router"], "dev", iface])
    return ok


def release(iface, lease):
    """Tells the server the address is free again and takes it off the port."""
    if lease and lease.get("server") and lease.get("server_mac") and lease.get("address"):
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
            with sock:
                sock.bind((iface, ETH_P_ALL))
                sock.send(build_release(lease["mac"], lease["xid"], lease["address"], lease["server"],
                                        lease["server_mac"]))
        except OSError:
            pass
    run(["ip", "-4", "addr", "flush", "dev", iface])


def describe(lease):
    """The DHCP row's text for a lease."""
    from ..checks import fmt_lease
    text = f"from {lease['server']}"
    if lease.get("lease"):
        text += f"  lease {fmt_lease(str(lease['lease']))}"
    if lease.get("took") is not None:
        took = lease["took"]
        text += f"  offer in {took * 1000:.0f} ms" if took < 1 else f"  offer in {took:.1f} s"
    return text

"""LLDP and Cisco CDP straight off the wire, for machines without lldpd: a raw socket on the
test port hears the switch's announcements (LLDP every 30 s, CDP every 60 s on Cisco) and the
parsers turn them into the same neighbour dict checks.parse_lldp makes from lldpctl:
via, switch, mgmt, port, descr, vlans, pvid. Needs root (raw socket), as the namespace run has."""

import socket
import struct
import threading
import time

from ..checks import PACKET_ADD_MEMBERSHIP, PACKET_MR_PROMISC, PACKET_OUTGOING, SOL_PACKET

LLDP_DST = bytes.fromhex("0180c200000e")
CDP_DST = bytes.fromhex("01000ccccccc")
ETH_P_LLDP = 0x88CC
CDP_SNAP = bytes.fromhex("aaaa0300000c2000")
ETH_P_ALL = 0x0003
# Cisco's long port names as the switch itself abbreviates them
SHORT = (("TwentyFiveGigE", "Twe"), ("HundredGigE", "Hu"), ("FortyGigabitEthernet", "Fo"),
         ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"),
         ("Ethernet", "Eth"))


def _mac(b):
    return ":".join(f"{x:02x}" for x in b)


def _text(b):
    """Printable text only: a name from the wire goes to a terminal, so control characters
    (escape sequences included) are dropped here."""
    text = b.decode("utf-8", "replace").strip("\0 \t\r\n")
    return "".join(ch for ch in text if ch.isprintable())


def short_port(name):
    for long, short in SHORT:
        if name.startswith(long):
            return short + name[len(long):]
    return name


def parse_lldp_frame(frame):
    """An LLDP frame (Ethernet header included) -> neighbour dict, or None."""
    if len(frame) < 16 or frame[:6] != LLDP_DST or frame[12:14] != b"\x88\xcc":
        return None
    n = {"via": "LLDP", "switch": None, "mgmt": None, "port": None, "descr": None,
         "vlans": [], "pvid": None, "chassis_mac": None}
    i, data = 14, frame
    while i + 2 <= len(data):
        head = int.from_bytes(data[i:i + 2], "big")
        kind, size = head >> 9, head & 0x1FF
        i += 2
        v = data[i:i + size]
        if len(v) < size:
            break
        i += size
        if kind == 0:
            break
        if kind == 1 and v:                               # chassis id
            if v[0] == 4 and len(v) == 7:
                n["chassis_mac"] = _mac(v[1:])
            elif v[0] in (5,) and len(v) == 6 and v[1] == 1:
                n.setdefault("chassis_ip", socket.inet_ntoa(v[2:6]))
            elif v[0] in (6, 7, 2, 3):
                n["chassis_name"] = _text(v[1:])
        elif kind == 2 and v:                             # port id
            if v[0] == 3 and len(v) == 7:
                n["port"] = n["port"] or _mac(v[1:])
            else:
                n["port"] = _text(v[1:]) or n["port"]
        elif kind == 4:
            n["descr"] = _text(v)
        elif kind == 5:
            n["switch"] = _text(v)
        elif kind == 8 and len(v) >= 2:                   # management address
            alen = v[0]
            if alen == 5 and v[1] == 1 and len(v) >= 6 and not n["mgmt"]:
                n["mgmt"] = socket.inet_ntoa(v[2:6])
        elif kind == 127 and len(v) >= 4:                 # organisation specific
            oui, sub, body = v[:3], v[3], v[4:]
            if oui == b"\x00\x80\xc2":
                if sub == 1 and len(body) >= 2:           # port VLAN id
                    pvid = int.from_bytes(body[:2], "big")
                    n["pvid"] = pvid or None
                elif sub == 3 and len(body) >= 3:         # VLAN name
                    vid = int.from_bytes(body[:2], "big")
                    if vid:
                        n["vlans"].append(vid)
    if not n["switch"]:
        n["switch"] = n.get("chassis_name") or n["chassis_mac"]
    n["port"] = short_port(n["port"]) if n["port"] else None
    n["vlans"] = sorted(set(n["vlans"]))
    return n if n["switch"] or n["port"] else None


def parse_cdp_frame(frame):
    """A Cisco CDP frame (Ethernet + LLC/SNAP) -> neighbour dict, or None."""
    if len(frame) < 26 or frame[:6] != CDP_DST or frame[14:22] != CDP_SNAP:
        return None
    n = {"via": "CDP", "switch": None, "mgmt": None, "port": None, "descr": None,
         "vlans": [], "pvid": None, "voice_vlan": None, "platform": None}
    i = 26  # version, ttl, checksum
    n["via"] = f"CDPv{frame[22]}" if frame[22] in (1, 2) else "CDP"
    while i + 4 <= len(frame):
        kind, size = struct.unpack("!HH", frame[i:i + 4])
        if size < 4 or i + size > len(frame):
            break
        v = frame[i + 4:i + size]
        i += size
        if kind == 0x0001:
            n["switch"] = _text(v)
        elif kind in (0x0002, 0x0016) and len(v) >= 4:      # addresses / management addresses
            count = int.from_bytes(v[:4], "big")
            j = 4
            for _ in range(count):
                if j + 2 > len(v):
                    break
                plen = v[j + 1]
                proto = v[j + 2:j + 2 + plen]
                j += 2 + plen
                if j + 2 > len(v):
                    break
                alen = int.from_bytes(v[j:j + 2], "big")
                addr = v[j + 2:j + 2 + alen]
                j += 2 + alen
                if proto == b"\xcc" and alen == 4 and not n["mgmt"]:
                    n["mgmt"] = socket.inet_ntoa(addr)
        elif kind == 0x0003:
            n["port"] = short_port(_text(v))
        elif kind == 0x0006:
            n["platform"] = _text(v)
        elif kind == 0x000A and len(v) >= 2:
            n["pvid"] = int.from_bytes(v[:2], "big") or None
        elif kind == 0x000E and len(v) >= 3:
            n["voice_vlan"] = int.from_bytes(v[1:3], "big") or None
    return n if n["switch"] or n["port"] else None


def parse_frame(frame):
    """The neighbour in an LLDP or CDP frame, or None; a malformed frame is just None."""
    try:
        return parse_lldp_frame(frame) or parse_cdp_frame(frame)
    except (ValueError, IndexError, struct.error, OSError, UnicodeError):
        return None


def merge(have, new):
    """LLDP wins the switch and port fields; CDP fills what LLDP left empty (its native VLAN)."""
    if not have:
        return dict(new)
    if new["via"] == "LLDP" and not have["via"].startswith("LLDP"):
        have, new = dict(new), have
    out = dict(have)
    for k, v in new.items():
        if out.get(k) in (None, [], "") and v not in (None, [], ""):
            out[k] = v
    out["via"] = have["via"] if have["via"] == new["via"] else f"{have['via']} + {new['via']}"
    return out


def open_socket(iface):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((iface, ETH_P_ALL))
    mreq = struct.pack("iHH8s", socket.if_nametoindex(iface), PACKET_MR_PROMISC, 0, b"")
    sock.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
    return sock


class Listener:
    """Hears LLDP/CDP on `iface` in a thread for up to `seconds`; .neighbour fills in as frames
    arrive (an LLDP frame after a CDP one upgrades it), .error says why it could not listen."""

    def __init__(self, iface, seconds, on_change=lambda: None):
        self.iface, self.seconds, self.on_change = iface, seconds, on_change
        self.neighbour, self.error, self.heard = None, None, set()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.thread.join(2)

    def _run(self):
        try:
            sock = open_socket(self.iface)
        except OSError as e:
            self.error = e.strerror or e.__class__.__name__
            return
        with sock:
            end = time.monotonic() + self.seconds
            while time.monotonic() < end and not self._stop.is_set():
                sock.settimeout(max(0.05, min(0.5, end - time.monotonic())))
                try:
                    frame, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if addr[2] == PACKET_OUTGOING or len(frame) < 14:
                    continue
                n = parse_frame(frame)
                if not n:
                    continue
                self.neighbour = merge(self.neighbour, n)
                self.heard.add(n["via"])
                self.on_change()
                if "LLDP" in self.heard and any(v.startswith("CDP") for v in self.heard):
                    break   # both heard: nothing more to learn

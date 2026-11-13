"""Network checks. Each one wraps a standard Linux tool and a pure parser,
so the parsers can be tested without a network."""

import json
import os
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass

PENDING, OK, WARN, FAIL, SKIP = "pending", "ok", "warn", "fail", "skip"


@dataclass
class Row:
    label: str
    value: str = ""
    state: str = PENDING

    def set(self, value, state):
        self.value, self.state = value, state


ROW_LABELS = ("LINK", "IP", "DHCP", "GATEWAY", "DNS", "INTERNET", "SWITCH", "PORT", "VLAN")


def new_report():
    return {label: Row(label) for label in ROW_LABELS}


def run(cmd, timeout=10):
    """Run a command and return stdout, or None if it failed or is missing."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


# --- link -------------------------------------------------------------------

def detect_iface():
    """First wired interface with a real device behind it."""
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name == "lo" or os.path.isdir(os.path.join(path, "wireless")):
            continue
        if os.path.exists(os.path.join(path, "device")):
            return name
    return "eth0"


def carrier(iface):
    try:
        with open(f"/sys/class/net/{iface}/carrier") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def admin_down(iface):
    """The interface exists but is not set up (IFF_UP clear): its carrier can't be read."""
    try:
        with open(f"/sys/class/net/{iface}/flags") as f:
            return not int(f.read().strip(), 16) & 0x1
    except (OSError, ValueError):
        return False


TIME_SYNCED = "/run/systemd/timesync/synchronized"


def time_synced():
    """The clock was set from the network since boot. The Pi 5's RTC has no battery here,
    so before that the time is whatever it was at the last shutdown."""
    return os.path.exists(TIME_SYNCED)


SHUTDOWN_TARGETS = {"poweroff.target": "poweroff", "halt.target": "poweroff",
                    "reboot.target": "reboot", "kexec.target": "reboot"}


def parse_shutdown(jobs):
    """"poweroff" or "reboot" from `systemctl list-jobs --no-legend --plain` while systemd
    takes the system down, else None."""
    for line in (jobs or "").splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] in SHUTDOWN_TARGETS:
            return SHUTDOWN_TARGETS[parts[1]]
    return None


def shutting_down():
    return parse_shutdown(run(["systemctl", "list-jobs", "--no-legend", "--plain"], timeout=3))


def parse_ethtool(text):
    info = {"speed": None, "duplex": None, "autoneg": None, "supported": [], "partner": []}
    key = None
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.strip().partition(":")
            val = val.strip()
        elif key:
            val = line.strip()
        else:
            continue
        if key == "Speed":
            m = re.match(r"(\d+)Mb/s", val)
            info["speed"] = int(m.group(1)) if m else None
        elif key == "Duplex":
            info["duplex"] = val if val in ("Full", "Half") else None
        elif key == "Auto-negotiation":
            info["autoneg"] = val == "on"
        elif key == "Supported link modes":
            info["supported"] += [m for m in val.split() if "base" in m]
        elif key == "Link partner advertised link modes":
            info["partner"] += [m for m in val.split() if "base" in m]
    return info


def max_speed(modes):
    speeds = [int(m.split("base")[0]) for m in modes if m.split("base")[0].isdigit()]
    return max(speeds) if speeds else None


def fmt_speed(mbps):
    return f"{mbps // 1000}G" if mbps >= 1000 else f"{mbps}M"


def check_link(iface, row):
    info = parse_ethtool(run(["ethtool", iface]) or "")
    if not info["speed"]:
        row.set("up, speed unknown", WARN)
        return info
    value = f"{fmt_speed(info['speed'])} {info['duplex'] or '?'}"
    good = info["speed"] >= 100 and info["duplex"] == "Full"
    port = max_speed(info["partner"])
    ours = max_speed(info["supported"])
    if port and ours and min(port, ours) > info["speed"]:
        # both ends can go faster: on gigabit this usually means a pair is broken
        value += f"  both do {fmt_speed(min(port, ours))}: check cable"
        good = False
    elif port and port > info["speed"]:
        value += f"  (port does {fmt_speed(port)})"
    if not info["autoneg"]:
        value += "  no autoneg"
    row.set(value, OK if good else WARN)
    return info


# --- addressing -------------------------------------------------------------

def parse_ip_addr(text):
    """`ip -j -4 addr show dev X` -> (address, prefix) or None."""
    for link in json.loads(text or "[]"):
        for a in link.get("addr_info", []):
            if a.get("family") == "inet":
                return a["local"], a["prefixlen"]
    return None


def parse_ip_route(text):
    """`ip -j -4 route show default dev X` -> gateway or None."""
    for r in json.loads(text or "[]"):
        if r.get("dst") == "default" and r.get("gateway"):
            return r["gateway"]
    return None


def parse_nmcli_dhcp(text):
    """`nmcli -t -f DHCP4 device show X` -> dict of DHCP options."""
    opts = {}
    for line in text.splitlines():
        _, _, kv = line.partition(":")
        k, sep, v = kv.partition(" = ")
        if sep:
            opts[k.strip()] = v.strip()
    return opts


def parse_nmcli_dns(text):
    """`nmcli -t -f IP4.DNS device show X` -> ["192.0.2.1", ...]"""
    return [l.split(":", 1)[1].strip() for l in text.splitlines()
            if l.startswith("IP4.DNS[") and ":" in l]


def parse_resolv_conf(text):
    return [l.split()[1] for l in text.splitlines()
            if l.startswith("nameserver") and len(l.split()) > 1]


def fmt_lease(seconds):
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400}d"
    if s >= 3600:
        return f"{s // 3600}h"
    return f"{s // 60}m"


def wait_for_ip(iface, timeout, still_up):
    end = time.monotonic() + timeout
    while time.monotonic() < end and still_up():
        addr = parse_ip_addr(run(["ip", "-j", "-4", "addr", "show", "dev", iface]))
        if addr and not addr[0].startswith("169.254."):
            return addr
        time.sleep(1)
    return parse_ip_addr(run(["ip", "-j", "-4", "addr", "show", "dev", iface]))


def renew_dhcp(iface):
    """After a cable swap NetworkManager can keep the old lease for a few seconds; drop the
    old address and ask for a fresh lease so IP/DHCP/GATEWAY describe the new port. With
    no address left over, NetworkManager is already asking afresh: leave it alone."""
    if not parse_ip_addr(run(["ip", "-j", "-4", "addr", "show", "dev", iface])):
        return
    run(["ip", "-4", "addr", "flush", "dev", iface])
    # re-activating the test profile restarts DHCP from scratch (a plain device connect
    # can be a no-op on an already connected device)
    if run(["nmcli", "--wait", "0", "connection", "up", "flugato-test"]) is None:
        run(["nmcli", "--wait", "0", "device", "connect", iface])


def check_ip(iface, ip_row, dhcp_row, still_up, timeout=30):
    """Returns (gateway, dns_servers)."""
    addr = wait_for_ip(iface, timeout, still_up)
    if not addr:
        ip_row.set("no address", FAIL)
        dhcp_row.set("no answer", FAIL)
        return None, []
    if addr[0].startswith("169.254."):
        ip_row.set(f"{addr[0]}/{addr[1]} link-local", FAIL)
        dhcp_row.set("no answer", FAIL)
        return None, []
    ip_row.set(f"{addr[0]}/{addr[1]}", OK)

    opts = parse_nmcli_dhcp(run(["nmcli", "-t", "-f", "DHCP4", "device", "show", iface]) or "")
    if opts.get("dhcp_server_identifier"):
        value = f"from {opts['dhcp_server_identifier']}"
        if opts.get("dhcp_lease_time", "").isdigit():
            value += f"  lease {fmt_lease(opts['dhcp_lease_time'])}"
        dhcp_row.set(value, OK)
    else:
        dhcp_row.set("static or unknown", SKIP)

    # the test port's routes live in their own table (pi/setup.sh): look in all of them
    gateway = parse_ip_route(run(["ip", "-j", "-4", "route", "show", "table", "all",
                                  "default", "dev", iface]))
    # only this interface's servers: with Wi-Fi up, the system list mixes in Wi-Fi's DNS
    dns = opts.get("domain_name_servers", "").split()
    if not dns:
        shown = run(["nmcli", "-t", "-f", "IP4.DNS", "device", "show", iface])
        if shown is not None:
            dns = parse_nmcli_dns(shown)
        else:  # no NetworkManager at all: the system list is all there is
            try:
                with open("/etc/resolv.conf") as f:
                    dns = parse_resolv_conf(f.read())
            except OSError:
                dns = []
    return gateway, dns


# --- reachability -----------------------------------------------------------

def parse_ping(text):
    """-> (loss_percent, avg_ms) ; avg_ms is None when nothing came back."""
    loss = re.search(r"([\d.]+)% packet loss", text or "")
    rtt = re.search(r"= [\d.]+/([\d.]+)/", text or "")
    return (float(loss.group(1)) if loss else 100.0,
            float(rtt.group(1)) if rtt else None)


def ping(host, iface, count=3):
    out = subprocess.run(["ping", "-n", "-I", iface, "-c", str(count), "-W", "1", host],
                         capture_output=True, text=True).stdout
    return parse_ping(out)


def bound_socket(iface, kind):
    """A socket that can only use `iface`, so a second link (Wi-Fi for management) can
    never make a broken test port look healthy. Binding needs root, as the service runs."""
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    except PermissionError:
        # kernels before 5.7 want CAP_NET_RAW for this; the terminal app without root then
        # runs on the system's routing (the Pi's service is root: never here)
        pass
    return sock


def build_dns_query(name, qid):
    """A recursive DNS query for the A record of `name`."""
    q = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").encode("idna").split(b"."):
        q += bytes([len(label)]) + label
    return q + b"\0" + struct.pack("!HH", 1, 1)


def _skip_name(msg, i):
    while True:
        n = msg[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:  # compression pointer ends the name
            return i + 2
        i += n + 1


def parse_dns_response(msg, qid):
    """-> (rcode, [IPv4 addresses]). Raises ValueError on a reply that isn't ours."""
    if len(msg) < 12:
        raise ValueError("short reply")
    rid, flags, qd, an = struct.unpack("!HHHH", msg[:8])
    if rid != qid or not flags & 0x8000:
        raise ValueError("not our reply")
    i = 12
    for _ in range(qd):
        i = _skip_name(msg, i) + 4
    addrs = []
    for _ in range(an):
        i = _skip_name(msg, i)
        rtype, _, _, rdlen = struct.unpack("!HHIH", msg[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(msg[i:i + 4]))
        i += rdlen
    return flags & 0x0F, addrs


RCODES = {2: "server failure", 3: "no such name", 5: "refused"}


def dns_lookup(server, name, iface, timeout=2.0):
    """-> (addresses, error). Asks `server` directly, over `iface` only."""
    qid = int.from_bytes(os.urandom(2), "big")
    try:
        query = build_dns_query(name, qid)
    except UnicodeError:
        return [], "bad name"
    try:
        sock = bound_socket(iface, socket.SOCK_DGRAM)
    except OSError as e:
        return [], f"cannot use {iface} ({e.strerror})"
    with sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(query, (server, 53))
            end = time.monotonic() + timeout
            while True:
                sock.settimeout(max(0.01, end - time.monotonic()))
                data, peer = sock.recvfrom(4096)
                if peer[0] != server:
                    continue
                try:
                    rcode, addrs = parse_dns_response(data, qid)
                except (ValueError, IndexError, struct.error):
                    continue
                if rcode:
                    return [], RCODES.get(rcode, f"error {rcode}")
                return addrs, None if addrs else "no address"
        except socket.timeout:
            return [], "no answer"
        except OSError as e:
            return [], e.strerror or e.__class__.__name__


def check_gateway(gateway, iface, row):
    if not gateway:
        row.set("no default route", FAIL)
        return
    loss, avg = ping(gateway, iface)
    if avg is None:
        row.set(f"{gateway}  no reply", FAIL)
    else:
        state = OK if loss == 0 else WARN
        row.set(f"{gateway}  {avg:.1f} ms" + (f"  {loss:.0f}% loss" if loss else ""), state)


def check_dns(dns, host, iface, row):
    """Asks the test port's own DNS servers, over the test port. Returns the resolved
    address, so the INTERNET check doesn't resolve twice."""
    servers = ", ".join(dns[:2]) if dns else "none"
    if not host:
        row.set(f"{servers}  (no CHECK_HOST set)", SKIP if dns else FAIL)
        return None
    if not dns:
        row.set("no DNS server on this port", FAIL)
        return None
    errors = []
    for server in dns[:2]:
        t0 = time.monotonic()
        addrs, err = dns_lookup(server, host, iface)
        if addrs:
            row.set(f"{server}  {(time.monotonic() - t0) * 1000:.0f} ms", OK)
            return addrs[0]
        errors.append(f"{server} {err}")
    row.set("  ".join(errors), FAIL)
    return None


def check_internet(host, addr, iface, row):
    if not host:
        row.set("no CHECK_HOST set", SKIP)
        return
    if not addr:
        row.set(f"{host}  not resolved", FAIL)
        return
    t0 = time.monotonic()
    try:
        with bound_socket(iface, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect((addr, 443))
    except OSError as e:
        row.set(f"{host}:443  {e.__class__.__name__}", FAIL)
        return
    row.set(f"{host}:443  {(time.monotonic() - t0) * 1000:.0f} ms", OK)


# --- switch discovery -------------------------------------------------------

def parse_lldp(text, iface):
    """`lldpctl -f keyvalue IFACE` -> dict with switch, port, descr, vlans, via.

    Keys repeat, so lines are read in order: each neighbour starts with a via= line (a Cisco
    switch can show up twice, as LLDP and as CDP) and each VLAN with a vlan.vlan-id line,
    followed by its own vlan.pvid. The LLDP neighbour wins when there are several."""
    prefix = f"lldp.{iface}."
    neighbours = []
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if not (sep and k.startswith(prefix)):
            continue
        k, v = k[len(prefix):], v.strip()
        if k == "via" or not neighbours:
            neighbours.append({"via": "LLDP", "vlans": [], "pvid": None})
        n = neighbours[-1]
        if k == "via":
            n["via"] = v
        elif k == "vlan.vlan-id":
            if v.isdigit():
                n["vlans"].append(int(v))
        elif k == "vlan.pvid":
            if v == "yes" and n["vlans"]:
                n["pvid"] = n["vlans"][-1]
        else:
            n.setdefault(k, v)
    if not neighbours:
        return None
    n = next((n for n in neighbours if n["via"] == "LLDP"), neighbours[0])
    return {
        "via": n["via"],
        "switch": n.get("chassis.name") or n.get("chassis.mac"),
        "mgmt": n.get("chassis.mgmt-ip"),
        "port": n.get("port.ifname") or n.get("port.local") or n.get("port.mac"),
        "descr": n.get("port.descr"),
        "vlans": sorted(set(n["vlans"])),
        "pvid": n["pvid"],
    }


def forget_neighbours():
    """lldpd keeps a neighbour until its TTL (~2 min) runs out; after a cable swap the
    old switch would show as current. A restart starts from an empty table."""
    run(["systemctl", "restart", "lldpd"])


def lldp_neighbor(iface):
    return parse_lldp(run(["lldpctl", "-f", "keyvalue", iface]) or "", iface)


def fill_switch_rows(n, switch_row, port_row, vlan_row):
    switch = n["switch"] or "unnamed"
    if n["mgmt"]:
        switch += f"  {n['mgmt']}"
    switch_row.set(f"{switch}  ({n['via']})", OK)
    port = n["port"] or "?"
    if n["descr"] and n["descr"] != port:
        port += f"  {n['descr']}"
    port_row.set(port, OK)
    if n["pvid"]:
        vlan_row.set(f"{n['pvid']} native", OK)
    elif n["vlans"]:
        vlan_row.set(", ".join(map(str, n["vlans"])), OK)
    else:
        vlan_row.set("not advertised", SKIP)
    return n["pvid"]


# --- trunk detection --------------------------------------------------------

ETH_P_ALL, SOL_PACKET, PACKET_ADD_MEMBERSHIP, PACKET_AUXDATA = 0x0003, 263, 1, 8
PACKET_MR_PROMISC, PACKET_OUTGOING = 1, 4
TP_STATUS_VLAN_VALID = 0x10
AUXDATA = struct.Struct("IIIHHHH")  # status, len, snaplen, mac, net, vlan_tci, vlan_tpid
VLAN_TPIDS = (0x8100, 0x88A8)


def frame_vlan(frame, aux=None):
    """VLAN id of a received frame, or None if untagged. The kernel usually strips the tag
    and hands it over in the auxdata; without offload it is still in the frame."""
    if aux and len(aux) >= AUXDATA.size:
        status, *_, tci, _ = AUXDATA.unpack(aux[:AUXDATA.size])
        if status & TP_STATUS_VLAN_VALID:
            return (tci & 0x0FFF) or None  # VLAN 0 = priority tag only
    if len(frame) >= 16 and int.from_bytes(frame[12:14], "big") in VLAN_TPIDS:
        return (int.from_bytes(frame[14:16], "big") & 0x0FFF) or None
    return None


def sniff_vlans(iface, seconds, stop=lambda: False):
    """Listens on `iface` and returns the set of VLAN ids seen in tagged frames. On a Cisco
    trunk, per-VLAN spanning tree sends a tagged BPDU on every allowed VLAN every 2 s, so a
    short listen finds them even on a quiet network. Needs root (raw socket)."""
    seen = set()
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)  # bound below, to iface only
    except OSError:
        return None
    with sock:
        try:
            sock.bind((iface, ETH_P_ALL))
            sock.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
            # promiscuous for the life of this socket only: catches the multicast BPDUs
            mreq = struct.pack("iHH8s", socket.if_nametoindex(iface), PACKET_MR_PROMISC, 0, b"")
            sock.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
        except OSError:
            return None
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stop():
            sock.settimeout(max(0.05, min(0.5, end - time.monotonic())))
            try:
                frame, anc, _, addr = sock.recvmsg(2048, socket.CMSG_SPACE(AUXDATA.size))
            except socket.timeout:
                continue
            except OSError:  # interface went away mid-listen: report what was heard
                break
            if addr[2] == PACKET_OUTGOING:
                continue
            aux = next((d for lvl, typ, d in anc if lvl == SOL_PACKET and typ == PACKET_AUXDATA), None)
            vid = frame_vlan(frame, aux)
            if vid:
                seen.add(vid)
    return seen


def fmt_vlans(vlans, limit=4):
    vlans = sorted(vlans)
    text = ", ".join(map(str, vlans[:limit]))
    return text + (f" +{len(vlans) - limit}" if len(vlans) > limit else "")


def vlan_summary(native, tagged, offers=None):
    """VLAN row text and state from the switch's native VLAN (CDP/LLDP, may be None), the
    tagged VLANs heard on the wire (None when the listen could not run) and, after the
    per-VLAN probe, {vlan: offer or None}."""
    parts = [f"{native} native"] if native else []
    if tagged and offers is not None:
        answered = sum(1 for o in offers.values() if o)
        parts.append(f"trunk {len(tagged)} VLANs  DHCP {answered}/{len(offers)}")
    elif tagged:
        count = f"{len(tagged)} VLAN" + ("s" if len(tagged) > 1 else "")
        parts.append(f"trunk {count}: {fmt_vlans(tagged)}")
    elif tagged is not None and not native:
        parts.append("untagged only")
    if not parts:
        return "not advertised", SKIP
    return "  ".join(parts), OK


def blink_port(iface, times=3, off=2.0, on=2.0, status=lambda title, detail: None):
    """Takes the link down and up so the switch port LED can be spotted. Each "on" phase
    starts only once the link is really back: gigabit takes 2-3 s to negotiate, and
    without the wait the LED never gets a clear on phase."""
    try:
        for i in range(1, times + 1):
            run(["ip", "link", "set", iface, "down"])
            status("PORT LED OFF", f"blink {i} of {times}, watch the switch port")
            time.sleep(off)
            run(["ip", "link", "set", iface, "up"])
            end = time.monotonic() + 8
            while not carrier(iface) and time.monotonic() < end:
                time.sleep(0.2)
            status("PORT LED ON", f"blink {i} of {times}, watch the switch port")
            time.sleep(on)
    finally:  # a stop in the middle must never leave the port down
        run(["ip", "link", "set", iface, "up"])

"""Cable test: both ends of a patch cable in the tester, one in the main port (eth0), one
in the second port (a USB adapter, eth1). Checks the link at each end, sends test frames
both ways and watches the CRC error counters. No per-pair test: neither port's PHY does
TDR (`ethtool --cable-test` is not supported), so a broken pair shows up as a slower link,
lost frames or CRC errors."""

import os
import socket
import struct
import time

from .checks import (ETH_P_ALL, FAIL, OK, PACKET_OUTGOING, SKIP, WARN, carrier, max_speed,
                     parse_ethtool, run)

ETH_P_TEST = 0x88B5            # IEEE "local experimental" ethertype
MAGIC = b"flugato cable test"
SOL_PACKET, PACKET_STATISTICS = 263, 6
# frames a switch sends on its own: spanning tree, LLDP, Cisco CDP/DTP/VTP and PVST+
SWITCH_DSTS = (bytes.fromhex("0180c2000000"), bytes.fromhex("0180c200000e"),
               bytes.fromhex("01000ccccccc"), bytes.fromhex("01000ccccccd"))


def second_port(main, configured=""):
    """The port the other end of the cable goes into: CABLE_IFACE, else the first wired
    interface that isn't the main one (the USB adapter shows up as eth1)."""
    if configured:
        return configured
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if name in ("lo", main) or os.path.isdir(os.path.join(path, "wireless")):
            continue
        if os.path.exists(os.path.join(path, "device")):
            return name
    return None


def mac_of(iface):
    with open(f"/sys/class/net/{iface}/address") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def link(iface):
    """{speed, duplex, supported, partner} from ethtool, falling back to sysfs: the USB
    adapter's driver (cdc_ncm) doesn't report link modes to ethtool."""
    info = parse_ethtool(run(["ethtool", iface]) or "")
    if not info["speed"]:
        try:
            with open(f"/sys/class/net/{iface}/speed") as f:
                speed = int(f.read().strip())
            info["speed"] = speed if speed > 0 else None
        except (OSError, ValueError):
            pass
    if not info["duplex"]:
        try:
            with open(f"/sys/class/net/{iface}/duplex") as f:
                duplex = f.read().strip().capitalize()
            info["duplex"] = duplex if duplex in ("Full", "Half") else None
        except OSError:
            pass
    return info


def errors(iface):
    """Receive errors that point at the cable: CRC and frame (alignment) errors."""
    total = 0
    for name in ("rx_crc_errors", "rx_frame_errors"):
        try:
            with open(f"/sys/class/net/{iface}/statistics/{name}") as f:
                total += int(f.read().strip())
        except (OSError, ValueError):
            pass
    return total


def classify(sources, dsts, ours):
    """From what the second port heard: "cable" when only our own ports talked, "switch"
    when a switch spoke for itself (spanning tree, LLDP, CDP) or anyone else showed up."""
    if any(d in SWITCH_DSTS for d in dsts):
        return "switch"
    if any(s not in ours for s in sources):
        return "switch"
    return "cable"


def listen_peer(iface, ours, seconds=3.0):
    """Listens on the second port; returns classify() of what was heard."""
    sources, dsts = set(), set()
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)  # bound below, to iface only
    except OSError:
        return None
    with sock:
        try:
            sock.bind((iface, ETH_P_ALL))
        except OSError:
            return None
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                frame, addr = sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if addr[2] == PACKET_OUTGOING or len(frame) < 14:
                continue
            dsts.add(frame[:6])
            sources.add(frame[6:12])
    return classify(sources, dsts, ours)


def test_frame(dst, src, seq, size):
    head = dst + src + struct.pack("!H", ETH_P_TEST) + MAGIC + struct.pack("!I", seq)
    return head + bytes(max(0, size - len(head)))


def frames_one_way(tx, rx, count=20000, size=1514, wait=1.0):
    """Sends `count` test frames from tx to rx and counts what arrives. The receiving
    socket's kernel statistics count every frame that reached it, including those the
    program was too slow to read, so a slow reader doesn't look like a bad cable.
    Returns (sent, received, seconds) or None when the sockets can't be opened."""
    try:
        rxs = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_TEST))
        txs = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_TEST))
    except OSError:
        return None
    with rxs, txs:
        try:
            rxs.bind((rx, ETH_P_TEST))
            txs.bind((tx, ETH_P_TEST))
            rxs.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)  # reading resets the counters
            dst, src = mac_of(rx), mac_of(tx)
            sent, t0 = 0, time.monotonic()
            for seq in range(count):
                try:
                    txs.send(test_frame(dst, src, seq, size))
                    sent += 1
                except BlockingIOError:
                    time.sleep(0.001)
                except OSError:
                    break
            seconds = time.monotonic() - t0
            time.sleep(wait)  # the last frames are still on their way
            received, _drops = struct.unpack("II", rxs.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
        except OSError:
            return None
    return sent, received, seconds


def verdict(a, b, ab, ba, crc_new):
    """(state, text) from both links, both frame runs and the new CRC/frame errors."""
    if not a["speed"] or not b["speed"]:
        return FAIL, "no link at one end"
    problems, warnings = [], []
    for name, run_ in (("A to B", ab), ("B to A", ba)):
        if not run_ or not run_[0]:
            problems.append(f"{name}: test frames could not be sent")
            continue
        sent, received, _ = run_
        lost = max(0, sent - received)
        if lost > sent * 0.001:
            problems.append(f"{name}: {lost} of {sent} frames lost")
        elif lost:
            warnings.append(f"{name}: {lost} frames lost")
    if crc_new:
        problems.append(f"{crc_new} CRC errors")
    top = min(max_speed(a["supported"]) or a["speed"], max_speed(b["supported"]) or b["speed"])
    speed = min(a["speed"], b["speed"])
    if top and speed < top:
        warnings.append(f"linked at {speed} Mb, both ends do {top}: check the pairs")
    if a["duplex"] == "Half" or b["duplex"] == "Half":
        warnings.append("half duplex")
    if problems:
        return FAIL, "; ".join(problems)
    if warnings:
        return WARN, "; ".join(warnings)
    return OK, "cable passes"


def fmt_run(run_):
    if not run_:
        return "could not send", SKIP
    sent, received, seconds = run_
    lost = max(0, sent - received)
    mbps = sent * 1514 * 8 / seconds / 1e6 if seconds else 0
    return f"{received}/{sent} frames, {lost} lost  ({mbps:.0f} Mb/s sent)", OK if not lost else FAIL

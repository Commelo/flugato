"""The command line: `flugato` (the full-screen app), --once / --json / --quiet / --watch for
scripts, update and uninstall. With root the test ports first go into their own network
namespace (ns.supervise) and the program runs again inside it."""

import argparse
import json
import os
import sys
import time

from .. import checks
from ..checks import FAIL, OK, PENDING, SKIP, WARN
from . import SITE, VERSION, history, ns, release, runner

CONFIG_KEYS = ("PORT", "CHECK_HOST", "LLDP_WAIT", "PROBE_VLANS")
DEFAULT_HOST = "flugato.com"
COLOURS = {OK: "\x1b[32m", WARN: "\x1b[33m", FAIL: "\x1b[31m", SKIP: "\x1b[90m", PENDING: "\x1b[90m"}
BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "flugato", "config")


def load_settings():
    out = {}
    try:
        with open(config_path()) as f:
            for line in f:
                k, sep, v = line.partition("=")
                if sep and k.strip() in CONFIG_KEYS and not line.lstrip().startswith("#"):
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def save_setting(key, value):
    p = config_path()
    lines = []
    try:
        with open(p) as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(key + "=")]
    except OSError:
        pass
    lines.append(f"{key}={value}")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        return False
    return True


def parse_args(argv):
    p = argparse.ArgumentParser(prog="flugato", add_help=True,
                                description="fluGATO: the pocket network tester, in your terminal.",
                                epilog="flugato update / flugato uninstall manage the installed file. "
                                       f"Settings: {config_path()} (PORT, CHECK_HOST, LLDP_WAIT, PROBE_VLANS).")
    p.add_argument("--port", "-p", metavar="IFACE", help="the port to test (default: the first wired port with a cable)")
    p.add_argument("--once", action="store_true", help="run the checks, print the rows, exit with the verdict")
    p.add_argument("--json", action="store_true", help="--once, as one JSON object")
    p.add_argument("--quiet", "-q", action="store_true", help="--once, printing nothing: the exit code is the verdict")
    p.add_argument("--watch", "-w", action="store_true", help="stay, and print a line whenever the port changes")
    p.add_argument("--host", metavar="NAME", help=f"name for the DNS and internet checks (default {DEFAULT_HOST})")
    p.add_argument("--lldp-wait", type=int, metavar="S", help="seconds to wait for LLDP/CDP (default 90, 35 with --once)")
    p.add_argument("--force", action="store_true", help="with root: test the port that carries your default route anyway")
    p.add_argument("--no-cat", action="store_true", help="no cat")
    p.add_argument("--demo", metavar="SCENARIO", choices=("good", "cable", "nodhcp", "nolink"),
                   help=argparse.SUPPRESS)
    p.add_argument("--version", "-V", action="store_true", help="version and licence")
    return p.parse_args(argv)


def pick_port(settings, wanted=None):
    if wanted:
        return wanted
    if settings.get("PORT"):
        return settings["PORT"]
    if ns.inside():
        ports = ns.ns_ports()
        return next((p for p in ports if checks.carrier(p)), ports[0] if ports else None)
    wired = ns.wired_ports()
    own = set(ns.default_route_devs())
    for p in wired:
        if p not in own and checks.carrier(p):
            return p
    for p in wired:
        if p not in own:
            return p
    return wired[0] if wired else None


def make_runner(args, settings, iface, root):
    host = args.host if args.host is not None else settings.get("CHECK_HOST", DEFAULT_HOST)
    wait = args.lldp_wait or int(settings.get("LLDP_WAIT") or 0) or (35 if (args.once or args.json or args.quiet) else 90)
    vlans = {int(v) for v in settings.get("PROBE_VLANS", "").replace(",", " ").split() if v.isdigit()}
    return runner.Runner(iface, root=root, check_host=host, lldp_wait=wait, probe_vlans=vlans)


def demo_runner(name):
    """A finished run from the site's scenarios, for screenshots and tests."""
    from .. import demo
    r = runner.Runner("enp3s0", root=True, check_host="host.example")
    r.link_up = name != "nolink"
    if name == "nolink":
        for row in r.report.values():
            row.set("waiting for a link", SKIP)
        r.done = True
        return r
    r.report, _ = demo.good()
    r.link = {"speed": 1000, "duplex": "Full", "autoneg": True}
    r.neighbour = {"via": "LLDP", "switch": "access-sw-02", "mgmt": "192.0.2.250", "port": "Gi1/0/14",
                   "descr": "desk 3B west wall", "vlans": [], "pvid": 40}
    r.native, r.tagged, r.offers = demo.DETAILS[0], set(demo.DETAILS[1]), dict(demo.DETAILS[2])
    r.lease = {"address": "192.0.2.57", "prefix": 24, "router": "192.0.2.1", "server": "192.0.2.1"}
    if name == "cable":
        r.report["LINK"].set("100M Full  both do 1G: check cable", WARN)
        r.report["PORT"].set("Gi1/0/9  meeting room 2", OK)
        r.link = {"speed": 100, "duplex": "Full", "autoneg": True}
    elif name == "nodhcp":
        r.report["IP"].set("no address", FAIL)
        r.report["DHCP"].set("no offer in 8 s", FAIL)
        for k in ("GATEWAY", "DNS", "INTERNET"):
            r.report[k].set("skipped", SKIP)
        r.report["VLAN"].set("40 native  trunk 10 VLANs  DHCP 2/10", WARN)
        r.lease = None
    r.took, r.done = 4.1, True
    return r


def clean(text):
    """Row text for the terminal: control characters out (a switch name comes off the wire)."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))


def print_report(r, out=sys.stdout, colour=None):
    colour = out.isatty() if colour is None else colour
    c = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    mode = "root" if r.root else "no root"
    out.write(f"{c(BOLD, 'flu')}{c(BOLD + chr(27) + '[38;5;208m', 'GATO')} {c(DIM, f'{VERSION} · {r.iface} · ' + time.strftime('%Y-%m-%d %H:%M') + f' · {mode}')}\n")
    for label, row in r.report.items():
        out.write(f"{c(COLOURS[row.state], '●')} {c(BOLD, label.ljust(9))} {clean(row.value)}\n")
    v = r.verdict
    code = runner.EXIT.get(v, 2)
    n = sum(1 for row in r.report.values() if row.state in (OK, WARN, FAIL))
    took = f"  {n} checks in {r.took:.1f} s" if r.took is not None else ""
    out.write(f"{c(BOLD + COLOURS[{0: OK, 1: WARN, 2: FAIL, 3: SKIP}[code]], v)}{c(DIM, took)}\n")
    if not r.root and r.link_up and r.report["SWITCH"].value == runner.NEEDS_ROOT:
        out.write(c(DIM, f"the switch, port and VLAN rows need root: sudo {ns.self_hint()} --once\n"))
    return code


def run_once(args, settings, iface, root):
    r = demo_runner(args.demo) if args.demo else make_runner(args, settings, iface, root)
    try:
        if not args.demo:
            r.run()
    except KeyboardInterrupt:
        r.stop.set()
        return 130
    finally:
        if not args.demo:
            r.release()
    code = runner.EXIT.get(r.verdict, 2)
    if r.link_up and not args.demo:
        history.append(history.entry(iface, r.verdict, code, r.report, r.neighbour, r.link))
    if args.json:
        json.dump(r.to_dict(VERSION), sys.stdout, indent=1)
        sys.stdout.write("\n")
    elif not args.quiet:
        print_report(r)
    return code


def watch_state(iface):
    """What --watch compares between polls."""
    up = checks.carrier(iface)
    addr = checks.parse_ip_addr(checks.run(["ip", "-j", "-4", "addr", "show", "dev", iface]))
    gw = checks.parse_ip_route(checks.run(["ip", "-j", "-4", "route", "show", "default", "dev", iface])) if addr else None
    return {"up": up, "addr": addr, "gw": gw}


def watch_lines(old, new, iface, link_row=None):
    """The lines one poll prints: (state, text) for each change from old to new."""
    out = []
    if old is None or old["up"] != new["up"]:
        if new["up"]:
            if link_row is None:
                link_row = checks.Row("LINK")
                checks.check_link(iface, link_row)
            out.append((link_row.state, "link up " + link_row.value))
        elif old is not None or not new["up"]:
            out.append((FAIL, "link down"))
    if (old or {}).get("addr") != new["addr"]:
        if new["addr"]:
            a, p = new["addr"]
            bad = a.startswith("169.254.")
            out.append((FAIL if bad else OK, f"{'link-local only ' if bad else 'address '}{a}/{p}"))
        elif old and old.get("addr"):
            out.append((WARN, "no address"))
    if (old or {}).get("gw") != new["gw"] and new["gw"]:
        out.append((OK, f"gateway {new['gw']}"))
    return out


def run_watch(args, iface, out=sys.stdout):
    colour = out.isatty()
    old = None
    try:
        while True:
            new = watch_state(iface)
            for state, text in watch_lines(old, new, iface):
                dot = f"{COLOURS[state]}●{RESET}" if colour else "●"
                out.write(f"{time.strftime('%H:%M:%S')}  {dot} {clean(text)}\n")
                out.flush()
            old = new
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def port_infos():
    """[{name, kind, own}] of the host's ports, handed to the run inside the namespace."""
    own = set(ns.default_route_devs())
    out = [{"name": p, "kind": "wired", "own": p in own} for p in ns.wired_ports()]
    out += [{"name": p, "kind": "wifi", "own": p in own} for p in ns.wireless_ports()]
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "update":
        return release.update()
    if argv and argv[0] == "uninstall":
        return release.uninstall(yes="--yes" in argv or "-y" in argv)
    args = parse_args(argv)
    if args.version:
        print(f"flugato {VERSION} (python {sys.version.split()[0]}, stdlib only) · MIT · Copyright (c) 2026 COMMELO LLC · {SITE}")
        return 0
    settings = load_settings()
    root, inside = ns.is_root(), ns.inside()
    if root and not inside and not args.demo:
        ports, err = ns.choose_ports(args.port or settings.get("PORT"), args.force)
        if err:
            sys.stderr.write(f"flugato: {err}\n")
            return 4
        return ns.supervise(ports, argv, port_infos())
    iface = "enp3s0" if args.demo else pick_port(settings, args.port)
    if not iface:
        sys.stderr.write("flugato: no wired port on this computer\n")
        return 4
    if not args.demo and not os.path.isdir(f"/sys/class/net/{iface}"):
        sys.stderr.write(f"flugato: no port named {iface}\n")
        return 4
    if args.once or args.json or args.quiet:
        return run_once(args, settings, iface, root and inside)
    if args.watch:
        return run_watch(args, iface)
    from . import tui
    result = tui.run(args, settings, iface, root and inside)
    if result == "sudo":
        cmd = ["sudo"] + ns.self_command() + [a for a in argv if a != "--force"] + (["--port", iface] if not args.port else [])
        try:
            os.execvp("sudo", cmd)
        except OSError as e:
            sys.stderr.write(f"flugato: cannot run sudo: {e}\n")
            return 4
    return result or 0

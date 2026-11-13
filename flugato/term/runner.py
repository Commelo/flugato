"""The check sequence of the terminal app, the pocket tester's (flugato/__main__.py
run_checks) with two ways to get the addressing rows: with root, inside the test port's
namespace, its own DHCP lease and a raw-socket LLDP/CDP listener; without root, what the
computer already knows (NetworkManager's lease, lldpctl if there is one), nothing touched.
One Runner per run; the screens and the --once output read its report and extras."""

import shutil
import threading
import time

from .. import checks, dhcp_probe
from ..checks import FAIL, OK, PENDING, SKIP, WARN, new_report
from . import dhcp_client, neighbours

NO_LINK, ALL_GOOD = "NO LINK", "ALL GOOD"
EXIT = {ALL_GOOD: 0, "CHECK WARNINGS": 1, "CHECK CABLE": 1, "PROBLEM FOUND": 2, "NO DHCP": 2,
        NO_LINK: 3, "TESTING…": 0}
MOOD = {OK: "good", WARN: "warn", FAIL: "fail", PENDING: "testing", SKIP: "good"}
NEEDS_ROOT = "needs root"
LINK_WAIT = 8.0            # seconds a root run gives a freshly moved port to bring its link back


def overall(report):
    states = [r.state for r in report.values()]
    for s in (FAIL, WARN, PENDING):
        if s in states:
            return s
    return OK


def verdict(report, link_up=True):
    """The banner's word, from the rows."""
    if not link_up:
        return NO_LINK
    o = overall(report)
    if o == FAIL:
        if report["LINK"].state != FAIL and report["DHCP"].state == FAIL:
            return "NO DHCP"
        return "PROBLEM FOUND"
    if o == WARN:
        return "CHECK CABLE" if "check cable" in report["LINK"].value else "CHECK WARNINGS"
    if o == PENDING:
        return "TESTING…"
    return ALL_GOOD


def mood(report, link_up=True):
    if not link_up:
        return "sleep"
    return MOOD[overall(report)]


def link_text(link):
    if not link or not link.get("speed"):
        return "speed unknown"
    return f"{checks.fmt_speed(link['speed'])} {link.get('duplex') or '?'}"


class Runner:
    def __init__(self, iface, root=False, check_host="", lldp_wait=90, probe_vlans=(),
                 on_change=lambda: None, sniff_seconds=25):
        self.iface, self.root, self.check_host = iface, root, check_host
        self.lldp_wait, self.probe_vlans, self.sniff_seconds = lldp_wait, set(probe_vlans), sniff_seconds
        self.on_change = on_change
        self.stop = threading.Event()
        self.report = new_report()
        self.link, self.lease, self.neighbour = {}, None, None
        self.native, self.tagged, self.offers = None, None, {}
        self.link_up, self.done, self.took = None, False, None
        self.started = None

    # --- plumbing ----------------------------------------------------------------------
    def interrupted(self):
        return self.stop.is_set()

    def changed(self):
        self.on_change()

    def wait(self, seconds):
        self.stop.wait(seconds)

    @property
    def verdict(self):
        return verdict(self.report, self.link_up is not False)

    @property
    def mood(self):
        return mood(self.report, self.link_up is not False)

    # --- the sequence ----------------------------------------------------------------------
    def run(self):
        self.started = time.monotonic()
        r = self.report = new_report()
        self.link_up = checks.carrier(self.iface)
        if not self.link_up and self.root:
            # the port was just moved into the namespace and set up: the link renegotiates
            # (gigabit takes 2-3 s, 2.5G a little more) before the carrier comes back
            for row in r.values():
                row.set("link coming up…", PENDING)
            self.changed()
            end = time.monotonic() + LINK_WAIT
            while not self.link_up and time.monotonic() < end and not self.interrupted():
                self.wait(0.25)
                self.link_up = checks.carrier(self.iface)
        if not self.link_up:
            for row in r.values():
                row.set("waiting for a link", SKIP)
            self.finish()
            return self
        sniff, sniffed = None, {}
        listener = None
        if self.root:
            sniff = threading.Thread(target=lambda: sniffed.update(
                tagged=checks.sniff_vlans(self.iface, self.sniff_seconds, stop=self.interrupted)), daemon=True)
            sniff.start()
            listener = neighbours.Listener(self.iface, self.lldp_wait, on_change=self.changed).start()
        try:
            self.link = checks.check_link(self.iface, r["LINK"])
            if not self.link.get("speed"):
                self.sysfs_link()
            self.changed()
            if self.interrupted():
                return self
            gateway, dns = self.addressing_root() if self.root else self.addressing_passive()
            self.changed()
            if self.interrupted():
                return self
            checks.check_gateway(gateway, self.iface, r["GATEWAY"])
            self.changed()
            resolved = checks.check_dns(dns, self.check_host, self.iface, r["DNS"])
            self.changed()
            checks.check_internet(self.check_host, resolved, self.iface, r["INTERNET"])
            self.changed()
            if self.interrupted():
                return self
            self.switch_rows(listener)
            if self.root and not self.interrupted():
                self.finish_vlans(sniff, sniffed, listener)
        finally:
            if listener:
                listener.stop()
            if sniff and sniff.is_alive():
                self.stop.set()
                sniff.join(2)
            self.finish()
        return self

    def finish(self):
        self.took = time.monotonic() - self.started if self.started else None
        self.done = True
        self.changed()

    def sysfs_link(self):
        """A driver that tells ethtool nothing (USB adapters on cdc_ncm): speed and duplex
        from sysfs instead."""
        try:
            with open(f"/sys/class/net/{self.iface}/speed") as f:
                speed = int(f.read().strip())
            with open(f"/sys/class/net/{self.iface}/duplex") as f:
                duplex = f.read().strip().capitalize()
        except (OSError, ValueError):
            return
        if speed > 0:
            self.link["speed"], self.link["duplex"] = speed, duplex if duplex in ("Full", "Half") else None
            self.report["LINK"].set(link_text(self.link), OK if speed >= 100 and duplex == "Full" else WARN)

    def addressing_root(self):
        r = self.report
        r["IP"].set("asking DHCP…", PENDING)
        r["DHCP"].set("discover sent", PENDING)
        self.changed()
        lease = dhcp_client.obtain(self.iface)
        if not lease:
            r["IP"].set("no address", FAIL)
            r["DHCP"].set("no offer in 8 s", FAIL)
            return None, []
        dhcp_client.apply(self.iface, lease)
        self.lease = lease
        r["IP"].set(f"{lease['address']}/{lease['prefix']}", OK)
        r["DHCP"].set(dhcp_client.describe(lease), OK)
        return lease.get("router"), [d for d in lease.get("dns") or [] if d]

    def addressing_passive(self):
        return checks.check_ip(self.iface, self.report["IP"], self.report["DHCP"],
                               lambda: not self.interrupted(), timeout=5)

    def switch_rows(self, listener):
        r = self.report
        if not self.root:
            n = checks.lldp_neighbor(self.iface) if shutil.which("lldpctl") else None
            if n:
                self.neighbour = n
                self.native = checks.fill_switch_rows(n, r["SWITCH"], r["PORT"], r["VLAN"])
            else:
                for k in ("SWITCH", "PORT", "VLAN"):
                    r[k].set(NEEDS_ROOT, SKIP)
            return
        end = time.monotonic() + self.lldp_wait
        while not self.interrupted():
            if listener.neighbour:
                self.neighbour = listener.neighbour
                self.native = checks.fill_switch_rows(self.neighbour, r["SWITCH"], r["PORT"], r["VLAN"])
                self.changed()
                return
            if listener.error:
                r["SWITCH"].set(f"cannot listen: {listener.error}", WARN)
                r["PORT"].set("", SKIP)
                r["VLAN"].set("", SKIP)
                return
            left = int(end - time.monotonic())
            if left <= 0:
                r["SWITCH"].set("no LLDP/CDP heard (unmanaged switch?)", WARN)
                r["PORT"].set("", SKIP)
                r["VLAN"].set("", SKIP)
                self.changed()
                return
            r["SWITCH"].set(f"listening for LLDP/CDP… {left} s", PENDING)
            self.changed()
            self.wait(1)

    def finish_vlans(self, sniff, sniffed, listener):
        r = self.report
        native = self.native
        while sniff.is_alive() and not self.interrupted():
            if r["VLAN"].state == PENDING or native:
                r["VLAN"].set((f"{native} native  " if native else "") + "checking for trunk…", PENDING)
            self.changed()
            self.wait(1)
        if self.interrupted():
            return
        heard = sniffed.get("tagged")
        self.tagged = (heard | self.probe_vlans) if heard is not None else (self.probe_vlans or None)
        r["VLAN"].set(*checks.vlan_summary(native, self.tagged))
        self.changed()
        if self.tagged:
            self.probe(native)
        if listener and listener.neighbour and listener.neighbour is not self.neighbour:
            # a second protocol arrived meanwhile: the merged neighbour has more to say
            self.neighbour = listener.neighbour
            pvid = checks.fill_switch_rows(self.neighbour, r["SWITCH"], r["PORT"], checks.Row("VLAN"))
            if pvid and not native:
                self.native = pvid
                r["VLAN"].set(*checks.vlan_summary(pvid, self.tagged, self.offers if self.tagged else None))
            self.changed()

    def probe(self, native, limit=32):
        r = self.report
        vlans = sorted(self.tagged)[:limit]
        r["VLAN"].set((f"{native} native  " if native else "") + f"asking DHCP on {len(vlans)} VLANs…", PENDING)
        self.changed()

        def arrived(vlan, offer):
            self.offers[vlan] = offer
            self.changed()

        result = dhcp_probe.dhcp_offers(self.iface, vlans, on_offer=arrived)
        for vlan in vlans:
            self.offers.setdefault(vlan, result.get(vlan))
        r["VLAN"].set(*checks.vlan_summary(native, self.tagged, self.offers))
        self.changed()

    # --- afterwards ----------------------------------------------------------------------
    def release(self):
        if self.lease:
            dhcp_client.release(self.iface, self.lease)
            self.lease = None

    def to_dict(self, version=""):
        d = {"version": version, "port": self.iface, "root": self.root,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "verdict": self.verdict,
             "exit": EXIT.get(self.verdict, 2), "link_up": bool(self.link_up),
             "seconds": round(self.took, 1) if self.took is not None else None,
             "rows": {k: {"value": r.value, "state": r.state} for k, r in self.report.items()}}
        if self.link:
            d["link"] = {k: self.link.get(k) for k in ("speed", "duplex", "autoneg")}
        if self.neighbour:
            d["switch"] = {k: self.neighbour.get(k) for k in ("via", "switch", "mgmt", "port", "descr", "pvid")}
        if self.tagged is not None or self.native:
            d["vlans"] = {"native": self.native, "tagged": sorted(self.tagged or []),
                          "offers": {str(v): o for v, o in self.offers.items()}}
        return d

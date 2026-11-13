"""The terminal app's screens drawn into grids from fake state, at 100x30 and at 80x24, and
the check runner with every tool faked (passive and root sequences)."""

import threading
import unittest
from unittest import mock

from flugato.checks import FAIL, OK, PENDING, SKIP, WARN, Row
from flugato.term import cli, grid, runner, screens, tui


def state(demo="good", root=True, w=100, h=30):
    import time
    now = time.mktime(time.strptime("2026-10-14 08:42:00", "%Y-%m-%d %H:%M:%S"))
    st = tui.State("enp3s0", root, now=lambda: now)
    st.runner = cli.demo_runner(demo)
    st.hardware = "Intel I219-V"
    st.history = [{"at": "2026-10-14T07:00:00", "port": "enp3s0", "verdict": "ALL GOOD", "exit": 0,
                   "speed": 1000, "switch": "access-sw-02", "switch_port": "Gi1/0/14"},
                  {"at": "2026-10-13T18:00:00", "port": "enx3c18a0", "verdict": "NO DHCP", "exit": 2}]
    st.history_all = st.history
    g = grid.Grid(w, h)
    return st, g


class Dashboard(unittest.TestCase):
    def test_full_size_with_cat(self):
        st, g = state()
        screens.dashboard(g, st)
        text = g.text()
        self.assertIn("fluGATO  ▸ enp3s0", g.line(0))
        self.assertIn("root · test in its own namespace", g.line(0))
        self.assertIn("ALL GOOD", g.line(3))
        self.assertIn("enp3s0 · Intel I219-V · 1G Full · 192.0.2.57/24", g.line(3))
        self.assertIn("● LINK       1G Full", g.line(6))
        self.assertIn("● VLAN       40 native", g.line(14))
        self.assertIn("RECENT TESTS", g.line(16))
        self.assertIn("access-sw-02 Gi1/0/14", g.line(17))
        self.assertIn("1G", g.line(17))
        self.assertIn("ALL GOOD", g.line(17))
        self.assertIn("NO DHCP", g.line(18))
        self.assertIn("▀", text)                                    # the cat
        self.assertIn("r re-run", g.line(29))
        self.assertIn("b blink port", g.line(29))
        self.assertNotIn("s sudo", g.line(29))

    def test_small_terminal_has_no_cat_and_the_sudo_key(self):
        st, g = state(root=False, w=80, h=24)
        st.runner.root = False
        st.runner.report["SWITCH"].set(runner.NEEDS_ROOT, SKIP)
        screens.dashboard(g, st)
        self.assertNotIn("▀", g.text())
        self.assertIn("no root · read only", g.line(0))
        self.assertIn("press s", g.line(12))
        self.assertIn("s sudo", g.line(23))
        self.assertNotIn("blink", g.line(23))
        for y in range(24):
            self.assertLessEqual(len(g.line(y).rstrip()), 80)

    def test_no_link(self):
        st, g = state("nolink")
        screens.dashboard(g, st)
        self.assertIn("NO LINK", g.line(3))
        self.assertIn("plug a cable into enp3s0", g.line(3))
        self.assertEqual(st.runner.mood, "sleep")

    def test_message_overrides_the_banner(self):
        st, g = state()
        st.message = ("PORT LED OFF", "blink 1 of 3, watch the switch port")
        screens.dashboard(g, st)
        self.assertIn("PORT LED OFF", g.line(3))
        self.assertIn("blink 1 of 3", g.line(3))

    def test_cable_key_only_with_a_second_port(self):
        st, g = state()
        st.cable_port = "enx3c18a0"
        screens.dashboard(g, st)
        self.assertIn("c cable test", g.line(29))


class OtherScreens(unittest.TestCase):
    def test_vlans(self):
        st, g = state()
        screens.vlans(g, st)
        self.assertIn("TRUNK  native 40 + 10 tagged", g.line(3))
        self.assertIn("access-sw-02 Gi1/0/14 (LLDP)", g.line(3))
        self.assertIn("40      native      ● lease   192.0.2.57/24", g.line(8))
        self.assertIn("10      tagged      ● offer   198.51.100.5/28", g.line(9))
        self.assertIn("30      tagged      ● no offer", g.line(11))
        self.assertIn("320     tagged      …         probing", g.line(18))

    def test_vlans_without_root(self):
        st, g = state(root=False)
        st.runner.tagged, st.runner.native, st.runner.neighbour = None, None, None
        screens.vlans(g, st)
        self.assertIn("needs root", g.line(8))

    def test_ports(self):
        st, g = state()
        st.ports = [{"name": "enp3s0", "hardware": "Intel I219-V", "state": "no cable", "role": "skip", "note": "test port", "testable": True},
                    {"name": "wlp0s20f3", "hardware": "Wi-Fi", "state": "up  192.0.2.80", "role": "ok",
                     "note": "your connection: never touched", "testable": False}]
        st.selected = 1
        screens.ports(g, st)
        self.assertIn("enp3s0      Intel I219-V          ● no cable", g.line(8))
        self.assertIn("▸ wlp0s20f3", g.line(9))
        self.assertIn("your connection: never touched", g.line(9))
        self.assertIn("own network namespace", g.line(11))
        self.assertIn("enter test it", g.line(29))

    def test_history(self):
        st, g = state()
        screens.history_screen(g, st)
        self.assertIn("when   port        switch and port", g.line(5))
        self.assertIn("enp3s0", g.line(6))
        self.assertIn("ALL GOOD", g.line(6))
        self.assertIn("yest.", g.line(7))

    def test_help_fits_80_columns(self):
        st, g = state(w=80, h=24)
        screens.help_screen(g, st)
        self.assertIn("r   run the checks again", g.line(5))
        self.assertIn("settings: ~/.config/flugato/config", g.text())
        for y in range(24):
            self.assertLessEqual(len(g.line(y).rstrip()), 80)

    def test_cable(self):
        st, g = state()
        st.cable_port, st.cable_word = "enx3c18a0", "CABLE PASS"
        st.cable_report = {k: Row(k) for k in ("PORT A", "PORT B", "A TO B", "B TO A", "ERRORS", "RESULT")}
        st.cable_report["RESULT"].set("cable passes", OK)
        screens.cable(g, st)
        self.assertIn("CABLE PASS", g.line(3))
        self.assertIn("enp3s0 to enx3c18a0", g.line(3))
        self.assertIn("● RESULT     cable passes", g.line(11))

    def test_every_screen_survives_a_tiny_terminal(self):
        for name, fn in screens.SCREENS.items():
            st, g = state(w=40, h=10)
            st.ports, st.cable_port = [], "x"
            fn(g, st)
            self.assertIn("flu", g.line(0), name)


class Cat(unittest.TestCase):
    def test_grid_cat_uses_half_blocks(self):
        g = grid.Grid(40, 12)
        g.cat(2, 1, ["oo..", "o.o.", "..o."])
        self.assertEqual(g.cells[1][2][0], "▀")           # both halves
        self.assertEqual(g.cells[1][3][0], "▀")           # upper only
        self.assertEqual(g.cells[1][4][0], "▄")           # lower only
        self.assertEqual(g.cells[2][4][0], "▀")           # a lone last row
        self.assertEqual(g.cells[1][5][0], " ")

    def test_moods_pick_poses(self):
        st = tui.State("eth0", True, now=lambda: 0)
        st.runner = cli.demo_runner("nolink")
        cat = tui.Cat(st)
        cat.tick(0)
        self.assertEqual(st.cat_mood, "sleep")
        self.assertTrue(st.cat_pose.startswith("loaf"))
        st.runner = cli.demo_runner("good")
        cat.tick(1)
        self.assertEqual(st.cat_mood, "good")
        self.assertIn(st.cat_pose, ("sit", "sit1"))
        self.assertEqual(tui.colour256((236, 150, 60)), 16 + 36 * 5 + 6 * 3 + 1)
        self.assertEqual(tui.colour256((20, 20, 20)), 233)


class FakeListener:
    neighbour_to_give = {"via": "LLDP", "switch": "access-sw-02", "mgmt": "192.0.2.250", "port": "Gi1/0/14",
                         "descr": "desk 3B", "vlans": [], "pvid": 70}

    def __init__(self, iface, seconds, on_change=lambda: None):
        self.neighbour, self.error = None, None
        self.on_change = on_change

    def start(self):
        threading.Timer(0.05, self.arrive).start()
        return self

    def arrive(self):
        self.neighbour = dict(self.neighbour_to_give)
        self.on_change()

    def stop(self):
        pass


def fake_link(iface, row):
    row.set("1G Full", OK)
    return {"speed": 1000, "duplex": "Full", "autoneg": True, "supported": [], "partner": []}


class RunnerSequence(unittest.TestCase):
    def common_patches(self):
        return [mock.patch.object(runner.checks, "carrier", lambda iface: True),
                mock.patch.object(runner.checks, "check_link", fake_link),
                mock.patch.object(runner.checks, "check_gateway", lambda gw, iface, row: row.set(f"{gw}  0.9 ms", OK)),
                mock.patch.object(runner.checks, "check_dns", lambda dns, host, iface, row: (row.set(f"{dns[0] if dns else 'none'}  14 ms", OK), "203.0.113.5")[1]),
                mock.patch.object(runner.checks, "check_internet", lambda host, addr, iface, row: row.set(f"{host}:443  38 ms", OK))]

    def run_with(self, patches, r):
        for p in patches:
            p.start()
        try:
            r.run()
        finally:
            for p in patches:
                p.stop()
        return r

    def test_passive_sequence(self):
        patches = self.common_patches() + [
            mock.patch.object(runner.checks, "check_ip", lambda iface, ip, dhcp, still, timeout=30: (ip.set("192.0.2.57/24", OK), dhcp.set("from 192.0.2.1  lease 12h", OK), ("192.0.2.1", ["192.0.2.1"]))[2]),
            mock.patch.object(runner.shutil, "which", lambda name: None)]
        changes = []
        r = self.run_with(patches, runner.Runner("eth0", root=False, check_host="host.example", on_change=lambda: changes.append(1)))
        rows = {k: (row.value, row.state) for k, row in r.report.items()}
        self.assertEqual(rows["LINK"], ("1G Full", OK))
        self.assertEqual(rows["IP"], ("192.0.2.57/24", OK))
        self.assertEqual(rows["GATEWAY"], ("192.0.2.1  0.9 ms", OK))
        self.assertEqual(rows["DNS"], ("192.0.2.1  14 ms", OK))
        self.assertEqual(rows["INTERNET"], ("host.example:443  38 ms", OK))
        self.assertEqual(rows["SWITCH"], (runner.NEEDS_ROOT, SKIP))
        self.assertEqual(rows["VLAN"], (runner.NEEDS_ROOT, SKIP))
        self.assertEqual((r.verdict, r.done, r.link_up), ("ALL GOOD", True, True))
        self.assertGreater(len(changes), 4)
        self.assertIsNotNone(r.took)

    def test_root_sequence_with_lease_switch_and_trunk(self):
        lease = {"address": "192.0.2.57", "prefix": 24, "router": "192.0.2.1", "dns": ["192.0.2.1"],
                 "server": "192.0.2.1", "lease": 43200, "took": 0.4}
        applied, released, probed = [], [], []
        patches = self.common_patches() + [
            mock.patch.object(runner.dhcp_client, "obtain", lambda iface, timeout=8.0, tries=2: dict(lease)),
            mock.patch.object(runner.dhcp_client, "apply", lambda iface, l: applied.append(l) or True),
            mock.patch.object(runner.dhcp_client, "release", lambda iface, l: released.append(l)),
            mock.patch.object(runner.neighbours, "Listener", FakeListener),
            mock.patch.object(runner.checks, "sniff_vlans", lambda iface, seconds, stop=None: {10, 20}),
            mock.patch.object(runner.dhcp_probe, "dhcp_offers", lambda iface, vlans, on_offer=None, **k: (probed.extend(vlans), on_offer(10, {"address": "198.51.100.5", "prefix": 28, "router": "198.51.100.1", "server": "192.0.2.1", "dns": [], "lease": 1}), {10: {"address": "198.51.100.5"}, 20: None})[2]),
        ]
        r = self.run_with(patches, runner.Runner("eth0", root=True, check_host="host.example", lldp_wait=3, sniff_seconds=0.1, probe_vlans={30}))
        rows = {k: (row.value, row.state) for k, row in r.report.items()}
        self.assertEqual(rows["IP"], ("192.0.2.57/24", OK))
        self.assertEqual(rows["DHCP"], ("from 192.0.2.1  lease 12h  offer in 400 ms", OK))
        self.assertEqual(rows["SWITCH"], ("access-sw-02  192.0.2.250  (LLDP)", OK))
        self.assertEqual(rows["PORT"], ("Gi1/0/14  desk 3B", OK))
        self.assertEqual(rows["VLAN"], ("70 native  trunk 3 VLANs  DHCP 1/3", OK))
        self.assertEqual(sorted(probed), [10, 20, 30])
        self.assertEqual(r.offers[10]["address"], "198.51.100.5")
        self.assertIsNone(r.offers[20])
        self.assertEqual(applied, [lease])
        with mock.patch.object(runner.dhcp_client, "release", lambda iface, l: released.append(l)):
            r.release()
        self.assertEqual(released, [lease])
        d = r.to_dict("0.4.0")
        self.assertEqual(d["vlans"]["tagged"], [10, 20, 30])
        self.assertEqual(d["switch"]["pvid"], 70)

    def test_root_without_a_switch_or_an_offer(self):
        class Quiet(FakeListener):
            def arrive(self):
                pass
        patches = self.common_patches() + [
            mock.patch.object(runner.dhcp_client, "obtain", lambda iface, timeout=8.0, tries=2: None),
            mock.patch.object(runner.neighbours, "Listener", Quiet),
            mock.patch.object(runner.checks, "sniff_vlans", lambda iface, seconds, stop=None: set()),
            mock.patch.object(runner.checks, "check_gateway", lambda gw, iface, row: row.set("no default route", FAIL))]
        r = self.run_with(patches, runner.Runner("eth0", root=True, check_host="", lldp_wait=1, sniff_seconds=0.1))
        rows = {k: (row.value, row.state) for k, row in r.report.items()}
        self.assertEqual(rows["IP"], ("no address", FAIL))
        self.assertEqual(rows["DHCP"], ("no offer in 8 s", FAIL))
        self.assertEqual(rows["SWITCH"], ("no LLDP/CDP heard (unmanaged switch?)", WARN))
        self.assertEqual(rows["VLAN"], ("untagged only", OK))
        self.assertEqual(r.verdict, "NO DHCP")

    def test_root_run_waits_for_the_link_to_come_back(self):
        calls = []
        patches = self.common_patches() + [
            mock.patch.object(runner.checks, "carrier", lambda iface: calls.append(1) or len(calls) > 3),
            mock.patch.object(runner.dhcp_client, "obtain", lambda iface, timeout=8.0, tries=2: None),
            mock.patch.object(runner.neighbours, "Listener", FakeListener),
            mock.patch.object(runner.checks, "sniff_vlans", lambda iface, seconds, stop=None: set()),
            mock.patch.object(runner.dhcp_probe, "dhcp_offers", lambda *a, **k: {})]
        r = self.run_with(patches, runner.Runner("eth0", root=True, lldp_wait=1, sniff_seconds=0.1))
        self.assertTrue(r.link_up)
        self.assertEqual(r.report["LINK"].value, "1G Full")
        with mock.patch.object(runner.checks, "carrier", lambda iface: False):
            r = runner.Runner("eth0", root=False).run()        # without root: no wait, the link is what it is
        self.assertEqual(r.verdict, "NO LINK")

    def test_no_link(self):
        with mock.patch.object(runner.checks, "carrier", lambda iface: False):
            r = runner.Runner("eth0").run()
        self.assertEqual((r.verdict, r.link_up, r.done), ("NO LINK", False, True))
        self.assertEqual(r.report["LINK"].value, "waiting for a link")

    def test_interrupt_stops_the_wait(self):
        class Never(FakeListener):
            def arrive(self):
                pass
        patches = self.common_patches() + [
            mock.patch.object(runner.dhcp_client, "obtain", lambda iface, timeout=8.0, tries=2: None),
            mock.patch.object(runner.neighbours, "Listener", Never),
            mock.patch.object(runner.checks, "sniff_vlans", lambda iface, seconds, stop=None: set())]
        r = runner.Runner("eth0", root=True, lldp_wait=60, sniff_seconds=0.1)
        threading.Timer(0.3, r.stop.set).start()
        import time
        t0 = time.monotonic()
        self.run_with(patches, r)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(r.report["SWITCH"].state, PENDING)


if __name__ == "__main__":
    unittest.main()

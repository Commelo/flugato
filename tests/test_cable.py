"""Cable test logic that runs without the two ports."""

import unittest

from flugato import cable
from flugato.checks import FAIL, OK, WARN

A_MAC, B_MAC = bytes.fromhex("88a29e005301"), bytes.fromhex("9c69d3005302")
GIG = ["10baseT/Full", "100baseT/Full", "1000baseT/Full"]


def port(speed, duplex="Full", supported=GIG):
    return {"speed": speed, "duplex": duplex, "supported": supported, "partner": []}


class Classify(unittest.TestCase):
    def test_only_our_ports_is_a_cable(self):
        ours = {A_MAC, B_MAC}
        self.assertEqual(cable.classify({A_MAC}, {b"\xff" * 6}, ours), "cable")
        self.assertEqual(cable.classify(set(), set(), ours), "cable")

    def test_switch_frames_or_strangers_mean_a_switch(self):
        ours = {A_MAC, B_MAC}
        stp = bytes.fromhex("0180c2000000")
        self.assertEqual(cable.classify({A_MAC}, {stp}, ours), "switch")
        self.assertEqual(cable.classify({A_MAC, bytes.fromhex("00005e005399")}, set(), ours),
                         "switch")


class Verdict(unittest.TestCase):
    def test_clean_gigabit_cable_passes(self):
        state, _ = cable.verdict(port(1000), port(1000), (20000, 20000, 0.5),
                                 (20000, 20000, 0.5), 0)
        self.assertEqual(state, OK)

    def test_slow_link_on_gigabit_ends_warns(self):
        state, text = cable.verdict(port(100), port(100), (20000, 20000, 1),
                                    (20000, 20000, 1), 0)
        self.assertEqual(state, WARN)
        self.assertIn("check the pairs", text)

    def test_loss_or_crc_fails(self):
        self.assertEqual(cable.verdict(port(1000), port(1000), (20000, 19000, 1),
                                       (20000, 20000, 1), 0)[0], FAIL)
        self.assertEqual(cable.verdict(port(1000), port(1000), (20000, 20000, 1),
                                       (20000, 20000, 1), 3)[0], FAIL)
        self.assertEqual(cable.verdict(port(None), port(1000), None, None, 0)[0], FAIL)

    def test_unknown_adapter_modes_dont_invent_a_warning(self):
        # the USB adapter's driver reports no link modes, only the speed
        state, _ = cable.verdict(port(1000), port(1000, supported=[]), (100, 100, 1),
                                 (100, 100, 1), 0)
        self.assertEqual(state, OK)


class Frames(unittest.TestCase):
    def test_test_frame_layout(self):
        f = cable.test_frame(B_MAC, A_MAC, 7, 1514)
        self.assertEqual(len(f), 1514)
        self.assertEqual(f[:6], B_MAC)
        self.assertEqual(f[6:12], A_MAC)
        self.assertEqual(int.from_bytes(f[12:14], "big"), cable.ETH_P_TEST)
        self.assertTrue(f[14:].startswith(cable.MAGIC))


if __name__ == "__main__":
    unittest.main()

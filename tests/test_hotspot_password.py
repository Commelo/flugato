"""The hotspot password: manual from the settings, or generated and kept for some days in
a file on the boot partition (a fake clock, a temp file, a fake NetworkManager)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from flugato import config, hotspot
from tests.test_wifi import FakeNM

DAY = hotspot.DAY
T0 = 1_760_000_000


def write(path, text, mode=0o644):
    with open(path, "w") as f:
        f.write(text)


class Choose(unittest.TestCase):
    def cfg(self, password="", days="7"):
        return dict(config.DEFAULTS, HOTSPOT_PASSWORD=password, HOTSPOT_PASSWORD_DAYS=days)

    def test_manual_is_used_as_is_and_never_kept(self):
        pw, source, keep = hotspot.choose_password(self.cfg("Lab Wi-Fi 2026!"), None, T0)
        self.assertEqual((pw, source, keep), ("Lab Wi-Fi 2026!", "manual", None))

    def test_a_bad_manual_one_falls_back_to_generated(self):
        _, source, _ = hotspot.choose_password(self.cfg("short"), None, T0)
        self.assertEqual(source, "new")

    def test_kept_for_the_days_then_new(self):
        state = {"password": "abcdefgh", "created": T0}
        self.assertEqual(hotspot.choose_password(self.cfg(), state, T0 + 6 * DAY)[:2], ("abcdefgh", "kept"))
        pw, source, keep = hotspot.choose_password(self.cfg(), state, T0 + 7 * DAY)
        self.assertEqual(source, "new")
        self.assertNotEqual(pw, "abcdefgh")
        self.assertEqual(keep, {"password": pw, "created": T0 + 7 * DAY})

    def test_zero_days_is_new_every_time_and_nothing_kept(self):
        state = {"password": "abcdefgh", "created": T0}
        pw, source, keep = hotspot.choose_password(self.cfg(days="0"), state, T0 + 60)
        self.assertEqual((source, keep), ("new", None))
        self.assertNotEqual(pw, "abcdefgh")

    def test_a_password_from_the_future_is_dropped(self):
        state = {"password": "abcdefgh", "created": T0 + 30 * DAY}  # the clock was wrong
        self.assertEqual(hotspot.choose_password(self.cfg(), state, T0)[1], "new")
        state = {"password": "abcdefgh", "created": T0 + 3600}      # small skew: still kept
        self.assertEqual(hotspot.choose_password(self.cfg(), state, T0)[1], "kept")

    def test_empty_or_bad_days_mean_the_default(self):
        state = {"password": "abcdefgh", "created": T0}
        for days in ("", "x", "999"):
            self.assertEqual(hotspot.choose_password(self.cfg(days=days), state, T0 + DAY)[1], "kept")

    def test_state_file_parsing(self):
        good = json.dumps({"password": "abcdefgh", "created": T0})
        self.assertEqual(hotspot.parse_state(good), {"password": "abcdefgh", "created": T0})
        for bad in ("", "junk", "[]", json.dumps({"password": "abc", "created": T0}),
                    json.dumps({"password": "ABCDEFGH", "created": T0}),
                    json.dumps({"password": "abcdefgh", "created": "today"})):
            self.assertIsNone(hotspot.parse_state(bad), bad)


class Validation(unittest.TestCase):
    def test_settings(self):
        self.assertEqual(config.validate("HOTSPOT_PASSWORD", " Lab Wi-Fi 2026! "), ("Lab Wi-Fi 2026!", None))
        self.assertEqual(config.validate("HOTSPOT_PASSWORD", ""), ("", None))
        for bad in ("short", "x" * 64, "pässwörd", "with#hash", 'with"quote'):
            self.assertIsNone(config.validate("HOTSPOT_PASSWORD", bad)[0], bad)
        self.assertEqual(config.validate("HOTSPOT_PASSWORD_DAYS", "07"), ("7", None))
        self.assertEqual(config.validate("HOTSPOT_PASSWORD_DAYS", "0"), ("0", None))
        for bad in ("-1", "366", "1.5", "a week"):
            self.assertIsNone(config.validate("HOTSPOT_PASSWORD_DAYS", bad)[0], bad)
        for key in config.AT_HOTSPOT_START:
            self.assertIn(key, config.DEFAULTS)
            self.assertIn(key, config.HELP)


class Kept(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "class", "net", "wlan0", "wireless"))
        self.state = os.path.join(self.tmp, "flugato-hotspot.json")
        self.cfg = dict(config.DEFAULTS)
        self.now = T0
        self.nm = FakeNM()
        self.out = io.StringIO()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def hotspot(self):
        """A fresh Hotspot, as after an app restart or a reboot."""
        return hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=os.path.join(self.tmp, "l"),
                               load_config=lambda: dict(self.cfg), state_file=self.state,
                               clock=lambda: self.now,
                               write=write)

    def start(self):
        hs = self.hotspot()
        with redirect_stdout(self.out), redirect_stderr(self.out):
            info = hs.start()
            hs.stop()
        return info

    def psk(self):
        add = [c for c in self.nm.cmds if c[:3] == ["nmcli", "connection", "add"]][-1]
        return add[add.index("wifi-sec.psk") + 1]

    def test_manual_password_is_used_and_never_written(self):
        self.cfg["HOTSPOT_PASSWORD"] = "Lab Wi-Fi 2026!"
        info = self.start()
        self.assertEqual((info["password"], info["password_source"], self.psk()),
                         ("Lab Wi-Fi 2026!", "manual", "Lab Wi-Fi 2026!"))
        self.assertFalse(os.path.exists(self.state))

    def test_kept_across_restarts_until_it_expires(self):
        first = self.start()
        self.assertEqual(first["password_source"], "new")
        with open(self.state) as f:
            self.assertEqual(json.load(f), {"password": first["password"], "created": T0})
        self.now += 3 * DAY                                            # a restart days later
        again = self.start()
        self.assertEqual((again["password"], again["password_source"]), (first["password"], "kept"))
        self.now = T0 + 7 * DAY + 1                                    # past the 7 days
        fresh = self.start()
        self.assertEqual(fresh["password_source"], "new")
        self.assertNotEqual(fresh["password"], first["password"])
        with open(self.state) as f:
            self.assertEqual(json.load(f)["password"], fresh["password"])
        self.assertNotIn(first["password"], self.out.getvalue())      # never logged
        self.assertNotIn(fresh["password"], self.out.getvalue())

    def test_zero_days_new_at_every_start(self):
        self.cfg["HOTSPOT_PASSWORD_DAYS"] = "0"
        seen = {self.start()["password"] for _ in range(5)}
        self.assertGreater(len(seen), 1)
        self.assertFalse(os.path.exists(self.state))

    def test_a_bad_file_is_replaced(self):
        for junk in ("not json", "\udcff", json.dumps({"password": 5})):
            with open(self.state, "w", errors="surrogateescape") as f:
                f.write(junk)
            info = self.start()
            self.assertEqual(info["password_source"], "new")
            with open(self.state) as f:
                self.assertEqual(json.load(f)["password"], info["password"])

    def test_a_setting_change_applies_at_the_next_start(self):
        first = self.start()
        self.cfg["HOTSPOT_PASSWORD"] = "Lab Wi-Fi 2026!"
        self.assertEqual(self.start()["password"], "Lab Wi-Fi 2026!")
        self.cfg["HOTSPOT_PASSWORD"] = ""                              # back to the kept one
        self.assertEqual(self.start()["password"], first["password"])

    def test_cannot_keep_it_still_runs_and_says_so_without_the_password(self):
        def broken(path, text, mode=0o644):
            raise OSError(30, "Read-only file system")
        hs = self.hotspot()
        hs.write = broken
        with redirect_stdout(self.out):
            info = hs.start()
        self.assertTrue(hs.status()["on"])
        self.assertIn("could not keep", self.out.getvalue())
        self.assertNotIn(info["password"], self.out.getvalue())

    def test_join_fallback_keeps_the_running_password(self):
        hs = self.hotspot()
        info = hs.start()
        self.now += 30 * DAY                                           # even when it expired
        self.assertEqual(hs.start(password=info["password"], _fallback=True)["password"], info["password"])
        again = hotspot.Hotspot(run=self.nm, sys_root=self.tmp, leases=os.path.join(self.tmp, "l"),
                                load_config=lambda: dict(self.cfg), state_file=self.state,
                                clock=lambda: self.now, write=lambda p, t, mode=0o644: None)
        hs.stop()
        self.assertEqual(again.start(password="abcdefgh", _fallback=True)["password_source"], "same")


if __name__ == "__main__":
    unittest.main()

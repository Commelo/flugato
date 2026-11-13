"""Gesture timing, fed through a named pipe the way /dev/input delivers events."""

import os
import tempfile
import threading
import time
import unittest

from flugato import touch


class Gestures(unittest.TestCase):
    def setUp(self):
        self.saved = touch.HOLD_SECONDS
        touch.HOLD_SECONDS = 0.6  # keep the test short
        self.dir = tempfile.mkdtemp()
        self.fifo = os.path.join(self.dir, "event")
        os.mkfifo(self.fifo)
        self.got = []
        t = touch.Touch(on_tap=lambda pos: self.got.append(("tap", pos)),
                        on_double=lambda pos: self.got.append(("double", pos)),
                        on_hold=lambda pos: self.got.append(("hold", pos)), start=False)
        threading.Thread(target=t._read, args=(self.fifo,), daemon=True).start()
        self.w = open(self.fifo, "wb", buffering=0)

    def tearDown(self):
        self.w.close()
        os.unlink(self.fifo)
        os.rmdir(self.dir)
        touch.HOLD_SECONDS = self.saved

    def ev(self, value, wait, at=None):
        self.w.write(touch.EVENT.pack(0, 0, touch.EV_KEY, touch.BTN_TOUCH, value))
        if at:  # raw positions reported while pressed: a wild first one, then steady
            for x, y in [(100, 100)] + [at] * 8:
                self.w.write(touch.EVENT.pack(0, 0, touch.EV_ABS, touch.ABS_X, x))
                self.w.write(touch.EVENT.pack(0, 0, touch.EV_ABS, touch.ABS_Y, y))
        time.sleep(wait)

    def test_gestures(self):
        centre = (1921, 2066)  # raw reading of a tap in the middle of the panel
        self.ev(1, 0.1, centre); self.ev(0, 0.7)                         # single tap
        self.ev(1, 0.1); self.ev(0, 0.2); self.ev(1, 0.1); self.ev(0, 0.7)  # double tap
        self.ev(1, 0.8); self.ev(0, 0.5)                                 # hold
        self.ev(1, 0.3); self.ev(0, 0.05); self.ev(1, 0.5); self.ev(0, 0.5)  # hold with a bounce
        self.assertEqual([g for g, _ in self.got], ["tap", "double", "hold", "hold"])
        x, y = self.got[0][1]
        self.assertTrue(200 < x < 260 and 140 < y < 190, (x, y))
        self.assertIsNone(self.got[1][1])  # no position reported

    def test_calibration(self):
        self.assertEqual(touch.parse_cal(""), touch.DEFAULT_CAL)
        self.assertEqual(touch.parse_cal("", "ili9486"), touch.PANEL_CAL["ili9486"])
        self.assertEqual(touch.parse_cal("bad", "ILI9486 "), touch.PANEL_CAL["ili9486"])
        self.assertEqual(touch.parse_cal("", "unknown"), touch.DEFAULT_CAL)
        self.assertEqual(touch.parse_cal("1,0,0,0,1,0", "ili9486"), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))
        # the ili9486 fit: the five taps that made it land on their targets within 6 px
        for raw, target in (((489, 3505), (30, 30)), ((3684, 3460), (450, 30)), ((485, 484), (30, 290)),
                            ((3757, 616), (450, 290)), ((2130, 2038), (240, 160))):
            x, y = touch.to_screen(*raw, touch.PANEL_CAL["ili9486"])
            self.assertLessEqual(abs(x - target[0]) + abs(y - target[1]), 9, (raw, target, (x, y)))
        self.assertEqual(touch.parse_cal("1,0,0,0,1,0"), (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))
        self.assertEqual(touch.to_screen(377, 474, touch.DEFAULT_CAL), (27, 33))  # cross at 30,30
        self.assertEqual(touch.to_screen(0, 0, touch.DEFAULT_CAL), (0, 0))  # clamped


class Calibration(unittest.TestCase):
    def test_fit_recovers_the_mapping(self):
        from flugato import calibrate
        true = touch.DEFAULT_CAL
        # raw readings that the default mapping sends exactly onto the targets
        a, b, c, d, e, f = true
        det = a * e - b * d
        raw = [((e * (x - c) - b * (y - f)) / det, (a * (y - f) - d * (x - c)) / det)
               for x, y in calibrate.TARGETS]
        cal = calibrate.fit(raw, calibrate.TARGETS)
        self.assertTrue(all(abs(p - q) < 1e-6 for p, q in zip(cal, true)), cal)
        self.assertLessEqual(calibrate.worst_error(cal, raw, calibrate.TARGETS), 1)

    def test_write_conf_replaces_or_appends(self):
        from flugato import calibrate
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("IFACE=eth0\nTOUCH_CAL=\n")
        try:
            calibrate.write_conf(f.name, (1, 0, 0, 0, 1, 0))
            text = open(f.name).read()
            self.assertIn("TOUCH_CAL=1.000000,0.000000,0.000000,0.000000,1.000000,0.000000", text)
            self.assertEqual(text.count("TOUCH_CAL="), 1)
            self.assertIn("IFACE=eth0", text)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()

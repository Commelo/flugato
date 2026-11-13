"""The title's wave: the title 1 mm to the right, a wave of rising orange letters every
10 to 30 s on the waiting screens, only the title bar redrawn, never at night or mid-test."""

import time
import unittest

from PIL import Image, ImageChops, ImageDraw

from flugato import checks, config, display
from flugato.__main__ import App


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Rng:
    """uniform() gives these values in turn, the last one from then on."""

    def __init__(self, *values):
        self.values = list(values)

    def uniform(self, a, b):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class Screen:
    def __init__(self):
        self.bands = []

    def show(self, img, band=None):
        self.bands.append(band)


def due():
    """A wave that starts at once, steps a frame (1/8 s) at every look, then rests long."""
    clock = Clock()
    wave = display.TitleWave(clock=clock, rng=Rng(0, 100))

    def frame():
        f = display.TitleWave.frame(wave)
        clock.t += 1 / display.TITLE_FPS
        return f
    wave.frame = frame
    return wave


class Title(unittest.TestCase):
    def test_at_rest_the_title_is_the_word_moved_1_mm(self):
        word = Image.new("RGB", (200, 38))
        ImageDraw.Draw(word).text((display.TITLE_X, 19), "fluGATO", font=display.F_TITLE,
                                  fill=display.TEXT, anchor="lm")
        parts = Image.new("RGB", (200, 38))
        display._title(ImageDraw.Draw(parts))
        self.assertIsNone(ImageChops.difference(word, parts).getbbox())  # the same pixels
        self.assertEqual(display.TITLE_X, 19)
        self.assertEqual("".join(display.TITLE_PARTS), display.TITLE)

    def test_the_wave_rises_turns_orange_and_settles(self):
        self.assertEqual(display.title_wave(0, 3), (0, display.TEXT))  # not there yet
        dy, colour = display.title_wave(4, 0)                            # the top of it
        self.assertEqual((round(dy), colour), (-3, display.FUR))
        for i in range(len(display.TITLE_PARTS)):                       # all back at the end
            self.assertEqual(display.title_wave(display.TITLE_PASS - 1, i), (0, display.TEXT))

    def test_passes_10_to_30_s_apart(self):
        clock, rng = Clock(), Rng(17)
        wave = display.TitleWave(clock=clock, rng=rng)
        self.assertIsNone(wave.frame())
        clock.t += 16.9
        self.assertIsNone(wave.frame())
        clock.t += 0.2
        self.assertEqual(wave.frame(), 0)
        clock.t += 1.0
        self.assertEqual(wave.frame(), 8)
        clock.t += 2.0                      # past the pass: rest, the next one 17 s on
        self.assertIsNone(wave.frame())
        self.assertAlmostEqual(wave.next, clock.t + 17)
        self.assertEqual(display.TITLE_GAP, (10, 30))


class Pause(unittest.TestCase):
    def setUp(self):
        import flugato.__main__ as m
        self.m = m
        self.screen = Screen()
        self.app = App(dict(config.DEFAULTS, IFACE="lo"), self.screen, "lo")
        self.app.wifi, self.app.port_b = None, None
        self.app.keep_port_b = lambda: None  # no second port from this machine
        self.saved = (m.time.sleep, checks.carrier)
        m.time.sleep = lambda s: None

    def tearDown(self):
        self.m.time.sleep, checks.carrier = self.saved

    def test_a_pass_redraws_the_title_bar_only(self):
        self.app.title = due()
        drawn = []
        screen = lambda: drawn.append(display.title_frame) or Image.new("RGB", (480, 320))
        for _ in range(4):
            self.app.pause(1, screen)
        self.assertEqual(drawn, list(range(display.TITLE_PASS)) + [None])  # then at rest
        self.assertEqual(set(self.screen.bands), {(0, display.HEADER_H)})
        self.assertIsNone(self.app.title_drawn)
        self.assertIsNone(display.title_frame)  # never left set for the other screens

    def test_no_screen_no_wave(self):
        self.app.title = due()
        self.app.title_drawn = 5  # a pass was on when a still screen came
        self.app.pause(1, None)
        self.assertEqual(self.screen.bands, [])
        self.assertIsNone(self.app.title_drawn)

    def run_no_link(self, desk, ticks=6):
        self.app.title = due()
        self.app.desk_mode = lambda quiet: desk
        self.app.forecast = lambda: None
        count = {"n": 0}

        def carrier(iface):
            count["n"] += 1
            return count["n"] > ticks
        checks.carrier = carrier
        self.app.no_link()
        return [b for b in self.screen.bands if b == (0, display.HEADER_H)]

    def test_on_no_link_and_by_day(self):
        self.assertTrue(self.run_no_link(None))
        self.screen.bands.clear()
        self.assertTrue(self.run_no_link("clock"))

    def test_not_at_night(self):
        self.assertEqual(self.run_no_link("night"), [])
        self.assertEqual(self.run_no_link("night_weather"), [])

    def test_results_only_once_the_test_is_done(self):
        checks.carrier = lambda iface: True
        self.app.title = due()
        self.app.idle(0.6)                 # mid-test (the LLDP listen): still
        self.app.title = due()
        self.assertNotIn((0, display.HEADER_H), self.screen.bands)
        self.app.idle(0.6, done=True)      # the results are in: the wave
        self.assertIn((0, display.HEADER_H), self.screen.bands)
        self.assertIsNone(self.app.title_drawn)  # leaving: at rest for the next screen


if __name__ == "__main__":
    unittest.main()

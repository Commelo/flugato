"""The cat's places: every scene draws, has its walking surface the whole width where the
cat's feet are, and is cached; the screens draw on them."""

import time
import unittest

from flugato import display, pet, scenes, weather
from flugato.demo import forecast


class Scenes(unittest.TestCase):
    def surface(self, img, row):
        """The colour of the walking surface along the whole width, at a cell row."""
        y = row * scenes.K + 1
        return {img.getpixel((x, y)) for x in range(0, scenes.W, scenes.K)}

    def test_every_place_draws_and_has_a_floor_the_whole_width(self):
        for img, row in ((scenes.closet(0), scenes.SHELF_ROW), (scenes.desk(), scenes.DESK_ROW),
                         (scenes.window("sun"), scenes.DESK_ROW), (scenes.rooftop(1), scenes.LEDGE_ROW)):
            self.assertEqual(img.size, (scenes.W, scenes.H))
            self.assertEqual(len(self.surface(img, row)), 1)  # one colour, edge to edge

    def test_the_floors_meet_the_cats_feet(self):
        feet = display.BIG_CAT_Y + pet.CH * display.BIG_PET_SCALE
        self.assertEqual(feet, scenes.SHELF_ROW * scenes.K)             # NO LINK, night
        self.assertEqual(display.DESK_CAT[1] + pet.CH * display.BIG_PET_SCALE,
                         scenes.DESK_ROW * scenes.K)                     # desk mode

    def test_every_weather_and_the_night(self):
        for kind in weather.KINDS:
            for night in (False, True):
                for frame in range(3):
                    self.assertEqual(scenes.window(kind, night, frame).size, (scenes.W, scenes.H))
        self.assertNotEqual(list(scenes.window("rain", False, 0).getdata()),
                            list(scenes.window("rain", False, 1).getdata()))  # the rain falls

    def test_the_mugs_steam_rises_in_a_loop_over_the_mug(self):
        from PIL import ImageChops
        frames = [scenes.desk(f) for f in range(scenes.STEAM_FRAMES)]
        k = scenes.K
        for a, b in zip(frames, frames[1:] + frames[:1]):
            box = ImageChops.difference(a, b).getbbox()
            self.assertIsNotNone(box)                        # it moves every frame
            # only the steam changes: over the mug, below the monitor's edge, under the wall top
            self.assertGreaterEqual(box[0], 36 * k)
            self.assertLessEqual(box[2], 43 * k)
            self.assertLessEqual(box[3], (scenes.DESK_ROW - 8) * k)
        self.assertEqual(scenes.desk(scenes.STEAM_FRAMES).tobytes(), frames[0].tobytes())  # loops

    def test_cached_and_handed_out_as_copies(self):
        a, b = scenes.desk(), scenes.desk()
        self.assertIsNot(a, b)
        a.putpixel((0, 0), (255, 0, 0))
        self.assertNotEqual(scenes.desk().getpixel((0, 0)), (255, 0, 0))

    def test_the_screens_use_them(self):
        now = time.localtime()
        img = display.render_message("NO LINK", "plug in a cable", "eth0", mood="sleep",
                                     pose="loaf", scene="closet")
        self.assertEqual(img.getpixel((5, scenes.SHELF_ROW * scenes.K + 1)),
                         scenes.closet().getpixel((5, scenes.SHELF_ROW * scenes.K + 1)))
        plain = display.render_message("PORT LED ON", "blink", "eth0", mood="blink_on")
        self.assertEqual(plain.getpixel((5, 300)), display.BG)  # the other messages: no place
        for f in (forecast("rain"), None):
            img = display.render_desk(now, weather.in_units(f) if f else None,
                                      {"tests": 1, "fails": 0}, "plug in a cable", "eth0", pose="loaf")
            self.assertEqual(img.size, (display.W, display.H))
        self.assertEqual(display.render_night(now, "plug in a cable", "eth0").size, (display.W, display.H))

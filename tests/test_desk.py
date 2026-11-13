"""NO LINK's fun: desk mode (clock, weather presenter, night), the variety show, the laser
dot, the outfits, the day's tests and the fish after a pass."""

import time
import unittest

from flugato import checks, config, display, pet
from flugato.__main__ import App
from flugato.demo import forecast


class Screen:
    def __init__(self):
        self.shown = 0

    def show(self, img, band=None):
        self.shown += 1


CFG = {"CHECK_HOST": "", "LLDP_WAIT": "1", "FB": "", "ROTATE": "", "IFACE": "lo", "TOUCH_CAL": ""}


class Outfits(unittest.TestCase):
    def test_every_pose_renders_with_a_hat_and_held_things_only_when_sitting(self):
        for pose in pet.POSES:
            for wear in (["witch"], ["santa"], ["nightcap"], ["rain", "umbrella", "bow"],
                         ["snow", "scarf", "pointer", "sunglasses"]):
                rows, x, y = pet.dressed("sleep", pose, 1, wear)
                self.assertEqual(len(rows), pet.CH + pet.PAD_T)
                self.assertEqual({len(r) for r in rows}, {pet.CW + pet.PAD_L + pet.PAD_R})
                self.assertEqual((x, y), (pet.PAD_L, pet.PAD_T))
        loaf = "".join(pet.dressed("sleep", "loaf", 0, ["pointer", "umbrella"])[0])
        self.assertNotIn("u", loaf)  # no umbrella, no pointer on a lying cat
        self.assertNotIn("x", loaf)
        sitting = "".join(pet.dressed("sleep", "sit", 0, ["umbrella"])[0])
        self.assertIn("u", sitting)

    def test_a_mirrored_pose_keeps_its_hat_on_its_head(self):
        rows, x, y = pet.dressed("sleep", "stand_L", 0, ["witch"])
        self.assertEqual(x, pet.PAD_R)
        plain, _, _ = pet.dressed("sleep", "stand", 0, ["witch"])
        self.assertEqual(rows, [r[::-1] for r in plain])

    def test_the_sprite_is_offset_to_where_the_bare_cat_would_be(self):
        sp, dx, dy = pet.dressed_sprite("sleep", 0, 5, "sit", ["bow"])
        self.assertEqual((dx, dy), (pet.PAD_L * 5, pet.PAD_T * 5))
        self.assertEqual(sp.size, ((pet.CW + pet.PAD_L + pet.PAD_R) * 5, (pet.CH + pet.PAD_T) * 5))

    def test_season_and_outfit(self):
        self.assertEqual(pet.season(10, 1), "witch")
        self.assertEqual(pet.season(12, 24), "santa")
        self.assertIsNone(pet.season(12, 27))
        self.assertIsNone(pet.season(6, 15))
        self.assertIn("umbrella", pet.outfit("rain")[0])
        self.assertIn("sunglasses", pet.outfit("sun")[0])
        self.assertNotIn("sunglasses", pet.outfit("sun", night=True)[0])
        self.assertEqual(pet.outfit("storm")[1], "fail")
        wear, _ = pet.outfit("cloud", hat="santa")
        self.assertIn("santa", wear)
        self.assertNotIn("bow", wear)  # the hat replaces the bow


class Acts(unittest.TestCase):
    def test_the_show_plays_every_act_and_comes_home(self):
        import random
        for start in ("loaf", "sit"):
            seq = pet.variety(random.Random(1), start)
            poses = [p for p, _, _ in seq]
            for acts in (pet.IDLE_ACTS, pet.INLINE_ACTS, pet.ROUTER_ACTS, pet.WEB_ACTS):
                for act in acts.values():
                    self.assertTrue(set(p for p, _, _ in act) <= set(poses))
            self.assertEqual(sum(t * dx for _, t, dx in seq), 0)  # the walks come back
            for p in poses:
                pet.cells("sleep", p)  # every pose exists
        self.assertEqual(pet.variety(random.Random(1))[-1][0], "loaf")

    def test_chase_goes_there_pounces_and_comes_back(self):
        for n in (20, 12, -15, -5, 0):
            seq, catch = pet.chase(n)
            ticks = pet.expand(seq)
            reach = max(0, abs(n) - pet.PAW_REACH) * (1 if n >= 0 else -1)
            self.assertEqual(sum(dx for _, dx in ticks[:catch]), reach)  # paws on the dot
            self.assertEqual(ticks[catch][0], "pin" + ("_L" if n < 0 else ""))  # lands on it
            self.assertEqual(sum(dx for _, dx in ticks), 0)
        self.assertTrue(any(p.endswith("_L") for p, _ in pet.expand(pet.chase(-5)[0])[:10]))

    def test_the_walk_is_a_profile_stride_and_the_chase_a_prowl(self):
        ticks = pet.expand(pet.SLEEP_ACTS["walk"])
        walking = [(p, dx) for p, dx in ticks if p.startswith("walk")]
        self.assertEqual([p for p, _ in walking[:4]], ["walk1", "walk2", "walk3", "walk4"])
        self.assertTrue(all(abs(dx) == 1 for _, dx in walking))  # one cell a frame: no sliding
        seq, catch = pet.chase(9)
        ticks = pet.expand(seq)
        self.assertTrue(all(p.startswith(("prowl", "leap")) for p, dx in ticks[:catch] if dx))
        poses = [p for p, _ in ticks]
        for p in ("crouch_up", "leap1", "leap2", "pin_peek", "huh", "huh_l"):
            self.assertIn(p, poses)
        self.assertLess(poses.index("crouch_up"), poses.index("leap1"))
        self.assertLess(poses.index("pin_peek"), poses.index("huh"))
        self.assertIn("Y", "".join(pet.cells("sleep", "huh")))  # the big yellow "?"
        self.assertTrue(all(p.startswith("walk") for p, dx in ticks[catch:] if dx))
        rows = pet.cells("sleep", "walk2")
        self.assertEqual(sum(r.count("k") for r in rows[:12]), 4)  # one eye (2x2, catchlight) + mouth

    def test_idle_names_what_plays(self):
        idle = pet.Idle(pet.SLEEP_BASE, {}, {})
        idle.tick()
        self.assertIsNone(idle.current)
        idle.play([("hop", 2, 0)], "poke")
        idle.tick()
        self.assertEqual(idle.current, "poke")
        idle.tick()
        self.assertIsNone(idle.current)


class Screens(unittest.TestCase):
    def test_desk_screens_render(self):
        now = time.localtime()
        stats = {"tests": 2, "fails": 0}
        for kind in ("sun", "part", "cloud", "fog", "rain", "snow", "storm"):
            wear, mood = pet.outfit(kind, False, "witch")
            img = display.render_desk(now, forecast(kind), stats, "plug in a cable", "eth0",
                                      wear=wear, mood=mood, button="Wi-Fi Hotspot")
            self.assertEqual(img.size, (display.W, display.H))
        img = display.render_desk(now, None, stats, "plug in a cable", "eth0", pose="loaf")
        self.assertEqual(img.size, (display.W, display.H))
        self.assertEqual(display.render_night(now, "plug in a cable", "eth0").size,
                         (display.W, display.H))

    def test_day_line(self):
        self.assertEqual(display.day_line({"tests": 0, "fails": 0}), "today: no tests yet")
        self.assertEqual(display.day_line({"tests": 1, "fails": 0}), "today: 1 test")
        self.assertEqual(display.day_line({"tests": 7, "fails": 2}), "today: 7 tests · 2 fails")

    def test_the_cat_is_where_it_walked_to(self):
        x = display.BIG_CAT_X + 70
        self.assertTrue(display.big_cat_hit((x, display.BIG_CAT_Y + 40)))
        self.assertFalse(display.big_cat_hit((x, display.BIG_CAT_Y + 40), xcells=40))
        self.assertTrue(display.big_cat_hit((x + 200, display.BIG_CAT_Y + 40), xcells=40))
        self.assertFalse(display.big_cat_hit(None))

    def test_settings(self):
        self.assertEqual(config.validate("WEATHER_LAT", "-10.25"), ("-10.25", None))
        self.assertIsNone(config.validate("WEATHER_LAT", "91")[0])
        self.assertEqual(config.validate("WEATHER_LON", "179.5"), ("179.5", None))
        self.assertIsNone(config.validate("WEATHER_LON", "abc")[0])
        self.assertEqual(config.validate("DESK_MINUTES", "0"), ("0", None))
        self.assertIsNone(config.validate("SHOW_MINUTES", "999")[0])
        self.assertEqual(config.validate("WEATHER_UNITS", "c"), ("C", None))
        self.assertIsNone(config.validate("WEATHER_UNITS", "K")[0])
        self.assertEqual(config.DEFAULTS["WEATHER_UNITS"], "F")


class NoLink(unittest.TestCase):
    """The NO LINK loop with the carrier, the clock and the screens faked."""

    def setUp(self):
        import flugato.__main__ as m
        self.m = m
        self.app = App(dict(CFG), Screen(), "lo")
        self.app.wifi, self.app.port_b = None, None
        self.calls = []
        self.saved = (checks.carrier, m.time.sleep, checks.time_synced, display.render_message,
                      display.render_desk, display.render_night, pet.variety)

        def spy(name, real):
            def f(*a, **k):
                self.calls.append((name, k))
                return real(*a, **k)
            return f

        display.render_message = spy("message", display.render_message)
        display.render_desk = spy("desk", display.render_desk)
        display.render_night = spy("night", display.render_night)
        pet.variety = spy("variety", pet.variety)
        checks.time_synced = lambda: True
        m.time.sleep = lambda s: None

    def tearDown(self):
        (checks.carrier, self.m.time.sleep, checks.time_synced, display.render_message,
         display.render_desk, display.render_night, pet.variety) = self.saved

    def run_ticks(self, n, before=None):
        count = {"n": 0}

        def carrier(iface):
            count["n"] += 1
            if before:
                before(count["n"])
            return count["n"] > n

        checks.carrier = carrier
        self.app.no_link()

    def test_desk_mode_only_when_set(self):
        self.app.cfg["DESK_MINUTES"] = "0"
        self.run_ticks(5)
        self.assertNotIn("desk", [c[0] for c in self.calls])  # 0: never
        self.app.cfg["DESK_MINUTES"] = "3"
        self.app.desk_mode = lambda quiet: "weather"
        self.app.forecast = lambda: forecast("rain")
        self.calls.clear()
        self.run_ticks(5)
        desk = [k for name, k in self.calls if name == "desk"]
        self.assertTrue(desk)

    def test_desk_mode_needs_the_clock_and_knows_the_night(self):
        self.app.cfg["DESK_MINUTES"] = "3"
        self.assertIsNone(self.app.desk_mode(60))
        checks.time_synced = lambda: False
        self.assertIsNone(self.app.desk_mode(600))
        checks.time_synced = lambda: True
        self.assertIn(self.app.desk_mode(600), ("clock", "night"))  # no weather set up

    def test_the_weather_comes_and_goes_day_and_night(self):
        self.app.cfg["DESK_MINUTES"] = "3"
        self.app.forecast = lambda: forecast()
        start = 180
        for turn in (60, 15):  # the default DESK_TURN, then a setting
            self.app.cfg["DESK_TURN"] = str(turn)
            for hour, shown in ((14, ("weather", "clock")), (23, ("night_weather", "night"))):
                self.app.local = lambda when=None, h=hour: time.struct_time((2026, 10, 10, h, 0, 0, 5, 283, 0))
                got = [self.app.desk_mode(start + t) for t in (1, turn + 1, 2 * turn + 1)]
                self.assertEqual(got, [shown[0], shown[1], shown[0]])
        self.app.forecast = lambda: None  # no forecast: the clock all along
        self.assertEqual(self.app.desk_mode(start + 1), "night")

    def test_night_hours_setting(self):
        self.app.cfg["DESK_MINUTES"] = "3"
        self.app.forecast = lambda: None
        at = lambda h: (lambda when=None: time.struct_time((2026, 10, 10, h, 0, 0, 5, 283, 0)))
        for value, hour, shown in (("22-7", 23, "night"), ("22-7", 8, "clock"), ("1-5", 23, "clock"),
                                   ("1-5", 3, "night"), ("off", 2, "clock"), ("bad", 23, "night")):
            self.app.cfg["NIGHT_HOURS"] = value
            self.app.local = at(hour)
            self.assertEqual(self.app.desk_mode(200), shown, (value, hour))

    def test_a_tap_away_from_the_cat_is_a_laser_dot(self):
        self.app.on_tap((420, 110))
        self.run_ticks(6)
        lasers = [k.get("laser") for name, k in self.calls if name == "message"]
        self.assertIn((420, 110), lasers)

    def test_a_tap_on_the_cat_still_pokes_it(self):
        self.app.on_tap((display.BIG_CAT_X + 60, display.BIG_CAT_Y + 40))
        self.run_ticks(6)
        poses = [k.get("pose") for name, k in self.calls if name == "message"]
        self.assertIn("loaf_scared", poses)
        self.assertFalse(any(k.get("laser") for name, k in self.calls if name == "message"))

    def test_the_variety_show_after_the_show_minutes(self):
        self.app.cfg.update(DESK_MINUTES="0", SHOW_MINUTES="1")
        clock = {"t": 1000.0}
        saved = time.monotonic
        time.monotonic = lambda: clock["t"]
        try:
            def later(n):
                clock["t"] += 10  # ten seconds a tick: the minute passes on tick 7
            self.run_ticks(9, later)
        finally:
            time.monotonic = saved
        self.assertEqual([k for name, k in self.calls if name == "variety"], [{"start": "loaf"}])


class Zone(unittest.TestCase):
    def test_the_clock_follows_the_place_s_zone(self):
        app = App(dict(CFG), Screen(), "lo")
        cast = forecast()
        app.forecast = lambda: cast
        self.assertIsNone(app.zone())  # the demo forecast has no zone: the Pi's own
        cast["tz"] = "Asia/Tokyo"
        self.assertEqual(app.local(0).tm_hour, 9)  # 1970-01-01 09:00 in Tokyo
        self.assertEqual(app.local(0).tm_mday, 1)
        cast["tz"] = "Not/AZone"
        self.assertIsNone(app.zone())
        app.forecast = lambda: None
        self.assertIsNone(app.zone())

    def test_weather_never_starts_from_the_tests(self):
        app = App(dict(CFG), Screen(), "lo")
        app.settings_saved(dict(CFG))  # empty place: would look the IP up, if it were on
        self.assertIsNone(app.weather)
        self.assertEqual(config.validate("WEATHER_LAT", "Off"), ("off", None))


class WifiClient(unittest.TestCase):
    def test_parse_client(self):
        from flugato import hotspot
        self.assertEqual(hotspot.parse_client("100 (connected)\nhome\n192.0.2.10/24 | 198.51.100.2/24",
                                              "Home Net\ninfrastructure"),
                         {"ssid": "Home Net", "address": "192.0.2.10"})
        self.assertIsNone(hotspot.parse_client("100 (connected)\nflugato-hotspot\n10.42.0.1/24",
                                               "fluGATO\nap"))
        self.assertIsNone(hotspot.parse_client("70 (connecting)\nhome\n", "home\ninfrastructure"))
        self.assertIsNone(hotspot.parse_client("30 (disconnected)\n\n", ""))

    def test_no_link_shows_the_network_in_the_top_bar(self):
        import flugato.__main__ as m
        from flugato import hotspot

        class Radio:
            def available(self):
                return True

            def status(self):
                return {"on": False}

        app = App(dict(CFG), Screen(), "lo")
        app.wifi, app.port_b = Radio(), None
        seen, reads = [], []
        saved = (checks.carrier, m.time.sleep, hotspot.client, display.render_message)
        count = {"n": 0}

        def carrier(iface):
            count["n"] += 1
            return count["n"] > 6

        def client():
            reads.append(1)
            return {"ssid": "lab-wifi", "address": "192.0.2.10"}

        def render(*a, **k):
            seen.append(k.get("wifi"))
            return saved[3](*a, **k)

        checks.carrier, m.time.sleep, hotspot.client, display.render_message = carrier, lambda s: None, client, render
        try:
            app.no_link()
        finally:
            checks.carrier, m.time.sleep, hotspot.client, display.render_message = saved
        self.assertIn({"ssid": "lab-wifi", "address": "192.0.2.10"}, seen)
        self.assertEqual(len(reads), 1)  # read every 5 s, not every tick
        img = display.render_message("NO LINK", "plug in a cable", "eth0", mood="sleep",
                                     wifi={"ssid": "a network name far too long for the bar",
                                           "address": "198.51.100.123"})
        self.assertEqual(img.size, (display.W, display.H))


class DayCount(unittest.TestCase):
    def test_tests_fails_and_the_fish_after_a_pass(self):
        app = App(dict(CFG), Screen(), "lo")
        app.count_test(failed=True)
        self.assertIsNone(app.pet.current)
        app.count_test(failed=False)
        self.assertEqual(app.pet.current, "treat")
        self.assertEqual((app.day_stats()["tests"], app.day_stats()["fails"]), (2, 1))
        app.pet.queue, app.pet.current = [], None
        app.count_test(failed=False)  # a pass after a pass: no fish
        self.assertIsNone(app.pet.current)


if __name__ == "__main__":
    unittest.main()

"""The weather board's data: the Open-Meteo URL, codes to pictures and summaries, parsing
the answer, and the Weather keeper with a fake fetch and a fake clock (never the network).
Made-up coordinates."""

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout

from flugato import weather

LAT, LON = 10.0, 20.0


def sample(code=2, day_code=61, temp=17.6, hi=21.4, lo=11.5, rain=80, is_day=1):
    """An answer shaped like Open-Meteo's for forecast_days=1."""
    return {
        "latitude": LAT, "longitude": LON, "generationtime_ms": 0.05, "utc_offset_seconds": 0,
        "timezone": "GMT", "timezone_abbreviation": "GMT", "elevation": 12.0,
        "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C",
                          "weather_code": "wmo code", "is_day": ""},
        "current": {"time": "2026-10-10T14:15", "interval": 900, "temperature_2m": temp,
                    "weather_code": code, "is_day": is_day},
        "daily_units": {"time": "iso8601", "weather_code": "wmo code",
                        "temperature_2m_max": "°C", "temperature_2m_min": "°C",
                        "precipitation_probability_max": "%"},
        "daily": {"time": ["2026-10-10"], "weather_code": [day_code],
                  "temperature_2m_max": [hi], "temperature_2m_min": [lo],
                  "precipitation_probability_max": [rain]},
    }


class UrlTest(unittest.TestCase):
    def test_params(self):
        u = weather.url(LAT, LON)
        self.assertTrue(u.startswith("https://api.open-meteo.com/v1/forecast?"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        self.assertEqual(q["latitude"], ["10.0"])
        self.assertEqual(q["longitude"], ["20.0"])
        self.assertEqual(q["current"], ["temperature_2m,weather_code,is_day"])
        self.assertEqual(q["daily"], ["weather_code,temperature_2m_max,temperature_2m_min,"
                                      "precipitation_probability_max"])
        self.assertEqual(q["timezone"], ["auto"])
        self.assertEqual(q["forecast_days"], ["1"])
        self.assertIn("current=temperature_2m%2Cweather_code%2Cis_day", u)

    def test_edges_and_bad(self):
        weather.url(-90, 180)
        weather.url(90, -180)
        for lat, lon in ((90.1, 0), (-91, 0), (0, 180.5), (0, -181), (float("nan"), 0),
                         (0, float("inf")), ("north", 0), (None, 0)):
            with self.assertRaises(ValueError, msg=(lat, lon)):
                weather.url(lat, lon)

    def test_place(self):
        self.assertEqual(weather.place({"WEATHER_LAT": "10.0", "WEATHER_LON": " 20 "}),
                         (10.0, 20.0))
        self.assertIsNone(weather.place({}))
        self.assertIsNone(weather.place({"WEATHER_LAT": "10.0", "WEATHER_LON": ""}))
        self.assertIsNone(weather.place({"WEATHER_LAT": "", "WEATHER_LON": "20"}))
        self.assertIsNone(weather.place({"WEATHER_LAT": "100", "WEATHER_LON": "20"}))
        self.assertIsNone(weather.place({"WEATHER_LAT": "ten", "WEATHER_LON": "20"}))


class KindSummaryTest(unittest.TestCase):
    def test_kind_bands(self):
        bands = {"sun": (0, 1), "part": (2,), "cloud": (3, 4, 50, 70, 90, 100),
                 "fog": (45, 48), "rain": (51, 55, 61, 65, 66, 67, 80, 82),
                 "snow": (71, 75, 77, 85, 86), "storm": (95, 96, 99)}
        for expected, codes in bands.items():
            for code in codes:
                self.assertEqual(weather.kind(code), expected, code)
        self.assertEqual(set(bands), set(weather.KINDS))

    def test_summary_samples(self):
        self.assertEqual(weather.summary(0, 5), "clear · rain 5%")
        self.assertEqual(weather.summary(2, 20), "partly cloudy · rain 20%")
        self.assertEqual(weather.summary(63, 80), "rain · 80%")
        self.assertEqual(weather.summary(95, 70), "thunderstorms · 70%")
        self.assertEqual(weather.summary(73, None), "snow")
        self.assertEqual(weather.summary(45, None), "fog")
        self.assertEqual(weather.summary(3, None), "cloudy")

    def test_summary_length(self):
        for code in range(0, 100):
            for rain in (None, 0, 5, 55, 100):
                self.assertLessEqual(len(weather.summary(code, rain)), 26, (code, rain))


class ParseTest(unittest.TestCase):
    def test_sample(self):
        got = weather.parse(sample(), 1000.0)
        self.assertEqual(got, {"kind": "part", "night": False, "temp": 18, "hi": 21, "lo": 12,
                               "rain": 80, "summary": "rain · 80%", "code": 2, "at": 1000.0,
                               "tz": "GMT", "raw": {"temp": 17.6, "hi": 21.4, "lo": 11.5}})

    def test_night_and_current_kind(self):
        got = weather.parse(sample(code=0, day_code=3, rain=10, is_day=0), 5)
        self.assertTrue(got["night"])
        self.assertEqual(got["kind"], "sun")  # the current sky, not the day's
        self.assertEqual(got["summary"], "cloudy · rain 10%")

    def test_null_rain(self):
        got = weather.parse(sample(day_code=0, rain=None), 5)
        self.assertIsNone(got["rain"])
        self.assertEqual(got["summary"], "clear")

    def test_negative_temperatures(self):
        got = weather.parse(sample(temp=-3.4, hi=-0.6, lo=-8.5, day_code=71, rain=40), 5)
        self.assertEqual((got["temp"], got["hi"], got["lo"]), (-3, -1, -8))
        self.assertEqual(got["kind"], "part")
        self.assertEqual(got["summary"], "snow · 40%")

    def test_missing_or_wrong(self):
        broken = []
        for part, key in (("current", "temperature_2m"), ("current", "weather_code"),
                          ("current", "is_day"), ("daily", "weather_code"),
                          ("daily", "temperature_2m_max"), ("daily", "temperature_2m_min"),
                          ("daily", "precipitation_probability_max")):
            raw = sample()
            del raw[part][key]
            broken.append(raw)
        raw = sample()
        raw["current"]["temperature_2m"] = "warm"
        broken.append(raw)
        raw = sample()
        raw["daily"]["temperature_2m_max"] = []
        broken.append(raw)
        raw = sample()
        raw["current"]["weather_code"] = 2.5
        broken.append(raw)
        raw = sample()
        del raw["daily"]
        broken.append(raw)
        broken += [None, [], "text", {}]
        for raw in broken:
            with self.assertRaises(ValueError):
                weather.parse(raw, 5)


class FakeFetch:
    """Returns the queued answers in turn; an exception in the queue is raised."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, lat, lon, timeout=10):
        self.calls.append((lat, lon))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class WeatherTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "weather.json")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make(self, fetch, **kw):
        return weather.Weather(LAT, LON, path=self.path, fetch=fetch, clock=self.clock, **kw)

    def test_refresh_writes_cache(self):
        w = self.make(FakeFetch(sample()))
        self.assertIsNone(w.current())
        self.assertTrue(w.refresh())
        self.assertEqual(w.current()["summary"], "rain · 80%")
        with open(self.path) as f:
            cached = json.load(f)
        self.assertEqual(cached["forecast"]["temp"], 18)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_expiry(self):
        w = self.make(FakeFetch(sample()))
        w.refresh()
        self.clock.now += 3600
        self.assertIsNotNone(w.current(max_age=7200))
        self.clock.now += 3601
        self.assertIsNone(w.current(max_age=7200))
        self.assertIsNotNone(w.current(max_age=86400))

    def test_new_instance_loads_cache(self):
        self.make(FakeFetch(sample())).refresh()
        fetch = FakeFetch(OSError("unused"))
        again = self.make(fetch)
        self.assertEqual(again.current()["temp"], 18)
        self.assertEqual(fetch.calls, [])

    def test_cache_for_another_place_is_ignored(self):
        self.make(FakeFetch(sample())).refresh()
        other = weather.Weather(LAT, 21.0, path=self.path, fetch=FakeFetch(sample()),
                                clock=self.clock)
        self.assertIsNone(other.current())

    def test_unreadable_cache(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.make(FakeFetch(sample())).current())

    def test_failure_keeps_old_forecast_and_logs_once(self):
        fetch = FakeFetch(sample(), urllib.error.URLError(OSError(101, "Network is unreachable")),
                          ValueError("bad"), sample(code=95))
        w = self.make(fetch)
        self.assertTrue(w.refresh())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(w.refresh())
            self.assertFalse(w.refresh())
        self.assertEqual(out.getvalue(), "weather: Network is unreachable\n")
        self.assertEqual(w.current()["kind"], "part")
        self.assertNotIn("10.0", out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertTrue(w.refresh())
        self.assertEqual(w.current()["kind"], "storm")

    def test_bad_answer_fails(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(self.make(FakeFetch({"current": {}})).refresh())
        self.assertTrue(out.getvalue().startswith("weather: unreadable answer"))
        self.assertFalse(os.path.exists(self.path))

    def test_http_error_reason(self):
        err = urllib.error.HTTPError(weather.url(LAT, LON), 429, "Too Many Requests", {}, None)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertFalse(self.make(FakeFetch(err)).refresh())
        self.assertEqual(out.getvalue(), "weather: HTTP 429\n")

    def test_set_place_drops_forecast(self):
        fetch = FakeFetch(sample())
        w = self.make(fetch)
        w.refresh()
        w.set_place(-10.0, -20.0)
        self.assertIsNone(w.current())
        self.assertFalse(os.path.exists(self.path))
        w.refresh()
        self.assertEqual(fetch.calls[-1], (-10.0, -20.0))
        self.assertIsNotNone(w.current())
        with self.assertRaises(ValueError):
            w.set_place(95, 0)

    def test_place_change_during_fetch_is_dropped(self):
        w = None

        def fetch(lat, lon, timeout=10):
            w.set_place(-10.0, -20.0)
            return sample()

        w = self.make(fetch)
        self.assertFalse(w.refresh())
        self.assertIsNone(w.current())

    def test_bad_place(self):
        with self.assertRaises(ValueError):
            weather.Weather(91, 0, path=self.path)

    def test_thread_start_stop(self):
        done = threading.Event()
        calls = []

        def fetch(lat, lon, timeout=10):
            calls.append(1)
            if len(calls) >= 3:
                done.set()
            return sample()

        w = self.make(fetch, every=0.01, retry=0.01)
        w.start()
        w.start()  # once only
        self.assertTrue(done.wait(5))
        w.stop()
        w._thread.join(5)
        self.assertFalse(w._thread.is_alive())
        self.assertIsNotNone(w.current())


if __name__ == "__main__":
    unittest.main()


class AutoPlaceTest(unittest.TestCase):
    """No place in the settings: the IP address says where the Pi is."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "weather.json")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_locate_reads_the_loc_field(self):
        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        saved = weather.urllib.request.urlopen
        try:
            weather.urllib.request.urlopen = lambda req, timeout: Answer(
                json.dumps({"ip": "192.0.2.7", "loc": "10.0000,20.5000",
                            "timezone": "Etc/GMT"}).encode())
            self.assertEqual(weather.locate(), (10.0, 20.5))
            weather.urllib.request.urlopen = lambda req, timeout: Answer(b'{"ip": "192.0.2.7"}')
            with self.assertRaises(ValueError):
                weather.locate()
        finally:
            weather.urllib.request.urlopen = saved

    def test_auto_locates_at_every_refresh_and_caches(self):
        places = [(10.0, 20.0), (-10.0, -20.0)]
        fetch = FakeFetch(sample())
        w = weather.Weather(path=self.path, fetch=fetch, clock=self.clock,
                            locate=lambda: places.pop(0))
        self.assertTrue(w.auto)
        self.assertTrue(w.refresh())
        self.assertTrue(w.refresh())
        self.assertEqual(fetch.calls, [(10.0, 20.0), (-10.0, -20.0)])  # the tester moved
        self.assertEqual(w.place, (-10.0, -20.0))
        again = weather.Weather(path=self.path, fetch=FakeFetch(OSError("unused")),
                                clock=self.clock, locate=lambda: (0.0, 0.0))
        self.assertEqual(again.current()["temp"], 18)  # an auto cache is good for auto

    def test_a_failed_lookup_keeps_the_forecast(self):
        def nowhere():
            raise OSError(101, "Network is unreachable")
        w = weather.Weather(path=self.path, fetch=FakeFetch(sample()), clock=self.clock,
                            locate=lambda: (10.0, 20.0))
        w.refresh()
        w._locate = nowhere
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(w.refresh())
        self.assertIn("weather: Network is unreachable", out.getvalue())
        self.assertIsNotNone(w.current())

    def test_set_place_switches_between_fixed_and_auto(self):
        w = weather.Weather(LAT, LON, path=self.path, fetch=FakeFetch(sample()), clock=self.clock,
                            locate=lambda: (-10.0, -20.0))
        w.set_place()
        self.assertTrue(w.auto)
        w.refresh()
        self.assertEqual(w.place, (-10.0, -20.0))
        w.set_place(LAT, LON)
        self.assertFalse(w.auto)

    def test_off(self):
        self.assertTrue(weather.disabled({"WEATHER_LAT": "OFF"}))
        self.assertFalse(weather.disabled({"WEATHER_LAT": ""}))
        self.assertFalse(weather.disabled({}))


class UnitsTest(unittest.TestCase):
    def test_fahrenheit_from_the_unrounded_celsius(self):
        f = weather.parse(sample(temp=17.6, hi=21.4, lo=11.5), 1000.0)
        got = weather.in_units(f)  # F is the default
        self.assertEqual((got["temp"], got["hi"], got["lo"], got["unit"]), (64, 71, 53, "F"))
        self.assertEqual(weather.in_units(f, "c")["temp"], 18)
        self.assertEqual(weather.in_units(f, "C")["unit"], "C")
        self.assertEqual(f["temp"], 18)  # the forecast itself is untouched

    def test_old_cache_without_raw(self):
        f = {"temp": 20, "hi": 25, "lo": -40}
        got = weather.in_units(f, "F")
        self.assertEqual((got["temp"], got["hi"], got["lo"]), (68, 77, -40))


class RetryTest(unittest.TestCase):
    def test_quick_retries_after_a_failure_then_the_usual_wait(self):
        w = weather.Weather(LAT, LON, path=os.devnull, every=3600, retry=300)
        self.assertEqual([w.wait_after(False, n) for n in (1, 2, 3, 4, 5)], [30, 60, 120, 240, 300])
        self.assertEqual(w.wait_after(True, 0), 3600)


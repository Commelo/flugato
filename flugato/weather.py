"""The weather board for desk mode on the NO LINK screen: today's forecast for one place,
from Open-Meteo (no account, no key), fetched over the Pi's Wi-Fi network.

One call an hour is plenty for a board that shows the current sky, today's high and low
and the chance of rain. A daemon thread keeps it fresh; the screen only ever reads the
last forecast and never waits on the network.

The last forecast is also kept in /run/flugato-weather.json (RAM): an app restart shows
it at once, a reboot forgets it. The place is in that file too, so a forecast for an old
place is never shown after the settings changed. The coordinates never go to the log.

With no place in the settings, the place comes from the Pi's public IP address (ipinfo.io,
no account), looked up again at every refresh: a pocket tester moves. The forecast also
tells the place's time zone, which the desk clock follows."""

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.open-meteo.com/v1/forecast"
LOCATE = "https://ipinfo.io/json"
KINDS = ("sun", "part", "cloud", "fog", "rain", "snow", "storm")
PATH = "/run/flugato-weather.json"
SUMMARY_MAX = 26


def _coords(lat, lon):
    """(lat, lon) as floats, or ValueError when they are not a place on Earth."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("latitude and longitude are numbers") from None
    if not (math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude is -90 to 90, longitude -180 to 180")
    return lat, lon


def url(lat, lon):
    lat, lon = _coords(lat, lon)
    return API + "?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "timezone": "auto", "forecast_days": 1})


def fetch(lat, lon, timeout=10):
    """The forecast JSON as a dict. Raises OSError (network, HTTP) or ValueError (not JSON)."""
    req = urllib.request.Request(url(lat, lon), headers={"User-Agent": "fluGATO"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(1 << 20)  # the answer is about 1 kB; never more than 1 MB
    return json.loads(body)


def locate(timeout=10):
    """(lat, lon) of the Pi's public IP address. Raises OSError or ValueError."""
    req = urllib.request.Request(LOCATE, headers={"User-Agent": "fluGATO",
                                                  "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read(1 << 16))
    loc = data.get("loc") if isinstance(data, dict) else None
    if not isinstance(loc, str) or loc.count(",") != 1:
        raise ValueError("no location for this address")
    return _coords(*loc.split(","))


def disabled(cfg):
    """WEATHER_LAT=off: no weather and no IP lookup."""
    return (cfg.get("WEATHER_LAT") or "").strip().lower() == "off"


def place(cfg):
    """(lat, lon) from WEATHER_LAT and WEATHER_LON in the settings, or None when either is
    missing, empty or not a place on Earth."""
    lat, lon = (cfg.get("WEATHER_LAT") or "").strip(), (cfg.get("WEATHER_LON") or "").strip()
    if not lat or not lon:
        return None
    try:
        return _coords(lat, lon)
    except ValueError:
        return None


def kind(code):
    """WMO weather code -> one of KINDS, for the picture on the board."""
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "part"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 95 <= code <= 99:
        return "storm"
    return "cloud"


def _label(code):
    if code == 0:
        return "clear"
    if code == 1:
        return "mostly clear"
    if code == 2:
        return "partly cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67:
        return "rain"
    if 80 <= code <= 82:
        return "showers"
    if 71 <= code <= 77:
        return "snow"
    if code in (85, 86):
        return "snow showers"
    if 95 <= code <= 99:
        return "thunderstorms"
    return "cloudy"


def summary(code, rain):
    """One short line for the board, at most SUMMARY_MAX characters. When it rains or
    snows the percentage says how likely; on a dry day it says the chance of rain."""
    label = _label(code)
    if rain is None:
        text = label
    elif kind(code) in ("rain", "snow", "storm"):
        text = f"{label} · {rain}%"
    else:
        text = f"{label} · rain {rain}%"
    return text[:SUMMARY_MAX]


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} is not a number")
    return value


def _code(value, what):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} is not a weather code")
    return value


def _today(daily, key):
    values = daily.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"no daily {key}")
    return values[0]


def parse(raw, now):
    """Open-Meteo's answer -> the board's forecast. The picture and the temperature are
    the current ones; the summary, high, low and rain chance are today's.
    Raises ValueError when a field is missing or of the wrong type."""
    if not isinstance(raw, dict):
        raise ValueError("the answer is not an object")
    cur, daily = raw.get("current"), raw.get("daily")
    if not isinstance(cur, dict) or not isinstance(daily, dict):
        raise ValueError("no current or daily forecast")
    code = _code(cur.get("weather_code"), "current weather_code")
    temp = _number(cur.get("temperature_2m"), "current temperature_2m")
    is_day = _number(cur.get("is_day"), "current is_day")
    day_code = _code(_today(daily, "weather_code"), "daily weather_code")
    hi = _number(_today(daily, "temperature_2m_max"), "daily temperature_2m_max")
    lo = _number(_today(daily, "temperature_2m_min"), "daily temperature_2m_min")
    rain = _today(daily, "precipitation_probability_max")
    if rain is not None:  # null when the model has no probability for the place
        rain = int(round(_number(rain, "daily precipitation_probability_max")))
    tz = raw.get("timezone")  # the place's zone ("timezone": "auto" in the request)
    return {"kind": kind(code), "night": is_day == 0, "temp": int(round(temp)),
            "hi": int(round(hi)), "lo": int(round(lo)), "rain": rain,
            "summary": summary(day_code, rain), "code": code, "at": float(now),
            "tz": tz if isinstance(tz, str) and 0 < len(tz) < 64 else None,
            # unrounded Celsius, so Fahrenheit is converted from the real value
            "raw": {"temp": float(temp), "hi": float(hi), "lo": float(lo)}}


def in_units(forecast, units="F"):
    """The forecast with temp, hi and lo in units ("F", the default, or "C"), rounded, and
    "unit" set. Open-Meteo answers in Celsius; a forecast cached before "raw" existed is
    converted from its rounded values."""
    unit = "C" if str(units).strip().upper() == "C" else "F"
    raw = forecast.get("raw") or {k: forecast[k] for k in ("temp", "hi", "lo")}
    conv = (lambda c: int(round(c))) if unit == "C" else (lambda c: int(round(c * 9 / 5 + 32)))
    return dict(forecast, unit=unit, **{k: conv(raw[k]) for k in ("temp", "hi", "lo")})


def _reason(e):
    """A short reason for the log; never the URL (it holds the coordinates)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        return getattr(reason, "strerror", None) or str(reason) or "network error"
    if isinstance(e, TimeoutError):
        return "timed out"
    if isinstance(e, OSError):
        return e.strerror or type(e).__name__
    if isinstance(e, ValueError):
        return f"unreadable answer ({e})" if str(e) else "unreadable answer"
    return type(e).__name__


def _valid(forecast):
    return (isinstance(forecast, dict) and forecast.get("kind") in KINDS
            and isinstance(forecast.get("at"), (int, float))
            and all(k in forecast for k in ("night", "temp", "hi", "lo", "rain", "summary")))


class Weather:
    """Keeps a forecast fresh in a daemon thread."""

    def __init__(self, lat=None, lon=None, path=PATH, every=3600, retry=300, fetch=fetch,
                 clock=time.time, locate=locate):
        # lat None: the place comes from the IP address at every refresh (auto)
        self.auto = lat is None
        self._place = None if self.auto else _coords(lat, lon)
        self._locate = locate
        self.path, self.every, self.retry = path, every, retry
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._forecast = None
        self._loaded = False  # the cache file is read on first use, not at start-up
        self._generation = 0  # bumped by set_place: a fetch already under way is dropped
        self._failing = False
        self._wake, self._stopped = threading.Event(), threading.Event()
        self._thread = None

    @property
    def place(self):
        """(lat, lon) the forecast is for."""
        with self._lock:
            return self._place

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopped.set()
        self._wake.set()

    def wait_after(self, ok, failures):
        """Seconds to the next refresh: `every` after a success; after failures 30 s, 60,
        120, then `retry` (at boot the Wi-Fi comes up a minute or two after the app: the
        forecast, and with it the place's time zone, should not wait 5 minutes for it)."""
        if ok:
            return self.every
        return min(self.retry, 30 * 2 ** max(0, failures - 1))

    def _run(self):
        failures = 0
        while not self._stopped.is_set():
            ok = self.refresh()
            failures = 0 if ok else failures + 1
            self._wake.wait(self.wait_after(ok, failures))
            self._wake.clear()

    def refresh(self):
        """One fetch, parse and save. False on any failure; the old forecast stays."""
        with self._lock:
            auto, place, generation = self.auto, self._place, self._generation
        try:
            if auto:
                place = _coords(*self._locate())
            forecast = parse(self._fetch(*place), self._clock())
        except Exception as e:  # the thread must live on whatever the network sends
            return self._failed(e)
        with self._lock:
            if generation != self._generation:  # the place changed while we fetched
                return False
            self._place = place
            self._forecast, self._loaded = forecast, True
        try:
            self._save({"place": list(place), "auto": auto, "forecast": forecast})
        except OSError as e:
            return self._failed(e)
        self._failing = False
        return True

    def _failed(self, e):
        if not self._failing:  # once per failure streak, not every retry
            print(f"weather: {_reason(e)}", flush=True)
            self._failing = True
        return False

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def _load(self):
        """The cached forecast when it is for the current place, else None."""
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not (isinstance(data, dict) and isinstance(data.get("place"), list)
                and _valid(data.get("forecast"))):
            return None
        if self.auto and data.get("auto") is True:  # wherever the IP said it was
            return data["forecast"]
        if not self.auto and tuple(data["place"]) == self._place:
            return data["forecast"]
        return None

    def current(self, max_age=6 * 3600):
        """The last forecast, or None when there is none or it is older than max_age."""
        with self._lock:
            if not self._loaded:
                self._forecast, self._loaded = self._load(), True
            forecast = self._forecast
        # either way: a clock set after the fetch can put it in the future too
        if forecast is None or abs(self._clock() - forecast["at"]) > max_age:
            return None
        return dict(forecast)

    def set_place(self, lat=None, lon=None):
        """The settings changed: forget the old place's forecast and fetch the new one now.
        lat None: from the IP address from now on."""
        new = None if lat is None else _coords(lat, lon)
        with self._lock:
            self.auto, self._place = new is None, new
            self._generation += 1
            self._forecast, self._loaded = None, True
        try:
            os.remove(self.path)
        except OSError:
            pass
        self._failing = False
        self._wake.set()

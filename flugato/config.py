"""Settings from a KEY=value file. Site-specific values such as CHECK_HOST live only there,
never in the repository.

The file lives on the boot partition (/boot/firmware/flugato.conf): it survives the
read-only root, and it can be edited from any PC with the SD card in a reader.
/etc/flugato.conf is still read when there is no file on the boot partition (installs
from before the move; pi/setup.sh moves it once)."""

import os
import re
import subprocess
import threading

BOOT_PATHS = ("/boot/firmware/flugato.conf", "/boot/flugato.conf")
ETC_PATH = "/etc/flugato.conf"

DEFAULTS = {
    "IFACE": "",        # empty: first wired interface
    "CHECK_HOST": "",   # name used for the DNS and INTERNET checks; empty skips them
    "LLDP_WAIT": "90",  # seconds to wait for LLDP/CDP after link up
    "PANEL": "",        # mz61581 (Tontec, the default) or ili9486; setup.sh installs its overlay
    "FB": "",           # framebuffer device; empty: auto-detect the SPI panel
    "ROTATE": "",       # 0/90/180/270; empty: rotate portrait panels to landscape
    "TOUCH_CAL": "",    # a,b,c,d,e,f raw touch -> screen; empty: the PANEL's default
    "PROBE_VLANS": "",  # VLANs to ask DHCP on even if never heard tagged, e.g. 60,61
    "CABLE_IFACE": "",  # second port for the cable test; empty: the first other wired port
    "HOTSPOT_PASSWORD": "",       # Wi-Fi hotspot password; empty: a generated one
    "HOTSPOT_PASSWORD_DAYS": "7",  # days a generated password is kept; 0: new at every start
    "WEATHER_LAT": "",  # where the weather on the desk screen is for; empty: from the IP; off
    "WEATHER_LON": "",
    "WEATHER_UNITS": "F",  # F or C on the weather board
    "WEB_PIN": "1234",  # PIN for the web UI on a joined Wi-Fi network; off: hotspot only
    "DESK_MINUTES": "3",   # minutes of NO LINK before the desk screen (clock, weather); 0: never
    "SHOW_MINUTES": "10",  # minutes between the cat's variety shows on NO LINK; 0: never
    "HOTSPOT_SSID": "fluGATO",  # the hotspot's network name
    "NIGHT_HOURS": "22-7",  # desk mode's night, from-to hours; off: no night screen
    "DESK_TURN": "60",     # seconds the weather board and the clock take turns in desk mode
    "POWER_ASK": "10",     # seconds "Turn off?" waits before it cancels itself
}

PANELS = ("mz61581", "ili9486")   # the overlays in pi/overlays/, flugato-<panel>.dts

# read at every hotspot start, not at app start (see hotspot.py)
AT_HOTSPOT_START = ("HOTSPOT_SSID", "HOTSPOT_PASSWORD", "HOTSPOT_PASSWORD_DAYS")

# the web settings page, a group each: (title, what it covers, keys); every key once
CATEGORIES = (
    ("Network tests", "the ports, the DNS and internet checks, LLDP, VLANs",
     ("IFACE", "CABLE_IFACE", "CHECK_HOST", "LLDP_WAIT", "PROBE_VLANS")),
    ("Wi-Fi and web UI", "the hotspot's name and password, the PIN",
     ("HOTSPOT_SSID", "HOTSPOT_PASSWORD", "HOTSPOT_PASSWORD_DAYS", "WEB_PIN")),
    ("Desk mode and weather", "the clock, the night, the weather, the variety show",
     ("DESK_MINUTES", "DESK_TURN", "NIGHT_HOURS", "SHOW_MINUTES", "WEATHER_LAT", "WEATHER_LON",
      "WEATHER_UNITS")),
    ("Screen and power", "the panel, the touch, the power-off question",
     ("PANEL", "FB", "ROTATE", "TOUCH_CAL", "POWER_ASK")),
)

# one line each, for the web settings page
HELP = {
    "IFACE": "Wired port to test. Empty: the first wired port (eth0).",
    "CHECK_HOST": "Name looked up for the DNS row and reached on port 443 for INTERNET. "
                  "Empty skips both.",
    "LLDP_WAIT": "Seconds to listen for LLDP/CDP after link up (switches send every 30-60 s).",
    "PANEL": "mz61581 (Tontec MZ61581, the default) or ili9486 (the 3.5\" RPi LCD (A) kind "
             "with XPT2046 touch). The installer sets up its overlay: after a change, run "
             "deploy.sh again and reboot. Also picks the default touch calibration.",
    "FB": "Framebuffer device, e.g. /dev/fb0. Empty: find the SPI panel.",
    "ROTATE": "0, 90, 180 or 270. Empty: portrait panels are turned to landscape.",
    "TOUCH_CAL": "Six numbers a,b,c,d,e,f from the calibration screen. Empty: the PANEL's "
                 "measured default.",
    "PROBE_VLANS": "VLANs to ask DHCP on even if never heard tagged, comma separated, e.g. 60,61.",
    "CABLE_IFACE": "Second port for the cable test and inline mode. Empty: the first other "
                   "wired port (eth1).",
    "HOTSPOT_SSID": "Name of the Wi-Fi hotspot fluGATO makes, up to 32 characters (no #, \" "
                    "or '). Default fluGATO.",
    "HOTSPOT_PASSWORD": "Password for the Wi-Fi hotspot, 8 to 63 characters (no #, "
                        "\" or '). Empty: a generated one, shown on the panel.",
    "HOTSPOT_PASSWORD_DAYS": "Days a generated hotspot password is kept, across restarts, "
                             "before a new one is made. 0: a new one at every start. Default 7.",
    "WEATHER_LAT": "Latitude for the weather on the desk screen, e.g. 10.5 (south is "
                   "negative). Empty: found from the Pi's internet address (ipinfo.io). "
                   "off: no weather, the clock only.",
    "WEATHER_LON": "Longitude for the weather, e.g. -20.25 (west is negative). The forecast "
                   "comes from Open-Meteo over the Wi-Fi network, once an hour.",
    "WEATHER_UNITS": "F (Fahrenheit, the default) or C (Celsius) for the temperatures on the "
                     "weather board.",
    "WEB_PIN": "PIN for these pages on the Wi-Fi network fluGATO joined, 4 to 8 digits "
               "(default 1234: change it). off: the web UI on the hotspot only. The hotspot "
               "needs no PIN.",
    "DESK_MINUTES": "Minutes of NO LINK with no touch before the desk screen (clock, weather, "
                    "the day's tests). 0: never. Default 3.",
    "SHOW_MINUTES": "Minutes of NO LINK with no touch between the cat's variety shows (every "
                    "act once). 0: never. Default 10.",
    "NIGHT_HOURS": "When desk mode shows the night screen (the cat asleep on the rooftop), "
                   "from-to in hours 0 to 23, e.g. 22-7. off: never. Default 22-7.",
    "DESK_TURN": "Seconds the weather board and the clock each stay up in desk mode before "
                 "they swap, 10 to 3600. Default 60.",
    "POWER_ASK": "Seconds the panel's \"Turn off?\" waits for an answer before it cancels "
                 "itself, 3 to 60. Default 10.",
}

_IFNAME = re.compile(r"[A-Za-z0-9_.-]{1,15}")
_HOST = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?)+")
_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE]-?[0-9]+)?")
_save_lock = threading.RLock()


def _digits(value):
    return value.isascii() and value.isdigit()  # "²".isdigit() is True, int("²") fails


def path(given=None):
    """The file to read and write: `given` when it names some other file; otherwise the
    boot partition's when it exists, then /etc's when it exists, then the boot
    partition's (where a new one is made). /etc/flugato.conf as `given` means "the
    default", so callers that still pass it get the boot partition's file."""
    if given and given != ETC_PATH:
        return given
    for candidate in BOOT_PATHS + (ETC_PATH,):
        if os.path.isfile(candidate):
            return candidate
    for candidate in BOOT_PATHS:
        if os.path.isdir(os.path.dirname(candidate)):
            return candidate
    return ETC_PATH


def parse(text):
    cfg = dict(DEFAULTS)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.split("#", 1)[0].strip().strip("\"'")
    return cfg


def load(given=None):
    """Settings from path(given); defaults where a key is missing or there is no file."""
    try:
        with open(path(given)) as f:
            return parse(f.read())
    except OSError:
        return dict(DEFAULTS)


def validate(key, value):
    """(clean value, None) or (None, short reason) for one setting. Empty is always
    allowed: it means the default."""
    value = value.strip()
    if any(c in value for c in "\n\r#\"'"):
        return None, "no line breaks, quotes or #"
    if value == "":
        return value, None
    if key in ("IFACE", "CABLE_IFACE"):
        return (value, None) if _IFNAME.fullmatch(value) else (None, "an interface name, e.g. eth0")
    if key == "CHECK_HOST":
        return (value, None) if _HOST.fullmatch(value) else (None, "a host name")
    if key == "LLDP_WAIT":
        ok = _digits(value) and 5 <= int(value) <= 600
        return (str(int(value)), None) if ok else (None, "seconds, 5 to 600")
    if key == "PANEL":
        return (value.lower(), None) if value.lower() in PANELS else (None, "mz61581 or ili9486")
    if key == "FB":
        return (value, None) if re.fullmatch(r"/dev/fb[0-9]{1,2}", value) else (None, "e.g. /dev/fb0")
    if key == "ROTATE":
        return (value, None) if value in ("0", "90", "180", "270") else (None, "0, 90, 180 or 270")
    if key == "TOUCH_CAL":
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 6 and all(_NUMBER.fullmatch(p) for p in parts):
            return ",".join(parts), None
        return None, "six numbers separated by commas"
    if key == "HOTSPOT_PASSWORD":
        ok = 8 <= len(value) <= 63 and all(32 <= ord(c) <= 126 for c in value)
        return (value, None) if ok else (None, "8 to 63 letters, digits or symbols")
    if key == "HOTSPOT_PASSWORD_DAYS":
        ok = _digits(value) and int(value) <= 365
        return (str(int(value)), None) if ok else (None, "days, 0 to 365")
    if key == "WEATHER_LAT" and value.lower() == "off":
        return "off", None
    if key in ("WEATHER_LAT", "WEATHER_LON"):
        limit = 90 if key == "WEATHER_LAT" else 180
        ok = re.fullmatch(r"-?[0-9]{1,3}(\.[0-9]{1,6})?", value) and -limit <= float(value) <= limit
        return (value, None) if ok else (None, f"degrees, -{limit} to {limit}, e.g. 10.5")
    if key == "WEB_PIN":
        if value.lower() == "off":
            return "off", None
        ok = _digits(value) and 4 <= len(value) <= 8
        return (value, None) if ok else (None, "4 to 8 digits, or off")
    if key == "WEATHER_UNITS":
        return (value.upper(), None) if value.upper() in ("F", "C") else (None, "F or C")
    if key in ("DESK_MINUTES", "SHOW_MINUTES"):
        ok = _digits(value) and int(value) <= 240
        return (str(int(value)), None) if ok else (None, "minutes, 0 to 240")
    if key == "HOTSPOT_SSID":
        ok = len(value.encode()) <= 32 and all(32 <= ord(c) <= 126 for c in value)
        return (value, None) if ok else (None, "up to 32 letters, digits or symbols")
    if key == "NIGHT_HOURS":
        if value.lower() == "off":
            return "off", None
        hours = night_hours({"NIGHT_HOURS": value}, None)
        return (f"{hours[0]}-{hours[1]}", None) if hours else (None, "from-to hours, e.g. 22-7")
    if key == "DESK_TURN":
        ok = _digits(value) and 10 <= int(value) <= 3600
        return (str(int(value)), None) if ok else (None, "seconds, 10 to 3600")
    if key == "POWER_ASK":
        ok = _digits(value) and 3 <= int(value) <= 60
        return (str(int(value)), None) if ok else (None, "seconds, 3 to 60")
    if key == "PROBE_VLANS":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if parts and all(_digits(p) and 1 <= int(p) <= 4094 for p in parts):
            return ",".join(str(int(p)) for p in parts), None
        return None, "VLAN numbers 1 to 4094, comma separated"
    return value, None


def night_hours(cfg, default=(22, 7)):
    """(from, to) hours of desk mode's night from NIGHT_HOURS ("22-7"); None for off; the
    default when the value is no good (a hand edit)."""
    value = str(cfg.get("NIGHT_HOURS", "22-7")).strip().lower()
    if value == "off":
        return None
    m = re.fullmatch(r"([0-9]{1,2}) *- *([0-9]{1,2})", value)
    if m and int(m[1]) <= 23 and int(m[2]) <= 23 and int(m[1]) != int(m[2]):
        return int(m[1]), int(m[2])
    return default


def is_night(hours, hour):
    """hour (0-23) is inside (from, to); the night may run past midnight (22-7) or not (1-5)."""
    if not hours:
        return False
    start, end = hours
    return start <= hour < end if start < end else hour >= start or hour < end


def setting(cfg, key, low, high):
    """A whole-number setting, the default when it is no good, kept within low..high."""
    try:
        value = int(str(cfg.get(key, DEFAULTS[key])).strip())
    except ValueError:
        value = int(DEFAULTS[key])
    return max(low, min(high, value))


def update_text(text, values):
    """The file text with each KEY=value line in `values` replaced, comments and order
    kept; keys not in the file yet are added at the end. A key set twice (a hand edit)
    keeps one line, where it first was: parse() would otherwise read the later one."""
    out, done = [], set()
    for line in text.splitlines():
        key = None
        if not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
        if key in values:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in done]
    return "\n".join(out) + "\n"


def _replace(target, text, mode=0o644):
    tmp = target + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.chmod(tmp, mode)  # also when a leftover .new had other bits (vfat ignores both)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)  # a power pull leaves the old file or the new one, never half
    os.sync()  # vfat has no journal: get the rename onto the card now, not in 30 s


def _mount_point(target):
    mount = os.path.dirname(os.path.abspath(target))
    while not os.path.ismount(mount) and mount != "/":
        mount = os.path.dirname(mount)
    return mount


def _remount(mount, mode):
    subprocess.run(["mount", "-o", f"remount,{mode}", mount], check=False, capture_output=True)


def write_file(target, text, remount=_remount, mode=0o644):
    """Writes `text` to `target` safely (temp file, rename, sync). A boot partition
    mounted read-only is remounted read-write for the write and read-only again after.
    Raises OSError when it can't write."""
    with _save_lock:  # two writes at once would share the temp file and the remount
        try:
            _replace(target, text, mode)
        except OSError as e:
            if e.errno != 30:  # EROFS
                raise
            mount = _mount_point(target)
            remount(mount, "rw")
            try:
                _replace(target, text, mode)
            finally:
                remount(mount, "ro")


def save(values, given=None, remount=_remount):
    """Writes `values` into path(given), keeping everything else in the file (see
    write_file). Returns the path written; raises OSError when it can't write."""
    target = path(given)
    with _save_lock:  # read and write as one, against a second save in between
        try:
            with open(target) as f:
                text = f.read()
        except FileNotFoundError:
            text = ""
        write_file(target, update_text(text, values), remount)
    return target

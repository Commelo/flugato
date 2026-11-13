"""Wi-Fi networks for fluGATO to join, typed into the web UI (SSID and password; hidden
networks too, there is no scan list).

They are kept in flugato-wifi.json next to flugato.conf on the boot partition, so they
survive the read-only root. vfat has no file permissions: the passwords sit on the card
in the clear (accepted), so the file is only as private as the SD card.

At app start, sync() loads them into NetworkManager as in-memory profiles (`save no`,
like the hotspot): nothing lands in /etc/NetworkManager, a reboot forgets them, and the
next start loads them again. The Pi's own home network profile is never touched; ours
are all named flugato-wifi-<8 hex of the SSID>."""

import hashlib
import json
import os

from . import checks, config

FILE = "flugato-wifi.json"
PREFIX = "flugato-wifi-"
MAX_SAVED = 16


def path():
    """flugato-wifi.json in the folder of the settings file (the boot partition)."""
    return os.path.join(os.path.dirname(config.path()), FILE)


def validate(ssid, password):
    """(None) when good, else a short reason. SSID: 1 to 32 bytes. Password: empty for
    an open network, else 8 to 63 printable ASCII characters (a WPA2 passphrase)."""
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeError:  # a lone surrogate from a hand-edited file
        return "the network name is not valid text"
    if not 1 <= size <= 32:
        return "the network name is 1 to 32 bytes"
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        return "no control characters in the name"
    if password and not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)):
        return "the password is 8 to 63 characters (letters, digits, symbols), or empty for an open network"
    return None


def parse(text, strict=False):
    """The file's JSON -> [{ssid, password}], skipping anything malformed. strict: None
    instead when anything had to be skipped, so a save never drops what it couldn't read."""
    try:
        items = json.loads(text or "[]")
    except ValueError:
        return None if strict else []
    if not isinstance(items, list):
        return None if strict else []
    saved = []
    for item in items:
        if (isinstance(item, dict) and isinstance(item.get("ssid"), str)
                and isinstance(item.get("password", ""), str)
                and validate(item["ssid"], item.get("password", "")) is None):
            saved.append({"ssid": item["ssid"], "password": item.get("password", "")})
    if strict and (len(saved) != len(items) or len(saved) > MAX_SAVED):
        return None
    return saved[:MAX_SAVED]


def _read(where):
    try:  # utf-8-sig: a file saved by an editor on a PC may start with a BOM
        with open(where or path(), encoding="utf-8-sig") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        return None


def load(where=None):
    text = _read(where)
    return parse(text) if text is not None else []


def _load_for_change(where):
    """The saved list for add/forget, or ValueError when the file holds something we
    can't read: rewriting it would lose that."""
    text = _read(where)
    saved = parse(text, strict=True) if text is not None else None
    if saved is None:
        raise ValueError("the networks file on the SD card has entries fluGATO can't read: "
                         "fix or delete flugato-wifi.json first")
    return saved


def active(run=checks.run, iface="wlan0"):
    """The NetworkManager profile wlan0 is on now ("" when none)."""
    return (run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface]) or "").strip()


def _write(saved, where=None, write=config.write_file):
    write(where or path(), json.dumps(saved, indent=1, ensure_ascii=False) + "\n", mode=0o600)


def profile(ssid):
    return PREFIX + hashlib.sha1(ssid.encode("utf-8")).hexdigest()[:8]


def add_command(ssid, password, iface="wlan0"):
    """nmcli arguments for an in-memory client profile. hidden yes: the Pi asks for the
    name directly, so a hidden network is found too (and a visible one just the same).
    autoconnect no: only a Connect joins it, so NetworkManager never moves wlan0 off the
    home network on its own."""
    cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", iface,
           "con-name", profile(ssid), "autoconnect", "no", "save", "no", "ssid", ssid,
           "802-11-wireless.hidden", "yes", "ipv4.method", "auto", "ipv4.may-fail", "no"]
    if password:
        cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    return cmd


def nm_add(net, run=checks.run, iface="wlan0"):
    """(Re)creates the network's profile; True when NetworkManager took it."""
    run(["nmcli", "connection", "delete", profile(net["ssid"])])
    return run(add_command(net["ssid"], net["password"], iface)) is not None


def nm_ours(run=checks.run):
    return [n for n in (run(["nmcli", "-t", "-f", "NAME", "connection", "show"]) or "").splitlines()
            if n.startswith(PREFIX)]


def sync(run=checks.run, where=None, iface="wlan0"):
    """At app start: NetworkManager holds exactly the saved networks (ours only, the
    home network profile stays as it is). Returns the SSIDs loaded."""
    saved = load(where)
    wanted = {profile(n["ssid"]) for n in saved}
    # after an app restart the Pi may be on one of ours: re-creating it would drop the link
    on = active(run, iface)
    for name in nm_ours(run):
        if name not in wanted:
            run(["nmcli", "connection", "delete", name])
    return [n["ssid"] for n in saved
            if profile(n["ssid"]) == on or nm_add(n, run, iface)]


def add(ssid, password, run=checks.run, where=None, write=config.write_file, iface="wlan0"):
    """Saves a network (the same SSID again replaces its password) and loads it into
    NetworkManager. Raises ValueError with the reason when it doesn't validate, OSError
    when the file can't be written."""
    reason = validate(ssid, password)
    if reason:
        raise ValueError(reason)
    if active(run, iface) == profile(ssid):  # re-creating it would drop the link we're on
        raise ValueError("fluGATO is on this network now: change it from the hotspot")
    with config._save_lock:  # read and write as one against a second save at once
        saved = [n for n in _load_for_change(where) if n["ssid"] != ssid]
        if len(saved) >= MAX_SAVED:
            raise ValueError(f"{MAX_SAVED} networks at most: forget one first")
        saved.append({"ssid": ssid, "password": password})
        _write(saved, where, write)
    nm_add(saved[-1], run, iface)
    return saved


def forget(ssid, run=checks.run, where=None, write=config.write_file, iface="wlan0"):
    """Removes a saved network from the file and from NetworkManager. Not the one wlan0
    is on: deleting it would leave the Pi on no network at all (ValueError)."""
    if active(run, iface) == profile(ssid):
        raise ValueError("fluGATO is on this network now: forget it from the hotspot")
    with config._save_lock:
        saved = _load_for_change(where)
        left = [n for n in saved if n["ssid"] != ssid]
        if len(left) != len(saved):
            _write(left, where, write)
    run(["nmcli", "connection", "delete", profile(ssid)])
    return left


def find(ssid, where=None):
    return next((n for n in load(where) if n["ssid"] == ssid), None)

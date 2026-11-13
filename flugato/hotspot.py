"""Wi-Fi mode: the Pi becomes an access point, SSID fluGATO (HOTSPOT_SSID), with a short
password shown on the screen, so a phone can reach the web UI (settings, captures).

The password (choose_password, read at every hotspot start): HOTSPOT_PASSWORD from the
settings when set, used as is; else a generated one, kept for HOTSPOT_PASSWORD_DAYS
across restarts and reboots in flugato-hotspot.json next to flugato.conf on the boot
partition (with the time it was made; 0 days: a new one at every start, nothing kept).
Like the saved Wi-Fi networks, a kept password sits on the card in the clear. A manual
password is never copied into that file.

The hotspot REPLACES the Pi's own Wi-Fi client on wlan0 while it is on (one radio can't
do both reliably); when it goes off, wlan0 rejoins the network it was on before.

The NetworkManager profile is made in memory only (`save no`): NetworkManager keeps it
under /run (RAM, root only), a reboot forgets it, and stop() deletes it. We never log the
password; it is visible in nmcli's command line for the moment that call runs. A crashed app can't leave the Pi stuck as an access point:
the service's stop step runs `python3 -m flugato.hotspot cleanup`, which deletes the
profile and reconnects the client.

ipv4.method shared gives phones an address (NetworkManager runs dnsmasq for it, package
dnsmasq-base). It also turns on forwarding with NAT towards the main routing table's
default route: while the hotspot is on there is none (wlan0's client is gone and the test
port routes in its own table), so phones reach the Pi and nothing beyond. A wired
management link with a default route in the main table would change that."""

import json
import os
import secrets
import subprocess
import sys
import threading
import time

from . import checks, config, networks

IFACE = "wlan0"
PROFILE = "flugato-hotspot"
SSID = "fluGATO"
# lower case only (no shift on a phone keyboard), without i, l and o: nothing to mistake
# for 1, I or 0 on the small screen. 8 letters is WPA2's minimum, about 36 bits.
LETTERS = "abcdefghjkmnpqrstuvwxyz"
LEASES = "/var/lib/NetworkManager/dnsmasq-{iface}.leases"
STATE_FILE = "flugato-hotspot.json"
DAY = 86400


class HotspotError(Exception):
    """The hotspot could not come up; the text says why, short enough for the screen."""


def new_password(length=8):
    return "".join(secrets.choice(LETTERS) for _ in range(length))


def state_path():
    """flugato-hotspot.json in the folder of the settings file (the boot partition)."""
    return os.path.join(os.path.dirname(config.path()), STATE_FILE)


def parse_state(text):
    """The kept password's file -> {password, created} or None when it isn't one of ours."""
    try:
        state = json.loads(text or "")
    except ValueError:
        return None
    if (isinstance(state, dict) and isinstance(state.get("password"), str)
            and len(state["password"]) == 8 and set(state["password"]) <= set(LETTERS)
            and isinstance(state.get("created"), (int, float))):
        return {"password": state["password"], "created": state["created"]}
    return None


def choose_password(cfg, state, now):
    """(password, source, state to write or None) for the next hotspot start.
    source: "manual" (HOTSPOT_PASSWORD), "kept" (the generated one, still within
    HOTSPOT_PASSWORD_DAYS) or "new". A kept one made more than a day "in the future" is
    dropped: the clock was wrong when it was made (a Pi without network time at boot)."""
    manual, problem = config.validate("HOTSPOT_PASSWORD", cfg.get("HOTSPOT_PASSWORD", ""))
    if manual and not problem:
        return manual, "manual", None
    days, problem = config.validate("HOTSPOT_PASSWORD_DAYS", cfg.get("HOTSPOT_PASSWORD_DAYS", ""))
    days = int(days) if days and not problem else int(config.DEFAULTS["HOTSPOT_PASSWORD_DAYS"])
    if days and state and -DAY < now - state["created"] < days * DAY:
        return state["password"], "kept", None
    password = new_password()
    return password, "new", ({"password": password, "created": now} if days else None)


def add_command(iface, password, ssid=SSID, profile=PROFILE):
    """nmcli arguments that create the hotspot profile, in memory only."""
    return ["nmcli", "connection", "add", "type", "wifi", "ifname", iface, "con-name", profile,
            "autoconnect", "no", "save", "no", "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "disabled",
            # WPA2 with AES only, no PMF: the Pi's Wi-Fi chip (brcmfmac) is picky in AP mode
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable",
            "wifi-sec.psk", password]


def parse_leases(text):
    """dnsmasq leases ("expiry mac ip name client-id" per line) -> [{mac, ip, name}]."""
    leases = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            leases.append({"mac": parts[1], "ip": parts[2],
                           "name": None if parts[3] == "*" else parts[3]})
    return leases


def join_reason(err):
    """A short reason from nmcli's error text when joining a network fails."""
    text = (err or "").lower()
    if "secrets were required" in text or "password" in text or "802-1x" in text:
        return "wrong password"
    if "could not be found" in text or "no network with ssid" in text or "not found" in text:
        return "network not found"
    if "ip configuration" in text or "dhcp" in text:
        return "no address from the network (DHCP)"
    if "timeout" in text or "timed out" in text:
        return "no answer in time"
    return "could not connect"


def _run_err(cmd, timeout=10):
    """(ok, stderr): for the one call whose error text the screen shows."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as e:
        return False, str(e)
    return res.returncode == 0, res.stderr


def _profiles(run):
    return (run(["nmcli", "-t", "-f", "NAME", "connection", "show"]) or "").splitlines()


def _reconnect(run, iface, previous=None):
    """Hands wlan0 back to its client connection: the one it had, else whatever
    NetworkManager picks for it."""
    if previous and run(["nmcli", "--wait", "0", "connection", "up", "uuid", previous]) is not None:
        return
    run(["nmcli", "--wait", "0", "device", "connect", iface])


def _idle(run, iface):
    """True when wlan0 is on no network and not on its way to one (NetworkManager device
    state below 40: unmanaged, unavailable, disconnected)."""
    state = (run(["nmcli", "-g", "GENERAL.STATE", "device", "show", iface]) or "").strip()
    number = state.split(" ", 1)[0]
    return number.isdigit() and int(number) < 40


def cleanup(run=checks.run, iface=IFACE):
    """Removes a hotspot left behind (the app died with it on) and puts wlan0 back on the
    client network. Also when the app died half-way through joining a network: then
    there is no hotspot, and wlan0 may be on nothing at all. Returns what it did."""
    done = []
    if PROFILE in _profiles(run):
        run(["nmcli", "connection", "delete", PROFILE])
        _reconnect(run, iface)
        done = [f"deleted {PROFILE}", f"reconnected {iface}"]
    elif os.path.isdir(f"/sys/class/net/{iface}") and _idle(run, iface):
        run(["nmcli", "--wait", "0", "device", "connect", iface])
        done = [f"reconnected {iface}"]
    return done


class Hotspot:
    """start() / stop() / status(), safe from any thread. start() takes a few seconds
    (the access point coming up): call it off the drawing loop."""

    def __init__(self, iface=IFACE, run=checks.run, sys_root="/sys", leases=LEASES, port=80,
                 run_err=_run_err, load_config=config.load, state_file=None, clock=time.time,
                 write=config.write_file):
        self.iface, self.run, self.sys_root, self.port = iface, run, sys_root, port
        self.run_err = run_err
        self.load_config, self.state_file, self.clock, self.write = load_config, state_file, clock, write
        self.leases = leases.format(iface=iface)
        self._op = threading.Lock()    # one start or stop at a time (they take seconds)
        self._lock = threading.Lock()  # the state below, held only for moments
        self._info = None       # {ssid, password, address, url} while on
        self._previous = None   # UUID of the client connection to go back to
        self._join = {"joining": None, "joined": None, "failed": None, "fallback": None}
        self._name, self._name_at = SSID, None  # HOTSPOT_SSID while off, read now and then

    def ssid(self):
        """The hotspot's name from the settings (HOTSPOT_SSID); fluGATO when it is unset or
        no good (a hand edit)."""
        value, _ = config.validate("HOTSPOT_SSID", str(self.load_config().get("HOTSPOT_SSID", "")))
        return value or SSID

    def available(self):
        """True when this Pi has a Wi-Fi port (the Pi 1 has none)."""
        return os.path.isdir(os.path.join(self.sys_root, "class", "net", self.iface, "wireless"))

    def start(self, timeout=30, password=None, _fallback=False):
        """Brings the hotspot up and returns {ssid, password, address, url}. Raises
        HotspotError, with wlan0 back on its client network, when it can't. `password`
        reuses one (the fallback after a failed join keeps the running one, so phones
        that knew the hotspot rejoin by themselves); None: choose_password() from the
        settings, read now, so a change applies at the next start."""
        if not _fallback:
            with self._lock:
                if self._join["joining"]:
                    raise HotspotError("joining a network: wait for it")
        with self._op:
            with self._lock:
                if not _fallback:
                    self._join = {"joining": None, "joined": None, "failed": None,
                                  "fallback": None}
                if self._info:
                    return dict(self._info)
            if not self.available():
                raise HotspotError("no Wi-Fi on this Pi")
            run = self.run
            current = (run(["nmcli", "-g", "GENERAL.CON-UUID", "device", "show", self.iface]) or "").strip()
            if PROFILE in _profiles(run):  # left by a crash
                run(["nmcli", "connection", "delete", PROFILE])
            keep = None
            if password:
                source = "same"
            else:
                password, source, keep = self._choose()
            ssid = self.ssid()  # read now: a change applies at the next start
            if run(add_command(self.iface, password, ssid)) is None:
                raise HotspotError("NetworkManager refused the hotspot")
            if run(["nmcli", "--wait", str(timeout), "connection", "up", PROFILE],
                   timeout=timeout + 5) is None:
                run(["nmcli", "connection", "delete", PROFILE])
                _reconnect(run, self.iface, current)
                raise HotspotError("the hotspot did not come up")
            address = self._address(5)
            if keep:  # only once it is really up, so the panel and the file agree
                self._keep(keep)
            port = "" if self.port == 80 else f":{self.port}"
            info = {"ssid": ssid, "password": password, "address": address,
                    "url": f"http://{address}{port}/" if address else None,
                    "password_source": source}
            with self._lock:
                self._previous, self._info = current or None, info
            return dict(info)

    def _state_path(self):
        return self.state_file or state_path()

    def _choose(self):
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                state = parse_state(f.read())
        except (OSError, UnicodeError):
            state = None  # missing or unreadable: a new one is made and the file rewritten
        return choose_password(self.load_config(), state, self.clock())

    def _keep(self, state):
        try:
            self.write(self._state_path(), json.dumps(state) + "\n", mode=0o600)
        except OSError as e:  # the hotspot still runs; the password just isn't kept
            print(f"wifi: could not keep the hotspot password: {e.strerror or e}", flush=True)

    def stop(self):
        """Hotspot off, profile deleted, wlan0 back on the client network. Always safe.
        When NetworkManager can't even list its profiles, the delete and reconnect are
        tried anyway: better a needless reconnect than an access point left running."""
        with self._lock:
            if self._join["joining"]:
                return  # the join ends on a network or on the hotspot by itself
        with self._op:
            with self._lock:
                previous = self._previous
            run = self.run
            listed = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
            if listed is None or PROFILE in listed.splitlines():
                run(["nmcli", "connection", "delete", PROFILE])
                _reconnect(run, self.iface, previous)
            with self._lock:
                self._info, self._previous = None, None
                self._join = {"joining": None, "joined": None, "failed": None, "fallback": None}

    def join(self, ssid, stop_web=None, start_web=None, timeout=30, networks_file=None):
        """Leaves hotspot mode for a saved network (networks.py), and never ends on
        neither: if wlan0 has no address on it within `timeout` seconds, the hotspot comes
        back with the same password, and if even that fails, wlan0 goes back to the
        network it was on before the hotspot. Blocks for up to ~45 s: run it in a thread.

        The web UI lives on the hotspot only: stop_web() is called
        before the hotspot goes, and start_web(address) only when the hotspot comes back
        after a failed join. On the joined network the Pi is reachable by SSH alone.

        status() shows joining / joined / failed while and after it runs, and fallback:
        "same password", "new password" (there was no hotspot to keep it from) or "home"
        (the hotspot did not come back either). Returns True when joined."""
        net = networks.find(ssid, networks_file)
        with self._lock:
            if self._join["joining"]:
                return False  # one join at a time
            if not net:
                self._join = {"joining": None, "joined": None, "failed": f"{ssid}: not saved",
                              "fallback": None}
                return False
            self._join = {"joining": ssid, "joined": None, "failed": None, "fallback": None}
            password = self._info["password"] if self._info else None
            previous = self._previous
        _quietly(stop_web)
        address, reason = None, "could not connect"
        try:
            with self._op:
                run = self.run
                run(["nmcli", "connection", "delete", PROFILE])  # hotspot off, no rejoin yet
                with self._lock:
                    self._info = None
                if not networks.nm_add(net, run, self.iface):
                    reason = "NetworkManager refused the network"
                else:
                    ok, err = self.run_err(["nmcli", "--wait", str(timeout), "connection", "up",
                                            networks.profile(ssid)], timeout + 5)
                    if ok:
                        address = self._address(5)
                        reason = None if address else "no address from the network (DHCP)"
                    else:
                        reason = join_reason(err)
        except Exception as e:  # whatever went wrong, the fallback below still runs
            reason = f"error: {e.__class__.__name__}"
        if address:  # no web UI here: it is on the hotspot only
            with self._lock:
                self._previous = None
                self._join = {"joining": None, "joined": {"ssid": ssid, "address": address},
                              "failed": None, "fallback": None}
            return True
        try:
            info = self.start(password=password, _fallback=True)
            fallback = "same password" if password else "new password"
            with self._lock:
                self._previous = previous  # not the network that just failed
            _quietly(start_web, info["address"])
        except Exception:
            _reconnect(self.run, self.iface, previous)
            fallback = "home"
        with self._lock:
            self._join = {"joining": None, "joined": None, "failed": f"{ssid}: {reason}",
                          "fallback": fallback}
        return False

    def _address(self, seconds):
        end = time.monotonic() + seconds
        while True:
            found = checks.parse_ip_addr(self.run(["ip", "-j", "-4", "addr", "show", "dev", self.iface]))
            if found or time.monotonic() >= end:
                return found[0] if found else None
            time.sleep(0.2)

    def status(self):
        """{on, ssid, password, password_source, address, url, clients, joining, joined,
        failed, fallback}: cheap (no commands), fine to call on every redraw.
        password_source: manual, kept, new, or same (a join's fallback kept the running
        one). clients counts the phones holding a lease. joining: the SSID being joined (a hand start or stop waits for it to end:
        start raises HotspotError, stop does nothing); joined: {ssid, address} once on it
        (the hotspot and the web UI are off then: SSH only); failed: "SSID: reason" after
        a join did not work, with fallback saying what came back (see join). A hand
        start or stop of the hotspot clears them."""
        with self._lock:
            info = dict(self._info) if self._info else None
            join = dict(self._join)
        try:
            with open(self.leases) as f:
                clients = len(parse_leases(f.read()))
        except OSError:
            clients = 0
        if not info:
            now = time.monotonic()
            if self._name_at is None or now - self._name_at > 10:  # cheap: the file now and then
                self._name, self._name_at = self.ssid(), now
            return dict(join, on=False, ssid=self._name, password=None, password_source=None,
                        address=None, url=None, clients=0)
        return dict(info, on=True, clients=clients, **join)


def parse_client(device, ssid):
    """{ssid, address} from `nmcli -g GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS device show`
    and the connection's `802-11-wireless.ssid,802-11-wireless.mode`; None unless connected
    as a client with an IPv4 address (the hotspot is mode "ap")."""
    lines = (device or "").splitlines()
    if len(lines) < 3 or not lines[0].startswith("100") or lines[1].strip() == PROFILE:
        return None
    address = lines[2].split("|")[0].split("/")[0].strip()
    name, _, mode = (ssid or "").partition("\n")
    if not address or mode.strip() == "ap":
        return None
    return {"ssid": name.strip() or lines[1].strip(), "address": address}


def client(run=checks.run, iface=IFACE):
    """The network the Pi's Wi-Fi is on as a client, {ssid, address}, or None."""
    device = run(["nmcli", "-g", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", iface])
    lines = (device or "").splitlines()
    if len(lines) < 2 or not lines[1].strip():
        return None
    ssid = run(["nmcli", "-g", "802-11-wireless.ssid,802-11-wireless.mode", "connection", "show",
                lines[1].strip()])
    return parse_client(device, ssid)


def _quietly(fn, *args):
    """The app's web callbacks: a failure there must not stop the fallback."""
    if fn:
        try:
            fn(*args)
        except Exception:
            pass


def main(argv=None):
    """By hand, with the service stopped: start | stop | status | cleanup."""
    args = sys.argv[1:] if argv is None else argv
    action = args[0] if args else "status"
    if action == "cleanup":
        print("\n".join(cleanup()) or "nothing to clean up")
    elif action == "start":
        info = Hotspot().start()
        print(f"SSID {info['ssid']}  password {info['password']}  {info['url']}")
    elif action == "stop":
        Hotspot().stop()
    elif action == "status":
        active = checks.run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", IFACE]) or ""
        print("on" if active.strip() == PROFILE else "off")
    else:
        sys.exit("usage: python3 -m flugato.hotspot start|stop|status|cleanup")


if __name__ == "__main__":
    main()

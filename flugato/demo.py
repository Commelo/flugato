"""Made-up reports for previewing the screen. Addresses come from the
RFC 5737 documentation range and names are fictional."""

import math

from .checks import FAIL, OK, PENDING, SKIP, WARN, new_report


def _fill(values):
    r = new_report()
    for label, (value, state) in values.items():
        r[label].set(value, state)
    return r


def good():
    return _fill({
        "LINK": ("1G Full", OK),
        "IP": ("192.0.2.57/24", OK),
        "DHCP": ("from 192.0.2.1  lease 12h", OK),
        "GATEWAY": ("192.0.2.1  0.9 ms", OK),
        "DNS": ("192.0.2.1  14 ms", OK),
        "INTERNET": ("host.example:443  38 ms", OK),
        "SWITCH": ("access-sw-02  192.0.2.250  (LLDP)", OK),
        "PORT": ("Gi1/0/14  desk 3B west wall long description", OK),
        "VLAN": ("40 native  trunk 10 VLANs: 10, 20, 30, 100 +6", OK),
    }), "eth0"


def testing():
    return _fill({
        "LINK": ("1G Full", OK),
        "IP": ("192.0.2.57/24", OK),
        "DHCP": ("from 192.0.2.1  lease 12h", OK),
        "GATEWAY": ("192.0.2.1  1.2 ms", OK),
        "DNS": ("192.0.2.1  (no CHECK_HOST set)", SKIP),
        "INTERNET": ("no CHECK_HOST set", SKIP),
        "SWITCH": ("listening for LLDP/CDP… 63 s", PENDING),
    }), "eth0"


def bad():
    return _fill({
        "LINK": ("100M Full  both do 1G: check cable", WARN),
        "IP": ("169.254.12.7/16 link-local", FAIL),
        "DHCP": ("no answer", FAIL),
        "GATEWAY": ("no default route", FAIL),
        "DNS": ("none  (no CHECK_HOST set)", FAIL),
        "INTERNET": ("no CHECK_HOST set", SKIP),
        "SWITCH": ("no LLDP/CDP heard (unmanaged switch?)", WARN),
        "PORT": ("", SKIP),
        "VLAN": ("", SKIP),
    }), "eth0"


def nolink():
    return None, "eth0"


def _offer(i):
    """An offer from the i-th /28 inside 198.51.100.0/24 (RFC 5737)."""
    return {"server": "192.0.2.1", "address": f"198.51.100.{16 * i + 5}", "prefix": 28,
            "router": f"198.51.100.{16 * i + 1}", "dns": [], "lease": 43200}


# native VLAN, tagged VLANs, offers per VLAN (one still being probed) for the details screen
DETAILS = (40, {10, 20, 30, 100, 200, 210, 220, 300, 310, 320},
           {10: _offer(0), 20: _offer(1), 30: None, 100: _offer(2), 200: None, 210: _offer(3),
            220: None, 300: _offer(4)})

def cable_report():
    """A passing cable test, for the preview."""
    rows = {"PORT A": ("1G Full  main (eth0)", OK), "PORT B": ("1G Full  USB (eth1)", OK),
            "A TO B": ("20000/20000 frames, 0 lost  (612 Mb/s sent)", OK),
            "B TO A": ("20000/20000 frames, 0 lost  (588 Mb/s sent)", OK),
            "ERRORS": ("0 CRC / frame errors", OK), "RESULT": ("cable passes", OK)}
    from .checks import Row
    report = {}
    for label, (value, state) in rows.items():
        report[label] = Row(label)
        report[label].set(value, state)
    return report


def inline_snapshot():
    """An inline session with a slow device and one warning, for the preview."""
    hist = [(int(2e5 + 1.5e5 * math.sin(i / 4)), int(1.2e6 + 9e5 * math.sin(i / 6) ** 2))
            for i in range(60)]
    return {
        "state": "bridged", "error": None,
        "switch": {"name": "access-sw-02", "mgmt": "192.0.2.250", "port": "Gi1/0/14",
                   "descr": "desk 3B", "via": "CDPv2", "native_vlan": 40, "voice_vlan": 110,
                   "link": {"speed": 1000, "duplex": "Full"}},
        "device": {"mac": "00:00:5e:00:53:2a", "vendor": "Example Corp", "hostname": "printer-3b",
                   "vendor_class": None, "ip": "198.51.100.37", "prefix": 24,
                   "dhcp": {"state": "ack", "server": "192.0.2.1", "router": "198.51.100.1",
                            "dns": ["192.0.2.53"], "lease": 43200, "took": 0.4},
                   "link": {"speed": 100, "duplex": "Full"}, "vlans": []},
        "traffic": {"up_bps": hist[-1][0], "down_bps": hist[-1][1], "up_fps": 42, "down_fps": 310,
                    "history": hist,
                    "protocols": {"HTTPS": 61e6, "DNS": 9e6, "UDP": 8e6, "mDNS/SSDP": 7e6,
                                  "ARP": 5e6, "IPv6": 5e6, "HTTP": 3e6, "ICMP": 2e6},
                    "top": [("203.0.113.40", 18 << 20), ("198.51.100.1", 2 << 20),
                            ("203.0.113.9", 1 << 20)],
                    "dns": ["updates.example", "time.example", "cloud.example", "print.example",
                            "ocsp.example"], "bcast_ps": 12, "mcast_ps": 30},
        "capture": {"file": "inline-0000.pcap", "bytes": 3_300_000, "frames": 4100, "dropped": 0},
        "warnings": [("warn", "speed mismatch: switch 1G, device 100M")],
    }


SCENARIOS = {"good": good, "testing": testing, "bad": bad, "nolink": nolink, "blink": nolink,
             "details": good, "vlan": good, "cable": good,
             "inline": good, "inline_list": good, "inline_traffic": good, "wifi": nolink,
             **{k: nolink for k in ("desk", "desk_rain", "desk_snow", "desk_storm", "desk_clock",
                                    "night", "night_weather", "laser", "show", "power_ask",
                                    "power_off", "power_reboot")}}


def forecast(kind="sun", temp=21, hi=23, lo=14, rain=5, summary="clear · rain 5%", night=False):
    """A made-up forecast for the desk screen previews (weather.parse's shape)."""
    import time
    at = time.mktime(time.strptime("2026-10-10 14:00", "%Y-%m-%d %H:%M"))
    return {"kind": kind, "night": night, "temp": temp, "hi": hi, "lo": lo, "rain": rain,
            "summary": summary, "code": 0, "at": at}

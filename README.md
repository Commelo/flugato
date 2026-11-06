<p align="center">
  <img src="https://flugato.com/img/og.png" alt="fluGATO: the pocket network tester that lives in your terminal" width="640">
</p>

<h1 align="center">fluGATO</h1>

<p align="center">
  <strong>Plug a cable in. Ask the cat.</strong><br>
  The pocket network tester that lives in your Linux terminal.
</p>

<p align="center">
  <a href="https://flugato.com">flugato.com</a> ·
  <a href="#install">Install</a> ·
  <a href="#what-it-checks">Checks</a> ·
  <a href="#with-and-without-root">Root</a> ·
  <a href="#scriptable">Scripts</a> ·
  <a href="#the-screens">Screens</a>
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-orange">
  <img alt="Platform: Linux x86_64 / arm64" src="https://img.shields.io/badge/linux-x86__64%20%7C%20arm64-lightgrey">
  <img alt="Needs python3" src="https://img.shields.io/badge/needs-python3-blue">
</p>

---

fluGATO tests the cable port on your laptop the way a pocket tester would: link speed, DHCP, gateway, DNS, internet, and which switch port and VLANs you just landed on. In your terminal, in about four seconds. Judged by a chonky orange tabby who takes the results personally.

## Install

```sh
curl -fsSL https://flugato.com/install | sh
```

- No root needed to install or to run the basic checks.
- One file in `~/.local/bin`. The installer checks the file's sha256 before it installs it.
- Linux x86_64 and arm64. Needs `python3`, plus `ip` and `ping` (iproute2, iputils) and `ethtool`. `lldpd` is used for the switch and port rows.

Then:

```sh
flugato            # open the app
flugato update     # get the latest version
flugato uninstall  # gone without a trace
```

## What it checks

Nine rows, top to bottom, in the order you would debug it in. Green, amber or red; the banner says it in words, the cat says it in cat.

| Row | What you get |
|---|---|
| **LINK** | Speed, duplex, autoneg. "Check cable" when both ends could do gigabit but the link came up at 100M: on a gigabit run that is usually a broken pair. |
| **IP** | Address and prefix. Link-local means nobody answered DHCP. |
| **DHCP** | Who leased it, for how long, and how long the offer took. |
| **GATEWAY** | Ping time and loss to the default gateway. |
| **DNS** | The servers, and how long a lookup takes. |
| **INTERNET** | A TLS connect to a host out there, on 443. |
| **SWITCH** | Name and address of the switch, heard over LLDP or Cisco CDP. |
| **PORT** | The port you are on, with its description. The one the label on the faceplate lies about. |
| **VLAN** | Native VLAN, and on a trunk a DHCP probe of every tagged VLAN: which ones hand out an address. |

Verdicts: `● all good` · `● check warnings` · `● problem found` · `● testing…` · `● no link`

## With and without root

Testing a cable should never cut your own connection.

### `flugato` — no root

Reads what the computer already knows:

- link speed and duplex from the driver
- the address NetworkManager got, and its lease
- gateway ping, DNS, internet

The switch, port and VLAN rows say *needs root*. Nothing is configured, nothing is changed. Press `s` for the rest.

### `sudo flugato` — root

Press `s` in the app, or run `sudo ~/.local/bin/flugato`. The test ports get their own network namespace, and you get:

- a fresh DHCP offer, timed
- the switch and port over LLDP and CDP
- a DHCP probe on every tagged VLAN of a trunk
- `b` blinks the port LED so you can find it in the rack

The test port's address, routes and DNS never reach the rest of the machine. Your Wi-Fi, your VPN and the SSH session you are typing in stay exactly as they were.

### Cable test

Two cable ports (a USB adapter counts) give you a cable test: both ends of a patch cord in the tester, test frames both ways, the CRC counters watched.

## Scriptable

For the days you have forty ports to do. Every mode writes to stdout and means its exit code.

```console
$ flugato --once
fluGATO 0.4.0 · enp3s0 · 2026-10-14 14:02
● LINK      1G Full
● IP        192.0.2.57/24
● DHCP      from 192.0.2.1  lease 12h
● GATEWAY   192.0.2.1  0.9 ms
● DNS       192.0.2.1  14 ms
● INTERNET  host.example:443  38 ms
● SWITCH    access-sw-02  192.0.2.250  (LLDP)
● PORT      Gi1/0/14  desk 3B west wall
● VLAN      70 native  trunk  10 VLANs: 10, 20, 30, 522 +6
ALL GOOD  9 checks in 4.1 s

$ flugato --once --json | jq -r '.verdict, .rows.LINK.value'
ALL GOOD
1G Full

$ flugato --once --quiet --port enx3c18a0 || echo "port 14 needs a look"
port 14 needs a look

$ flugato --watch --port enp3s0     # one line per change, for a tmux pane
14:20:02 ● link up 1G Full
14:20:06 ● DHCP 192.0.2.57/24 from 192.0.2.1
14:31:40 ● link down
14:31:44 ● link up 100M Full: both do 1G, check the cable
```

| Flag | What it does |
|---|---|
| `--once` | Run the checks, print the rows, exit. |
| `--json` | The same, as one JSON object. |
| `--quiet` | Say nothing; the exit code is the verdict. |
| `--watch` | Stay, and print a line whenever the port changes. |
| `--port <iface>` | Pick the test port. |

Addresses in the examples are documentation addresses; the switches are made up.

## The screens

A full-screen terminal app with single-key commands. It follows your terminal's colours, so it is at home in a dark one and a light one.

- **Dashboard:** the verdict, the rows, recent tests, the cat.
- **VLANs:** one DHCP probe per tagged VLAN, from a throwaway MAC.
- **Ports:** pick the test port. Your own connection is never touched.
- **Without root:** what the machine already knows, and a hint for the rest.
- **Cable test:** a patch cord between two ports, frames both ways, the CRC counters watched.
- **The installer:** checks, downloads one file, verifies the checksum, done.

With no cable plugged in, the cat sleeps.

## It started as a thing in a pocket

fluGATO was first a hand-held tester: a Raspberry Pi with a 3.5-inch touch screen in a 3D-printed holder, on a USB power bank. Plug a cable in and the screen fills with the same rows; tap to re-run, hold to blink the port. With no cable it shows the cat asleep in the server closet, and between tests it becomes a desk clock with the weather.

The pocket tester also sits inline between a device and the switch, with a capture you can open in Wireshark; that part has not made it into the terminal app yet.

The terminal app is the same tester with the laptop you already carry as the hardware. The cat came along. Of course the cat came along.

## Issues and contributions

Found a port that fools it, a switch it cannot hear, or a distro where the installer trips? Open an issue with the output of `flugato --once --json` and the distro and kernel you are on.

## License

MIT. Copyright (c) 2026 COMMELO LLC.

fluGATO is a courtesy of [COMMELO LLC](https://www.commelo.com), AI automation and senior engineering, with teams in Miami, USA and Belo Horizonte, Brazil.

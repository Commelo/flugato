#!/usr/bin/env bash
# Installs flugato on a Raspberry Pi running Raspberry Pi OS Lite.
# Run as root from a copy of the repository: sudo bash pi/setup.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/flugato

# with the read-only overlay up, everything below would land in RAM and vanish at reboot
if [ "$(findmnt -n -o FSTYPE /)" = overlay ]; then
    echo "Root is read-only: run 'sudo bash pi/readonly.sh off', reboot, then install." >&2
    exit 1
fi

apt-get update
# fonts-dejavu-extra holds the Condensed faces the screen uses; overlayroot lets
# readonly.sh work without network; ieee-data names device manufacturers in inline mode;
# dnsmasq-base hands out addresses on the Wi-Fi hotspot (NetworkManager's shared mode)
apt-get install -y python3-pil fonts-dejavu-core fonts-dejavu-extra ethtool lldpd \
    iputils-ping overlayroot device-tree-compiler ieee-data dnsmasq-base

# journal in RAM, capped: it can't grow on a read-only root or fill the Pi 1's memory
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' > /etc/systemd/journald.conf.d/flugato.conf
systemctl restart systemd-journald

# Nothing but fluGATO on the panel: no login prompt on tty1 (it reprinted itself on every
# cable plug and switched the panel back to text at boot). SSH and the serial console still
# log in; `systemctl unmask getty@tty1` brings the panel's login back. The kernel's and
# systemd's messages are kept off it in cmdline.txt (below).
rm -f /etc/systemd/system/getty@tty1.service.d/flugato.conf
rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true
systemctl mask getty@tty1.service
systemctl daemon-reload

# The test port gets its own routing table (100). The Pi's own traffic (SSH, apt, its
# DNS) never sees the test port's routes, not even when the test port lands in the same
# subnet as the management link. The checks are bound to eth0, and the rule sends
# anything leaving through eth0 to table 100. eth0's DNS servers stay out of the Pi's
# resolver; the DNS check reads them from the DHCP lease.
# no auto-created "Wired connection" for the test ports: it would use the main table.
# eth1 (the USB adapter, the cable test's second port) is left to the tester entirely:
# no profile, no address, so plugging it in can never touch the Pi's own networking.
cat > /etc/NetworkManager/conf.d/flugato.conf <<'NMCONF'
[main]
no-auto-default=interface-name:eth0,interface-name:eth1

[keyfile]
unmanaged-devices=interface-name:eth1;interface-name:flugato-br
NMCONF
systemctl reload NetworkManager
TESTPORT_PROPS=(connection.interface-name eth0 connection.autoconnect-priority 100
    ipv4.method auto ipv4.route-table 100 ipv4.ignore-auto-dns yes
    ipv4.routing-rules "priority 100 oif eth0 table 100"
    ipv6.method auto ipv6.route-table 100 ipv6.ignore-auto-dns yes
    ipv6.routing-rules "priority 100 oif eth0 table 100")
# modify in place on a redeploy: deleting it would hand eth0 to another profile
if nmcli -t -f NAME connection show | grep -qx flugato-test; then
    nmcli connection modify flugato-test "${TESTPORT_PROPS[@]}"
else
    nmcli connection add type ethernet con-name flugato-test "${TESTPORT_PROPS[@]}" >/dev/null
fi
# other wired profiles must not grab eth0: only those bound to eth0, or to no interface
# and no other MAC; a profile for another port (USB-Ethernet management) stays untouched
ETH0_MAC="$(cat /sys/class/net/eth0/address)"
nmcli -t -f UUID,TYPE,NAME connection show | while IFS=: read -r uuid type name; do
    [ "$type" = 802-3-ethernet ] && [ "$name" != flugato-test ] || continue
    ifname="$(nmcli -g connection.interface-name connection show "$uuid")"
    mac="$(nmcli -g 802-3-ethernet.mac-address connection show "$uuid" | tr -d '\134' | tr 'A-F' 'a-f')"
    if [ "$ifname" = eth0 ] || [ "$ifname" = eth1 ] \
            || { [ -z "$ifname" ] && { [ -z "$mac" ] || [ "$mac" = "$ETH0_MAC" ]; }; }; then
        nmcli connection modify "$uuid" connection.autoconnect no
    fi
done
# make the live connection match: reapply when the test profile is active (a modify only
# changes the saved profile), hand eth0 over when something else holds it
if [ "$(nmcli -g GENERAL.CONNECTION device show eth0 2>/dev/null)" = flugato-test ]; then
    nmcli device reapply eth0 >/dev/null || nmcli --wait 0 connection up flugato-test >/dev/null || true
elif [ "$(cat /sys/class/net/eth0/carrier 2>/dev/null)" = 1 ]; then
    nmcli --wait 0 connection up flugato-test >/dev/null || true
fi

# eth0's routes live only in table 100, but the reverse-path filter checks a reply's
# sender against the main table: with no default route there (hotspot on, or no Wi-Fi
# network in the field) the gateway's and DNS servers' replies are dropped. The test port
# answers to its own table, so the filter is off on it alone.
printf 'net.ipv4.conf.eth0.rp_filter = 0\n' > /etc/sysctl.d/90-flugato.conf
sysctl -q -w net.ipv4.conf.eth0.rp_filter=0 || true

# lldpd speaks LLDP by default; -c adds CDP for Cisco gear
if grep -q '^DAEMON_ARGS=' /etc/default/lldpd 2>/dev/null; then
    sed -i 's/^DAEMON_ARGS=.*/DAEMON_ARGS="-c"/' /etc/default/lldpd
else
    echo 'DAEMON_ARGS="-c"' >> /etc/default/lldpd
fi
systemctl enable lldpd
systemctl restart lldpd

# The panel's overlay, ours in both cases (pi/overlays/): PANEL in flugato.conf picks it,
# mz61581 (Tontec, the default; the stock overlay switches this board's backlight off) or
# ili9486 (the "RPi LCD (A)" kind with XPT2046 touch). The other one's line goes.
BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
OVERLAYS="$(dirname "$BOOTCFG")/overlays"
# PANEL=ili9486 ./deploy.sh ... (or in the environment of this script) wins over the conf: a
# first install has no conf yet; the choice is then written into the conf below.
GIVEN_PANEL="${PANEL:-}"
PANEL="${GIVEN_PANEL:-$(sed -n 's/^PANEL=\([a-z0-9]*\).*/\1/p' "$(dirname "$BOOTCFG")/flugato.conf" /etc/flugato.conf 2>/dev/null | head -n 1)}"
case "$PANEL" in ili9486) OTHER=mz61581 ;; *) PANEL=mz61581; OTHER=ili9486 ;; esac
dtc -@ -q -I dts -O dtb -o "$OVERLAYS/flugato-$PANEL.dtbo" "$SRC/pi/overlays/flugato-$PANEL.dts"
sed -i "/^dtoverlay=mz61581/d; /^dtoverlay=flugato-$OTHER/d" "$BOOTCFG"
grep -q "^dtoverlay=flugato-$PANEL" "$BOOTCFG" || echo "dtoverlay=flugato-$PANEL" >> "$BOOTCFG"
echo "panel: $PANEL (dtoverlay=flugato-$PANEL)"

# Boot and shutdown messages off the panel: no console on tty1 (the serial console keeps
# them), quiet, no Raspberry logo, no cursor. cmdline.txt is one line and the Pi won't boot
# without root=, so the new line is checked first and the original kept once next to it.
CMDLINE="$(dirname "$BOOTCFG")/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    [ -f "$CMDLINE.flugato-orig" ] || cp "$CMDLINE" "$CMDLINE.flugato-orig"
    line=$(head -n 1 "$CMDLINE")
    new=$(printf '%s\n' "$line" | sed -e 's/\(^\| \)console=tty1\( \|$\)/\1\2/g' -e 's/  */ /g' -e 's/^ //; s/ $//')
    for arg in quiet loglevel=3 logo.nologo vt.global_cursor_default=0; do
        case " $new " in *" $arg "*) ;; *) new="$new $arg" ;; esac
    done
    case " $new " in
        *" root="*) [ "$new" = "$line" ] || { printf '%s\n' "$new" > "$CMDLINE.new" && mv "$CMDLINE.new" "$CMDLINE"; } ;;
        *) echo "cmdline.txt: no root= in the new line, left as it is" >&2 ;;
    esac
fi

mkdir -p "$DEST"
rm -rf "$DEST/flugato"
# a deploy over SSH is a new baseline: no roll-back tree or half update from flugato/update.py
rm -rf "$DEST/flugato.prev" "$DEST/.update" "$DEST/.just-updated"
cp -r "$SRC/flugato" "$DEST/"

# Settings live on the boot partition: they survive the read-only root and can be edited
# from any PC. An /etc/flugato.conf from before the move is moved there once (the app
# would still read it, but only while there is no file on the boot partition).
CONF="$(dirname "$BOOTCFG")/flugato.conf"
if [ -f /etc/flugato.conf ] && [ ! -f "$CONF" ]; then
    cp /etc/flugato.conf "$CONF" || exit 1   # never fall through to the example below
    rm /etc/flugato.conf
fi
[ -f "$CONF" ] || cp "$SRC/pi/flugato.conf.example" "$CONF"
if [ -n "$GIVEN_PANEL" ]; then        # the panel given to this install, kept for the next one
    if grep -q '^PANEL=' "$CONF"; then sed -i "s/^PANEL=.*/PANEL=$PANEL/" "$CONF"
    else printf 'PANEL=%s\n' "$PANEL" >> "$CONF"; fi
fi

install -m 644 "$SRC/pi/flugato.service" /etc/systemd/system/flugato.service
# gone: a "Safe to unplug" screen at the end of a power-off (the panel goes gray at the halt)
rm -f /usr/lib/systemd/system-shutdown/flugato-off
systemctl daemon-reload
systemctl enable flugato
systemctl try-restart flugato   # a redeploy runs the new code without a reboot

echo "flugato installed. Settings: $CONF (or the web UI on the fluGATO Wi-Fi). Reboot to bring up the screen."

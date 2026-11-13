#!/usr/bin/env bash
# Runs the root filesystem read-only with a RAM overlay, and the boot partition
# read-only, so pulling power can't corrupt the SD card. Changes apply at reboot.
# Usage: sudo bash pi/readonly.sh on|off|status
set -euo pipefail

BOOTDIR=/boot/firmware
[ -d "$BOOTDIR" ] || BOOTDIR=/boot
CMDLINE="$BOOTDIR/cmdline.txt"
BOOTCFG="$BOOTDIR/config.txt"

die() { echo "readonly: $*" >&2; exit 1; }

overlay_now() { [ "$(findmnt -n -o FSTYPE /)" = overlay ]; }

# overlayroot (current Pi OS) uses overlayroot=tmpfs, older raspi-config boot=overlay
overlay_pending() { grep -Eq '(^| )(overlayroot=tmpfs|boot=overlay)( |$)' "$CMDLINE"; }

boot_ro_now() { findmnt -n -o OPTIONS "$BOOTDIR" | grep -Eq '(^|,)ro(,|$)'; }

# While the overlay is up, /etc/fstab is the RAM copy; the one that counts at the
# next boot lives on the read-only lower layer.
lower_root() {
    local dir
    for dir in "$(findmnt -n -o OPTIONS / | sed -n 's/.*lowerdir=\([^,:]*\).*/\1/p')" \
               /media/root-ro /ro; do
        [ -n "$dir" ] && mountpoint -q "$dir" && [ -f "$dir/etc/fstab" ] && { echo "$dir"; return; }
    done
    return 1
}

fstab_file() {
    local lower
    if overlay_now; then
        lower="$(lower_root)" || die "overlay is active but its lower layer is not mounted; reboot and run this again"
        echo "$lower/etc/fstab"
    else
        echo /etc/fstab
    fi
}

boot_ro_pending() {
    local fstab
    fstab="$(fstab_file)" || return 2
    awk -v mp="$BOOTDIR" '$1 !~ /^#/ && $2 == mp && $4 ~ /(^|,)ro(,|$)/ { f = 1 } END { exit !f }' \
        "$fstab"
}

# Adds (ro) or removes (rw) the ro option on the boot partition's fstab line
set_boot_fstab() {
    local mode=$1 fstab lower="" tmp
    fstab="$(fstab_file)"
    awk -v mp="$BOOTDIR" '$1 !~ /^#/ && $2 == mp { f = 1 } END { exit !f }' "$fstab" \
        || die "no $BOOTDIR line in $fstab"
    if overlay_now; then
        lower="${fstab%/etc/fstab}"
        mount -o remount,rw "$lower"
    fi
    tmp="$(mktemp)"
    awk -v mp="$BOOTDIR" -v mode="$mode" 'BEGIN { OFS = "\t" }
        $1 !~ /^#/ && $2 == mp {
            n = split($4, o, ","); opts = ""
            for (i = 1; i <= n; i++) if (o[i] != "ro" && o[i] != "rw") opts = opts (opts ? "," : "") o[i]
            if (mode == "ro") opts = opts (opts ? "," : "") "ro"
            $4 = (opts ? opts : "defaults")
        }
        { print }' "$fstab" > "$tmp"
    cat "$tmp" > "$fstab"
    rm -f "$tmp"
    [ -z "$lower" ] || mount -o remount,ro "$lower" || echo "warning: could not remount $lower read-only"
}

state() {
    local rc=0
    "$@" 2>/dev/null || rc=$?
    case $rc in 0) echo read-only ;; 1) echo read-write ;; *) echo unknown ;; esac
}

status() {
    printf '%-16s now: %-11s after reboot: %s\n' \
        "root filesystem" "$(state overlay_now)" "$(state overlay_pending)" \
        "boot partition" "$(state boot_ro_now)" "$(state boot_ro_pending)"
}

case "${1:-}" in
    status)
        status
        exit 0
        ;;
    on|off) ;;
    *) die "usage: sudo bash pi/readonly.sh on|off|status" ;;
esac

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash pi/readonly.sh $1"
command -v raspi-config >/dev/null || die "raspi-config not found"

# cmdline.txt sits on the boot partition, which may be mounted read-only
boot_ro_now && mount -o remount,rw "$BOOTDIR"

if [ "$1" = on ]; then
    # The overlay is built by the initramfs; raspi-config's non-interactive mode
    # does not install the hook package itself.
    if ! dpkg -s overlayroot >/dev/null 2>&1; then
        overlay_now && die "overlayroot is not installed on the SD card; run off, reboot, then on"
        apt-get install -y overlayroot
    fi

    # raspi-config's exit code also covers the boot partition step, which it
    # refuses while an overlay is up, so check the result instead
    raspi-config nonint do_overlayfs 0 || true
    overlay_pending || die "raspi-config did not enable the overlay in $CMDLINE"
    boot_ro_pending || set_boot_fstab ro

    grep -Eq '^(auto_initramfs=1|initramfs )' "$BOOTCFG" \
        || echo "warning: $BOOTCFG loads no initramfs; the overlay will not come up"
    INITRD="/boot/initrd.img-$(uname -r)"
    if [ -f "$INITRD" ] && command -v lsinitramfs >/dev/null \
        && ! lsinitramfs "$INITRD" | grep overlayroot >/dev/null; then
        echo "warning: $INITRD has no overlayroot hook; run update-initramfs -u"
    fi
else
    raspi-config nonint do_overlayfs 1 || true
    overlay_pending && die "raspi-config did not disable the overlay in $CMDLINE"
    ! boot_ro_pending || set_boot_fstab rw
fi

status
echo "Reboot to apply."

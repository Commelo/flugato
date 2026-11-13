#!/usr/bin/env bash
# Copies the app and the Pi files to the Pi and runs the installer there.
# Usage: ./deploy.sh user@pi-address   (the address is never stored)
#        PANEL=ili9486 ./deploy.sh user@pi-address   for the "RPi LCD (A)" kind of panel
#        (first install; afterwards PANEL in flugato.conf on the Pi remembers it)
set -euo pipefail

TARGET="${1:?usage: ./deploy.sh user@pi-address}"
cd "$(dirname "$0")"

# only what the Pi needs; tar is on every Pi OS image, rsync may not be
tar -czf - --exclude=__pycache__ flugato pi \
    | ssh "$TARGET" 'rm -rf flugato && mkdir flugato && tar -xzf - -C flugato'
ssh -t "$TARGET" "sudo env PANEL='${PANEL:-}' bash flugato/pi/setup.sh"

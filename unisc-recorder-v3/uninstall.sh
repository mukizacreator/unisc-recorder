#!/bin/sh
set -eu
APP_DIR=/opt/unisc-recorder
DESKTOP=/usr/share/applications/unisc-recorder.desktop
LAUNCHER=/usr/local/bin/unisc-recorder
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo ./uninstall.sh" >&2; exit 1; fi
rm -rf -- "$APP_DIR"
rm -f -- "$DESKTOP" "$LAUNCHER"
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database /usr/share/applications || true; fi
echo "Unisc Recorder application files removed. User preferences remain in ~/.config/unisc-recorder/."

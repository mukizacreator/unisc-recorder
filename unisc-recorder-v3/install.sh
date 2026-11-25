#!/bin/sh
set -eu

APP_DIR=/opt/unisc-recorder
DESKTOP=/usr/share/applications/unisc-recorder.desktop
LAUNCHER=/usr/local/bin/unisc-recorder
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo ./install.sh" >&2; exit 1; fi
if [ ! -r /etc/os-release ]; then echo "Cannot identify this Linux distribution." >&2; exit 1; fi
. /etc/os-release
case "${ID:-} ${ID_LIKE:-}" in
  *debian*|*mx*) ;;
  *) echo "This installer supports Debian-based MX Linux systems." >&2; exit 1;;
esac
if [ ! -f "$SOURCE_DIR/unisc-recorder.py" ] || [ ! -f "$SOURCE_DIR/unisc-recorder.desktop" ]; then
  echo "Run this installer from the complete extracted project folder." >&2; exit 1
fi

apt-get install -y --no-install-recommends \
  python3 python3-gi gir1.2-gtk-3.0 ffmpeg x11-utils x11-xserver-utils xdotool slop \
  pulseaudio-utils xdg-utils

# Migrate the previous app's saved settings once, without replacing settings
# already created for Unisc Recorder. The old installed files are then removed.
TARGET_USER=${SUDO_USER:-}
if [ -n "$TARGET_USER" ]; then
  TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
else
  TARGET_HOME=${HOME:-/root}
fi
if [ -z "$TARGET_HOME" ]; then TARGET_HOME=/root; fi
OLD_SETTINGS="$TARGET_HOME/.config/kooha-style-recorder/settings.json"
NEW_CONFIG_DIR="$TARGET_HOME/.config/unisc-recorder"
if [ ! -e "$NEW_CONFIG_DIR/settings.json" ] && [ -f "$OLD_SETTINGS" ]; then
  install -d -m 755 "$NEW_CONFIG_DIR"
  cp -p "$OLD_SETTINGS" "$NEW_CONFIG_DIR/settings.json"
  if [ -n "$TARGET_USER" ]; then chown "$TARGET_USER" "$NEW_CONFIG_DIR" "$NEW_CONFIG_DIR/settings.json"; fi
fi

rm -rf -- /opt/kooha-style-recorder
rm -f -- /usr/share/applications/kooha-style-recorder.desktop
install -d -m 755 "$APP_DIR"
install -m 755 "$SOURCE_DIR/unisc-recorder.py" "$APP_DIR/unisc-recorder.py"
install -m 644 "$SOURCE_DIR/README.md" "$APP_DIR/README.md"
install -m 644 "$SOURCE_DIR/unisc-recorder.desktop" "$DESKTOP"
ln -sfn "$APP_DIR/unisc-recorder.py" "$LAUNCHER"
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database /usr/share/applications || true; fi

echo "Unisc Recorder 3.0 installed. Existing preferences were preserved or migrated."
echo "Launch it from Applications > Multimedia > Unisc Recorder, or run: unisc-recorder"

#!/usr/bin/env bash
# Install CouchCam for the current user (~/.local). Re-run to update. --uninstall to remove.
set -euo pipefail
cd "$(dirname "$0")"
BIN="$HOME/.local/bin"; SHARE="$HOME/.local/share/couchcam"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons/hicolor/192x192/apps"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN/couchcam" "$APPS/couchcam.desktop" "$ICONS/couchcam.png"; rm -rf "$SHARE"
  echo "Removed. Settings remain in ~/.config/couchcam."; exit 0
fi

PKGS=(python3-gi gir1.2-gtk-4.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav)
missing=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "Installing: ${missing[*]}"
  sudo apt-get install -y "${missing[@]}"
fi
# Zero-copy GTK4 video sink, if this release packages it (Ubuntu 24.10+; optional).
apt-cache show gstreamer1.0-gtk4 >/dev/null 2>&1 && ! dpkg -s gstreamer1.0-gtk4 >/dev/null 2>&1 \
  && sudo apt-get install -y gstreamer1.0-gtk4 || true

install -Dm755 couchcam.py "$BIN/couchcam"
# The built-in server's setup wizard runs these.
install -Dm755 ../server/install.sh "$SHARE/server/install.sh"
install -Dm644 ../server/couchcam-hub.py "$SHARE/server/couchcam-hub.py"
install -Dm755 ../server/diagnose.py "$SHARE/server/diagnose.py"
install -Dm755 ../server/patches/wyze-cloud.py "$SHARE/server/patches/wyze-cloud.py"
install -Dm755 ../server/patches/go2rtc-weak-wifi.py "$SHARE/server/patches/go2rtc-weak-wifi.py"
install -Dm644 couchcam.desktop "$APPS/couchcam.desktop"
install -Dm644 couchcam.png "$ICONS/couchcam.png"
update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "Installed. Launch CouchCam from the app menu (or run: couchcam). First launch walks you through setup."

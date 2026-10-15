#!/bin/sh
# Run on the Switch from this folder: ./install.sh http://127.0.0.1:8096 (the panel's screen port)
set -e
cd "$(dirname "$0")"
mkdir -p ~/.local/bin ~/.config/autostart
install -m 755 control-center ~/.local/bin/control-center
[ -n "$1" ] && printf '%s\n' "$1" > ~/.config/control-center.url
cat > ~/.config/autostart/control-center.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=Control Center
Exec=$HOME/.local/bin/control-center
DESKTOP
gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
echo "Kiosk installed. URL: $(cat ~/.config/control-center.url 2>/dev/null || echo 'not set')"
echo "Turn on automatic login in Settings → Users, then log out and back in."

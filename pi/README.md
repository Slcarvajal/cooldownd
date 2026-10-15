# Raspberry Pi: the always-on panel

The Pi runs the main panel (the one your phone opens), because it's always on: it wakes your PCs, checks your servers, sends phone alerts, keeps the Life RPG's data and runs Home Assistant.

**Tested on:** Raspberry Pi 5, 8 GB, Debian 13 (trixie). A Pi 4 should work for everything but cameras.

```
Lights, plugs, sensors ─┐
Cameras (RTSP) ─────────┼─► Home Assistant ◄── panel (ha module) ◄── phone, kiosk
                        │
Workstation ◄── Wake-on-LAN, its cards over the tailnet ── Pi panel
```

## Setup

1. **Base system.** Raspberry Pi OS Lite 64-bit (or Debian), on Ethernet, with a static DHCP lease from your router. Install Docker and `python3-cryptography` (for phone alerts):
   ```bash
   sudo apt install -y docker.io docker-compose python3-cryptography
   sudo usermod -aG docker,video $USER   # video: vcgencmd, for throttling and supply voltage
   ```
2. **Tailscale.** Join your tailnet.
3. **The panel.** Install it (main README) with `config.pi.example.json` as `config.pi.json`, and `ADMIN_CONFIG=%h/homelab/admin/config.pi.json` in its service.
   - Put the hub token in `~/.config/homelab-admin/hub-token` (mode 600), the same file on every panel, so they can read each other: `python3 -c "import secrets; print(secrets.token_hex(16))"` once, then copy the file.
   - Phone alerts sign with `~/.config/homelab-admin/vapid.pem`, made on first use. A new key means every phone presses "Turn on alerts" again.
   - Meal estimates need an Anthropic API key in `~/.config/homelab-admin/anthropic-key` (mode 600).
   - The Life RPG keeps its data in `~/.local/share/homelab-admin/life.db` and `meals/` (mode 600). Back it up.
4. **Home Assistant.** `stack/docker-compose.yml` runs it with host networking (for discovery) and Bluetooth:
   ```bash
   mkdir -p ~/homelab/stack && cp stack/docker-compose.yml ~/homelab/stack/
   cd ~/homelab/stack && TZ=America/New_York docker compose up -d   # your time zone
   tailscale serve --bg --https=443 http://127.0.0.1:8123
   ```
   Create a long-lived access token (your profile → Security), save it as `~/.config/homelab/ha-token` (mode 600), and the `ha` card connects. `hawss.py` runs Home Assistant websocket commands inside the container, reading the token from stdin, for setup scripts.
5. **Wake-on-LAN.** Turn it on in your workstation's BIOS and OS, and list it in `machines` with its MAC and your LAN's broadcast address. The Pi sends the packet; the workstation's own panel can only sleep it.

## Worth buying

- The official 27 W power supply and an active cooler. The Pi card warns below 4.85 V, before the Pi throttles.
- An NVMe SSD on an M.2 HAT, to boot from and keep recordings. SD cards wear out.
- For cameras: a Hailo AI kit for Frigate's object detection, and wired PoE cameras with RTSP or ONVIF (Reolink, Tapo C, Amcrest). Avoid cloud-only cameras.
- A Zigbee adapter, only if your lights need one.

# Nintendo Switch: the house touchscreen

A first-generation Switch, running Linux, makes a good wall touchscreen: a 720p touch display, a battery, Wi-Fi, speakers and two controllers. On a USB-C stand it's the control center and agent pager. Docked to a TV, it's the Life RPG's game screen.

**Tested on:** Switch V1 (Erista, unpatched) with [Switchroot](https://switchroot.org/) L4T Ubuntu Jammy 5.1.2 (arm64, 4.9 kernel). Other models need other exploits or none at all; see [CONTRIBUTING.md](../CONTRIBUTING.md) if you get one working.

- Boot: RCM jig → Hekate → More Configs → "L4T Ubuntu Jammy".
- **Never** run Hekate's "Partition SD Card" once Linux is installed. It wipes it.

## Setup

1. **Install L4T Ubuntu** following Switchroot's guide, then update it:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   ```
2. **Join the tailnet** as a tagged device, so it has no user identity of its own:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --advertise-tags=tag:kiosk --hostname=switch
   sudo tailscale set --operator=$USER
   tailscale serve --bg --https=8443 http://127.0.0.1:8095
   ```
   Add `tag:kiosk` to `tagOwners` in your tailnet's access controls first. The 4.9 kernel's TUN works; userspace networking isn't needed.
3. **Install the panel** (main README) with `config.hub.example.json`, and copy the hub token from another panel to `~/.config/homelab-admin/hub-token` (mode 600): a tagged device has no login, so it reads the other panels with the token. Its service sets `ADMIN_CONFIG=…/config.hub.json SCREEN_PORT=8096`, and needs `loginctl enable-linger $USER`.
4. **Install the kiosk** from this folder:
   ```bash
   ./install.sh http://127.0.0.1:8096
   ```
   - Turn on automatic login (in `/etc/gdm3/custom.conf`, or Settings → Users).
   - Chromium runs with `--password-store=basic`; without it, automatic login leaves the GNOME keyring locked and Chromium asks for a keyring password on every boot.
   - Turn off automatic sleep: `gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing` (and `-battery-type`).
   - To change the URL later, edit `~/.config/control-center.url`. To leave the kiosk, press Alt+F4 on a keyboard.
   - **Restarting it over SSH:** run `pkill -x chrome`, then start `~/.local/bin/control-center` through `systemd-run --user`, passing the desktop session's `DISPLAY`, `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS` and `XDG_*` variables (read them from `/proc/$(pgrep -x gnome-shell)/environ`). Started straight from an SSH shell, it dies when SSH closes or comes up maximized under the top bar instead of full screen. Don't `pkill -f chrom…`: it also matches the SSH command running it.
5. **Mount it on a USB-C charging stand**, and dock it only for the game. The dock turns the touchscreen off. L4T drains the battery even while asleep, and being plugged in all the time can make the battery swell, so check it now and then.

**The screen port has no login.** The kiosk opens `http://127.0.0.1:8096`, a second listener that's never published, so it skips the Tailscale check: whoever is at the Switch has full control, including typing into agents. Keep it somewhere guests don't wander.

## Agent pager

When a coding agent starts waiting on you, the screen chimes and opens that agent full screen, with answer buttons above its terminal. No typing needed.

| Button | Sends |
|---|---|
| 💻 PC work | Nothing. The question is marked "at PC" and stops paging until it changes |
| ✅ Go ahead | "Yes, go ahead." |
| ✋ No | "No, don't do that." |
| 1 / 2 / 3 | That option in a numbered menu |

| Joy-Con | Does |
|---|---|
| ↑ / ↓ | Move the highlight (it starts on PC work) |
| A | Press the highlighted button |
| B | PC work |
| R or ZR | Next waiting agent |
| + | Close; with nothing open, switch to the game and back |

- The Joy-Cons never send raw keys. The terminal keys (esc, enter, ctrl-c) are at the bottom of the view, for touch.
- **Mapping:** Chromium doesn't know the layout of joycond's combined Joy-Con device (057e:2008), so its buttons are mapped by hand in `PADS` in `KIOSK_JS` (`admin/app.py`). An unknown controller sends nothing and shows "Joy-Con button N" for each press, so a new one can be mapped safely.
- **Chime:** the kiosk runs with `--autoplay-policy=no-user-gesture-required`.
- **On-screen keyboard:** not working yet; GNOME's and Onboard stay hidden when a Chromium text box gets focus. Use your phone for free-text replies.

## Game mode (docked)

With `"game": "/page/pi.life/game"` in the kiosk's config, docking to a TV shows the Life RPG, and taking it back to the stand returns to the panel.

- It switches when the screen is at least `DOCK_WIDTH` (1600 px) wide; the Switch's own screen is 1280. A 1280×720 TV won't trigger it, so press **+**.
- **+** switches between the panel and the game. Coming back with **+** keeps the panel until the next undock.
- In the game: ↑ / ↓ choose an activity, **A** twice logs it, **B** cancels.
- An agent that starts waiting while the game is up opens on the panel.

## Screen PIN

Actions you want to protect from whoever is at the screen can require a PIN: list them in the kiosk's config as `"screen_pin": ["module/action", …]` (a remote's module counts too, so `"ha/alarm"` also covers `pi.ha`). The action's form must carry a PIN field, `<input class="pin" name="pin">`, which only the kiosk shows; a plain button can't send one. Five wrong PINs in ten minutes lock those actions. Set the PIN over SSH:
```bash
ssh -t switch 'umask 077; read -sp "New screen PIN: " p; echo; printf "%s" "$p" > ~/.config/homelab-admin/screen-pin'
```

## Files

| File | What it does |
|---|---|
| `control-center` | Opens Chromium in kiosk mode (falling back to Firefox) on the given URL. Turns off screen blanking |
| `install.sh` | Installs `control-center` into `~/.local/bin`, adds it to autostart, saves the URL and turns off idle and the lock screen |

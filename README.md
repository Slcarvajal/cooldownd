# cooldownd

**Keep your agents moving and your body healthy.** cooldownd is a control room for developers who run AI coding agents all day.

Agents stall the moment they need you, so you stay glued to the desk to answer them, and your sleep, meals and workouts pay for it. cooldownd puts your agents on a touchscreen, your phone and a pair of Joy-Cons, so you can answer one from the kitchen or between sets. It also turns your workouts and meals (and your sleep, once you feed it in) into an RPG, so looking after yourself feels like part of the job.

It's one Python panel, standard library only. It runs on a Raspberry Pi, your workstation and a jailbroken Nintendo Switch, all on your tailnet. There's no cloud, no app store and no build step. The phone app is the panel added to your Home Screen, and its alerts are Web Push sent by the panel itself.

<p align="center"><img src="docs/pager-switch.png" width="720" alt="The agent pager on the Switch's screen: an agent asks which store to use for a rate limiter; PC work, Go ahead, No and 1/2/3 buttons above its terminal"></p>

## Your agents

Every coding agent (Claude Code, Codex…) running in your terminals shows up live, by machine and project: who needs you, who's working, who's idle. When one starts waiting on you:

- **The touchscreen chimes and opens it**, with answer buttons above its terminal: PC work, Go ahead, No, 1/2/3.
- **The Joy-Cons answer it:** ↑/↓ choose, A presses, B means "I'll handle it at the PC", R jumps to the next waiting agent. The highlight starts on PC work, the safe answer.
- **Your phone gets a push alert** after a minute, and opens the same screen, where you can also type a reply.
- **"PC work" sets a question aside** without answering it, and it stops paging until the question changes.

The pager never retries text that may already have arrived, and never types into an agent that's busy.

<p align="center">
  <img src="docs/agents-phone.png" width="300" alt="The Agents tab on a phone: agents grouped by machine and project, with needs you, working, at PC and idle states">
  &nbsp;
  <img src="docs/agent-sheet-phone.png" width="300" alt="One agent open on a phone: its question, the answer buttons and a reply box">
</p>
<p align="center"><img src="docs/agents-desktop.png" width="720" alt="The Agents tab on a wide screen"></p>

## Your body

The Life RPG turns looking after yourself into XP:

- **Log a workout with one tap:** gym and volleyball out of the box. The activities are config: add running, swimming or climbing in a few lines.
- **Log a meal with a photo or a sentence.** Claude (Haiku, with Sonnet when unsure) estimates the items, calories and protein, and you can correct it in plain words. It learns from your corrections.
- **Levels, skill trees, weekly quests and streaks** are computed from those events, so changing a rule rescores your whole history.
- **Your avatar.** With your own sprite sheets, it wears what you did today: gym clothes after the gym, the jersey after a match. Without them, a drawn pixel avatar stands in.
- **Docked to a TV**, the Switch shows the game full screen.

<p align="center"><img src="docs/game.png" width="720" alt="The Life RPG game screen: avatar, skill trees, weekly quests, today's macros"></p>
<p align="center"><img src="docs/panel-phone.png" width="300" alt="The panel on a phone: the Life card with quests, streaks, calories and protein, and one-tap logging"></p>

## Your machines

- **One codebase, many panels.** Each machine runs `admin/app.py` (Python standard library only; phone alerts also need `python3-cryptography`) with its own config. A panel can show any other panel's cards (`"desk.agents"` is the workstation's agents card, shown on the Pi), and taps, sheets and actions are forwarded to the panel that owns them.
- **Your PCs.** CPU, GPU, temperatures, wall power against the PSU, services you can restart, a fixed list of commands (never a free shell), Wake-on-LAN from the always-on Pi, and Sleep.
- **The Pi.** Temperature graph, throttling and under-voltage flags, fan, 5 V supply, NVMe, a Docker stack with Home Assistant (lights, alarm, cameras), and uptime checks for your servers with phone alerts when one goes down.
- **A Nintendo Switch as the house touchscreen.** Switchroot Ubuntu in kiosk mode on a USB-C stand: the agent pager and your home. Docked to a TV, it shows the game instead.

## How it fits together

```
 iPhone (Home Screen web app, Web Push)
   │
   ▼            tailnet only (tailscale serve, never funnel)
 Raspberry Pi ── main panel, always on: Home Assistant, uptime, Life RPG, Wake-on-LAN
   │   ▲
   │   └──── Nintendo Switch ── kiosk touchscreen + Joy-Con pager; game screen when docked
   ▼
 Workstation ── its own panel: hardware, services, coding agents
```

Every panel listens on `127.0.0.1` and is published with `tailscale serve`, which adds the `Tailscale-User-Login` header that gates every request. Panels call each other with a shared token (`~/.config/homelab-admin/hub-token`, mode 600), so a tagged device with no user, like the Switch, can still use them. The token gives full control, not just reading: see the security model.

## Quick start

On each machine, with Python 3.10 or newer and Tailscale:

```bash
git clone https://github.com/Slcarvajal/cooldownd ~/homelab
cd ~/homelab/admin
cp config.example.json config.json        # config.pi.example.json on the Pi, config.hub.example.json on a kiosk
$EDITOR config.json                        # allowed_users: your Tailscale login; machines, services, commands
python3 app.py                             # listens on 127.0.0.1:8095 (a plain local visit gets 403: no login)
tailscale serve --bg --https=8443 http://127.0.0.1:8095
```

Then open `https://<machine>.<your-tailnet>.ts.net:8443`. To keep it running, add a user service:

```ini
# ~/.config/systemd/user/homelab-admin.service
[Unit]
Description=cooldownd panel
[Service]
WorkingDirectory=%h/homelab/admin
Environment=ADMIN_CONFIG=%h/homelab/admin/config.json
ExecStart=/usr/bin/python3 -I %h/homelab/admin/app.py
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now homelab-admin
loginctl enable-linger $USER              # keep it running when you're logged out
```

Setting up each piece:
- **Raspberry Pi** (Home Assistant stack, Pi health card): [`pi/README.md`](pi/README.md)
- **Nintendo Switch kiosk** (Switchroot, Joy-Cons, game mode): [`switch/README.md`](switch/README.md)
- **Phone alerts:** open the Pi panel from its Home Screen icon (iOS 16.4+), then Machine → Phone alerts → **Turn on alerts on this device**. Needs `python3-cryptography` on the Pi.
- **Meal estimates:** an Anthropic API key in `~/.config/homelab-admin/anthropic-key` (mode 600) on the panel that runs `life`.
- **Your avatar:** draw or generate 4×4 sprite sheets and cut them with [`admin/life/make_sprites.py`](admin/life/make_sprites.py). Without them, a drawn pixel avatar stands in.

## Configuration

Everything is in the panel's JSON config; the examples in `admin/` cover each key. The main ones:

| Key | What it does |
|---|---|
| `name`, `title` | Your name for the greeting; the page title |
| `allowed_users` | Tailscale logins allowed in |
| `hosts` | Extra host names the panel answers to, besides loopback and `*.ts.net` (e.g. your own domain) |
| `modules` | Cards to show, in order. `"<remote>.<module>"` shows another panel's card |
| `remotes`, `remote_modules` | Other panels' URLs, and the title and tab of their cards here |
| `tabs`, `titles` | Move or rename a card |
| `machines` | PCs to wake (MAC, broadcast) and sleep (through their own panel) |
| `services`, `commands` | systemd units to watch or control, and the fixed commands you can run |
| `ha` | Home Assistant URL and token file |
| `uptime` | URLs to check every minute, with the status and text you expect |
| `notify` | The panel URL alerts open, and how long an agent waits before your phone buzzes |
| `life` | Your name and food context, activities (`label`, `emoji`, `skill`, `xp`, `per_week`, `quest_bonus`), skills, sprite folder |
| `game` | On a kiosk: the game page to show when docked to a TV |
| `screen_pin` | On a kiosk: actions (`"module/action"`) that need a PIN at the screen |
| `psu_watts` | The workstation's power supply, for the PSU load bar (550) |

A Life RPG with running and swimming:

```json
"life": {
  "name": "Alex",
  "food_context": "Lisbon, Portugal",
  "activities": {
    "run":  {"label": "Run",  "emoji": "🏃", "skill": "endurance", "xp": 40, "per_week": 3, "quest_bonus": 80},
    "swim": {"label": "Swim", "emoji": "🏊", "skill": "endurance", "xp": 50, "per_week": 1, "quest_bonus": 50}
  }
}
```

## Modules

Each card is a file in `admin/modules/` that provides `NAME`, `TITLE`, `TAB` and `card(cfg)`, plus any of `ACTIONS`, `SHEETS` (bottom sheets), `FILES` (images, JSON), `PAGES` (full-screen pages), `UPLOADS` and a background `sample(cfg)`. The contract is at the top of `admin/app.py`.

| Module | Card |
|---|---|
| `overview` | The workstation at a glance: temperatures, power, agent counts |
| `agents` | Coding agents by machine and project; the pager (Orca adapter today, see below) |
| `machine` | CPU, GPU, temperatures, wall power vs PSU, alerts |
| `pi` | Raspberry Pi health |
| `power` | Wake-on-LAN and Sleep |
| `services`, `commands` | systemd units; a fixed list of commands |
| `tailnet` | Your devices on the tailnet |
| `ha` | Home Assistant: alarm, lights, cameras, switches |
| `uptime` | Server checks with alerts |
| `notify`, `webpush` | Phone alerts, with no app (RFC 8291 encryption, VAPID) |
| `life` | The Life RPG and its full-screen game |

## Security model

Read this before you run it.

- **The panel controls your machines.** It types into your agents, restarts services, runs its commands and sleeps PCs. It's only as safe as who can reach it.
- **Tailnet only.** Publish it with `tailscale serve`, **never `tailscale funnel`**. Every request must carry a `Tailscale-User-Login` in `allowed_users`, or the shared hub token. Anyone on your tailnet whose login isn't listed gets a 403.
- **Every local process is trusted.** The panel listens on `127.0.0.1` and trusts the login header `tailscale serve` adds, so any program or user on the same machine can send that header too, including containers with host networking (Home Assistant on the Pi). Run panels on machines whose local users and software you trust.
- **The hub token is full control.** Any panel holding it can run every action on every panel. It sits on the kiosk, which logs in by itself; if you lose the device, make a new token (`python3 -c "import secrets; print(secrets.token_hex(16))"`), put it in `~/.config/homelab-admin/hub-token` on every panel you keep, and remove the lost device from your tailnet.
- **Only known host names are answered** (loopback, `*.ts.net`, config `hosts`), so a web page can't reach a panel through DNS rebinding.
- **The kiosk's screen port has no login, on purpose.** `SCREEN_PORT` listens on `127.0.0.1` only, for the touchscreen itself, so whoever stands at the Switch has full control. Keep that in mind for guests.
- **Secrets stay out of git**, in mode-600 files under `~/.config/` (hub token, Home Assistant token, VAPID key, Anthropic key). Data stays on the machine under `~/.local/share/homelab-admin/`.
- **Actions only accept same-origin requests** (`Sec-Fetch-Site`), so another site can't trigger them.

Found a hole? See [SECURITY.md](SECURITY.md).

## Contributing: adapters wanted

cooldownd grew around one person's machines. Its best future is other people's: other agent IDEs, other consoles and handhelds, other sports and data sources. **See [CONTRIBUTING.md](CONTRIBUTING.md)**. The most wanted:

- **Agent adapters:** [T3 Code](https://github.com/pingdotgg/t3code), Cursor, Zed, Windsurf, VS Code terminals, plain tmux. Today the pager speaks to [Orca](https://github.com/stablyai/orca)'s CLI.
- **Device ports:** PS Vita, PSP, Switch Lite / OLED / Switch 2, Steam Deck, Wii U GamePad, old Android tablets, Kindles. Anything with a browser and a screen can be a room's touchscreen.
- **Data sources:** Fitbit, Garmin, Strava, Apple Health (via Shortcuts), Oura, Withings.
- **Sprite packs** and new skill trees.

## License

[MIT](LICENSE).

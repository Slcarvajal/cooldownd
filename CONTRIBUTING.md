# Contributing to cooldownd

cooldownd is for developers who run AI coding agents all day and want to stay healthy doing it. It started as one person's home lab: a Raspberry Pi, a workstation, a Nintendo Switch on a stand and an iPhone, each wired in by hand. **The way it grows is adapters**: other agent IDEs, other screens, other data. If you have one of those at home, you're who this is for.

Open an issue first for anything bigger than a fix, so we can agree on the shape. Small PRs are easiest to land.

## Ground rules

- **Standard library only** in `admin/` (phone alerts' `cryptography` is the one exception). The panel must run as-is on a Pi, a desktop and a 2017 console with a stock `python3`. A module that needs a package imports it lazily, inside the function that uses it (see `modules/webpush.py`), and says so in its docstring.
- **Tailnet only.** Nothing may listen on anything but `127.0.0.1`, or need `tailscale funnel`. No free shell: commands are a fixed list in config.
- **No secrets or personal data in the repo.** Tokens go in mode-600 files under `~/.config/`, data under `~/.local/share/homelab-admin/`. Example configs use `example.com` and placeholders.
- **Match the code around you**: short modules, a docstring that says what the card shows and what it needs, comments that say why.
- **Check before you send:** `python3 -m pyflakes admin` (or `uvx pyflakes admin`), and run the panel with one of the example configs to see your card render.

## Wanted: agent adapters

The pager (`admin/modules/agents.py`) shows coding agents and answers them. Today it talks to [Orca](https://github.com/stablyai/orca) through its `orca-ide` CLI (`orca()` and `orca_send()` in `admin/modules/common.py`). We'd love adapters for **[T3 Code](https://github.com/pingdotgg/t3code)**, Cursor, Zed, Windsurf, VS Code's terminals, plain **tmux** (which would make the pager work for anyone), and any other agentic IDE.

An adapter answers three questions:

| Call | Returns |
|---|---|
| **List** the agent terminals | For each: a stable `handle`, the `agent` (e.g. `claude`), the window `title`, the project's path (`worktreePath`), `branch`, the machine it runs on, when it last printed (`lastOutputAt`, ms), and whether it accepts input (`writable`) |
| **Read** a terminal's screen | The visible lines, oldest first |
| **Send** text to it, optionally pressing Enter | What really happened: `sent`, `typed` (accepted but the turn didn't start), `unknown` (it may have arrived; never retry) or `refused`, with a reason |

Everything else (who's waiting, the summary line, the Joy-Con answers, phone alerts) is computed from the screen, so it works for any agent the adapter can read. The honest `send` result matters: the pager never retries text that may already have arrived.

Plan: move the Orca calls behind a small adapter interface (`list / read / send`) chosen in config, with Orca as the first implementation. A PR that does that refactor is a great first contribution.

## Wanted: device ports

Anything with a browser and a screen can be a room's touchscreen. The Switch port lives in `switch/`: an OS install guide, a kiosk launcher, and the Joy-Con map in `KIOSK_JS` in `admin/app.py`. We'd love the same for:

- **Nintendo:** Switch Lite, OLED and Switch 2 where Linux runs; Wii U GamePad.
- **PlayStation handhelds:** PS Vita and PSP, through their browsers or homebrew. These are small and old, so a lighter page may be needed.
- **Others:** Steam Deck (game mode), old Android tablets, Kindles and e-ink readers, retro handhelds running Linux.

A port is usually:
1. a guide in its own folder (`vita/README.md`): flashing, the browser, kiosk mode, keeping it awake;
2. its controller's button numbers in `PADS` in `KIOSK_JS`, mapped by hand (press each button: an unknown controller shows "button N" on screen and sends nothing);
3. screenshots, and anything the page needs to fit the screen.

## Wanted: data sources and game content

The Life RPG (`admin/modules/life.py`) stores events and computes everything else, so a new source just writes events:

- **Sleep and activity:** Fitbit, Garmin, Strava, Oura, Withings, Apple Health (no web API; an iOS Shortcut can post to the panel). Sleep goes to the `sleep` table, one row per night. Sessions go to `activity` with a `kind` that matches config `life.activities`.
- **Activities with amounts** (km, minutes, sets) scoring per unit. This is open.
- **Sprite packs:** 4×4 sheets (front, left, right, back × stand, step, step, action; the last cell a celebration) cut with `admin/life/make_sprites.py`. Packs must be yours to share.
- **Skill trees and perk names** for more sports, in `KNOWN_SKILLS`.

## Reporting bugs

Say which machine and OS, which config modules, and what the panel log printed (`journalctl --user -u homelab-admin`). Strip tokens, tailnet names and URLs first.

For security problems, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

#!/usr/bin/env python3
"""admin: the home lab control center. Standard library only, so it runs
as-is on the desktop today and on the Raspberry Pi later.

Listens on 127.0.0.1. `tailscale serve` publishes it to the tailnet only (never
Funnel) and adds the Tailscale-User-Login header that gates every request.

Remotes: a panel can show another panel's cards. "desk.agents" in config "modules" is
the agents card of the remote "desk" (config "remotes"); its sheets, actions, files and
waiting agents are forwarded there (modules/remote.py). The Pi shows the desktop this way.
A remote accepts the Tailscale login header or the shared token in
~/.config/homelab-admin/hub-token (the Switch is tagged, so it has no login).
SCREEN_PORT adds a second 127.0.0.1 listener for the hub's own touchscreen; it's
never published, so it skips the Tailscale login check.

/live returns {"cards", "waiting", "pager", "notices", "quiet"}:
  notices  [{id, text, until}] kiosk notices from common.STATE["notices"] (the desk's presence
           module) plus the remotes' (remote.notices), one per global id, expired ones dropped.
           The kiosk toasts each new id once.
  quiet    night.quiet(cfg): config "night" says it's night. The kiosk then neither chimes nor
           opens a waiting agent; notices still toast, without the chime.

SHARE_PORT (with "coop" in "modules") adds a third 127.0.0.1 listener for friends: ShareHandler,
which answers only GET /share/v1/week and refuses every other method (405) and path (404). It
shares nothing with Handler: no login, no hub token, no modules, no /live. coop.serve(cfg,
authorization_header) -> (status, JSON bytes) checks the friend's share token and sets no
headers; ShareHandler sends Content-Type and Cache-Control: no-store on every answer. Publish it
with `tailscale serve` on its own HTTPS port, never Funnel.

Modules (modules/*.py, enabled in config.json) each provide:
  NAME, TITLE, TAB            tab is home / agents / machine / commands
  card(cfg) -> HTML           re-rendered on every live poll (~3 s)
  ACTIONS = {name: fn(cfg, arg, body) -> str | {"message", "title"?, "output"?}}
  SHEETS  = {name: fn(cfg, arg) -> HTML}           bottom-sheet views
  FILES   = {name: fn(cfg, arg) -> (content type, bytes[, cacheable])}   images, JSON
  PAGES   = {name: fn(cfg, arg) -> HTML}           full-screen pages at /page/<module>/<name>
  UPLOADS = {action: max bytes}                     actions that take more than 64 KB (photos)
  sample(cfg), SAMPLE_EVERY   optional background sampler (fills common.STATE)
"""
import hmac
import html
import importlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # python -I leaves the script dir off sys.path
from modules.common import STATE, token_ok  # noqa: E402

PORT = int(os.environ.get("ADMIN_PORT", "8095"))
CONFIG_PATH = os.environ.get("ADMIN_CONFIG") or os.path.join(HERE, "config.json")  # config.pi.json on the Pi
SCREEN_PORT = int(os.environ.get("SCREEN_PORT") or 0)
SHARE_PORT = int(os.environ.get("SHARE_PORT") or 0)  # friends' listener, see ShareHandler
PIN_PATH = os.path.expanduser("~/.config/homelab-admin/screen-pin")
ICON_PATH = os.path.join(HERE, "icon.png")
TABS = [("home", "Home", "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"),
        ("agents", "Agents", "M12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8zm-7 17a7 7 0 0 1 14 0"),
        ("machine", "Machine", "M4 5h16v10H4zM9 19h6M12 15v4"),
        ("commands", "Commands", "M5 7l5 5-5 5M12 17h7")]

# Shared by the panel and full-screen module pages (PAGES), where %%KIOSK_JS%% is replaced.
# Button numbers per controller. Joy-Cons on the rails (joycond's combined device) aren't a layout
# Chromium knows, so they were mapped by hand on 2026-10-12 (no d-pad left/right there yet: the kiosk faces
# change with a tap on their date line). Any other unknown controller sends nothing.
# DOCK_WIDTH: the Switch's own screen is 1280 px wide; docked to a TV it's wider, and the kiosk shows
# the game (config "game").
KIOSK_JS = r"""const PAD = {a: 1, b: 0, up: 12, down: 13, r: 5, zr: 7, plus: 9};
const PADS = {'057e Product: 2008': {a: 1, b: 0, up: 14, down: 15, r: 6, zr: 8, plus: 10}};
function padMap(gp) {
  const known = Object.keys(PADS).find(k => gp.id.includes(k));
  return known ? PADS[known] : gp.mapping === 'standard' ? PAD : null;
}
const DOCK_WIDTH = 1600;
// Joy-Con rumble where Chromium exposes it; nothing where it doesn't.
function rumble(ms) {
  try {
    const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find(g => g && g.connected);
    gp?.vibrationActuator?.playEffect('dual-rumble', {duration: ms, strongMagnitude: 0.8, weakMagnitude: 0.4})?.catch(() => {});
  } catch (e) {}
}"""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Control">
<meta name="theme-color" content="#f2f2f4">
<link rel="apple-touch-icon" href="icon.png"><link rel="manifest" href="manifest.webmanifest">
<title>%%TITLE%%</title><style>
:root{color-scheme:light;--bg:#f2f2f4;--card:#fff;--card2:#f2f2f4;--line:#e5e5ea;--fg:#1c1c1e;--mute:#636366;
--violet:#5b4bc4;--cyan:#0369a1;--green:#137333;--amber:#a15c07;--red:#d70015;--orange:#c2410c;
--idle:#e3e3e8;--idle-fg:#3a3a3c;--on:#d6d0f5;--on-fg:#3f2fa0;--need:#ffd9a8;--need-fg:#8a4200;--work:#cde3ff;--work-fg:#0b4a9c;
--done:#cdefd6;--done-fg:#1b5e2e;--caution:#ffedb0;--caution-fg:#6b4e00;--alert:#ffd6d3;--alert-fg:#8e1d14;
--r:28px;--shadow:0 1px 2px rgba(28,28,30,.04);
--sans:system-ui,-apple-system,"Segoe UI",Ubuntu,Cantarell,sans-serif;--mono:ui-monospace,"DejaVu Sans Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{overflow-x:hidden;margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 var(--sans)}
body{padding:calc(env(safe-area-inset-top) + 14px) 16px calc(env(safe-area-inset-bottom) + 84px)}
header{max-width:560px;margin:0 auto 14px;padding:0 4px;display:flex;align-items:center;justify-content:space-between;gap:10px}
header h1{font-size:28px;margin:0;letter-spacing:-.02em;font-weight:700;min-width:0}
header h1 small{display:block;font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;margin-top:2px}
#live{font:12px var(--mono);color:var(--mute);display:flex;align-items:center;gap:6px;flex:none}
#live i{width:8px;height:8px;border-radius:50%;background:#30b050;box-shadow:0 0 0 3px rgba(48,176,80,.18)}
#live.stale i{background:var(--red);box-shadow:none}
.pane{display:none;max-width:560px;margin:0 auto;gap:14px;grid-template-columns:minmax(0,1fr)}.pane.on{display:grid}
@media (min-width:900px){header,.pane{max-width:1240px}.pane.on{display:block;columns:2;column-gap:16px}.pane>section{break-inside:avoid;margin-bottom:16px}.pane[data-tab=home].on{columns:1}.pane>section:only-child{column-span:all}.pane .tiles{grid-template-columns:repeat(6,minmax(0,1fr))}.pane[data-tab=home] .tile{aspect-ratio:1/1}.lists{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px;align-items:start}}
section{min-width:0;background:var(--card);border-radius:var(--r);padding:18px 18px 14px;box-shadow:var(--shadow)}
h2{font-size:17px;letter-spacing:-.01em;color:var(--fg);font-weight:700;margin:0 0 12px}
h3{font-size:13px;color:var(--violet);margin:16px 0 6px;font-weight:600}h2+h3,section>div>h3:first-child{margin-top:2px}
h3.host{color:var(--cyan)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
h2+.row,h3+.row{border-top:0}.row>span:last-child{text-align:right;display:flex;gap:6px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
small,.muted{color:var(--mute)}small{font-size:12px}
b{font-variant-numeric:tabular-nums}
.ok{color:var(--green)}.warn{color:var(--amber)}.bad{color:var(--red)}.dim{color:var(--mute)}.busy{color:var(--cyan)}
.pill{font-size:12px;font-weight:600;padding:3px 10px;border-radius:99px;white-space:nowrap;background:var(--idle);color:var(--idle-fg)}
.pill.ok{background:var(--done);color:var(--done-fg)}.pill.bad{background:var(--alert);color:var(--alert-fg)}.pill.warn{background:var(--caution);color:var(--caution-fg)}
.pill.busy{background:var(--work);color:var(--work-fg)}.pill.dim{background:var(--idle);color:var(--idle-fg)}
.agent .pill.warn,.counts .pill.warn,.sheet-meta .pill.warn{background:var(--need);color:var(--need-fg)}
.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.tile{background:var(--idle);color:var(--idle-fg);border:0;border-radius:24px;padding:14px 16px;display:flex;flex-direction:column;justify-content:flex-end;gap:2px;min-width:0;min-height:100px;text-align:left;font:inherit}
.tile b{font-size:24px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:inherit;overflow-wrap:anywhere;line-height:1.2}
.tile small{color:inherit;font-weight:600;font-size:12.5px}
button.tile b{font-size:16px;letter-spacing:0}
.ic{width:24px;height:24px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.tile .ic{width:30px;height:30px;margin-bottom:auto}
.meter{display:grid;grid-template-columns:56px 52px 1fr;align-items:center;gap:8px;padding:5px 0}
.meter b{font:600 15px var(--mono);text-align:right}
.bar{height:8px;border-radius:99px;background:var(--idle);overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px;background:#30b050}
.bar.warn i{background:#f5a400}.bar.bad i{background:#ff453a}.bar.dim i{background:#aeaeb2}
.sparks{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;align-items:center;margin:6px 0 2px;font-size:12px;color:var(--mute)}
.spark{width:100%;height:26px}.spark polyline{fill:none;stroke-width:1.6;stroke:#0a84ff}
.spark.violet polyline{stroke:var(--violet)}.spark.green polyline{stroke:#30b050}.spark.amber polyline{stroke:#f5a400}
.alert{font:12px/1.4 var(--mono);padding:8px 10px;border-radius:14px;margin:4px 0;background:color-mix(in srgb,currentColor 10%,transparent);word-break:break-word}
.alert.bad{background:var(--alert);color:var(--alert-fg)}.alert.warn{background:var(--caution);color:var(--caution-fg)}
button{font:inherit;color:inherit;cursor:pointer}
section button:not(.agent):not(.mini):not(.cmd):not(.tile),.sheet form button{width:100%;padding:14px;border:0;border-radius:18px;background:var(--violet);color:#fff;font-weight:700;font-size:16px}
.machine{display:grid;gap:8px}.machine+.machine{margin-top:12px}.machine small{line-height:1.3}
img.cam{display:block;width:100%;border-radius:20px;background:var(--card2);min-height:120px}
a.open{display:block;text-align:center;padding:12px;background:var(--on);border-radius:18px;color:var(--on-fg);text-decoration:none;font-weight:600}
button.mini{border:0;background:var(--idle);color:var(--idle-fg);border-radius:99px;padding:4px 11px;font-size:12.5px;font-weight:600}
button.agent{display:grid;grid-template-columns:10px 1fr auto;gap:10px;align-items:center;width:100%;text-align:left;
background:var(--card2);border:0;border-radius:18px;padding:11px 14px;margin:6px 0}
button.agent.s-waiting{background:#fff1e0}
.agent .who{display:grid;min-width:0}.agent .who b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent .who small{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.agent .when{display:grid;justify-items:end;gap:3px}
.dot{width:10px;height:10px;border-radius:50%;background:currentColor}.dot.busy{animation:pulse 1.2s infinite}
.dot.ok{color:#30b050}.dot.busy{color:#0a84ff}.dot.warn{color:#f5a400}.dot.bad{color:#ff3b30}.dot.dim{color:#aeaeb2}.agent .dot.warn{color:#ff9f0a}
@keyframes pulse{50%{opacity:.35}}
.counts{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px}
.project{display:flex;justify-content:space-between;gap:12px;font-weight:600;margin:12px 0 0;font-size:14px}
.project small{font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
button.cmd{display:grid;width:100%;text-align:left;background:var(--card2);border:0;border-radius:18px;padding:12px 14px;margin:6px 0;gap:2px}
.report pre,.screen,.output{font:11px/1.35 var(--mono);white-space:pre-wrap;word-break:break-word;margin:4px 0 8px;color:var(--fg)}
.report pre{background:var(--card2);padding:10px;border-radius:14px}
details summary{color:var(--mute);padding:6px 0}
.err{color:var(--red);font:12px var(--mono)}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;background:color-mix(in srgb,var(--bg) 82%,transparent);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--line);padding:6px 4px calc(env(safe-area-inset-bottom) + 6px);z-index:5}
nav button{background:none;border:0;display:grid;justify-items:center;gap:2px;font-size:11px;font-weight:600;color:var(--mute);padding:4px 10px;position:relative}
nav button.on{color:var(--violet)}nav svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
nav .badge{position:absolute;top:0;right:6px;background:#ff9f0a;color:#1c1c1e;font:700 10px var(--mono);border-radius:99px;padding:1px 5px}
#shade{position:fixed;inset:0;background:rgba(28,28,30,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:9}
#shade.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;left:0;right:0;bottom:0;max-height:88vh;background:var(--card);border-radius:var(--r) var(--r) 0 0;z-index:10;
transform:translateY(100%);transition:transform .25s,visibility .25s;visibility:hidden;display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom)}
.sheet.on{transform:none;visibility:visible}
.sheet header{margin:0;padding:14px 18px;border-bottom:1px solid var(--line);gap:8px;max-width:none}
.sheet header b{font-size:17px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sheet header button{background:var(--idle);color:var(--idle-fg);border:0;border-radius:99px;padding:7px 14px;font-size:13px;font-weight:600}
#padhelp{color:var(--violet);font-weight:600;white-space:nowrap}
.quick{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:4px 0 10px}
.quick button{border:0;background:var(--idle);color:var(--idle-fg);border-radius:20px;padding:10px 6px;min-height:64px;min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;text-align:center;font:inherit}
.quick button b{font-size:15px;font-weight:700;line-height:1.15}.quick .q-1 b,.quick .q-2 b,.quick .q-3 b{font-size:22px;line-height:1}
.quick button small{font-size:11.5px;font-weight:500;color:inherit;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.quick .q-pc small{display:none}.quick .ic{width:22px;height:22px}
.quick button:disabled,.keys button:disabled{opacity:.45}.quick button.sel{box-shadow:0 0 0 5px var(--violet)}
.tile.tint-on,.quick .tint-on{background:var(--on);color:var(--on-fg)}.tile.tint-need,.quick .tint-need{background:var(--need);color:var(--need-fg)}
.tile.tint-busy,.quick .tint-busy{background:var(--work);color:var(--work-fg)}.tile.tint-ok,.quick .tint-ok{background:var(--done);color:var(--done-fg)}
.tile.tint-warn,.quick .tint-warn{background:var(--caution);color:var(--caution-fg)}.tile.tint-bad,.quick .tint-bad{background:var(--alert);color:var(--alert-fg)}
@media (min-width:900px){
.pager{display:grid;grid-template-columns:minmax(0,470px) minmax(0,1fr);grid-template-rows:minmax(clamp(300px,50vh,440px),auto);gap:22px;margin:8px 0 10px}
.pager .quick{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:repeat(3,minmax(0,1fr));gap:16px;margin:0}
.pager .quick button{border-radius:30px;gap:6px;padding:10px}.pager .quick button b{font-size:22px}
.pager .quick .q-1 b,.pager .quick .q-2 b,.pager .quick .q-3 b{font-size:44px}
.pager .quick button small{font-size:15px}.pager .quick .q-pc small{display:block}.pager .quick .ic{width:40px;height:40px}
.pager .screen{contain:size;max-height:none;margin:0;border-radius:30px;padding:20px 24px;font-size:13px}}
#sheet-body{overflow:auto;padding:12px 18px 16px;-webkit-overflow-scrolling:touch}
.sheet-meta{font-size:13px;margin-bottom:8px}
.screen{background:#141416;color:#e5e5ea;border-radius:20px;padding:12px 14px;max-height:46vh;overflow:auto}
.sheet form{display:grid;gap:8px;margin-top:8px}
textarea{width:100%;font:16px/1.35 var(--sans);color:var(--fg);background:var(--card2);border:1px solid var(--line);border-radius:18px;padding:12px;resize:vertical}
.keys{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.keys button{flex:1;min-width:52px;background:var(--idle);color:var(--idle-fg);border:0;border-radius:99px;padding:9px 0;font:600 13px var(--mono)}
#toast{position:fixed;left:16px;right:16px;bottom:calc(env(safe-area-inset-bottom) + 76px);max-width:528px;margin:0 auto;background:var(--fg);color:var(--bg);
padding:12px 16px;border-radius:18px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:40;font-weight:600}
#toast.show{opacity:1}
.headline{font-size:19px;font-weight:650;line-height:1.35;margin:0 0 10px;letter-spacing:-.01em}
.advice{background:var(--on);color:var(--on-fg);border-radius:18px;padding:10px 14px;margin-bottom:6px;font-size:14px}
.today{display:grid;gap:4px 18px}
@media (min-width:900px){.today{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) minmax(0,1fr)}}
ol.next{margin:0;padding:0;list-style:none;counter-reset:n}
ol.next li{counter-increment:n;display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:start;background:var(--card2);border-radius:18px;padding:11px 14px;margin:6px 0}
ol.next li::before{content:counter(n);width:26px;height:26px;border-radius:50%;background:var(--violet);color:#fff;font-weight:700;font-size:13px;display:grid;place-items:center}
ol.next li b{display:block;font-weight:600;line-height:1.3}ol.next li small{display:block;margin-top:2px}
ol.next li>span{white-space:nowrap;display:flex;gap:6px;align-items:center}
.meet{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:8px 0;border-top:1px solid var(--line)}
h3+.meet{border-top:0}.meet small.prep{color:var(--violet);margin-top:2px}.meet b{color:var(--violet);font-variant-numeric:tabular-nums}.meet small{display:block}
.task{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
h3+.task{border-top:0}.task small{display:block}.task>small{white-space:nowrap}.task.late>small{color:var(--red);font-weight:600}
h4{font-size:12px;color:var(--mute);margin:10px 0 2px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
button.ask{display:grid;gap:2px;width:100%;text-align:left;margin:4px 0 8px;padding:14px 16px;border:0;border-radius:22px;background:var(--violet);color:#fff}
button.ask b{font-size:16px}button.ask small{color:#ece8ff}
.chat{display:grid;gap:10px;margin-bottom:10px}.msg{display:grid;gap:6px}
.msg .q{justify-self:end;max-width:85%;background:var(--violet);color:#fff;border-radius:18px 18px 4px 18px;padding:8px 12px}
.msg .a{justify-self:start;max-width:92%;background:var(--card2);border-radius:18px 18px 18px 4px;padding:8px 12px;display:grid;gap:6px;justify-items:start}
.proposal{background:#fff4e6;border:1px solid var(--need);border-radius:16px;padding:8px 10px;display:grid;gap:6px;width:100%}
form.confirm{display:flex;gap:6px;align-items:center;margin:0}form.confirm button:not(.mini){width:auto;padding:8px 16px}
.pin{display:none;width:90px;font:16px var(--mono);padding:7px 8px;border:1px solid var(--line);border-radius:12px}
body.kiosk .pin{display:block}
body.kiosk.typing .sheet{top:0;bottom:auto;max-height:50vh;border-radius:0 0 var(--r) var(--r)}
.foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:10px}
.life-top{display:grid;grid-template-columns:76px minmax(0,1fr);gap:14px;align-items:center;margin-bottom:6px}
.life-top .av{width:76px;height:76px;background:var(--card2);border-radius:22px;overflow:hidden;display:flex;align-items:flex-end;justify-content:center;position:relative}
.life-top .av .frame{position:relative;flex:none;height:72px;overflow:hidden;animation:bob 2.4s ease-in-out infinite}
.life-top .av .frame img{position:absolute;left:0;top:0;height:100%;width:auto!important;max-width:none}
.life-top .av>img{position:absolute;left:0;bottom:2px;height:72px;width:auto!important;max-width:none;animation:bob 2.4s ease-in-out infinite}
.life-top .av>svg{width:60px;height:60px;margin:auto;animation:bob 2.4s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-3px)}}.life-top b{display:block;margin-bottom:4px}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0 6px}
section .duo button:not(.agent):not(.mini):not(.cmd){background:var(--on);color:var(--on-fg);border-radius:22px;padding:14px 16px;min-height:76px;font-size:16px;text-align:left;
display:flex;flex-direction:column;justify-content:flex-end;align-items:flex-start;gap:6px}
section .duo button.alt:not(.agent):not(.mini):not(.cmd){background:var(--idle);color:var(--idle-fg)}.duo button b{font-weight:700}.duo .ic{width:26px;height:26px}
.field{display:grid;grid-template-columns:minmax(0,1fr) 150px;align-items:center;gap:8px;font-size:14px}
.field input,.field select{font:16px var(--sans);color:var(--fg);background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:8px;width:100%;min-width:0}
#photo-in{position:fixed;left:-9999px;opacity:0}
#face{position:fixed;inset:0;z-index:30;background:#000;color:#fff;font-family:var(--sans);overflow:hidden;user-select:none;-webkit-user-select:none;touch-action:none}
#face[hidden]{display:none}
#face .fc{position:absolute;inset:0;display:none;transition:opacity .6s}#face.quiet .fc{opacity:.4}
#face[data-face=agents] #face-agents,#face[data-face=body] #face-body{display:block}
#face-body{background:#0e0e10}
.face-date{font-size:26px;font-weight:600;color:#98989d;letter-spacing:.08em;padding:6px 28px;cursor:pointer}
#fa-ring .tick{position:absolute;width:30px;height:8px;border-radius:4px}
#fa-ring .tlabel{position:absolute;width:180px;margin-left:-90px;text-align:center;font-size:17px;font-weight:600;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#fa-mid{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;align-items:center;gap:4px;text-align:center}
#fa-clock{font-size:min(33vh,19vw);line-height:1;font-weight:600;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
#fa-count{font-size:32px;font-weight:700;letter-spacing:.04em;margin-top:10px;color:#98989d}#fa-count.need{color:#ff9f0a}
#fa-hint{font-size:20px;color:#8e8e93;margin-top:8px;min-height:1.4em}
.fb-stage{position:absolute;left:50%;top:50%;width:1280px;height:720px;margin:-360px 0 0 -640px;transform:scale(var(--k,1));padding:48px 56px;display:flex;gap:40px;box-sizing:border-box}
.fb-stage .face-date{position:absolute;top:8px;right:28px;font-size:18px}
.fb-left{width:400px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}
.fb-lvl{font-size:22px;font-weight:600;color:#98989d;letter-spacing:.08em;text-align:center}
.fb-me{height:322px;max-width:400px;background-repeat:no-repeat;background-size:1600% 100%;background-position:0 0;image-rendering:pixelated;animation:fbob 2.4s ease-in-out infinite}
.fb-svg{width:280px;height:280px;animation:fbob 2.4s ease-in-out infinite}.fb-svg svg{width:100%;height:100%}
@keyframes fbob{50%{transform:translateY(-6px)}}
.fb-bar{width:320px;height:12px;border-radius:6px;background:#2c2c2e;overflow:hidden}.fb-bar i{display:block;height:100%;border-radius:6px;background:#bf5af2}
.fb-sub{font-size:18px;color:#98989d}
.fb-right{flex:1;min-width:0;display:flex;flex-direction:column;gap:22px;justify-content:center}
.fb-rings{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:18px}
.fb-ring{border-radius:30px;background:#1c1c1e;padding:22px 10px 18px;display:flex;flex-direction:column;align-items:center;gap:8px;text-align:center}
.fb-ring b{font-size:22px}.fb-ring small{font-size:16px;color:#98989d}
.fb-boss{border-radius:30px;background:#1c1c1e;padding:22px 26px;display:flex;flex-direction:column;gap:12px}
.fb-boss header{max-width:none;margin:0;padding:0;align-items:baseline}.fb-boss header b{font-size:22px}.fb-boss header span{font-size:18px;color:#ff9f0a;font-weight:600}
.fb-boss .fb-bar{width:auto;height:16px;border-radius:8px}.fb-boss .fb-bar i{background:#ff453a}.fb-boss small{font-size:17px;color:#98989d}
.fb-quests{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.fb-q{border-radius:24px;background:#2c2c2e;padding:16px 18px;display:flex;flex-direction:column;gap:4px;min-width:0}
.fb-q b{font-size:19px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fb-q small{font-size:16px;color:#98989d}
.fb-q.done{background:#173b22}.fb-q.done b{color:#5be07a}.fb-q.done small{color:#8fd9a3}
</style></head><body>
<header><h1 id="hello">%%TITLE%%</h1><span id="live"><i></i><span id="live-t">%%HOST%%</span></span></header>
%%PANES%%
<nav>%%NAV%%</nav>
<div id="shade"></div>
<div class="sheet" id="sheet"><header><b id="sheet-title"></b><small id="padhelp" hidden>Joy-Con: ↑↓ choose · A press · B PC work · R next · + close</small><span><button id="sheet-refresh">Refresh</button> <button id="sheet-close">Close</button></span></header><div id="sheet-body"></div></div>
<div id="toast"></div>
<input type="file" id="photo-in" accept="image/*">
<div id="face" hidden><div class="fc" id="face-agents"><div id="fa-ring"></div><div id="fa-mid"><div class="face-date" id="fa-date"></div>
<div id="fa-clock"></div><div id="fa-count"></div><div id="fa-hint"></div></div></div>
<div class="fc" id="face-body"><div class="fb-stage"><div class="face-date" id="fb-date"></div><div id="fb-main" style="display:contents"></div></div></div></div>
<script>
const $ = s => document.querySelector(s);
const store = { get(k) { try { return localStorage.getItem(k) } catch (e) { return null } },
                set(k, v) { try { localStorage.setItem(k, v) } catch (e) {} } };
let sheetPath = null, lastOk = Date.now();
let faceOn = false, lastInput = Date.now(), swallowUntil = 0;  // the kiosk's ambient faces, further down

function toast(m) { const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 3500) }

function showTab(tab) {
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.dataset.tab === tab));
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  store.set('tab', tab); scrollTo(0, 0);
}
document.querySelectorAll('nav button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
showTab(document.querySelector(`nav button[data-tab="${store.get('tab')}"]`) ? store.get('tab') : 'home');

async function live() {
  if (document.hidden) return;
  try {
    const r = await fetch('live', {cache: 'no-store'}); if (!r.ok) throw 0;
    const d = await r.json();
    for (const [id, html] of Object.entries(d.cards)) { const el = document.getElementById('m-' + id); if (el && el.innerHTML !== html) el.innerHTML = html }
    const badge = $('#badge-agents'); if (badge) { badge.textContent = d.waiting || ''; badge.hidden = !d.waiting }
    quietNow = !!d.quiet; pager(d.pager || []); notices(d.notices || []); if (faceOn) drawFace();
    lastOk = Date.now(); $('#live').classList.remove('stale'); $('#live-t').textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  } catch (e) { if (Date.now() - lastOk > 10000) $('#live').classList.add('stale') }
}
function hello() {
  const d = new Date(), h = d.getHours();
  const name = %%NAME%%;  // config "name"
  $('#hello').innerHTML = (h < 12 ? 'Good morning' : h < 19 ? 'Good afternoon' : 'Good evening') + (name ? ', ' + name.replace(/[&<>]/g, '') : '')
    + '<small class="muted">' + d.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'long'}) + '</small>';
}
hello(); setInterval(hello, 60000);
setInterval(live, 3000); document.addEventListener('visibilitychange', live); live();
setInterval(() => { if (document.hidden) return;
  document.querySelectorAll('img[data-live]').forEach(i => { if (i.complete) i.src = i.src.replace(/\?t=\d+.*$/, '') + '?t=' + Date.now() }) }, 1000);

async function post(path, body) {
  const r = await fetch('action/' + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const d = await r.json().catch(() => ({message: 'Request failed'}));
  if (d.output !== undefined) openSheet(null, d.title || 'Output', '<pre class="output">' + d.output.replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c])) + '</pre>');
  toast(d.message); setTimeout(live, 600); return r.ok;
}
async function openSheet(path, title, html) {
  hideFace(); sheetPath = path; $('#sheet-title').textContent = title || '';
  $('#padhelp').hidden = !(KIOSK && isAgentView(path));
  $('#sheet').classList.add('on'); $('#shade').classList.add('on');
  if (html !== undefined) { $('#sheet-body').innerHTML = html; return }
  const r = await fetch('sheet/' + path, {cache: 'no-store'}); const keep = $('#sheet-body textarea')?.value;
  $('#sheet-body').innerHTML = await r.text();
  const ta = $('#sheet-body textarea'); if (ta && keep) ta.value = keep;
  const sc = $('#sheet-body .screen'); if (sc) sc.scrollTop = sc.scrollHeight;
  if (KIOSK) markSel();
}
function closeSheet() { $('#sheet').classList.remove('on'); $('#shade').classList.remove('on'); sheetPath = null }
$('#sheet-close').onclick = closeSheet; $('#shade').onclick = closeSheet;
$('#sheet-refresh').onclick = () => sheetPath && openSheet(sheetPath, $('#sheet-title').textContent);
setInterval(() => { if (sheetPath && !document.hidden && !['TEXTAREA', 'INPUT'].includes(document.activeElement?.tagName)) openSheet(sheetPath, $('#sheet-title').textContent) }, 4000);

// Agent pager, on the Switch's own screen only: when an agent starts waiting on you, chime and open it;
// the Joy-Cons answer it. Uses the standard gamepad layout (A is the right face button).
// At night (/live "quiet") it neither chimes nor opens anything.
const KIOSK = document.body.classList.contains('kiosk');
let waitingNow = [], pagedKeys = new Set(), quietNow = false;
function chime() {
  try {
    const c = chime.ctx || (chime.ctx = new AudioContext());
    [660, 880, 1320].forEach((f, i) => {
      const o = c.createOscillator(), g = c.createGain(), t = c.currentTime + i * 0.16;
      o.frequency.value = f; o.connect(g); g.connect(c.destination);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.3, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
      o.start(t); o.stop(t + 0.45);
    });
  } catch (e) {}
}
// Kiosk notices (/live "notices", e.g. the desk's break reminders): a toast per new id, a chime only by day.
// Ids already shown are remembered across reloads, since the kiosk goes back and forth to the game.
function notices(list) {
  if (!KIOSK) return;
  let shown; try { shown = JSON.parse(store.get('notices') || '[]') } catch (e) { shown = [] }
  const fresh = list.filter(n => n && n.id && !shown.includes(n.id));
  if (!fresh.length) return;
  store.set('notices', JSON.stringify(list.map(n => n.id)));
  fresh.forEach((n, i) => setTimeout(() => { toast(String(n.text || '')); if (!quietNow) chime() }, i * 4000));
}
// an agent's sheet, here or on a remote panel ("desk.agents/view/<handle>")
const isAgentView = p => /^([\w-]+\.)*agents\/view\//.test(String(p));
function openAgent(a) { padSel = 0; openSheet(a.sheet, '🔔 ' + a.title + ' · ' + a.project) }
function pager(list) {
  waitingNow = list;
  if (!KIOSK) return;
  const fresh = list.find(a => !pagedKeys.has(a.key));
  pagedKeys = new Set(list.map(a => a.key));  // an agent that stops and later waits again pages again
  if (!fresh || quietNow) return;
  chime();
  const busy = (sheetPath && !isAgentView(sheetPath)) || ['TEXTAREA', 'INPUT'].includes(document.activeElement?.tagName);  // don't pull you out of another sheet
  if (busy) toast('🔔 ' + fresh.title + ' needs you. Press R on the Joy-Con.'); else openAgent(fresh);
}
%%KIOSK_JS%%
let padPrev = {};
let padSel = 0;  // highlighted answer button; 0 is PC work, the safe default
function markSel() { document.querySelectorAll('#sheet-body .quick button').forEach((b, i) => b.classList.toggle('sel', i === padSel)) }
async function padPress(name) {
  const view = isAgentView(sheetPath) ? sheetPath : null;
  if (name === 'plus') return sheetPath ? closeSheet() : GAME && (location.href = GAME + '?kiosk=' + (screen.width >= DOCK_WIDTH ? 'dock' : '1'));
  if (name === 'r' || name === 'zr' || (!view && name === 'a')) {
    if (!waitingNow.length) return toast('No agents are waiting on you.');
    const i = waitingNow.findIndex(a => a.sheet === view);
    return openAgent(waitingNow[(i + 1) % waitingNow.length]);
  }
  const buttons = [...document.querySelectorAll('#sheet-body .quick button')];
  if (!view || !buttons.length) return;
  if (name === 'up' || name === 'down') { padSel = (padSel + (name === 'down' ? 1 : -1) + buttons.length) % buttons.length; return markSel() }
  if (name === 'b') padSel = 0;
  if (name === 'a' || name === 'b') { buttons[padSel].click(); padSel = 0 }
}
function padLoop() {
  const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find(g => g && g.connected);
  const map = gp && padMap(gp);
  if (map) {
    for (const [name, i] of Object.entries(map)) {
      const down = !!(gp.buttons[i] && gp.buttons[i].pressed);
      if (down && !padPrev[name]) { if (faceOn) facePad(name); else padPress(name); lastInput = Date.now() }
      padPrev[name] = down;
    }
  } else if (gp) {  // a layout we don't know: send nothing, just show and log which button it is
    gp.buttons.forEach((b, i) => {
      if (b.pressed && !padPrev['raw' + i]) {
        lastInput = Date.now();
        if (faceOn) hideFace(); else { toast('Joy-Con button ' + i); fetch('pad-debug?id=' + encodeURIComponent(gp.id) + '&i=' + i).catch(() => {}) }
      }
      padPrev['raw' + i] = b.pressed;
    });
  }
}
if (KIOSK) setInterval(padLoop, 40);
// Ambient faces, on the hub's own screen only: after FACE_IDLE with no touch, no Joy-Con press and no sheet
// open, a full-screen face: the agents ring (default) or the body. Any touch or button brings the panel back,
// and that touch does nothing else. A tap on the date line, or the d-pad left/right where the controller's
// layout maps them, changes the face. A waiting agent still opens as before (pager), except at night,
// when the face stays, dimmed, and shows the count. Data: the desk's agent party (FILES party/now) and the
// life state (FILES state/now), through whichever module serves them here (body data-party, data-life).
const FACE_IDLE = 60000;
const PARTY = document.body.dataset.party || '', LIFE = document.body.dataset.life || '';
const FACES = LIFE ? ['agents', 'body'] : ['agents'];
const FACE_COLOR = {waiting: '#FF9F0A', working: '#0A84FF', atpc: '#BF5AF2', idle: '#48484A', stuck: '#FF453A'};
const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const num = v => Number.isFinite(+v) ? +v : 0;  // numbers from a state file, never markup
let faceIdx = Math.max(0, FACES.indexOf(store.get('face'))), faceParty = null, faceLife = null, ringKey = '', bodyKey = '';
function showFace() { faceOn = true; $('#face').hidden = false; drawFace(); loadFace() }
function hideFace() { if (!faceOn) return; faceOn = false; $('#face').hidden = true; lastInput = Date.now() }
function cycleFace(step) { faceIdx = (faceIdx + step + FACES.length) % FACES.length; store.set('face', FACES[faceIdx]); drawFace(); loadFace() }
function facePad(name) { if (name === 'left' || name === 'right') cycleFace(name === 'right' ? 1 : -1); else hideFace() }
function drawFace() {
  const f = $('#face'); f.dataset.face = FACES[faceIdx]; f.classList.toggle('quiet', quietNow);
  faceClock();
  if (FACES[faceIdx] === 'agents') agentsFace(); else bodyFace();
}
function faceClock() {
  const d = new Date(), day = DAYS[d.getDay()] + ' ' + d.getDate();
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  $('#fa-date').textContent = day; $('#fa-clock').textContent = hm; $('#fb-date').textContent = day + ' · ' + hm;
}
async function loadFace() {
  const face = FACES[faceIdx];
  try {
    if (face === 'agents' && PARTY) {
      const r = await fetch('file/' + PARTY + '/party/now', {cache: 'no-store'}), d = r.ok ? await r.json() : null;
      faceParty = d && Array.isArray(d.members) ? d.members.filter(m => m && typeof m === 'object') : null;
    } else if (face === 'body' && LIFE) {
      const r = await fetch('file/' + LIFE + '/state/now', {cache: 'no-store'});
      if (r.ok) faceLife = await r.json();
    }
  } catch (e) { if (face === 'agents') faceParty = null }
  if (faceOn) drawFace();
}
// Agents face: the clock, how many agents need you, and a ring of ticks along the screen's edge, one segment
// per agent coloured by its status. Without the party (desk asleep), the waiting agents from /live.
function agentsFace() {
  const list = faceParty || waitingNow.map(a => ({project: a.project, title: a.title, status: 'waiting'}));
  const need = list.filter(a => a.status === 'waiting').length;
  $('#fa-count').textContent = need ? need + (need === 1 ? ' AGENT NEEDS YOU' : ' AGENTS NEED YOU') : list.length ? 'ALL AGENTS BUSY' : 'NO AGENTS RUNNING';
  $('#fa-count').classList.toggle('need', need > 0);
  $('#fa-hint').textContent = !need ? '' : quietNow ? 'Night mode. Touch the screen to answer.' : 'Touch the screen, then R on the Joy-Con for the next one';
  ring(list);
}
// Points along a rounded rectangle (a superellipse, |x/a|^n + |y/b|^n = 1) inset from the screen's edge,
// evenly spaced by arc length from the top centre, clockwise: [x, y, angle of the outward normal].
function ringTicks(W, H, N) {
  const a = W / 2 - 56, b = H / 2 - 36, n = 7, M = 1600, pts = [], acc = [0], out = [];
  for (let i = 0; i <= M; i++) {
    const t = -Math.PI / 2 + 2 * Math.PI * i / M, c = Math.cos(t), s = Math.sin(t);
    pts.push([a * Math.sign(c) * Math.pow(Math.abs(c), 2 / n), b * Math.sign(s) * Math.pow(Math.abs(s), 2 / n)]);
  }
  for (let i = 1; i < pts.length; i++) acc.push(acc[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const total = acc[acc.length - 1];
  for (let k = 0, j = 0; k < N; k++) {
    const d = (k + 0.5) / N * total;
    while (j < acc.length - 1 && acc[j] < d) j++;
    const f = j ? (d - acc[j - 1]) / ((acc[j] - acc[j - 1]) || 1) : 0, p = pts[j ? j - 1 : 0];  // samples are sparse near the axes
    const x = p[0] + (pts[j][0] - p[0]) * f, y = p[1] + (pts[j][1] - p[1]) * f;
    const nx = Math.sign(x) * Math.pow(Math.abs(x / a), n - 1) / a, ny = Math.sign(y) * Math.pow(Math.abs(y / b), n - 1) / b;
    out.push([x, y, Math.atan2(ny, nx) * 180 / Math.PI]);
  }
  return out;
}
function ring(list) {
  const W = innerWidth, H = innerHeight, key = W + 'x' + H + JSON.stringify(list.map(a => [a.project, a.status]));
  if (key === ringKey) return;
  ringKey = key;
  const k = list.length, per = k ? Math.max(3, Math.floor(60 / k)) : 60, cx = W / 2, cy = H / 2, out = [];
  ringTicks(W, H, k ? per * k : 60).forEach(([x, y, deg], i) => {
    const g = Math.floor(i / per), inSeg = i % per, a = list[g];
    if (a && inSeg === per - 1) return;  // the gap after each agent's segment
    const st = a && FACE_COLOR[a.status] ? a.status : 'idle';
    const alpha = !a ? 0.35 : st === 'working' ? 0.45 + 0.55 * inSeg / Math.max(1, per - 2) : 1;
    out.push(`<i class="tick" style="left:${(cx + x - 15).toFixed(1)}px;top:${(cy + y - 4).toFixed(1)}px;transform:rotate(${deg.toFixed(1)}deg);background:${FACE_COLOR[st]};opacity:${alpha.toFixed(2)}"></i>`);
    if (a && k <= 12 && inSeg === Math.floor((per - 1) / 2) && (g === 0 || list[g - 1].project !== a.project)) {
      const len = Math.hypot(x, y) || 1;
      out.push(`<span class="tlabel" style="left:${(cx + x - x / len * 74).toFixed(1)}px;top:${(cy + y - y / len * 56 - 10).toFixed(1)}px;color:${st === 'idle' ? '#8E8E93' : FACE_COLOR[st]}">${esc(a.project || a.title || '')}</span>`);
    }
  });
  $('#fa-ring').innerHTML = out.join('');
}
// Body face: the avatar (frame 0 of its sprite strip, or the drawn one), level and XP, the HP / Energy /
// Stamina rings (no HP without sleep data), the weekly boss and this week's quests.
function bodyFace() {
  const S = faceLife && typeof faceLife === 'object' ? faceLife : null;
  $('.fb-stage').style.setProperty('--k', Math.min(innerWidth / 1280, innerHeight / 720));
  const key = S ? JSON.stringify([S.level, S.title, S.into, S.span, S.sprite, S.avatar, S.bars, S.boss, S.quests, S.today, S.sleep, new Date().getDay()]) : '';
  if (key === bodyKey && key) return;
  bodyKey = key;
  if (!S) { $('#fb-main').innerHTML = '<div class="fb-left"><div class="fb-lvl">LOADING…</div></div>'; return }
  const pct = (a, b) => Math.max(0, Math.min(100, num(b) ? num(a) / num(b) * 100 : 0));
  const sp = S.sprite && typeof S.sprite === 'object' ? S.sprite : null;
  const me = sp ? `<div class="fb-me" role="img" aria-label="Your avatar" style="aspect-ratio:${num(sp.w) || 1}/${num(sp.h) || 1};background-image:url('${esc(String(sp.url).replace(/^file\/life\//, 'file/' + LIFE + '/').replace(/["'\\()]/g, ''))}')"></div>`
    : `<div class="fb-svg">${typeof S.avatar === 'string' ? S.avatar : ''}</div>`;
  const b = S.bars && typeof S.bars === 'object' ? S.bars : {}, t = S.today && typeof S.today === 'object' ? S.today : {};
  const sl = S.sleep && typeof S.sleep === 'object' ? S.sleep : {}, quests = Array.isArray(S.quests) ? S.quests.filter(q => q && typeof q === 'object') : [];
  const last = sl.last && typeof sl.last === 'object' ? num(sl.last.minutes) : 0;
  const C = 2 * Math.PI * 52;
  const rings = [['HP', 'hp', '#FF375F', last ? `Slept ${Math.floor(last / 60)} h ${String(last % 60).padStart(2, '0')}` + (sl.nights_on_time ? ` · on time ${num(sl.nights_on_time)} nights` : '') : 'From your sleep'],
    ['Energy', 'energy', '#FFD60A', `${Math.round(num(t.kcal)).toLocaleString()} kcal · ${Math.round(num(t.protein))} g protein`],
    ['Stamina', 'stamina', '#30D158', quests.filter(q => q.key !== 'protein_days').map(q => String(q.name).split(' ×')[0] + ' ' + num(q.have)).join(' · ') + ' this week']]
    .filter(([, k]) => b[k] !== null && b[k] !== undefined)
    .map(([label, k, color, sub]) => `<div class="fb-ring"><svg width="128" height="128" viewBox="0 0 128 128" role="img" aria-label="${label} ${Math.round(num(b[k]))} percent">
      <circle cx="64" cy="64" r="52" fill="none" stroke="#2C2C2E" stroke-width="14"/>
      <circle cx="64" cy="64" r="52" fill="none" stroke="${color}" stroke-width="14" stroke-linecap="round" stroke-dasharray="${(C * pct(b[k], 100) / 100).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 64 64)"/>
      <text x="64" y="74" text-anchor="middle" font-size="30" font-weight="700" fill="#FFFFFF">${Math.round(num(b[k]))}</text></svg>
      <b style="color:${color}">${label}</b><small>${esc(sub)}</small></div>`).join('');
  const boss = S.boss && typeof S.boss === 'object' ? S.boss : null, left = (7 - new Date().getDay()) % 7;
  const bossHtml = !boss ? '' : `<div class="fb-boss"><header><b>Weekly boss · ${esc(boss.name || '')}</b>
    <span>${boss.defeated ? 'Defeated' : left ? left + (left === 1 ? ' day left' : ' days left') : 'Last day'}</span></header>
    <div class="fb-bar"><i style="width:${pct(boss.hp, boss.max).toFixed(1)}%"></i></div>
    <small>${boss.defeated ? 'Every quest done this week.' : 'Each quest you finish hits it. Finish them all by Sunday to beat it.'}</small></div>`;
  $('#fb-main').innerHTML = `<div class="fb-left"><div class="fb-lvl">${esc(('Level ' + num(S.level) + ' · ' + (S.title || '')).toUpperCase())}</div>${me}
    <div class="fb-bar"><i style="width:${pct(S.into, S.span).toFixed(1)}%"></i></div><div class="fb-sub">${num(S.into)} / ${num(S.span)} XP to level ${num(S.level) + 1}</div></div>
    <div class="fb-right">${rings ? `<div class="fb-rings">${rings}</div>` : ''}${bossHtml}
    <div class="fb-quests">${quests.map(q => `<div class="fb-q${q.done ? ' done' : ''}"><b>${esc(q.name)}</b><small>${q.done ? 'Done' : num(q.have) + ' of ' + num(q.need)}</small></div>`).join('')}</div></div>`;
}
if (KIOSK) {
  setInterval(() => {
    const typing = ['TEXTAREA', 'INPUT'].includes(document.activeElement?.tagName);
    if (faceOn) { if (sheetPath) hideFace(); else faceClock() }
    else if (!sheetPath && !typing && !document.hidden && Date.now() - lastInput >= FACE_IDLE) showFace();
  }, 1000);
  setInterval(() => { if (faceOn) loadFace() }, 10000);
  addEventListener('resize', () => { if (faceOn) drawFace() });
  // A touch on a face only wakes the panel (or, on the date line, changes the face): it and the events that
  // follow it (up to the click) stop here, so nothing under the finger is pressed.
  addEventListener('pointerdown', ev => {
    if (!faceOn) { lastInput = Date.now(); return }
    ev.preventDefault(); ev.stopPropagation(); swallowUntil = Date.now() + 700;
    if (ev.target.closest('.face-date')) cycleFace(1); else hideFace();
  }, true);
  const swallow = ev => { if (faceOn || Date.now() < swallowUntil) { ev.preventDefault(); ev.stopPropagation() } };
  ['pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu', 'touchstart', 'touchend'].forEach(t => addEventListener(t, swallow, {capture: true, passive: false}));
  addEventListener('keydown', () => { lastInput = Date.now(); hideFace() }, true);
}
// Docked to a TV the kiosk shows the game (config "game"), and goes back when undocked. + switches
// by hand; coming back with + (?stay=1) keeps the panel until the next undock.
const GAME = document.body.dataset.game || '';
const session = { get(k) { try { return sessionStorage.getItem(k) } catch (e) { return null } },
                  set(k, v) { try { v === null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v) } catch (e) {} } };
if (new URLSearchParams(location.search).get('stay')) { session.set('stay', 1); history.replaceState(null, '', location.pathname + location.hash) }
if (KIOSK && GAME) setInterval(() => {
  if (screen.width < DOCK_WIDTH) session.set('stay', null);
  else if (!session.get('stay') && !sheetPath && !document.hidden) location.href = GAME + '?kiosk=dock';
}, 2000);
if (KIOSK) {
  document.addEventListener('focusin', ev => { if (ev.target.matches('textarea, input')) { document.body.classList.add('typing'); setTimeout(() => ev.target.scrollIntoView({block: 'center'}), 150) } });
  document.addEventListener('focusout', () => setTimeout(() => { if (!['TEXTAREA', 'INPUT'].includes(document.activeElement?.tagName)) document.body.classList.remove('typing') }, 200));
}

document.addEventListener('click', async ev => {
  const a = ev.target.closest('[data-action]'), s = ev.target.closest('[data-sheet]'), k = ev.target.closest('[data-key]');
  const p = ev.target.closest('[data-push]'), ph = ev.target.closest('[data-photo]'), pg = ev.target.closest('[data-page]');
  if (ph) { photoIn.dataset.post = ph.dataset.photo; photoIn.value = ''; photoIn.click() }
  else if (pg) location.href = '/page/' + pg.dataset.page;
  else if (p) { p.disabled = true; try { await pushOn(p) } catch (err) { toast('Alerts: ' + err.message) } p.disabled = false }
  else if (a) { if (a.dataset.confirm && !confirm(a.dataset.confirm)) return; a.disabled = true; await post(a.dataset.action); a.disabled = false }
  else if (s) openSheet(s.dataset.sheet, s.querySelector('b')?.textContent || '');
  else if (k) {
    if (document.body.dataset.sending) return;  // one answer at a time: no double taps
    document.body.dataset.sending = 1; document.querySelectorAll('[data-key]').forEach(b => b.disabled = true);
    try { await post(k.dataset.post, {key: k.dataset.key}) } finally {
      setTimeout(() => { delete document.body.dataset.sending; document.querySelectorAll('[data-key]').forEach(b => b.disabled = false) }, 3000);
    }
    setTimeout(() => sheetPath && openSheet(sheetPath, $('#sheet-title').textContent), 1200);
  }
});
document.addEventListener('submit', async ev => {
  const f = ev.target.closest('form[data-post]'); if (!f) return; ev.preventDefault();
  const body = Object.fromEntries(new FormData(f)), ta = f.querySelector('textarea');
  if (ta && !ta.value.trim()) return;
  if (await post(f.dataset.post, body)) f.reset();
  setTimeout(() => sheetPath && openSheet(sheetPath, $('#sheet-title').textContent), 1500);
});
// A photo for a data-photo button: shrunk to 1568 px (what the model reads anyway), sent as a JPEG.
const photoIn = $('#photo-in');
async function shrink(file, max = 1568) {
  const img = await new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => bad(new Error("That photo couldn't be read.")); i.src = URL.createObjectURL(file) });
  const k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight)), c = document.createElement('canvas');
  c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(img.src);
  return c.toDataURL('image/jpeg', 0.85);
}
photoIn.onchange = async () => {
  const f = photoIn.files[0]; if (!f) return;
  toast('Sending the photo…');
  try { await post(photoIn.dataset.post, {image: await shrink(f)}) } catch (err) { toast(err.message) }
};
// a phone alert links here as #sheet=<path>&title=<title>: open that sheet, then drop the hash
function openFromHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  const path = h.get('sheet') || '';
  if (/^[\w.-]+\/[\w-]+\/[^/]*$/.test(path) && !path.includes('..')) { openSheet(h.get('sheet'), h.get('title') || ''); history.replaceState(null, '', location.pathname + location.search) }
}
openFromHash(); addEventListener('hashchange', openFromHash);

// Phone alerts (Web Push, modules/notify.py). The service worker shows them and, on a tap,
// hands the alert's link to an open panel or opens one.
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
  navigator.serviceWorker.addEventListener('message', ev => { if (ev.data && ev.data.open) { history.replaceState(null, '', new URL(ev.data.open).hash); openFromHash() } });
}
async function pushOn(btn) {
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window))
    return toast(ios && !navigator.standalone ? 'Open this panel from its Home Screen icon first (Share → Add to Home Screen).' : "This browser can't get alerts.");
  if (await Notification.requestPermission() !== 'granted') return toast('Alerts are blocked. Allow them in Settings → Notifications.');
  const reg = await navigator.serviceWorker.ready;
  const b64 = btn.dataset.vapid.replace(/-/g, '+').replace(/_/g, '/');
  const key = Uint8Array.from(atob(b64 + '='.repeat((4 - b64.length % 4) % 4)), c => c.charCodeAt(0));
  let sub = await reg.pushManager.getSubscription();
  if (sub && btoa(String.fromCharCode(...new Uint8Array(sub.options.applicationServerKey || []))) !== btoa(String.fromCharCode(...key))) { await sub.unsubscribe(); sub = null }
  sub = sub || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
  const ua = navigator.userAgent, label = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Mac/.test(ua) ? 'Mac' : 'Computer';
  await post(btn.dataset.post, {sub: sub.toJSON(), label});
}
</script></body></html>"""

SW = r"""// Service worker: shows phone alerts (modules/notify.py) and opens their link on a tap.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', ev => ev.waitUntil(self.clients.claim()));
self.addEventListener('push', ev => {
  let d = {};
  try { d = ev.data.json() } catch (e) {}
  ev.waitUntil(self.registration.showNotification(d.title || 'Control center',
    {body: d.body || '', tag: d.tag || undefined, icon: 'icon.png', data: {url: d.url || './'}}));
});
self.addEventListener('notificationclick', ev => {
  ev.notification.close();
  const url = new URL(ev.notification.data.url, self.registration.scope).href;
  ev.waitUntil((async () => {
    for (const w of await self.clients.matchAll({type: 'window', includeUncontrolled: true}))
      if (new URL(w.url).origin === new URL(url).origin) { await w.focus(); w.postMessage({open: url}); return }
    await self.clients.openWindow(url);
  })());
});
"""

MANIFEST = {"name": "Control center", "short_name": "Control", "display": "standalone", "start_url": "./",
            "background_color": "#f2f2f4", "theme_color": "#f2f2f4",
            "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}


def load():
    from modules import remote
    cfg = json.load(open(CONFIG_PATH))
    # "desk.agents" is the agents card of the remote panel "desk" (see modules/remote.py)
    return cfg, [remote.Remote(m, cfg) if "." in m else importlib.import_module(f"modules.{m}")
                 for m in cfg["modules"]]


def module(cfg, mods, name):
    """Enabled module by name; any "<remote>.<module>" of a configured remote also resolves,
    so a card can forward to a remote module it doesn't show (e.g. desk.power/sleep)."""
    from modules import remote
    hit = next((m for m in mods if m.NAME == name), None)
    if hit is None and name.split(".", 1)[0] in cfg.get("remotes", {}) and "." in name:
        hit = remote.Remote(name, cfg)
    return hit


def live_payload(cfg, mods):
    """What /live returns: every card, the waiting agents (here and on remotes), kiosk notices and night mode."""
    from modules import night, remote
    from modules.agents import key_of
    pager = [{"key": key_of(a), "sheet": f"agents/view/{a['handle']}", "title": a["title"],
              "project": a["project"], "summary": a["summary"]}
             for a in (STATE.get("agents") or {}).get("items", []) if a["status"] == "waiting"]
    pager += remote.pager(cfg)
    now, notices, ids = time.time(), [], set()
    for n in list(STATE.get("notices") or []) + remote.notices(cfg):
        if n["id"] not in ids and n["until"] > now:
            ids.add(n["id"])
            notices.append(n)
    return {"cards": {m.NAME: render(m, cfg) for m in mods}, "waiting": len(pager), "pager": pager,
            "notices": notices, "quiet": night.quiet(cfg)}


def face_sources(mods):
    """(party module, life module) for the kiosk's ambient faces: the first enabled module, here or on a remote
    ("desk.agenthealth", "pi.life"), whose FILES serve party/now and state/now; "" when there's none."""
    def first(base):
        return next((m.NAME for m in mods if m.NAME.rsplit(".", 1)[-1] == base), "")
    return first("agenthealth"), first("life")


def host_allowed(host, cfg):
    """Refuse DNS rebinding: a page on another site that points its own name at 127.0.0.1 would
    otherwise be same-origin here. Only loopback, the tailnet (*.ts.net) and config "hosts" pass."""
    host = (host or "").lower()
    host = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".ts.net") or host in cfg.get("hosts", [])


def tab_of(m, cfg):
    """A module's tab; config.json "tabs" can move it ({"power": "home"} on the Pi)."""
    return cfg.get("tabs", {}).get(m.NAME) or getattr(m, "TAB", "home")


def render(m, cfg):
    try:
        return m.card(cfg)
    except Exception as ex:  # one broken module shouldn't take the panel down
        traceback.print_exc()
        return f'<p class="err">{html.escape(type(ex).__name__)}: {html.escape(str(ex))}</p>'


def sampler(m):
    while True:
        try:
            m.sample(load()[0])
        except Exception:
            traceback.print_exc()
        time.sleep(getattr(m, "SAMPLE_EVERY", 5))


class Handler(BaseHTTPRequestHandler):
    screen = False

    def gate(self, cfg, parts, body):
        """An error message when this action needs more than the login, else None."""
        return None

    def host_ok(self, cfg):
        """host_allowed() for this request; answers 403 when it fails."""
        if host_allowed(self.headers.get("Host"), cfg):
            return True
        self.send(403, "text/plain", b"Unknown host name.\n")
        return False

    def allowed(self, cfg):
        if not self.host_ok(cfg):
            return False
        if self.headers.get("Tailscale-User-Login") in cfg["allowed_users"] or token_ok(self.headers.get("X-Hub-Token")):
            return True
        self.send(403, "text/plain", b"Not allowed for this Tailscale account.\n")
        return False

    def send(self, code, ctype, body, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, code, payload):
        payload = payload if isinstance(payload, dict) else {"message": payload}
        self.send(code, "application/json", json.dumps(payload).encode())

    def do_GET(self):
        cfg, mods = load()
        if not self.allowed(cfg):
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self.send(200, "text/html", self.page(cfg, mods).encode())
        if path == "/live":
            return self.reply(200, live_payload(cfg, mods))
        if path.startswith("/sheet/"):
            parts = path.split("/")[2:]  # sheet/<module>/<name>/<arg>
            fn = getattr(module(cfg, mods, parts[0]), "SHEETS", {}).get(parts[1]) if len(parts) == 3 else None
            if fn is None:
                return self.send(404, "text/plain", b"Not found\n")
            try:
                return self.send(200, "text/html", fn(cfg, parts[2]).encode())
            except Exception as ex:
                return self.send(200, "text/html", f'<p class="err">{html.escape(str(ex))}</p>'.encode())
        if path.startswith("/file/"):
            parts = path.split("/")[2:]  # file/<module>/<name>/<arg>: images such as camera snapshots
            fn = getattr(module(cfg, mods, parts[0]), "FILES", {}).get(parts[1]) if len(parts) == 3 else None
            if fn is None:
                return self.send(404, "text/plain", b"Not found\n")
            try:
                res = fn(cfg, parts[2])
            except Exception as ex:
                return self.send(502, "text/plain", f"{ex}\n".encode())
            return self.send(200, res[0].split(";")[0] or "application/octet-stream", res[1], cache=res[2:] == (True,))
        if path.startswith("/page/"):
            parts = path.split("/")[2:]  # page/<module>/<name>: a full-screen page such as the game
            fn = getattr(module(cfg, mods, parts[0]), "PAGES", {}).get(parts[1]) if len(parts) == 2 else None
            if fn is None:
                return self.send(404, "text/plain", b"Not found\n")
            try:
                return self.send(200, "text/html", fn(cfg, None).replace("%%KIOSK_JS%%", KIOSK_JS).encode())
            except Exception as ex:
                return self.send(502, "text/plain", f"{ex}\n".encode())
        if path == "/icon.png" and os.path.exists(ICON_PATH):
            return self.send(200, "image/png", open(ICON_PATH, "rb").read(), cache=True)
        if path == "/sw.js":
            return self.send(200, "application/javascript", SW.encode())
        if path == "/manifest.webmanifest":
            return self.send(200, "application/manifest+json", json.dumps(MANIFEST).encode())
        self.send(404, "text/plain", b"Not found\n")

    def do_POST(self):
        cfg, mods = load()
        if not self.allowed(cfg):
            return
        # only our own page may trigger actions, not a form on some other site; a browser without
        # Fetch Metadata is checked by its Origin, and panels calling each other send neither
        site, origin = self.headers.get("Sec-Fetch-Site"), self.headers.get("Origin")
        if site not in (None, "same-origin") or (site is None and origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host")):
            return self.reply(400, "Cross-site request refused.")
        parts = self.path.strip("/").split("/")  # action/<module>/<action>[/<arg>]
        if len(parts) not in (3, 4) or parts[0] != "action":
            return self.reply(404, "Unknown action.")
        mod = module(cfg, mods, parts[1])
        size = int(self.headers.get("Content-Length") or 0)
        if size > getattr(mod, "UPLOADS", {}).get(parts[2], 64 * 1024):
            return self.reply(413, "Too large.")
        try:
            body = json.loads(self.rfile.read(size) or b"{}") if size else {}
        except ValueError:
            return self.reply(400, "Bad JSON.")
        refused = self.gate(cfg, parts, body)
        if refused:
            return self.reply(403, refused)
        body.pop("pin", None)
        fn = getattr(mod, "ACTIONS", {}).get(parts[2])
        if fn is None:
            return self.reply(404, "Unknown action.")
        try:
            self.reply(200, fn(cfg, parts[3] if len(parts) == 4 else None, body))
        except Exception as ex:
            self.reply(500, f"{type(ex).__name__}: {ex}")

    def page(self, cfg, mods):
        panes = []
        tabs = [t for t in TABS if any(tab_of(m, cfg) == t[0] for m in mods)]  # hide empty tabs
        for tab, _, _ in tabs:
            secs = "".join(f'<section><h2>{html.escape(cfg.get("titles", {}).get(m.NAME, m.TITLE))}</h2><div id="m-{m.NAME}">{render(m, cfg)}</div></section>'
                           for m in mods if tab_of(m, cfg) == tab)
            panes.append(f'<div class="pane" data-tab="{tab}">{secs}</div>')
        nav = "".join(f'<button data-tab="{t}"><svg viewBox="0 0 24 24"><path d="{d}"/></svg>{label}'
                      f'{"<span class=badge id=badge-agents hidden></span>" if t == "agents" else ""}</button>'
                      for t, label, d in tabs)
        title = html.escape(cfg.get("title", "Control center"))
        game = html.escape(cfg.get("game", ""), quote=True)
        party, life = (html.escape(n, quote=True) for n in face_sources(mods))
        page = PAGE.replace("</style></head><body>", f'</style></head><body class="kiosk" data-game="{game}" '
                                                      f'data-party="{party}" data-life="{life}">') if self.screen else PAGE
        name = json.dumps(cfg.get("name", "")).replace("<", "\\u003c")
        return (page.replace("%%TITLE%%", title).replace("%%KIOSK_JS%%", KIOSK_JS).replace("%%NAME%%", name).replace("%%HOST%%", html.escape(os.uname().nodename))
                .replace("%%PANES%%", "".join(panes)).replace("%%NAV%%", nav))

    def log_message(self, fmt, *args):
        if "/live" not in (args[0] if args else ""):  # the 3 s poll would flood the journal
            who = self.headers.get("Tailscale-User-Login") or ("hub" if self.headers.get("X-Hub-Token") else "-")
            print(f"{who} {fmt % args}", flush=True)


class ScreenHandler(Handler):
    """The hub's own touchscreen: whoever is in front of it is allowed, but the actions in config
    "screen_pin" ("module/action"; a remote's module counts too) also need the PIN in
    ~/.config/homelab-admin/screen-pin."""
    screen = True
    misses = []  # times of wrong PINs; 5 within 10 minutes lock the screen's changes

    def allowed(self, cfg):
        return self.host_ok(cfg)

    def gate(self, cfg, parts, body):
        if f'{parts[1].rsplit(".", 1)[-1]}/{parts[2]}' not in cfg.get("screen_pin", []):  # "services/restart" covers "desk.services"
            return None
        now = time.time()
        ScreenHandler.misses = [t for t in ScreenHandler.misses if now - t < 600]
        if len(ScreenHandler.misses) >= 5:
            return "Too many wrong PINs. Try again in 10 minutes, or do it from your phone."
        try:
            want = open(PIN_PATH).read().strip()
        except OSError:
            return "Set a screen PIN first (see switch/README.md), or do it from your phone."
        if want and hmac.compare_digest(str(body.get("pin", "")).encode(), want.encode()):
            return None
        ScreenHandler.misses.append(now)
        return "Wrong PIN."

    def log_message(self, fmt, *args):
        if "/live" not in (args[0] if args else ""):
            print(f"screen {fmt % args}", flush=True)


class ShareHandler(BaseHTTPRequestHandler):
    """Friends' listener (SHARE_PORT): GET /share/v1/week only, gated by the friend's share token in
    modules/coop.py. Deliberately not a Handler: no login, no hub token, no modules, no /live, no files.
    Every answer is JSON with Cache-Control: no-store, and the log never shows headers or query strings."""
    timeout = 10  # a client that stalls doesn't hold a thread

    def send_json(self, code, body):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if code == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # malformed requests too: JSON and no-store, never http.server's HTML page
        self.send_json(code, {"error": "Bad request." if code < 500 else "Not available right now."})

    def do_GET(self):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return self.send_json(503, {"error": "Not available right now."})
        if not host_allowed(self.headers.get("Host"), cfg):
            return self.send_json(403, {"error": "Unknown host name."})
        if self.path.split("?", 1)[0] != "/share/v1/week":
            return self.send_json(404, {"error": "Not found."})
        try:
            client = self.headers.get("Tailscale-User-Login") or self.client_address[0]  # coop's per-client limit
            status, body = importlib.import_module("modules.coop").serve(cfg, self.headers.get("Authorization", ""), client)
        except Exception as ex:  # the type only: a message could carry what the friend sent
            print(f"share: {type(ex).__name__}", flush=True)
            return self.send_json(503, {"error": "Not available right now."})
        self.send_json(status, body)

    def refuse(self):
        self.send_json(405, {"error": "Only GET."})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = refuse

    def __getattr__(self, name):  # any other method http.server would answer with 501
        if name.startswith("do_"):
            return self.refuse
        raise AttributeError(name)

    def log_message(self, fmt, *args):
        print("share " + _no_query(fmt % args), flush=True)


def _no_query(line):
    """A log line without query strings (a token pasted into a URL stays out of the journal)."""
    return " ".join(w.split("?", 1)[0] for w in line.split(" "))


if __name__ == "__main__":
    cfg, mods = load()
    for m in mods:
        if hasattr(m, "sample"):
            threading.Thread(target=sampler, args=(m,), daemon=True).start()
    if SCREEN_PORT:
        screen = ThreadingHTTPServer(("127.0.0.1", SCREEN_PORT), ScreenHandler)
        threading.Thread(target=screen.serve_forever, daemon=True).start()
    if SHARE_PORT and "coop" in cfg.get("modules", []):
        share = ThreadingHTTPServer(("127.0.0.1", SHARE_PORT), ShareHandler)
        threading.Thread(target=share.serve_forever, daemon=True).start()
    elif SHARE_PORT:
        print('SHARE_PORT is set but "coop" isn\'t in modules: no share listener.', flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

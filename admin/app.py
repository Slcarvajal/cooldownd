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
PIN_PATH = os.path.expanduser("~/.config/homelab-admin/screen-pin")
ICON_PATH = os.path.join(HERE, "icon.png")
TABS = [("home", "Home", "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"),
        ("agents", "Agents", "M12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8zm-7 17a7 7 0 0 1 14 0"),
        ("machine", "Machine", "M4 5h16v10H4zM9 19h6M12 15v4"),
        ("commands", "Commands", "M5 7l5 5-5 5M12 17h7")]

# Shared by the panel and full-screen module pages (PAGES), where %%KIOSK_JS%% is replaced.
# Button numbers per controller. Joy-Cons on the rails (joycond's combined device) aren't a layout
# Chromium knows, so they were mapped by hand on 2026-10-12. Any other unknown controller sends nothing.
# DOCK_WIDTH: the Switch's own screen is 1280 px wide; docked to a TV it's wider, and the kiosk shows
# the game (config "game").
KIOSK_JS = r"""const PAD = {a: 1, b: 0, up: 12, down: 13, r: 5, zr: 7, plus: 9};
const PADS = {'057e Product: 2008': {a: 1, b: 0, up: 14, down: 15, r: 6, zr: 8, plus: 10}};
function padMap(gp) {
  const known = Object.keys(PADS).find(k => gp.id.includes(k));
  return known ? PADS[known] : gp.mapping === 'standard' ? PAD : null;
}
const DOCK_WIDTH = 1600;"""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Control">
<meta name="theme-color" content="#f6f4ef">
<link rel="apple-touch-icon" href="icon.png"><link rel="manifest" href="manifest.webmanifest">
<title>%%TITLE%%</title><style>
:root{color-scheme:light;--bg:#f6f4ef;--card:#fff;--card2:#f4f2ed;--line:#e8e4db;--fg:#1f2330;--mute:#6b7080;
--violet:#6d4fd8;--cyan:#0284c7;--green:#15803d;--amber:#b45309;--red:#dc2626;--orange:#ea580c;
--shadow:0 1px 2px rgba(31,35,48,.05),0 6px 20px rgba(31,35,48,.06);
--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{overflow-x:hidden;margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,"Ubuntu",sans-serif}
body{padding:calc(env(safe-area-inset-top) + 12px) 16px calc(env(safe-area-inset-bottom) + 84px)}
header{max-width:560px;margin:0 auto 12px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:26px;margin:0;letter-spacing:-.02em;font-weight:700}header h1 small{display:block;font-size:13px;font-weight:500;letter-spacing:0;margin-top:2px}
#live{font:12px var(--mono);color:var(--mute);display:flex;align-items:center;gap:6px}
#live i{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.18)}
#live.stale i{background:var(--red);box-shadow:none}
.pane{display:none;max-width:560px;margin:0 auto;gap:12px;grid-template-columns:minmax(0,1fr)}.pane.on{display:grid}
@media (min-width:900px){header,.pane{max-width:1240px}.pane.on{display:block;columns:2;column-gap:12px}.pane>section{break-inside:avoid;margin-bottom:12px}.pane[data-tab=home].on{columns:1}.pane>section:only-child{column-span:all}.pane .tiles{grid-template-columns:repeat(6,minmax(0,1fr))}.lists{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px;align-items:start}}
section{min-width:0;background:var(--card);border-radius:22px;padding:16px 16px 12px;box-shadow:var(--shadow)}
h2{font-size:15px;letter-spacing:-.01em;color:var(--fg);font-weight:700;margin:0 0 10px}
h3{font-size:13px;color:var(--violet);margin:14px 0 4px;font-weight:600}h2+h3,section>div>h3:first-child{margin-top:2px}
h3.host{color:var(--cyan)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
h2+.row,h3+.row{border-top:0}.row>span:last-child{text-align:right;display:flex;gap:6px;align-items:center;justify-content:flex-end;flex-wrap:wrap}
small,.muted{color:var(--mute)}small{font-size:12px}
b{font-variant-numeric:tabular-nums}
.ok{color:var(--green)}.warn{color:var(--amber)}.bad{color:var(--red)}.dim{color:var(--mute)}.busy{color:var(--cyan)}
.pill{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:99px;white-space:nowrap;background:color-mix(in srgb,currentColor 15%,transparent)}
.pill.ok{color:var(--green)}.pill.bad{color:var(--red)}.pill.warn{color:var(--amber)}.pill.busy{color:var(--cyan)}.pill.dim{color:var(--mute)}
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.tile{background:var(--card2);border-radius:16px;padding:12px 14px;display:grid;gap:2px}
.tile b{font-size:24px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.meter{display:grid;grid-template-columns:56px 52px 1fr;align-items:center;gap:8px;padding:5px 0}
.meter b{font:600 15px var(--mono);text-align:right}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden}
.bar i{display:block;height:100%;border-radius:99px;background:var(--green)}
.bar.warn i{background:var(--amber)}.bar.bad i{background:var(--red)}.bar.dim i{background:var(--mute)}
.sparks{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;align-items:center;margin:6px 0 2px;font-size:12px;color:var(--mute)}
.spark{width:100%;height:26px}.spark polyline{fill:none;stroke-width:1.6;stroke:var(--cyan)}
.spark.violet polyline{stroke:var(--violet)}.spark.green polyline{stroke:var(--green)}.spark.amber polyline{stroke:var(--amber)}
.alert{font:12px/1.4 var(--mono);padding:6px 8px;border-radius:8px;margin:4px 0;background:color-mix(in srgb,currentColor 10%,transparent);word-break:break-word}
.alert.bad{color:var(--red)}.alert.warn{color:var(--amber)}
button{font:inherit;color:inherit;cursor:pointer}
section button:not(.agent):not(.mini):not(.cmd),.sheet form button{width:100%;padding:14px;border:0;border-radius:12px;background:var(--violet);color:#fff;font-weight:700;font-size:16px}
.machine{display:grid;gap:8px}.machine+.machine{margin-top:12px}.machine small{line-height:1.3}
img.cam{display:block;width:100%;border-radius:12px;background:var(--card2);min-height:120px}
a.open{display:block;text-align:center;padding:11px;border:1px solid var(--line);border-radius:12px;color:var(--violet);text-decoration:none;font-weight:600}
button.mini{border:1px solid var(--line);background:var(--card2);border-radius:8px;padding:3px 9px;font-size:12px}
button.agent{display:grid;grid-template-columns:10px 1fr auto;gap:10px;align-items:center;width:100%;text-align:left;
background:var(--card2);border:0;border-radius:12px;padding:10px 12px;margin:6px 0}
.agent .who{display:grid;min-width:0}.agent .who b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent .who small{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.agent .when{display:grid;justify-items:end;gap:3px}
.dot{width:10px;height:10px;border-radius:50%;background:currentColor}.dot.busy{animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.35}}
.counts{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px}
.project{display:flex;justify-content:space-between;gap:12px;font-weight:600;margin:12px 0 0;font-size:14px}
.project small{font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
button.cmd{display:grid;width:100%;text-align:left;background:var(--card2);border:0;border-radius:12px;padding:12px;margin:6px 0;gap:2px}
.report pre,.screen,.output{font:11px/1.35 var(--mono);white-space:pre-wrap;word-break:break-word;margin:4px 0 8px;color:var(--fg)}
.report pre{background:var(--card2);padding:8px;border-radius:8px}
details summary{color:var(--mute);padding:6px 0}
.err{color:var(--red);font:12px var(--mono)}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;background:color-mix(in srgb,var(--bg) 82%,transparent);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-top:1px solid var(--line);padding:6px 4px calc(env(safe-area-inset-bottom) + 6px);z-index:5}
nav button{background:none;border:0;display:grid;justify-items:center;gap:2px;font-size:11px;color:var(--mute);padding:4px 10px;position:relative}
nav button.on{color:var(--violet)}nav svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
nav .badge{position:absolute;top:0;right:6px;background:var(--red);color:#fff;font:700 10px var(--mono);border-radius:99px;padding:1px 5px}
#shade{position:fixed;inset:0;background:rgba(31,35,48,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:9}
#shade.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;left:0;right:0;bottom:0;max-height:88vh;background:var(--card);border-radius:20px 20px 0 0;z-index:10;
transform:translateY(100%);transition:transform .25s,visibility .25s;visibility:hidden;display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom)}
.sheet.on{transform:none;visibility:visible}
.sheet header{margin:0;padding:12px 14px;border-bottom:1px solid var(--line);gap:8px}
.sheet header b{font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sheet header button{background:var(--card2);border:0;border-radius:99px;padding:6px 12px;font-size:13px}
#padhelp{color:var(--violet);font-weight:600;white-space:nowrap}
.quick{display:grid;grid-template-columns:repeat(3,1fr) repeat(3,64px);gap:8px;margin:4px 0 10px}.quick button{border:2px solid var(--line);background:var(--card2);border-radius:14px;padding:14px 6px;font:700 16px inherit}.quick button:disabled,.keys button:disabled{opacity:.45}.quick button.sel{border-color:var(--violet);background:#efe9ff;box-shadow:0 0 0 3px rgba(109,79,216,.25)}
#sheet-body{overflow:auto;padding:10px 14px 14px;-webkit-overflow-scrolling:touch}
.sheet-meta{font-size:13px;margin-bottom:6px}
.screen{background:#16181f;color:#e6e8ef;border-radius:12px;padding:10px;max-height:46vh;overflow:auto}
.sheet form{display:grid;gap:8px;margin-top:8px}
textarea{width:100%;font:16px/1.35 -apple-system,system-ui,sans-serif;color:var(--fg);background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:10px;resize:vertical}
.keys{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.keys button{flex:1;min-width:52px;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:9px 0;font:600 13px var(--mono)}
#toast{position:fixed;left:16px;right:16px;bottom:calc(env(safe-area-inset-bottom) + 76px);max-width:528px;margin:0 auto;background:var(--fg);color:var(--bg);
padding:12px 14px;border-radius:12px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:20;font-weight:600}
#toast.show{opacity:1}
.headline{font-size:19px;font-weight:650;line-height:1.35;margin:0 0 10px;letter-spacing:-.01em}
.advice{background:#f3efff;color:#3f2f8f;border-radius:14px;padding:10px 12px;margin-bottom:6px;font-size:14px}
.today{display:grid;gap:4px 18px}
@media (min-width:900px){.today{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) minmax(0,1fr)}}
ol.next{margin:0;padding:0;list-style:none;counter-reset:n}
ol.next li{counter-increment:n;display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:start;background:var(--card2);border-radius:14px;padding:10px 12px;margin:6px 0}
ol.next li::before{content:counter(n);width:26px;height:26px;border-radius:50%;background:var(--violet);color:#fff;font-weight:700;font-size:13px;display:grid;place-items:center}
ol.next li b{display:block;font-weight:600;line-height:1.3}ol.next li small{display:block;margin-top:2px}
ol.next li>span{white-space:nowrap;display:flex;gap:6px;align-items:center}
.meet{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:8px 0;border-top:1px solid var(--line)}
h3+.meet{border-top:0}.meet small.prep{color:var(--violet);margin-top:2px}.meet b{color:var(--violet);font-variant-numeric:tabular-nums}.meet small{display:block}
.task{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--line)}
h3+.task{border-top:0}.task small{display:block}.task>small{white-space:nowrap}.task.late>small{color:var(--red);font-weight:600}
h4{font-size:12px;color:var(--mute);margin:10px 0 2px;font-weight:600;text-transform:uppercase;letter-spacing:.06em}
button.ask{display:grid;gap:2px;width:100%;text-align:left;margin:4px 0 8px;padding:14px 16px;border:0;border-radius:16px;background:var(--violet);color:#fff}
button.ask b{font-size:16px}button.ask small{color:#e6defc}
.chat{display:grid;gap:10px;margin-bottom:10px}.msg{display:grid;gap:6px}
.msg .q{justify-self:end;max-width:85%;background:var(--violet);color:#fff;border-radius:16px 16px 4px 16px;padding:8px 12px}
.msg .a{justify-self:start;max-width:92%;background:var(--card2);border-radius:16px 16px 16px 4px;padding:8px 12px;display:grid;gap:6px;justify-items:start}
.proposal{background:#fff7e6;border:1px solid #f5d9a6;border-radius:12px;padding:8px 10px;display:grid;gap:6px;width:100%}
form.confirm{display:flex;gap:6px;align-items:center;margin:0}form.confirm button:not(.mini){width:auto;padding:8px 16px}
.pin{display:none;width:90px;font:16px var(--mono);padding:7px 8px;border:1px solid var(--line);border-radius:10px}
body.kiosk .pin{display:block}
body.kiosk.typing .sheet{top:0;bottom:auto;max-height:50vh;border-radius:0 0 20px 20px}
.foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:10px}
.life-top{display:grid;grid-template-columns:64px minmax(0,1fr);gap:12px;align-items:center;margin-bottom:6px}
.life-top .av{width:64px;height:64px;background:var(--card2);border-radius:14px;overflow:hidden;display:block;position:relative}
.life-top .av img{position:absolute;left:0;bottom:2px;height:60px;width:auto!important;max-width:none;animation:breathe 2.6s ease-in-out infinite}
@keyframes breathe{50%{transform:translateY(-2px)}}.life-top b{display:block;margin-bottom:4px}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0 4px}
section .duo button:not(.agent):not(.mini):not(.cmd){padding:12px 6px;font-size:15px}
section .duo button.alt:not(.agent):not(.mini):not(.cmd){background:var(--card2);color:var(--fg)}.duo button b{font-weight:700}
.field{display:grid;grid-template-columns:minmax(0,1fr) 150px;align-items:center;gap:8px;font-size:14px}
.field input,.field select{font:16px -apple-system,system-ui,sans-serif;color:var(--fg);background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px;width:100%;min-width:0}
#photo-in{position:fixed;left:-9999px;opacity:0}
</style></head><body>
<header><h1 id="hello">%%TITLE%%</h1><span id="live"><i></i><span id="live-t">%%HOST%%</span></span></header>
%%PANES%%
<nav>%%NAV%%</nav>
<div id="shade"></div>
<div class="sheet" id="sheet"><header><b id="sheet-title"></b><small id="padhelp" hidden>Joy-Con: ↑↓ choose · A press · B PC work · R next · + close</small><span><button id="sheet-refresh">Refresh</button> <button id="sheet-close">Close</button></span></header><div id="sheet-body"></div></div>
<div id="toast"></div>
<input type="file" id="photo-in" accept="image/*">
<script>
const $ = s => document.querySelector(s);
const store = { get(k) { try { return localStorage.getItem(k) } catch (e) { return null } },
                set(k, v) { try { localStorage.setItem(k, v) } catch (e) {} } };
let sheetPath = null, lastOk = Date.now();

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
    pager(d.pager || []);
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
  sheetPath = path; $('#sheet-title').textContent = title || '';
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
const KIOSK = document.body.classList.contains('kiosk');
let waitingNow = [], pagedKeys = new Set();
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
// an agent's sheet, here or on a remote panel ("desk.agents/view/<handle>")
const isAgentView = p => /^([\w-]+\.)*agents\/view\//.test(String(p));
function openAgent(a) { padSel = 0; openSheet(a.sheet, '🔔 ' + a.title + ' · ' + a.project) }
function pager(list) {
  waitingNow = list;
  if (!KIOSK) return;
  const fresh = list.find(a => !pagedKeys.has(a.key));
  pagedKeys = new Set(list.map(a => a.key));  // an agent that stops and later waits again pages again
  if (!fresh) return;
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
      if (down && !padPrev[name]) padPress(name);
      padPrev[name] = down;
    }
  } else if (gp) {  // a layout we don't know: send nothing, just show and log which button it is
    gp.buttons.forEach((b, i) => {
      if (b.pressed && !padPrev['raw' + i]) { toast('Joy-Con button ' + i); fetch('pad-debug?id=' + encodeURIComponent(gp.id) + '&i=' + i).catch(() => {}) }
      padPrev['raw' + i] = b.pressed;
    });
  }
}
if (KIOSK) setInterval(padLoop, 40);
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
            "background_color": "#f6f4ef", "theme_color": "#f6f4ef",
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
        """Refuse DNS rebinding: a page on another site that points its own name at 127.0.0.1 would
        otherwise be same-origin here. Only loopback, the tailnet (*.ts.net) and config "hosts" pass."""
        host = (self.headers.get("Host") or "").lower()
        host = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost", "::1") or host.endswith(".ts.net") or host in cfg.get("hosts", []):
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
            from modules import remote
            from modules.agents import key_of
            pager = [{"key": key_of(a), "sheet": f"agents/view/{a['handle']}", "title": a["title"],
                      "project": a["project"], "summary": a["summary"]}
                     for a in (STATE.get("agents") or {}).get("items", []) if a["status"] == "waiting"]
            pager += remote.pager(cfg)
            return self.reply(200, {"cards": {m.NAME: render(m, cfg) for m in mods}, "waiting": len(pager), "pager": pager})
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
        page = PAGE.replace("</style></head><body>", f'</style></head><body class="kiosk" data-game="{game}">') if self.screen else PAGE
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
        if f'{parts[1].rsplit(".", 1)[-1]}/{parts[2]}' not in cfg.get("screen_pin", []):  # "today/confirm" covers "desk.today"
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


if __name__ == "__main__":
    cfg, mods = load()
    for m in mods:
        if hasattr(m, "sample"):
            threading.Thread(target=sampler, args=(m,), daemon=True).start()
    if SCREEN_PORT:
        screen = ThreadingHTTPServer(("127.0.0.1", SCREEN_PORT), ScreenHandler)
        threading.Thread(target=screen.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

"""Home Assistant: alarm, lights, cameras (live view), switches and sirens.

Talks to Home Assistant's REST API with a long-lived token kept outside the repo
(config "ha.token_file", mode 600). "ha.exclude" hides entity ids.
"""
import json
import os
import time
import urllib.error
import urllib.request

from modules.common import e, pill

NAME, TITLE, TAB = "ha", "Home", "home"
GROUPS = [("alarm_control_panel", "Alarm"), ("light", "Lights"), ("camera", "Cameras"),
          ("siren", "Sirens"), ("switch", "Switches")]
ALARM_OPS = {"arm_away": "Arm (away)", "arm_home": "Arm (home)", "arm_night": "Arm (night)", "disarm": "Disarm"}
ALARM_LEVEL = {"disarmed": "dim", "triggered": "bad", "pending": "warn", "arming": "warn"}  # armed_* -> ok
DOWN = ("unavailable", "unknown")


def _conf(cfg):
    return cfg.get("ha", {})


def _token(cfg):
    path = os.path.expanduser(_conf(cfg).get("token_file", "~/.config/homelab/ha-token"))
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _api(cfg, path, body=None, timeout=5):
    token = _token(cfg)
    if not token:
        raise RuntimeError("Home Assistant isn't connected yet (no token).")
    url = _conf(cfg).get("url", "http://127.0.0.1:8123").rstrip("/") + "/api/" + path
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def _states(cfg):
    raw, _ = _api(cfg, "states")
    skip = set(_conf(cfg).get("exclude", []))
    return [s for s in json.loads(raw) if s["entity_id"] not in skip]


def _name(s):
    return s["attributes"].get("friendly_name") or s["entity_id"]


def _row(label, right):
    return f'<div class="row"><span>{label}</span><span>{right}</span></div>'


def _entity(s):
    eid, state, dom = s["entity_id"], s["state"], s["entity_id"].split(".")[0]
    name = e(_name(s))
    if state in DOWN:
        return _row(name, pill("dim", "offline"))
    if dom == "alarm_control_panel":
        lvl = ALARM_LEVEL.get(state, "ok")
        ops = ("disarm",) if state != "disarmed" else ("arm_away", "arm_home")
        btns = "".join(f'<button class="mini" data-action="ha/{op}/{e(eid)}">{ALARM_OPS[op]}</button>' for op in ops)
        return _row(f"🚨 {name}", f"{btns}{pill(lvl, state.replace('_', ' '))}")
    if dom == "camera":
        return _row(f"📷 {name}", f'<button class="mini" data-sheet="ha/camera/{e(eid)}"><b hidden>{name}</b>Live ▶</button>')
    on = state == "on"
    icon = {"light": "💡", "siren": "📢"}.get(dom, "🔌")
    btn = f'<button class="mini" data-action="ha/toggle/{e(eid)}">{"Turn off" if on else "Turn on"}</button>'
    return _row(f"{icon} {name}", f'{btn}{pill(on, "on" if on else "off")}')


def card(cfg):
    if not _token(cfg):
        return ('<p class="muted">Home Assistant isn\'t connected yet. In Home Assistant: your profile → '
                'Security → Long-lived access tokens → Create token, then save it on the Pi as '
                '<code>~/.config/homelab/ha-token</code>.</p>')
    try:
        states = _states(cfg)
    except (OSError, urllib.error.URLError, ValueError) as ex:
        return f'<p class="bad">Home Assistant unreachable: {e(ex)}</p>'
    out = []
    for dom, title in GROUPS:
        items = sorted((s for s in states if s["entity_id"].startswith(dom + ".")), key=_name)
        if items:
            out.append(f"<h3>{title}</h3>" + "".join(_entity(s) for s in items))
    return "".join(out) or '<p class="muted">No lights, cameras or alarm in Home Assistant yet.</p>'


def _call(service, verb):
    def act(cfg, eid, body):
        dom = eid.split(".")[0]
        if dom not in {d for d, _ in GROUPS}:
            raise ValueError(f"{eid} isn't controllable from here")
        _api(cfg, f"services/{dom}/{service}", {"entity_id": eid}, timeout=15)
        print(f"ha.{service} {eid}", flush=True)
        return f"{verb} {eid.split('.', 1)[1].replace('_', ' ')}."
    return act


def camera_sheet(cfg, eid):
    # data-live: the page swaps in a fresh snapshot every second while the sheet is open
    return (f'<img class="cam" data-live src="file/ha/camera/{e(eid)}?t={time.time():.0f}" alt="">'
            '<small>Live snapshots, about one per second.</small>')


def camera_file(cfg, eid):
    if not eid.startswith("camera."):
        raise ValueError("not a camera")
    return _api(cfg, f"camera_proxy/{eid}", timeout=10)[::-1]  # (content type, bytes)


ACTIONS = {"toggle": _call("toggle", "Toggled"), **{op: _call(f"alarm_{op}", label) for op, label in ALARM_OPS.items()}}
SHEETS = {"camera": camera_sheet}
FILES = {"camera": camera_file}

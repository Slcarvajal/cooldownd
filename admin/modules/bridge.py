"""Desk bridge (on the Pi): events from the desktop's outbox become phone alerts and Life XP.

The Pi pulls; the desktop never writes here. Every SAMPLE_EVERY seconds it reads the desktop's outbox
(modules/agentlog.py, served as file/agenthealth/outbox/<cursor>) with remote.py's token and transport,
up to PAGES pages a run, and the party (file/agenthealth/party/now) into STATE["party"] for the game.

- The cursor, the outbox epoch and the alerts held for the morning live in
  ~/.local/share/homelab-admin/bridge.json (600). A new epoch (the desktop's database was recreated)
  starts the cursor over. If the desktop sleeps, its outbox keeps 30 days and the Pi catches up.
- answer, break, presence, sit and nudge go to life.record(); its (src, ref) key dedupes them.
- finished, stuck, limit, sit and nudge become alerts, only while fresh (config "bridge.fresh_s"), so
  catching up never sends old ones. At night (modules/night.py) finished/stuck/limit are held; after wake
  they're handed to notify.overnight(), which sends them in its one morning summary together with the
  agents that waited overnight. sit/nudge are dropped. Alerts show on a locked phone, so their text is cut short.

config "bridge": {"remote": "desk", "fresh_s": 600}"""
import json
import os
import time

from modules import night, notify, remote
from modules.common import STATE, ago, e, row

NAME, TITLE, TAB = "bridge", "Desk bridge", "agents"
SAMPLE_EVERY = 20
PAGES = 5              # outbox pages per run
PATH = os.path.expanduser("~/.local/share/homelab-admin/bridge.json")
RECORD = ("answer", "break", "presence", "sit", "nudge")
PUSH = ("finished", "stuck", "limit", "sit", "nudge")
HOLD = ("finished", "stuck", "limit")  # at night these wait for the morning summary; the rest is dropped
MAX_HELD = 50
CUT = 120              # characters of a recap or a screen line in an alert
PARTY_KEYS = ("key", "title", "project", "agent", "status")
_log = {"pull": None, "error": None, "events": 0, "pushed": 0}


def _conf(cfg):
    return cfg.get("bridge") or {}


def _load():
    try:
        with open(PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    held = d.get("held") if isinstance(d.get("held"), list) else []
    cursor = d.get("cursor")
    return {"epoch": d.get("epoch") if isinstance(d.get("epoch"), str) else None,
            "cursor": cursor if isinstance(cursor, int) and cursor >= 0 else 0,
            "held": [h for h in held if isinstance(h, dict)][-MAX_HELD:]}


def _save(st):
    os.makedirs(os.path.dirname(PATH), mode=0o700, exist_ok=True)
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # a .tmp left by a crash keeps its old mode otherwise
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, PATH)


def _cut(s, n=CUT):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _record(src, ref, kind, ts, data):
    from modules import life  # life imports notify; loaded late so the bridge works without it
    return life.record(src, ref, kind, ts, data)


def _push(cfg, title, body, url=None, tag=None, urgency="high"):
    return notify.send(cfg, title, body, url=url, tag=tag, urgency=urgency)


def _alert(cfg, kind, d):
    """(title, body, url, tag, urgency) of an event's alert."""
    who, project = _cut(d.get("title") or "An agent", 60), _cut(d.get("project"), 60)
    agent = notify.link(cfg, f"desk.agents/view/{d.get('handle', '')}", f"{who} · {project}".strip(" ·"))
    if kind == "finished":
        sheet = f"desk.agenthealth/ready/{d.get('task', '')}"
        return (f"{who} finished", f"{project}: {_cut(d.get('recap'))}".strip(": "),
                notify.link(cfg, sheet, f"Ready to review · {who}"), sheet, "high")
    if kind == "stuck":
        why = "the same error keeps coming back" if d.get("why") == "repeating" else "its screen hasn't changed"
        return f"{who} looks stuck", _cut(f"{project}: {why}".strip(": ")), agent, f"stuck:{d.get('handle', '')}", "high"
    if kind == "limit":
        return f"{who} hit a usage limit", _cut(d.get("line")) or project, agent, f"limit:{d.get('handle', '')}", "high"
    if kind == "sit":
        return "Time to stand up", f"You've been at the desk {_int(d.get('active_min'))} min.", None, "sit", "normal"
    return "Agents are busy: take a break", "Every agent is working. A good moment to move.", None, "nudge", "normal"


def _summary(cfg, held):
    """What was held overnight, in one line for the morning summary: counts by kind and who."""
    counts = {}
    for h in held:
        kind = str(h.get("kind", "update"))
        counts[kind] = counts.get(kind, 0) + 1
    words = {"finished": "finished", "stuck": "stuck", "limit": "hit a limit"}
    parts = [f"{n} {words.get(k, k)}" for k, n in counts.items()]
    names = ", ".join(dict.fromkeys(h.get("who", "") for h in held if h.get("who")))
    return _cut(" · ".join(parts) + (f" ({names})" if names else ""))


def holding(cfg):
    """True while bridge.json still holds updates for the morning: notify waits for them, so a wake makes one push."""
    return bool(_conf(cfg)) and bool(_load()["held"])


def _wake(cfg, st, now):
    """After wake, hand the held alerts to notify, which owns the one morning summary. Handed over before the
    save, so holding() stays True until notify has them. Handed once: a failed send isn't retried."""
    if st["held"] and not night.quiet(cfg, now):
        notify.overnight(len(st["held"]), _summary(cfg, st["held"]))
        st["held"] = []
        _save(st)


def _valid(ev):
    return (isinstance(ev, dict) and isinstance(ev.get("id"), int) and isinstance(ev.get("kind"), str)
            and isinstance(ev.get("ts"), (int, float)) and isinstance(ev.get("data"), dict))


def dispatch(cfg, events, now, st):
    """Record and alert each event in order, moving st["cursor"] past every one handled. A failing
    life.record stops the run there, so that event is tried again on the next pull."""
    fresh_s = _conf(cfg).get("fresh_s", 600)
    quiet = night.quiet(cfg, now)
    src = f"desk:{st['epoch']}"
    for ev in events:
        if not _valid(ev) or ev["id"] <= st["cursor"]:
            continue
        kind, data, ts = ev["kind"], ev["data"], float(ev["ts"])
        if kind in RECORD:
            _record(src, str(ev["id"]), kind, ts, data)  # raises: stop here, retried next pull
        if kind in PUSH and now - ts <= fresh_s:
            if quiet:
                if kind in HOLD:
                    st["held"] = (st["held"] + [{"kind": kind, "ts": ts, "who": _cut(data.get("title"), 40)}])[-MAX_HELD:]
            else:
                try:  # an odd event loses its alert, never the cursor
                    title, body, url, tag, urgency = _alert(cfg, kind, data)
                    if _push(cfg, title, body, url=url, tag=tag, urgency=urgency):
                        _log["pushed"] += 1
                except Exception as ex:
                    _log["error"] = f"{time.strftime('%H:%M')} alert for event {ev['id']}: {type(ex).__name__}: {ex}"
        st["cursor"] = ev["id"]
        _log["events"] += 1


def _int(v):
    return max(0, min(10**6, v)) if isinstance(v, int) and not isinstance(v, bool) else 0


def _party(cfg, rid):
    d = json.loads(remote._fetch(cfg, rid, "file/agenthealth/party/now")[1])
    members = []
    for m in (d.get("members") if isinstance(d, dict) else None) or []:
        if isinstance(m, dict):
            members.append({**{k: _cut(m.get(k) if isinstance(m.get(k), str) else "", 80) for k in PARTY_KEYS},
                            "level": _int(m.get("level")), "done": _int(m.get("done")),
                            "forgotten": bool(m.get("forgotten"))})
    STATE["party"] = {"at": time.time(), "members": members[:12]}


def sample(cfg):
    c = _conf(cfg)
    if not c:
        return
    now, st = time.time(), _load()
    _wake(cfg, st, now)
    rid = c.get("remote", "desk")
    if rid not in cfg.get("remotes", {}):
        _log["error"] = f'No remote "{rid}" in config "remotes".'
        return
    try:
        for _ in range(PAGES):
            d = json.loads(remote._fetch(cfg, rid, f"file/agenthealth/outbox/{st['cursor']}")[1])
            epoch = d.get("epoch")
            if not isinstance(epoch, str) or not epoch:
                raise ValueError("the outbox has no epoch")
            if epoch != st["epoch"]:  # a new database on the desktop: its ids start over
                if st["epoch"] is not None:
                    print(f"bridge: new outbox epoch {epoch}, cursor reset", flush=True)
                st.update(epoch=epoch, cursor=0)
                _save(st)
                continue
            events = d.get("events") if isinstance(d.get("events"), list) else []
            before = st["cursor"]
            try:
                dispatch(cfg, events, now, st)
            finally:
                _save(st)
            if not events or st["cursor"] == before:
                break
        _party(cfg, rid)
        _log.update(pull=now, error=None)
    except Exception as ex:  # desk asleep, a bad page or life.record failing: the cursor stays put
        _log["error"] = f"{time.strftime('%H:%M')} {type(ex).__name__}: {ex}"


def card(cfg):
    if not _conf(cfg):
        return '<p class="muted">Add config "bridge" to read the desktop\'s agent events.</p>'
    st = _load()
    last = f"{ago(time.time() - _log['pull'])} ago" if _log["pull"] else "not yet"
    party = (STATE.get("party") or {}).get("members")
    out = [row("Last pull", e(last)),
           row("Cursor", e(f"{st['cursor']} · epoch {(st['epoch'] or '—')[:8]}")),
           row("Since start", e(f"{_log['events']} events · {_log['pushed']} alerts")),
           row("Party", e(f"{len(party)} on the desk" if party is not None else "—"))]
    if st["held"]:
        out.append(row("Held for the morning", e(len(st["held"]))))
    if night.quiet(cfg):
        out.append(row("Night", '<span class="muted">alerts wait until wake</span>'))
    if _log["error"]:
        out.append(f'<div class="alert bad">{e(_log["error"][:300])}</div>')
    return "".join(out)

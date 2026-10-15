"""Agent health on the desk: time agents spent waiting on you, finished work to review, stuck agents,
usage limits, forgotten terminals and today's tokens.

agents.sample() hands every sample to observe(); the pure Watch below turns it into events, and the glue
stores them in the agent log (modules/agentlog.py), whose outbox the Pi pulls (modules/bridge.py):
- Blocked: one `seen` row per status change, plus "gone" (the terminal closed) and "offline" (no sample for
  over a minute, e.g. the desk slept). Minutes waiting on you are computed on read, cut at midnight.
- Stuck (status "stuck", event "stuck" once per episode): while working, either the screen hasn't changed for
  stuck_min minutes (spinner line dropped, digits ignored; waiting for background agents is exempt), or the
  same error line showed up anew in `repeat` screens within 30 min. An unchanged episode ends when the screen
  changes, a repeating one when that error line leaves the screen; both end when the agent stops working.
- Finished (event "finished", a row in `task`): working for 60 s or more, then idle for finish_after_s.
  The recap on screen at that moment is kept, trimmed to 220 characters. "Ready to review" until dismissed.
- Limit (event "limit", once per episode): a Claude Code usage-limit banner in the last 6 lines of an agent
  that isn't working. A diff that merely mentions a rate limit doesn't count.
- Forgotten: idle for more than forgotten_days.
- answered(): a reply sent from the panel emits "answer"; away when this desk's keyboard has been idle for
  away_idle_s or more (modules/idle.py), so answering from the phone or the Switch counts as away.

Tokens come from this desktop's transcripts only (modules/tokens.py).

config "agenthealth" (every key optional): stuck_min, repeat, finish_after_s, forgotten_days, away_idle_s,
tokens (false skips reading transcripts).

FILES outbox/<since> (the Pi's pull) and party/now (running agents with a level per project and agent,
1 + int(sqrt(tasks done))). SHEETS report/<YYYY-MM-DD> and ready/<task>. ACTIONS dismiss/<task|all>."""
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from modules import agentlog, agents, idle, tokens
from modules.common import STATE, ago, e, pill, row

NAME, TITLE, TAB = "agenthealth", "Agent health", "agents"
SAMPLE_EVERY = 300  # tokens and outbox pruning; status changes arrive with every agents sample
DEFAULTS = {"stuck_min": 10, "repeat": 3, "finish_after_s": 45, "forgotten_days": 2, "away_idle_s": 120, "tokens": True}
RECAP = 220           # longest recap or screen line in an event
REPEAT_WINDOW = 1800  # an error line has to repeat within this many seconds
WORKED_S = 60         # a finished task needs at least this much work before going idle
OFFLINE_S = 60        # no sample for longer than this: the agents' state in between is unknown
OFFLINE = "*"         # the handle of an "offline" row in `seen`; it closes every open interval
BLOCKED = ("waiting", "atpc")
WORKING = ("working", "stuck")
ANSWERS_IGNORED = ("esc", "ctrl-c", "up", "down", "defer")  # stopping or moving isn't answering
ERROR = re.compile(r"Traceback|Error|FAILED|FAIL:|✗|AssertionError")
# Claude Code's own banners only. Each line is checked on its own, and diff or code lines are skipped.
LIMIT = re.compile(r"usage limit reached|you['’]ve (hit|reached) your .*limit|limit (will )?reset|rate[- ]limited|"
                   r"API Error: 429", re.I)
DIFF = re.compile(r"^\s*([+\-]|\d+)\s")  # "+ added", "- removed", "42  numbered code"


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("agenthealth") or {})}


def _trim(s, n=RECAP):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _error_key(line):
    s = line.strip().lstrip("⎿●✻✽✶✢·*│> ").strip()
    return re.sub(r"\s+", " ", re.sub(r"\d+", "0", re.sub(r"0x[0-9a-fA-F]+", "0x", s)))[:200]


def _quiet(body):
    """The screen without Claude Code's spinner line, the part that ticks while nothing else moves."""
    return [l for l in body if not (agents.SPINNER.match(l) or "esc to interrupt" in l.lower())]


# --- The watch: pure, no database and no clock ---

class Watch:
    """Remembers each agent between samples. observe() may set an item's status to "stuck" and always sets
    its "forgotten" flag; it returns events as (kind, data): "seen" for a status change (or "gone"),
    "finished", "stuck" and "limit"."""

    def __init__(self, c=None):
        self.c = {**DEFAULTS, **(c or {})}
        self.agents = {}

    def observe(self, items, bodies, now):
        events, live = [], set()
        for a in items:
            h = a["handle"]
            live.add(h)
            m = self.agents.setdefault(h, {"status": None, "hash": None, "since": now, "stuck": None, "line": "",
                                           "hits": {}, "counts": {}, "work": None, "idle": None, "limit": False})
            m["info"] = {"handle": h, "title": a.get("title", ""), "project": a.get("project", ""),
                         "agent": a.get("agent", "")}
            body, raw = bodies.get(h) or [], a["status"]
            events += self._stuck(m, a, body, raw, now)
            events += self._finished(m, a, raw, now)
            events += self._limit(m, body, raw)
            a["forgotten"] = raw == "idle" and a.get("age", 0) > self.c["forgotten_days"] * 86400
            if a["status"] != m["status"]:
                m["status"] = a["status"]
                events.append(("seen", {**m["info"], "status": a["status"]}))
        for h in [h for h in self.agents if h not in live]:
            events.append(("seen", {**self.agents.pop(h)["info"], "status": "gone"}))
        return events

    def _stuck(self, m, a, body, raw, now):
        c = self.c
        if raw != "working":
            m.update(hash=None, stuck=None, hits={}, counts={})
            return []
        text = _quiet(body)
        digest = hashlib.sha1(re.sub(r"\d+", "0", "\n".join(l.rstrip() for l in text)).encode()).hexdigest()
        if digest != m["hash"]:
            m["hash"], m["since"] = digest, now
            if m["stuck"] == "unchanged":
                m["stuck"] = None
        # An error line that's new on this screen (more copies than the last one had) is one appearance.
        counts = {}
        for l in text:
            if ERROR.search(l):
                k = _error_key(l)
                counts[k] = counts.get(k, 0) + 1
        for k, n in counts.items():
            if n > m["counts"].get(k, 0):
                m["hits"].setdefault(k, []).append(now)
        m["counts"] = counts
        m["hits"] = {k: kept for k, ts in m["hits"].items() if (kept := [t for t in ts if t >= now - REPEAT_WINDOW])}
        if m["stuck"] == "repeating" and m["line"] not in counts:
            m["hits"].pop(m["line"], None)  # the error left the screen: a new episode needs fresh repeats
            m["stuck"] = None
        events = []
        if m["stuck"] is None:
            tail = "\n".join(body[-20:])
            repeating = next((k for k, ts in m["hits"].items() if len(ts) >= c["repeat"]), None)
            if not agents.BACKGROUND.search(tail) and now - m["since"] >= c["stuck_min"] * 60:
                last = next((l for l in reversed(text) if agents._meaningful(l)), "")
                m["stuck"], m["line"] = "unchanged", last.strip()
            elif repeating:
                m["stuck"], m["line"] = "repeating", repeating
            if m["stuck"]:
                i = m["info"]
                events.append(("stuck", {"handle": i["handle"], "title": i["title"], "project": i["project"],
                                         "why": m["stuck"], "line": _trim(m["line"])}))
        if m["stuck"]:
            a["status"] = "stuck"
        return events

    def _finished(self, m, a, raw, now):
        if raw == "working":
            if m["work"] is None:
                m["work"] = now
            m["idle"] = None  # back to work before the idle settled: the same run goes on
            return []
        if raw != "idle" or m["work"] is None:
            m["work"] = m["idle"] = None  # a question or no run seen: nothing finished
            return []
        if m["idle"] is None:
            m["idle"] = now
        if now - m["idle"] < self.c["finish_after_s"]:
            return []
        worked = m["idle"] - m["work"]
        m["work"] = m["idle"] = None
        if worked < WORKED_S:
            return []
        return [("finished", {**m["info"], "recap": _trim(a.get("summary"))})]

    def _limit(self, m, body, raw):
        hit = None
        if raw != "working":
            hit = next((l.strip() for l in reversed(body[-6:]) if LIMIT.search(l) and not DIFF.match(l)), None)
        if not hit:
            m["limit"] = False
            return []
        if m["limit"]:
            return []
        m["limit"] = True
        i = m["info"]
        return [("limit", {"handle": i["handle"], "title": i["title"], "project": i["project"], "line": _trim(hit)})]


# --- Blocked time: pure, from `seen` rows ---

def _spans(rows, start, end, until, statuses):
    """Seconds each handle spent in `statuses` between start and end. rows: `seen` rows, oldest first, starting
    with each handle's last row before start. An "offline" row closes every open interval, a "gone" row its
    own; the last open ones run until `until` (the last sample)."""
    open_, out = {}, {}

    def close(h, t):
        since, r = open_.pop(h)
        secs = min(t, end) - max(since, start)
        if secs > 0:
            x = out.setdefault(h, {"handle": h, "title": r["title"], "project": r["project"], "secs": 0.0})
            x["secs"] += secs

    for r in rows:
        if r["handle"] == OFFLINE:
            for h in list(open_):
                close(h, r["ts"])
            continue
        if r["handle"] in open_:
            close(r["handle"], r["ts"])
        if r["status"] in statuses:
            open_[r["handle"]] = (r["ts"], r)
    for h in list(open_):
        close(h, until)
    return out


def blocked(rows, start, end, until):
    """{"total": minutes, "by": [{"handle", "title", "project", "min"}]} spent waiting on you, most first."""
    spans = sorted(_spans(rows, start, end, until, BLOCKED).values(), key=lambda x: -x["secs"])
    by = [{"handle": x["handle"], "title": x["title"], "project": x["project"], "min": round(x["secs"] / 60)} for x in spans]
    return {"total": round(sum(x["secs"] for x in spans) / 60), "by": by}


def sessions(rows, start, end, until):
    """Handles that worked between start and end."""
    return set(_spans(rows, start, end, until, WORKING))


def level(done):
    return 1 + int(math.sqrt(done))


# --- Glue: the agent log ---

_watch = Watch()
_lock = threading.Lock()  # agents.card() may run a sample while the sampler thread runs another


def _day(ts):
    return date.fromtimestamp(ts).isoformat()


def _bounds(day):
    d = date.fromisoformat(day)
    return (datetime.combine(d, dtime.min).timestamp(), datetime.combine(d + timedelta(days=1), dtime.min).timestamp())


def _record(events, now):
    for kind, d in events:
        if kind == "seen":
            agentlog.w("INSERT INTO seen(ts, day, handle, project, title, agent, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
                       (now, _day(now), d["handle"], d["project"], d["title"], d["agent"], d["status"]))
        elif kind == "finished":
            tid = agentlog.w("INSERT INTO task(ts, day, handle, project, title, agent, recap) VALUES(?, ?, ?, ?, ?, ?, ?)",
                             (now, _day(now), d["handle"], d["project"], d["title"], d["agent"], d["recap"]))
            agentlog.emit("finished", {"task": tid, "handle": d["handle"], "title": d["title"],
                                       "project": d["project"], "recap": d["recap"]}, ts=now)
        else:
            agentlog.emit(kind, d, ts=now)


def observe(cfg, items, bodies, now):
    """Called by agents.sample() with every sample, before it sorts. Never raises over the agent log; agents.sample()
    catches anything else. One sample at a time: the watch and the last_sample bookkeeping aren't thread-safe."""
    with _lock:
        _watch.c = settings(cfg)
        try:
            last = float(agentlog.meta("last_sample") or 0)
            if last and now - last > OFFLINE_S:
                agentlog.w("INSERT INTO seen(ts, day, handle, status) VALUES(?, ?, ?, 'offline')", (last, _day(last), OFFLINE))
                _watch.agents.clear()  # whatever happened in the gap is unknown: start over
        except (sqlite3.Error, OSError, ValueError) as ex:
            print(f"agenthealth: {ex}", flush=True)
        events = _watch.observe(items, bodies, now)
        try:
            _record(events, now)
            agentlog.meta("last_sample", now)
        except (sqlite3.Error, OSError) as ex:
            print(f"agenthealth: {ex}", flush=True)


def answered(cfg, a, what):
    """A reply went to agent `a` from the panel: emit "answer", away when this desk is idle. Never raises."""
    try:
        if what in ANSWERS_IGNORED:
            return
        secs = idle.idle_seconds()
        away = secs is not None and secs >= settings(cfg)["away_idle_s"]
        agentlog.emit("answer", {"handle": a["handle"], "title": a["title"], "project": a["project"],
                                 "away": away, "idle_s": int(secs or 0)})
    except Exception as ex:  # the reply is already sent; the log must not turn it into an error
        print(f"agenthealth.answered: {type(ex).__name__}: {ex}", flush=True)


def _seen_rows(start, end):
    return agentlog.q("SELECT ts, handle, project, title, status FROM seen WHERE id IN "
                      "(SELECT max(id) FROM seen WHERE ts < ? GROUP BY handle) OR (ts >= ? AND ts < ?) ORDER BY ts, id",
                      (start, start, end))


def _until():
    return float(agentlog.meta("last_sample") or time.time())


def blocked_minutes(day):
    start, end = _bounds(day)
    return blocked(_seen_rows(start, end), start, end, _until())


def sample(cfg):
    if settings(cfg)["tokens"]:
        tokens.scan()
    agentlog.prune()


# --- Card, sheets, actions and files ---

def _n(v):
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e3 else str(v)


def _tokens_row(t):
    if t is None:
        return row("Tokens", '<small class="muted">no transcripts on this desktop</small>')
    return row("Tokens", f'<b>{_n(t["input"] + t["output"])}</b><small>in {_n(t["input"])} · out {_n(t["output"])}'
                         f' · cache {_n(t["cache_read"] + t["cache_write"])} · this desktop only</small>')


def _task_row(t):
    return (f'<button class="agent" data-sheet="agenthealth/ready/{t["id"]}"><i class="dot ok"></i>'
            f'<span class="who"><b>{e(t["title"])}</b><small>{e(t["recap"] or t["project"])}</small></span>'
            f'<span class="when"><small>{e(t["project"])}</small><small>{ago(time.time() - t["ts"])}</small></span></button>')


def card(cfg):
    c, today = settings(cfg), date.today().isoformat()
    items = (STATE.get("agents") or {}).get("items", [])
    b = blocked_minutes(today)
    out = [row("Waiting on you today", pill("warn" if b["total"] else "ok", f'{b["total"]} min'))]
    for x in b["by"][:5]:
        if x["min"]:
            out.append(row(f'{x["title"]} · {x["project"]}', f'<small>{x["min"]} min</small>'))
    stuck = [a for a in items if a["status"] == "stuck"]
    if stuck:
        out.append(f"<h3>Stuck ({len(stuck)})</h3>" + "".join(agents.item_row(a) for a in stuck))
    ready = agentlog.q("SELECT * FROM task WHERE dismissed IS NULL AND ts > ? ORDER BY ts DESC LIMIT 8",
                       (time.time() - 7 * 86400,))
    if ready:
        out.append(f"<h3>Ready to review ({len(ready)})</h3>" + "".join(_task_row(t) for t in ready))
    forgotten = [a for a in items if a.get("forgotten")]
    if forgotten:
        out.append(f"<h3>Forgotten ({len(forgotten)})</h3>" + "".join(agents.item_row(a) for a in forgotten))
    if c["tokens"]:
        out.append(_tokens_row(tokens.today()))
    out.append(f'<div class="foot"><button class="mini" data-sheet="agenthealth/report/{today}"><b>Daily report</b></button>'
               + ('<button class="mini" data-action="agenthealth/dismiss/all" data-confirm="Mark every finished task as reviewed?">'
                  'Dismiss all</button>' if ready else "") + "</div>")
    return "".join(out)


def _ready_sheet(cfg, arg):
    r = agentlog.q("SELECT * FROM task WHERE id = ?", (int(arg),))
    if not r:
        return '<p class="muted">That task is gone.</p>'
    t = r[0]
    out = [f'<div class="sheet-meta">{e(t["agent"])} · {e(t["project"])} · '
           f'{time.strftime("%a %d %b %H:%M", time.localtime(t["ts"]))}</div>',
           f'<p>{e(t["recap"] or "No recap on screen when it finished.")}</p>']
    if any(a["handle"] == t["handle"] for a in (STATE.get("agents") or {}).get("items", [])):
        out.append(f'<button class="alt" data-sheet="agents/view/{e(t["handle"])}"><b>{e(t["title"])}</b> · open its screen</button>')
    if t["dismissed"]:
        out.append(f'<p class="muted"><small>Reviewed {ago(time.time() - t["dismissed"])} ago.</small></p>')
    else:
        out.append(f'<button data-action="agenthealth/dismiss/{t["id"]}">Reviewed, dismiss</button>')
    return "".join(out)


def _report_sheet(cfg, arg):
    d = date.fromisoformat(arg)
    day, today = d.isoformat(), date.today()
    start, end = _bounds(day)
    rows, until = _seen_rows(start, end), _until()
    b = blocked(rows, start, end, until)
    tasks = agentlog.q("SELECT * FROM task WHERE day = ? ORDER BY ts", (day,))
    counts = {r["kind"]: r["n"] for r in agentlog.q(
        "SELECT kind, count(*) AS n FROM outbox WHERE ts >= ? AND ts < ? AND kind IN ('stuck', 'limit') GROUP BY kind",
        (start, end))}
    prev, nxt = d - timedelta(days=1), d + timedelta(days=1)
    out = [f'<div class="foot"><button class="mini" data-sheet="agenthealth/report/{prev}">← <b>Report {prev:%a %d %b}</b></button>'
           + (f'<button class="mini" data-sheet="agenthealth/report/{nxt}"><b>Report {nxt:%a %d %b}</b> →</button>'
              if nxt <= today else "") + "</div>",
           f'<h3>{d:%A %d %B}</h3>',
           row("Sessions", f'<b>{len(sessions(rows, start, end, until))}</b><small>agents that worked</small>'),
           row("Waiting on you", f'<b>{b["total"]} min</b>')]
    out += [row(f'{x["title"]} · {x["project"]}', f'<small>{x["min"]} min</small>') for x in b["by"] if x["min"]]
    out.append(row("Stuck", f'<b>{counts.get("stuck", 0)}</b>'))
    out.append(row("Usage limits", f'<b>{counts.get("limit", 0)}</b>'))
    if settings(cfg)["tokens"]:
        t = tokens.day(day)
        out.append(_tokens_row(t) if t is not None else row("Tokens", '<small class="muted">not recorded</small>'))
    out.append(f"<h3>Finished ({len(tasks)})</h3>")
    out += [_task_row(t) for t in tasks] or ['<p class="muted">None.</p>']
    return "".join(out)


def dismiss(cfg, arg, body):
    now = time.time()
    if arg == "all":
        n = len(agentlog.q("SELECT id FROM task WHERE dismissed IS NULL"))
        agentlog.w("UPDATE task SET dismissed = ? WHERE dismissed IS NULL", (now,))
        return f"{n} finished task{'s' if n != 1 else ''} marked as reviewed."
    tid = int(arg or "")
    if not agentlog.q("SELECT id FROM task WHERE id = ?", (tid,)):
        raise ValueError("That task is gone.")
    agentlog.w("UPDATE task SET dismissed = ? WHERE id = ? AND dismissed IS NULL", (now, tid))
    return "Marked as reviewed."


def outbox_file(cfg, arg):
    return "application/json", json.dumps(agentlog.outbox(int(arg))).encode()


def party(now=None):
    """Running agents as party members: a level per (project, agent) from the tasks it finished."""
    s = STATE.get("agents") or {}
    done = {(r["project"], r["agent"]): r["n"]
            for r in agentlog.q("SELECT project, agent, count(*) AS n FROM task GROUP BY project, agent")}
    members = []
    for a in s.get("items", []):
        n = done.get((a["project"], a["agent"]), 0)
        members.append({"key": a["handle"], "title": a["title"], "project": a["project"], "agent": a["agent"],
                        "status": a["status"], "level": level(n), "done": n, "forgotten": bool(a.get("forgotten"))})
    return {"at": s.get("at") or (time.time() if now is None else now), "members": members}


def party_file(cfg, arg):
    if arg != "now":
        raise ValueError("Only party/now.")
    return "application/json", json.dumps(party()).encode()


ACTIONS = {"dismiss": dismiss}
SHEETS = {"report": _report_sheet, "ready": _ready_sheet}
FILES = {"outbox": outbox_file, "party": party_file}

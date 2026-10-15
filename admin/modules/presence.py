"""Desk time: how long you've been sitting at this desktop, your breaks, and when your agents are busy enough
that it's a good moment to stand up.

The sampler reads this desk's keyboard and mouse idle time (modules/idle.py) every SAMPLE_EVERY seconds and
hands it, with the agents from STATE["agents"], to the pure Desk below. Desk turns samples into events, which
go to the agent log's outbox (modules/agentlog.py) for the Pi to pull (modules/bridge.py):
- presence {start, end}: an activity span closed. A span starts at the first input after a break and ends
  at the last input before one. You're active while idle_s < active_idle_s; shorter pauses stay in the span.
- break {idle_s, span_min, after_sit, nudge}: idle for break_min minutes closes the span. after_sit: a sit
  reminder went out for that span (or within 5 minutes before it ended). nudge: an "Agents busy" window is
  open, or closed less than 5 minutes before the span ended.
- sit {active_min, span_start}: a span reached sit_min minutes. Once per span.
- nudge {window}: busy_min_working agents or more are working (or stuck), none waits on you, for
  busy_after_min minutes, and you're at the desk. Once per window; the window closes when that stops.
A gap between samples longer than 3 * SAMPLE_EVERY + 30 s (the desk slept) closes the span at the last input,
with a presence event only. The Desk lives in memory only: a panel restart with a span open loses that span,
so no presence event ever reports it (a night it overlapped can then be judged on time, modules/rpg.py).

The sit and nudge reminders are also notices for the kiosk: STATE["notices"] = [{id, text, until}], with ids
such as "desk:nudge:<window start>" that stay the same when the Switch reads them through the Pi. /live
carries them (app.py), and a break clears them.

config "presence" (every key optional): sit_min, break_min, active_idle_s, busy_after_min, busy_min_working.
tiles(): Desk time and Move for the Home grid."""
import time
from datetime import datetime

from modules import idle
from modules.common import STATE, bar, e, pill, row

NAME, TITLE, TAB = "presence", "Desk time", "home"
SAMPLE_EVERY = 15
DEFAULTS = {"sit_min": 50, "break_min": 3, "active_idle_s": 60, "busy_after_min": 20, "busy_min_working": 2}
GRACE = 300        # a sit or a nudge this recent still counts for the break that follows
NOTICE_S = 600     # how long a notice stays on the kiosk
WORKING = ("working", "stuck")
BLOCKED = ("waiting", "atpc")


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("presence") or {})}


def agents_busy(agents, min_working):
    """Enough agents working, and none waiting on you (an idle terminal doesn't count either way)."""
    statuses = [a.get("status") for a in agents or []]
    return sum(s in WORKING for s in statuses) >= min_working and not any(s in BLOCKED for s in statuses)


# --- The desk: pure, no database and no clock ---

class Desk:
    """Remembers the open span and the busy window between samples. step() returns events as
    (kind, data, ts) and keeps the notices; notices(now) lists the ones still showing."""

    def __init__(self, c=None):
        self.c = {**DEFAULTS, **(c or {})}
        self.last_at = None        # time of the previous sample
        self.span_start = None     # first input of the open span, or None
        self.last_input = None     # latest input seen in the open span
        self.sat = False           # a sit went out for the open span
        self.sit_at = None         # time of the latest sit
        self.window = None         # start of the open busy window, or None
        self.nudged = False        # a nudge went out for the open window
        self.window_closed = None  # when the latest nudged window closed
        self.nudge_due = False     # a nudge went out and no break has answered it yet
        self._notices = {}         # id -> {id, text, until}

    def _close(self, end, events, gap=False, idle_s=0, now=None):
        start = self.span_start
        events.append(("presence", {"start": round(start, 1), "end": round(end, 1)}, end))
        if not gap:
            nudge = self.nudge_due and (self.window is not None or (
                self.window_closed is not None and end - self.window_closed <= GRACE))
            self.nudge_due = False  # one break answers one nudge
            after_sit = self.sat or (self.sit_at is not None and 0 <= end - self.sit_at <= GRACE)
            events.append(("break", {"idle_s": int(idle_s), "span_min": int((end - start) // 60),
                                     "after_sit": after_sit, "nudge": nudge}, now))
            self._notices.clear()  # you took the break they asked for
        self.span_start = self.last_input = None
        self.sat = False

    def step(self, idle_s, now, agents):
        events = []
        if idle_s is None:  # no idle reading: nothing is known about this moment
            return events
        c = self.c
        idle_s = max(0.0, float(idle_s))
        # The desk slept (or the panel stopped): close the span at the last input, without a break.
        if self.last_at is not None and now - self.last_at > 3 * SAMPLE_EVERY + 30 and self.span_start is not None:
            self._close(self.last_input, events, gap=True)
        self.last_at = now
        active = idle_s < c["active_idle_s"]
        if active:
            if self.span_start is None:
                self.span_start = now - idle_s
            self.last_input = max(self.last_input or 0, now - idle_s)
        elif self.span_start is not None and idle_s >= c["break_min"] * 60:
            self._close(max(self.last_input, now - idle_s), events, idle_s=idle_s, now=now)

        if self.span_start is not None and not self.sat and self.last_input - self.span_start >= c["sit_min"] * 60:
            self.sat, self.sit_at = True, now
            mins = int((self.last_input - self.span_start) // 60)
            events.append(("sit", {"active_min": mins, "span_start": round(self.span_start, 1)}, now))
            self._notices[f"desk:sit:{int(self.span_start)}"] = {
                "id": f"desk:sit:{int(self.span_start)}", "text": f"{mins} min at the desk. Stand up and stretch.",
                "until": now + NOTICE_S}

        if agents_busy(agents, c["busy_min_working"]):
            if self.window is None:
                self.window, self.nudged = now, False
            if not self.nudged and active and now - self.window >= c["busy_after_min"] * 60:
                self.nudged = self.nudge_due = True
                events.append(("nudge", {"window": int(self.window)}, now))
                self._notices[f"desk:nudge:{int(self.window)}"] = {
                    "id": f"desk:nudge:{int(self.window)}", "text": "Your agents are busy. Good moment for a break.",
                    "until": now + NOTICE_S}
        elif self.window is not None:
            if self.nudged:
                self.window_closed = now
            self.window, self.nudged = None, False

        for k in [k for k, n in self._notices.items() if n["until"] <= now]:
            del self._notices[k]
        return events

    def notices(self, now):
        return [dict(n) for n in self._notices.values() if n["until"] > now]

    def snapshot(self, now):
        return {"at": now, "span_start": self.span_start, "last_input": self.last_input,
                "active_min": int((self.last_input - self.span_start) // 60) if self.span_start is not None else 0,
                "window": self.window, "nudged": self.nudged}


# --- Glue: sampler, outbox, card ---

_desk = []


def _emit(kind, data, ts):
    try:
        from modules import agentlog
        agentlog.emit(kind, data, ts)
    except Exception as ex:  # a broken log must not stop the timer
        print(f"presence: {kind} not logged: {type(ex).__name__}: {ex}", flush=True)


def sample(cfg):
    if not _desk:
        _desk.append(Desk(settings(cfg)))
    desk, now = _desk[0], time.time()
    desk.c = settings(cfg)
    secs = idle.idle_seconds()
    for kind, data, ts in desk.step(secs, now, (STATE.get("agents") or {}).get("items", [])):
        _emit(kind, data, ts)
    STATE["notices"] = desk.notices(now)
    STATE["presence"] = {**desk.snapshot(now), "idle_s": secs}


def _breaks_today():
    try:
        from modules import agentlog
        start = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp()
        return len(agentlog.q("SELECT id FROM outbox WHERE kind = 'break' AND ts >= ?", (start,)))
    except Exception:
        return None


def tiles(cfg):
    """Home tiles (app.py): Desk time (warn from 80 % of sit_min, bad from sit_min), and Move while an
    "Agents busy" window is open."""
    c, s = settings(cfg), STATE.get("presence")
    if not s or s.get("idle_s") is None:
        return []
    mins = s["active_min"]
    if s["span_start"] is None:
        value, tint = "Away", None
    elif mins >= c["sit_min"]:
        value, tint = f"{mins} min · stand up", "bad"
    elif mins >= c["sit_min"] * 0.8:
        value, tint = f"{mins} min · stand up soon", "warn"
    else:
        value, tint = f"{mins} min", None
    out = [{"key": "desk", "label": "Desk time", "value": value, "tint": tint, "icon": "timer", "open": None,
            "span": 1, "order": 60}]
    if s["window"] is not None:
        busy = int((s["at"] - s["window"]) // 60)
        out.append({"key": "move", "label": "Move", "value": f"Agents busy {busy} min" + (" · take a break" if s["nudged"] else ""),
                    "tint": "busy", "icon": "walk", "open": None, "span": 1, "order": 110})
    return out


def card(cfg):
    c, s = settings(cfg), STATE.get("presence")
    if not s:
        return '<p class="muted">Starting…</p>'
    if s.get("idle_s") is None:
        return '<p class="muted">Idle time isn\'t available here (needs GNOME\'s session bus).</p>'
    mins = s["active_min"]
    lvl = "bad" if mins >= c["sit_min"] else "warn" if mins >= c["sit_min"] * 0.8 else "ok"
    sitting = (f'<b>{mins} min</b><small>of {c["sit_min"]}</small>' if s["span_start"] is not None
               else '<small class="muted">away</small>')
    out = [row("Sitting", sitting), bar(mins / max(1, c["sit_min"]), lvl)]
    n = _breaks_today()
    out.append(row("Breaks today", f'<b>{n}</b>' if n is not None else '<small class="muted">unknown</small>'))
    if s["window"] is not None:
        busy = int((s["at"] - s["window"]) // 60)
        out.append(row("Agents busy", pill("warn" if s["nudged"] else "busy",
                                           f"{busy} min · take a break" if s["nudged"] else f"{busy} min")))
    else:
        out.append(row("Agents busy", pill("dim", "no")))
    return "".join(out) + "".join(f'<div class="alert warn">{e(x["text"])}</div>' for x in STATE.get("notices") or [])

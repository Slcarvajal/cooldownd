"""Life RPG: sessions of any sport, meals and sleep logged as events, and a game built from them.

Only events are stored (~/.local/share/homelab-admin/life.db, 600). XP, levels, streaks and quests are
computed from them on every read (the rules below), so changing a rule rescores the whole history.

- Activities (config "life.activities"; gym and volleyball by default): one tap on the card, or the
  Joy-Cons on the game screen. Each one feeds a skill, and its sessions a week make a weekly quest.
- Meals: a photo or a few words. Claude Haiku 5.5 estimates the items and macros in the background;
  Sonnet 5.5 takes over when Haiku is unsure, and for every correction. Corrections are kept and sent
  with later estimates, so it learns your portions. The key is ~/.config/homelab-admin/anthropic-key (600).
- Sleep: the `sleep` table (one row per night, keyed by the day you woke up), filled by a data source such
  as a Fitbit import, or by hand ("Slept…" on the card, ACTIONS["sleep"]). A night typed by hand replaces
  whatever that day had. The Recovery skill scores it.
- The desk's bridge (modules/bridge.py) hands its events to record(), which keeps them in the `bridge`
  table, once each by (src, ref). The Balance skill, bedtime nights and the weekly boss come from them;
  the rules are in modules/rpg.py and only count from the first day the bridge delivered anything.
- Bars (HP from sleep, Energy, Stamina), mood, achievements, titles and cosmetics (equipped from the game,
  ACTIONS["equip"], kept in `meta`): modules/rpg.py.
- PAGES["game"] is the full-screen game, read from life/game.html on every request. The Switch shows it
  when docked to a TV (config "game"). Its state (FILES["state"]) adds the desk's party (STATE["party"],
  when under 2 minutes old) and the co-op board (STATE["coop"], from modules/coop.py, under 30 minutes old).
- Phone alerts (modules/notify.py) on a level up, a new achievement, and on Friday evening when a weekly
  quest is behind.

Goals (calorie and macro targets, sessions a week) are set from the card and kept in the database.
tiles(): the avatar, the current quest and today's calories for the Home grid.

config "life" (every key optional):
  "name"          shown in the game, and in the meal prompt ("Ana")
  "food_context"  where the meals are, for ambiguous dishes ("Mexico City: tacos, chilaquiles, pozole…")
  "activities"    {kind: {"label", "emoji", "skill", "xp", "per_week", "quest_bonus"}}
  "skills"        {skill: {"label", "color", "perks": [5 names]}}; Nutrition, Recovery and Balance are built in
  "sprites"       folder of avatar strips (life/make_sprites.py); without one, a drawn pixel avatar
  "sleep_hours"   a night's target, for the sleep debt and HP (8)
  "coop"          {"board", "party_goal"}: the co-op board's name and the quests the party aims for a week"""
import base64
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta

from modules import night, notify, rpg
from modules.common import STATE, ago, bar, e, icon, pill, row

NAME, TITLE, TAB = "life", "Life", "home"
SAMPLE_EVERY = 60
DATA = os.path.expanduser("~/.local/share/homelab-admin")
DB_PATH = os.path.join(DATA, "life.db")
PHOTOS = os.path.join(DATA, "meals")
KEY_PATH = os.path.expanduser("~/.config/homelab-admin/anthropic-key")
API = "https://api.anthropic.com/v1/messages"
FAST, CAREFUL = "claude-haiku-5-5", "claude-sonnet-5-5"
SURE = 0.6  # a Haiku estimate less confident than this is redone by Sonnet
UPLOADS = {"meal": 8 << 20}  # a phone photo, already shrunk to 1568 px by the page
SLEEP_DAYS = 7  # a night typed by hand: at most this many days back

# --- Rules. Nothing below is stored: change a number (or the config) and the whole history is rescored. ---
DEFAULT_ACTIVITIES = {
    "gym": {"label": "Gym", "emoji": "🏋️", "skill": "strength", "xp": 50, "per_week": 3, "quest_bonus": 100},
    "volleyball": {"label": "Volleyball", "emoji": "🏐", "skill": "volleyball", "xp": 60, "per_week": 1, "quest_bonus": 50},
}
KNOWN_SKILLS = {
    "strength": {"label": "Strength", "color": "#ef6b4a", "perks": ["First rep", "Iron habit", "Progressive overload", "Powerhouse", "Titan"]},
    "volleyball": {"label": "Volleyball", "color": "#38bdf8", "perks": ["Bump", "Set", "Spike", "Block party", "Ace"]},
    "endurance": {"label": "Endurance", "color": "#f59e0b", "perks": ["First mile", "Steady pace", "Negative split", "Long haul", "Iron lungs"]},
    "nutrition": {"label": "Nutrition", "color": "#4ade80", "perks": ["Food log", "Protein aware", "Macro balance", "Meal prep", "Fuel master"]},
    "recovery": {"label": "Recovery", "color": "#a78bfa", "perks": ["Lights out", "Steady sleeper", "Deep rest", "Recharged", "Iron sleep"]},
    "balance": {"label": "Balance", "color": "#2dd4bf", "perks": ["Step away", "Stretch break", "Hands off", "Steady rhythm", "Zen master"]},
}
BUILT_IN = ("nutrition", "recovery", "balance")
COLORS = ["#ef6b4a", "#38bdf8", "#f59e0b", "#f472b6", "#2dd4bf", "#facc15"]
PERK_NAMES = ["Novice", "Adept", "Skilled", "Expert", "Master"]
XP_MEAL = 5            # nutrition, per meal logged
XP_PROTEIN_DAY = 30    # nutrition, the day protein reaches 90 % of the goal
XP_KCAL_DAY = 20       # nutrition, a finished day within 10 % of the calorie goal
XP_SLEEP = ((420, 30), (480, 15))  # recovery: 30 for a 7-hour night, 15 more at 8 hours
PROTEIN_WEEK = ("nutrition", 60, 5)  # skill, bonus and default days a week for the protein quest
PERKS_AT = (2, 4, 6, 8, 10)  # skill levels that unlock each perk
TITLES = ((1, "Rookie"), (3, "Apprentice"), (5, "Regular"), (8, "Athlete"), (12, "Veteran"), (16, "Champion"), (20, "Legend"))
MACROS = ("kcal", "protein", "carbs", "fat")
SLEEP_HOURS = 8        # default nightly target (config "life.sleep_hours")
COOP = {"board": "Weekly quest board", "party_goal": 12}
RECORD_KINDS = ("answer", "break", "presence", "sit", "nudge")  # desk events record() keeps
RECORD_MAX = 2048      # bytes of an event's JSON data
PARTY_FRESH_S = 120    # STATE["party"] older than this isn't shown
COOP_FRESH_S = 1800    # nor STATE["coop"] older than this (modules/coop.py rereads friends every 600 s)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meal(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, said TEXT NOT NULL DEFAULT '',
  photo INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, started REAL, title TEXT, items TEXT, kcal REAL, protein REAL,
  carbs REAL, fat REAL, confidence REAL, note TEXT, model TEXT, error TEXT, fixes TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sleep(day TEXT PRIMARY KEY, minutes INTEGER NOT NULL, deep INTEGER, rem INTEGER, score INTEGER,
  source TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS goal(key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bridge(src TEXT NOT NULL, ref TEXT NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL,
  kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(src, ref));
CREATE INDEX IF NOT EXISTS activity_day ON activity(day);
CREATE INDEX IF NOT EXISTS meal_day ON meal(day);
CREATE INDEX IF NOT EXISTS bridge_day ON bridge(day);
"""
_ready = threading.Lock()
_made = []
_ver = [0]  # bumped on every write here; with the file stamps, the computed game is cached until a change
_cache = {}
FRESH = 2  # seconds game() is reused without even checking the stamps: the card, the tiles and every poll share it
_game_lock = threading.RLock()

MEAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "items", "confidence", "note"],
    "properties": {
        "title": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "portion", "grams", "kcal", "protein", "carbs", "fat"],
            "properties": {"name": {"type": "string"}, "portion": {"type": "string"}, "grams": {"type": "number"},
                           "kcal": {"type": "number"}, "protein": {"type": "number"}, "carbs": {"type": "number"},
                           "fat": {"type": "number"}}}},
        "confidence": {"type": "number"},
        "note": {"type": "string"}}}
PROMPT = """You estimate the nutrition of one meal for {who} personal food log.{where}

- items: each food you can see or that they mention, with a portion in household terms ("2 eggs", "1 cup of rice"), its weight in grams, and its calories and grams of protein, carbs and fat. Include cooking oil, butter and sauces you can reasonably infer.
- title: a short name for the meal, at most 5 words.
- confidence: 0 to 1, how sure you are of the total calories and protein. Below 0.6 when portions are hard to judge or foods are hidden or unclear.
- note: one short sentence on what was hardest to judge.

Only list foods you can see in the photo or that they mention for this meal. If the photo shows no food and they said nothing, return no items, confidence 0, and say so in the note.

Write the title, item names and note in the language they used; if they wrote nothing, in English. When they correct an earlier estimate, their correction wins over what the photo seems to show."""


# --- Storage ---

def _db():
    with _ready:
        if not _made:
            os.makedirs(PHOTOS, mode=0o700, exist_ok=True)
            # Create the file 600 before SQLite touches it: a chmod afterwards leaves a short open window.
            fd = os.open(DB_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            with closing(sqlite3.connect(DB_PATH)) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA_SQL)
            _made.append(True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _q(sql, args=()):
    with closing(_db()) as con:
        return con.execute(sql, args).fetchall()


def _w(sql, args=()):
    with closing(_db()) as con, con:
        cur = con.execute(sql, args)
    _ver[0] += 1
    return cur.lastrowid


def _stamp():
    """Changes when another process (an import, a script) writes to the database."""
    try:
        return tuple(os.stat(p).st_mtime_ns for p in (DB_PATH, DB_PATH + "-wal") if os.path.exists(p))
    except OSError:
        return ()


def _meta(key, value=None):
    if value is None:
        r = _q("SELECT value FROM meta WHERE key = ?", (key,))
        return r[0]["value"] if r else None
    _w("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def record(src, ref, kind, ts, data):
    """Keep one desk event for the game. Called in-process by modules/bridge.py, never over HTTP.
    src is "desk:<epoch>" and ref the outbox id: an event already kept under (src, ref) is skipped, so
    pulling the same page twice scores once. Only RECORD_KINDS are kept; data (a dict) is cut to
    RECORD_MAX bytes of JSON. -> True if it was new and kept."""
    if kind not in RECORD_KINDS or not isinstance(data, dict) or isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    src, ref = str(src or ""), str(ref or "")
    if not src or not ref or len(src) > 100 or len(ref) > 40 or not 0 < ts < 1e11:
        return False
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text.encode()) > RECORD_MAX:  # keep the plain values, strings cut short
        data = {str(k)[:40]: v[:100] if isinstance(v, str) else v for k, v in data.items()
                if v is None or isinstance(v, (str, int, float, bool))}
        text = json.dumps(data, ensure_ascii=False)
        if len(text.encode()) > RECORD_MAX:
            text = "{}"
    with closing(_db()) as con, con:
        cur = con.execute("INSERT INTO bridge(src, ref, ts, day, kind, data) VALUES(?, ?, ?, ?, ?, ?)"
                          " ON CONFLICT(src, ref) DO NOTHING",
                          (src, ref, float(ts), date.fromtimestamp(ts).isoformat(), kind, text))
    if cur.rowcount != 1:
        return False
    _ver[0] += 1
    return True


def _bridge_rows():
    out = []
    for r in _q("SELECT ts, kind, data FROM bridge ORDER BY ts"):
        try:
            data = json.loads(r["data"])
        except ValueError:
            data = {}
        out.append({"ts": r["ts"], "kind": r["kind"], "data": data if isinstance(data, dict) else {}})
    return out


def rules(cfg):
    """The game's shape from config "life": activities, skills (activity skills first, then the built-in
    Nutrition, Recovery and Balance), weekly quests, goals, the sleep target and the co-op board."""
    c = (cfg or {}).get("life") or {}
    acts = {}
    for kind, a in (c.get("activities") or DEFAULT_ACTIVITIES).items():
        acts[kind] = {"label": kind.replace("_", " ").title(), "emoji": "", "skill": kind, "xp": 50, "per_week": 0,
                      "quest_bonus": 0, **a}
    keys = list(dict.fromkeys([a["skill"] for a in acts.values()] + list(BUILT_IN)))
    skills = []
    for i, k in enumerate(keys):
        spec = {**KNOWN_SKILLS.get(k, {}), **(c.get("skills") or {}).get(k, {})}
        skills.append((k, spec.get("label") or k.replace("_", " ").title(), spec.get("color") or COLORS[i % len(COLORS)],
                       (spec.get("perks") or PERK_NAMES)[:len(PERKS_AT)]))
    bonus = {kind: (a["skill"], a["quest_bonus"]) for kind, a in acts.items()}
    bonus["protein_days"] = PROTEIN_WEEK[:2]
    goal_fields = [("kcal", "Calories a day", "kcal"), ("protein", "Protein a day", "g"), ("carbs", "Carbs a day", "g"),
                   ("fat", "Fat a day", "g")]
    goal_fields += [(kind, f"{a['label']} sessions a week", "") for kind, a in acts.items()]
    goal_fields.append(("protein_days", "Protein days a week", ""))
    return {"acts": acts, "skills": skills, "bonus": bonus, "goal_fields": goal_fields,
            "default_goals": {**{kind: a["per_week"] for kind, a in acts.items()}, "protein_days": PROTEIN_WEEK[2]},
            "quests": {**{kind: a["label"] for kind, a in acts.items()}, "protein_days": "Protein days"},
            "name": c.get("name") or (cfg or {}).get("name") or "", "food": c.get("food_context") or "",
            "sprites": os.path.expanduser(c.get("sprites") or SPRITES), "key": json.dumps(c, sort_keys=True),
            "sleep_min": round(60 * float(c.get("sleep_hours") or SLEEP_HOURS)), "coop": {**COOP, **(c.get("coop") or {})}}


def goals(cfg):
    return {**rules(cfg)["default_goals"], **{r["key"]: r["value"] for r in _q("SELECT key, value FROM goal")}}


def _photo(mid):
    return os.path.join(PHOTOS, f"{int(mid)}.jpg")


# --- The game, computed from the events ---

def level_of(xp, base=50):
    """(level, XP into it, XP the level spans). Level L lasts 100·L XP: 100, 200, 300…"""
    lvl = 1
    while xp >= base * lvl * (lvl + 1):
        lvl += 1
    start = base * (lvl - 1) * lvl
    return lvl, int(xp - start), 2 * base * lvl


def _monday(d):
    return d - timedelta(days=d.weekday())


def _end_of(day):
    return datetime.combine(date.fromisoformat(day), datetime.max.time()).timestamp()


def game(cfg):
    """The whole game state (_game), computed once per FRESH for the same database, config and writes here,
    however many cards, tiles and clients ask; a write from this process (_ver) shows at once."""
    quick = (DB_PATH, _ver[0], date.today(), json.dumps(cfg or {}, sort_keys=True, default=str))
    with _game_lock:
        hit = _cache.get("fresh")
        if hit and hit[0] == quick and time.monotonic() - hit[1] < FRESH:
            return hit[2]
        g = _game(cfg)
        _cache["fresh"] = (quick, time.monotonic(), g)
        return g


def _game(cfg):
    R = rules(cfg)
    today, now = date.today(), time.time()
    key = (_ver[0], _stamp(), today, R["key"], night.next_wake(cfg, now), json.dumps((cfg or {}).get("night")))
    if _cache.get("key") == key:
        return _cache["game"]
    g = goals(cfg)
    acts = _q("SELECT id, ts, day, kind FROM activity ORDER BY ts")
    meals = _q("SELECT * FROM meal ORDER BY ts")
    events = []  # (ts, skill, xp, text)
    weeks = {}   # monday -> {quest: [ts, ...]}
    for a in acts:
        spec = R["acts"].get(a["kind"])
        if spec:  # sessions of an activity taken out of the config stay in the database, unscored
            events.append((a["ts"], spec["skill"], spec["xp"], spec["label"]))
            weeks.setdefault(_monday(date.fromisoformat(a["day"])), {}).setdefault(a["kind"], []).append(a["ts"])
    days = {}
    for m in meals:
        if m["status"] != "done" or not m["kcal"]:  # a photo with no food in it scores nothing
            continue
        events.append((m["ts"], "nutrition", XP_MEAL, m["title"] or "Meal"))
        d = days.setdefault(m["day"], {**dict.fromkeys(MACROS, 0.0), "hit": None})
        before = d["protein"]
        for k in MACROS:
            d[k] += m[k] or 0
        if g.get("protein") and before < 0.9 * g["protein"] <= d["protein"]:
            d["hit"] = m["ts"]
    for day, d in days.items():
        if d["hit"]:
            events.append((d["hit"], "nutrition", XP_PROTEIN_DAY, "Protein goal"))
            weeks.setdefault(_monday(date.fromisoformat(day)), {}).setdefault("protein_days", []).append(d["hit"])
        if g.get("kcal") and day < today.isoformat() and abs(d["kcal"] - g["kcal"]) <= 0.1 * g["kcal"]:
            events.append((_end_of(day), "nutrition", XP_KCAL_DAY, "Calories on target"))
    sleeps = _q("SELECT day, minutes, source FROM sleep")
    for s in sleeps:
        xp = sum(x for need, x in XP_SLEEP if s["minutes"] >= need)
        if xp:
            events.append((datetime.fromisoformat(s["day"] + "T07:00").timestamp(), "recovery", xp,
                           f"Slept {s['minutes'] // 60} h {s['minutes'] % 60:02d}"))
    for monday, got in weeks.items():
        for q, (skill, xp) in R["bonus"].items():
            need = int(g.get(q) or 0)
            if need and xp and len(got.get(q, [])) >= need:
                events.append((sorted(got[q])[need - 1], skill, xp, f"Quest: {R['quests'][q]} ×{need}"))

    # The desk's bridge (rpg.py): its XP, and the boss reward, only from the first day it delivered data.
    brows = _bridge_rows()
    since = datetime.combine(date.fromtimestamp(brows[0]["ts"]), datetime.min.time()).timestamp() if brows else None
    bridge_xp = rpg.bridge_events(brows, cfg, now)
    events += bridge_xp
    needs = {q: int(g.get(q) or 0) for q in R["bonus"] if int(g.get(q) or 0) and (q != "protein_days" or g.get("protein"))}
    defeats = rpg.boss_events(weeks, needs)
    if since is not None:
        events += [ev for ev in defeats if ev[0] >= since]

    this = _monday(today)

    def weekly(q):
        """(this week's count, streak of weeks with the quest done; this week counts once it's done)."""
        need = int(g.get(q) or 0)
        have = len(weeks.get(this, {}).get(q, []))
        n, wk = 0, this - timedelta(days=7)
        while need and len(weeks.get(wk, {}).get(q, [])) >= need:
            n, wk = n + 1, wk - timedelta(days=7)
        return have, n + (need > 0 and have >= need)

    hit_days = {d for d, v in days.items() if v["hit"]}
    streak, d = 0, today - timedelta(days=1)
    while d.isoformat() in hit_days:
        streak, d = streak + 1, d - timedelta(days=1)
    streak += today.isoformat() in hit_days

    quests, streaks = [], []
    names = {s[0]: s[1] for s in R["skills"]}
    for q, (skill, xp) in R["bonus"].items():
        need = int(g.get(q) or 0)
        if not need or (q == "protein_days" and not g.get("protein")):
            continue
        have, wk = weekly(q)
        label = R["quests"][q]
        quests.append({"key": q, "name": f"{label} ×{need}", "have": have, "need": need, "done": have >= need,
                       "reward": f"+{xp} {names.get(skill, 'XP')}" if xp else ""})
        if q != "protein_days":
            streaks.append({"name": f"{label} weeks", "short": f"{label} {wk} wk", "n": wk})
    if g.get("protein"):
        streaks.append({"name": "Protein days", "short": f"Protein {streak} d", "n": streak})

    skills = []
    for k, label, color, perks in R["skills"]:
        xp = sum(ev[2] for ev in events if ev[1] == k)
        lvl, into, span = level_of(xp, 25)
        skills.append({"key": k, "name": label, "color": color, "xp": xp, "level": lvl, "into": into, "span": span,
                       "perks": [{"at": at, "name": p, "on": lvl >= at} for at, p in zip(PERKS_AT, perks)]})
    total = sum(ev[2] for ev in events)
    lvl, into, span = level_of(total)
    t = today.isoformat()
    meals_today = [{"id": m["id"], "title": m["title"] or ("Estimating…" if m["status"] == "estimating" else "Meal"),
                    "status": m["status"], "time": time.strftime("%H:%M", time.localtime(m["ts"])),
                    **{k: round(m[k] or 0) for k in MACROS}} for m in meals if m["day"] == t]
    out = {
        "level": lvl, "title": [n for at, n in TITLES if lvl >= at][-1], "xp": total, "into": into, "span": span,
        "name": R["name"], "skills": skills, "quests": quests,
        "activities": [{"kind": k, "label": a["label"], "emoji": a["emoji"]} for k, a in R["acts"].items()], "streaks": streaks, "week": this.isoformat(),
        "today": {"date": t, **{k: round(days.get(t, {}).get(k, 0)) for k in MACROS},
                  "goals": {k: g.get(k) for k in MACROS}, "meals": meals_today[::-1]},
        "feed": [{"when": time.strftime("%a %H:%M", time.localtime(ts)), "text": text, "xp": xp, "skill": names.get(skill, "XP")}
                 for ts, skill, xp, text in sorted(events, key=lambda ev: -ev[0])[:10]],
    }
    out.update(_rpg(cfg, R, g, out, acts, meals, days, sleeps, brows, bridge_xp, defeats, events, now))
    did = [a["kind"] for a in acts if a["day"] == t]
    out["outfit"] = did[-1] if did and sprite(R, did[-1]) else "casual"
    out["sprite"] = sprite(R, out["outfit"])
    out["avatar"] = None if out["sprite"] else avatar(out)
    _cache.update(key=key, game=out)
    return out


def _rpg(cfg, R, g, out, acts, meals, days, sleeps, brows, bridge_xp, defeats, events, now):
    """The keys rpg.py adds to the game: bars, mood, sleep, boss, achievements, titles, equipped,
    cosmetics and daily."""
    today = date.today()
    first = date.fromtimestamp(brows[0]["ts"]) if brows else None
    n = rpg.nights(brows, cfg, first, now)
    judged = sorted(n["on_time"] + n["broken"])[-7:]
    debt = rpg.sleep_debt(sleeps, R["sleep_min"], today)
    last = max(sleeps, key=lambda r: r["day"], default=None)
    b = rpg.bars(debt, R["sleep_min"], out["today"], out["quests"])

    # When each achievement was first earned
    breaks = [ev[0] for ev in bridge_xp if ev[3] in (rpg.TEXT_BREAK, rpg.TEXT_NUDGE)]
    hits = sorted(d for d, v in days.items() if v["hit"])
    run, protein_week = 0, None
    for i, d in enumerate(hits):
        run = run + 1 if i and date.fromisoformat(d) - date.fromisoformat(hits[i - 1]) == timedelta(days=1) else 1
        if run == 7:
            protein_week = days[d]["hit"]
            break
    reached, total = {}, 0
    for ev in sorted(events, key=lambda ev: ev[0]):
        total += ev[2]
        for lvl in (5, 10):
            if lvl not in reached and level_of(total)[0] >= lvl:
                reached[lvl] = ev[0]
    rested = [datetime.fromisoformat(s["day"] + "T07:00").timestamp() for s in sleeps if s["minutes"] >= 480]
    facts = {
        "first_session": next((a["ts"] for a in acts if a["kind"] in R["acts"]), None),
        "food_log": next((m["ts"] for m in meals if m["status"] == "done" and m["kcal"]), None),
        "well_rested": min(rested, default=None),
        "protein_week": protein_week,
        "boss_slayer": min((ev[0] for ev in defeats), default=None),
        "level_5": reached.get(5), "level_10": reached.get(10),
        "remote": next((ev[0] for ev in bridge_xp if ev[3] == rpg.TEXT_AWAY), None),
        "stretch": sorted(breaks)[9] if len(breaks) >= 10 else None,
        "lights_out": n["seven"],
    }
    ach = rpg.achievements(facts)
    title_list = rpg.titles(out["level"], ach, TITLES)
    cosmetic_list = rpg.cosmetics(out["level"], ach)
    equipped = {}
    for slot in rpg.SLOTS:  # an item no longer unlocked (a goal changed, a session removed) comes off
        item = _meta(f"equip.{slot}")
        equipped[slot] = item if item and rpg.unlocked(slot, item, title_list, cosmetic_list) else None

    done = ([("session", a["ts"]) for a in acts if a["kind"] in R["acts"]]
            + [("meal", m["ts"]) for m in meals if m["status"] == "done" and m["kcal"]]
            + [("protein", v["hit"]) for v in days.values() if v["hit"]] + [("break", ts) for ts in breaks])
    return {
        "bars": b, "mood": rpg.mood(b),
        "sleep": {"debt_min": debt, "last": dict(last) if last else None,
                  "nights_on_time": sum(1 for ev in judged if ev in n["on_time"]), "bedtime_streak": n["streak"]},
        "boss": rpg.boss(_monday(today), out["quests"]),
        "achievements": ach, "titles": title_list, "equipped": equipped, "cosmetics": cosmetic_list,
        "daily": rpg.daily_standup(done, today, protein=bool(g.get("protein")), bridge=bool(brows)),
    }


# The avatar: 16-frame strips cut from 4×4 sprite sheets by life/make_sprites.py, one per outfit
# ("casual", plus one named like an activity, e.g. "gym.png"). Frames: 0-3 front (stand, step, step,
# its move), 4-7 facing left, 8-11 facing right, 12-14 back, 15 celebrating. The outfit is the last
# activity done today, when there's a strip for it. With no strips, a drawn pixel avatar stands in.
SPRITES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "life", "sprites")


def sprite(R, outfit):
    """{"url", "w", "h", "frames"} of an outfit's strip, or None; the URL changes when the file does."""
    path = os.path.join(R["sprites"], f"{outfit}.png")
    try:
        with open(path, "rb") as f:
            head = f.read(24)  # PNG: width and height are in the IHDR chunk
    except OSError:
        return None
    w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return {"url": f"file/life/sprite/{outfit}?v={int(os.path.getmtime(path))}", "w": w // 16, "h": h, "frames": 16}


# The drawn avatar: 16×16, its shirt the color of the top skill; gear comes with levels.
_SPRITE = ["................", ".....HHHHHH.....", "....HHHHHHHH....", "....HSSSSSSH....", "....SSESSESS....",
           "....SSSSSSSS....", ".....SSmmSS.....", "......SSSS......", "...BBBBBBBBBB...", "..SBBBBBBBBBBS..",
           "..SBBBBBBBBBBS..", "..S.BBBBBBBB.S..", "....BBBBBBBB....", "....PPP..PPP....", "....PPP..PPP....",
           "...KKKK..KKKK..."]
_PALETTE = {"H": "#3b2a20", "S": "#e0b48a", "E": "#1f2330", "m": "#b45309", "P": "#2b3a67", "K": "#1a1a1a",
            "W": "#ef4444", "G": "#f59e0b", "C": "#b91c1c", "Y": "#facc15", "V": "#7c3aed", "L": "#16a34a",
            "N": "#1e3a8a", "w": "#f8fafc", "R": "#e5e7eb"}


def avatar(state):
    """The drawn avatar. Without anything equipped, gear comes with levels: a headband at 3, a belt at 5, a
    cape at 10, a crown at 20. An equipped hat, cape or aura (rpg.py) replaces that slot; the mood sets the face."""
    top = max(state["skills"], key=lambda k: k["xp"])
    worn = state.get("equipped") or {}
    lvl = state["level"]
    hats = [worn["hat"]] if worn.get("hat") else [h for at, h in ((3, "headband"), (20, "crown")) if lvl >= at]
    cape = worn.get("cape") or ("cape" if lvl >= 10 else None)
    grid = [list(r) for r in _SPRITE]
    for y, line in rpg.MOODS.get((state.get("mood") or {}).get("key"), ("", "", {}))[2].items():
        grid[y] = list(line)
    if cape in rpg.CAPE_ART:  # a cape behind the body
        for y in range(8, 15):
            for x in (2, 13):
                if grid[y][x] == ".":
                    grid[y][x] = rpg.CAPE_ART[cape]
    for hat in hats:
        for y, line in rpg.HAT_ART.get(hat, {}).items():
            grid[y] = list(line)
    if lvl >= 5:
        grid[12] = list("....GGGGGGGG....")
    colors = {**_PALETTE, "B": top["color"] if top["xp"] else "#6d4fd8"}
    aura = rpg.AURA_ART.get(worn.get("aura"))
    rects = [f'<circle cx="8" cy="8.5" r="7.5" fill="{aura}" opacity=".3"/>'] if aura else []
    for y, line in enumerate(grid):
        x = 0
        while x < 16:
            c, run = line[x], 1
            while x + run < 16 and line[x + run] == c:
                run += 1
            if c != ".":
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{colors[c]}"/>')
            x += run
    return f'<svg viewBox="0 0 16 16" shape-rendering="crispEdges">{"".join(rects)}</svg>'


# --- Meal estimates (background thread) ---

def _prompt(R):
    where = (f" They live in {R['food']}: when a dish or portion is ambiguous, assume the food and home or"
             f" restaurant portions there.") if R["food"] else ""
    return PROMPT.format(who=f"{R['name']}'s" if R["name"] else "a person's", where=where)


def _ask(model, content, R):
    try:
        key = open(KEY_PATH).read().strip()
    except OSError:
        raise RuntimeError(f"No Anthropic key on this panel ({KEY_PATH})") from None
    facts = [f["text"] for r in _q("SELECT fixes FROM meal WHERE fixes != '[]' ORDER BY ts DESC LIMIT 30")
             for f in json.loads(r["fixes"])][:15]
    system = _prompt(R) + ("\n\nCorrections they made to estimates of past meals. They are not about this meal: use them only to judge"
                       " their usual portions and cooking, and never add a food because it appears here:\n"
                       + "\n".join(f"- {f}" for f in facts) if facts else "")
    body = {"model": model, "max_tokens": 4000, "system": system, "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": MEAL_SCHEMA}}}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
    except urllib.error.HTTPError as ex:
        try:
            msg = json.loads(ex.read())["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = ""
        raise RuntimeError(f"{model}: HTTP {ex.code} {msg}".strip()) from None
    if d.get("stop_reason") in ("refusal", "max_tokens"):
        raise RuntimeError(f"{model} stopped: {d['stop_reason']}")
    out = json.loads(next(b["text"] for b in d["content"] if b["type"] == "text"))
    for it in out["items"]:
        for k in ("grams", *MACROS):
            it[k] = max(0.0, float(it.get(k) or 0))
    out["confidence"] = min(1.0, max(0.0, float(out.get("confidence") or 0)))
    return out


def _estimate(R, mid, fix=None):
    m = _q("SELECT * FROM meal WHERE id = ?", (mid,))
    if not m:
        return
    m = m[0]
    try:
        content = []
        if m["photo"]:
            with open(_photo(mid), "rb") as f:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.b64encode(f.read()).decode()}})
        text = [f"Eaten {time.strftime('%A at %H:%M', time.localtime(m['ts']))}."]
        if m["said"]:
            text.append(f"They say: {m['said']}")
        if fix:
            text.append("Your earlier estimate:\n" + json.dumps({"title": m["title"], "items": json.loads(m["items"] or "[]")},
                                                                ensure_ascii=False))
            text.append(f"Their correction: {fix}")
        content.append({"type": "text", "text": "\n\n".join(text)})
        model = CAREFUL if fix else FAST
        out = _ask(model, content, R)
        if not fix and out["confidence"] < SURE:
            try:
                out, model = _ask(CAREFUL, content, R), CAREFUL
            except Exception as ex:  # Haiku's estimate stands
                print(f"life: Sonnet fallback failed for meal {mid}: {ex}", flush=True)
        sums = {k: sum(it[k] for it in out["items"]) for k in MACROS}
        _w("UPDATE meal SET status = 'done', title = ?, items = ?, kcal = ?, protein = ?, carbs = ?, fat = ?, confidence = ?,"
           " note = ?, model = ?, error = NULL WHERE id = ?",
           (out["title"][:80], json.dumps(out["items"], ensure_ascii=False), sums["kcal"], sums["protein"], sums["carbs"],
            sums["fat"], out["confidence"], out["note"][:300], model, mid))
        print(f"life: meal {mid} estimated by {model}: {sums['kcal']:.0f} kcal, {out['confidence']:.0%} sure", flush=True)
    except Exception as ex:
        # a failed correction keeps the estimate it was correcting
        _w("UPDATE meal SET status = ?, error = ? WHERE id = ?",
           ("done" if m["items"] else "failed", f"{type(ex).__name__}: {ex}"[:300], mid))
        print(f"life: meal {mid} failed: {ex}", flush=True)


def _start(cfg, mid, fix=None):
    _w("UPDATE meal SET status = 'estimating', started = ? WHERE id = ?", (time.time(), mid))
    threading.Thread(target=_estimate, args=(rules(cfg), mid, fix), daemon=True).start()


# --- Background: stuck estimates, level-up and weekly alerts ---

def sample(cfg):
    _w("UPDATE meal SET status = CASE WHEN items IS NULL THEN 'failed' ELSE 'done' END,"
       " error = 'Interrupted (the panel restarted). Try again.' WHERE status = 'estimating' AND started < ?",
       (time.time() - 600,))
    s = game(cfg)
    panel = (cfg.get("notify") or {}).get("panel", "")
    seen = _meta("level")
    if seen is not None and s["level"] > int(seen):
        notify.send(cfg, f"⬆️ Level {s['level']} · {s['title']}", f"{s['xp']} XP so far. Keep going.",
                    url=panel + "page/life/game", tag="life level", urgency="normal")
    if seen != str(s["level"]):
        _meta("level", s["level"])
    got = [a for a in s["achievements"] if a["on"]]
    told = _meta("achievements")
    if told is None:  # the first time, what's already earned is only noted
        _meta("achievements", json.dumps([a["key"] for a in got]))
    else:
        new = [a for a in got if a["key"] not in json.loads(told)]
        if new:
            _meta("achievements", json.dumps(sorted(set(json.loads(told)) | {a["key"] for a in new})))
            notify.send(cfg, "🏆 " + " · ".join(a["name"] for a in new), " ".join(a["about"] for a in new),
                        url=panel + "page/life/game", tag="life achievement", urgency="normal")
    now = datetime.now()
    behind = [q for q in s["quests"] if q["key"] != "protein_days" and not q["done"]]
    if now.weekday() == 4 and now.hour >= 18 and behind and _meta("nudged") != s["week"]:
        _meta("nudged", s["week"])
        notify.send(cfg, "The weekend is left", " · ".join(f"{q['name'].split(' ×')[0]} {q['have']}/{q['need']}" for q in behind)
                    + ". Finish them to keep your streaks.", url=panel + "page/life/game", tag="life week", urgency="normal")


# --- Card and sheets ---

def _fmt(v, unit=""):
    return f"{v:,.0f}{unit}"


def _face(s):
    """The avatar's markup: frame 0 of the sprite strip (the frame's own aspect ratio crops the rest), as the game
    does, or the drawn SVG. Built here from numbers and our own URL; remote.py prefixes its src="file/…"."""
    sp = s["sprite"]
    if sp:
        return (f'<span class="frame" style="aspect-ratio:{int(sp["w"])}/{int(sp["h"])}">'
                f'<img src="{e(sp["url"])}" alt=""></span>')
    return s["avatar"]


QUEST_ICONS = {"gym": "dumbbell", "volleyball": "ball"}


def tiles(cfg):
    """Home tiles (app.py): the avatar (two rows: level, title and XP; a tap opens the game), this week's first
    unfinished quest, and today's calories (a tap adds a meal)."""
    s = game(cfg)
    out = [{"key": "avatar", "label": f'Level {s["level"]} · {s["title"]}', "value": f'{s["into"]} / {s["span"]} XP',
            "progress": s["into"] / s["span"] if s["span"] else 0, "avatar_html": _face(s), "tint": None, "icon": None,
            "open": {"page": "life/game"}, "span": 2, "order": 50}]
    quests = s["quests"]
    q = next((q for q in quests if not q["done"]), quests[0] if quests else None)
    if q:
        out.append({"key": "quest", "label": q["name"].split(" ×")[0],
                    "value": "Done this week" if q["done"] else f'{q["have"]} of {q["need"]} this week',
                    "tint": "ok" if q["done"] else "on", "icon": QUEST_ICONS.get(q["key"], "flag"),
                    "open": {"page": "life/game"}, "span": 1, "order": 80})
    t = s["today"]
    goal = t["goals"].get("kcal")
    out.append({"key": "meal", "label": "Meal",
                "value": f'{_fmt(t["kcal"])} / {_fmt(goal)} kcal' if goal else f'{_fmt(t["kcal"])} kcal today',
                "tint": "warn" if goal and t["kcal"] > 1.1 * goal else None, "icon": "camera",
                "open": {"sheet": "life/add/meal", "title": "Add a meal"}, "span": 1, "order": 100})
    return out


def card(cfg):
    s = game(cfg)
    face = _face(s)
    out = [f'<div class="life-top"><span class="av">{face}</span><div><b>Level {s["level"]} · {e(s["title"])}</b>'
           f'{bar(s["into"] / s["span"])}<small>{s["into"]}/{s["span"]} XP to level {s["level"] + 1}</small></div></div>']
    b = s["bars"]
    out.append(row("Mood", f'{e(s["mood"]["emoji"])} {e(s["mood"]["name"])}'))
    for k, label in (("hp", "HP"), ("energy", "Energy"), ("stamina", "Stamina")):
        if b[k] is not None:  # no HP without sleep data
            out.append(row(label, f"<small>{b[k]}/100</small>"))
            out.append(bar(b[k] / 100, "bad" if b[k] < 35 else "warn" if b[k] < 60 else "ok"))
    sl = s["sleep"]
    if sl["debt_min"] is not None:
        out.append(row("Sleep debt, 7 days", f'<b>{sl["debt_min"] // 60} h {sl["debt_min"] % 60:02d}</b>'))
    if sl["bedtime_streak"]:
        out.append(row("In bed on time", f'🌙 {sl["bedtime_streak"]} nights in a row'))
    for q in s["quests"]:
        out.append(row(q["name"] + " this week", f'<small>{e(q["reward"])}</small> '
                       + pill("ok" if q["done"] else "dim", f'{q["have"]}/{q["need"]}' + (" ✓" if q["done"] else ""))))
    fire = [st["short"] for st in s["streaks"] if st["n"]]
    if fire:
        out.append(row("Streaks", "🔥 " + e(" · ".join(fire))))
    t = s["today"]
    for k, label, unit in (("kcal", "Calories", ""), ("protein", "Protein", " g")):
        goal = t["goals"].get(k)
        if goal:
            out.append(row(label, f'<b>{_fmt(t[k])}</b><small>/ {_fmt(goal, unit)}</small>'))
            out.append(bar(t[k] / goal, "warn" if k == "kcal" and t[k] > 1.1 * goal else "ok"))
        elif t[k]:
            out.append(row(label, f'<b>{_fmt(t[k], unit)}</b>'))
    out.append('<div class="duo">' + "".join(f'<button data-action="life/log/{e(a["kind"])}">{e(a["emoji"])} {e(a["label"])}</button>'
                                             for a in s["activities"])
               + f'<button class="alt" data-photo="life/meal">{icon("camera")}Meal photo</button>'
               f'<button class="alt" data-sheet="life/add/meal">{icon("pencil")}<b>Describe a meal</b></button></div>')
    for m in t["meals"]:
        dot = {"estimating": "busy", "failed": "bad"}.get(m["status"], "ok")
        sub = ("Estimating…" if m["status"] == "estimating" else "Couldn't estimate. Tap to retry." if m["status"] == "failed"
               else f'{m["kcal"]} kcal · {m["protein"]} g protein')
        out.append(f'<button class="agent" data-sheet="life/meal/{m["id"]}"><i class="dot {dot}"></i>'
                   f'<span class="who"><b>{e(m["title"])}</b><small>{e(sub)}</small></span>'
                   f'<span class="when"><small>{m["time"]}</small></span></button>')
    out.append('<div class="foot"><span><button class="mini" data-sheet="life/history/all"><b>History</b></button> '
               '<button class="mini" data-sheet="life/goals/all"><b>Goals</b></button> '
               '<button class="mini" data-sheet="life/sleep/new"><b>😴 Slept…</b></button></span>'
               '<button class="mini" data-page="life/game">🎮 Game</button></div>')
    return "".join(out)


def _meal_sheet(cfg, arg):
    r = _q("SELECT * FROM meal WHERE id = ?", (int(arg),))
    if not r:
        return '<p class="muted">That meal was removed.</p>'
    m, out = r[0], []
    if m["photo"]:
        out.append(f'<img class="cam" src="file/life/photo/{m["id"]}" alt="">')
    if m["said"]:
        out.append(f'<p class="sheet-meta">“{e(m["said"])}”</p>')
    if m["status"] == "estimating":
        out.append(f'<p class="busy">Estimating… ({ago(time.time() - (m["started"] or m["ts"]))})</p>')
    if m["error"]:
        out.append(f'<div class="alert bad">{e(m["error"])}</div>')
    if m["status"] == "failed":
        out.append(f'<button data-action="life/retry/{m["id"]}">Try again</button>')
    if m["items"]:
        for it in json.loads(m["items"]):
            out.append(row(f'{it["name"]} · {it["portion"]}',
                           f'<small>{it["kcal"]:.0f} kcal · P {it["protein"]:.0f} · C {it["carbs"]:.0f} · F {it["fat"]:.0f}</small>'))
        out.append(row("Total", f'<b>{m["kcal"]:.0f} kcal</b><small>P {m["protein"]:.0f} g · C {m["carbs"]:.0f} g · F {m["fat"]:.0f} g</small>'))
        out.append(f'<p class="muted"><small>{e(m["note"] or "")} {e(_model_name(m["model"]))}'
                   f' · {m["confidence"] or 0:.0%} sure</small></p>')
    for f in json.loads(m["fixes"]):
        out.append(f'<p class="muted"><small>You corrected ({e(f["when"])}): {e(f["text"])}</small></p>')
    if m["status"] != "estimating":
        out.append(f'<form data-post="life/fix/{m["id"]}"><textarea name="text" rows="2" placeholder="What did it get wrong? '
                   f'&quot;2 eggs, not 3&quot;, &quot;no rice&quot;, &quot;cooked in butter&quot;"></textarea>'
                   f'<button>Fix the estimate</button></form>')
    out.append(f'<div class="foot"><small class="muted">{time.strftime("%a %d %b %H:%M", time.localtime(m["ts"]))}</small>'
               f'<button class="mini" data-action="life/forget/m{m["id"]}" data-confirm="Remove this meal?">Remove meal</button></div>')
    return "".join(out)


def _model_name(model):
    """claude-sonnet-5-5 -> Sonnet 5.5"""
    name, *version = (model or "").replace("claude-", "").split("-")
    return f'{name.title()} {".".join(version)}'.strip()


def _add_sheet(cfg, arg):
    today = date.today()
    return ('<form data-post="life/meal"><textarea name="said" rows="3" placeholder="What did you eat? '
            '&quot;2 eggs, toast with butter and a coffee with milk&quot;"></textarea>'
            '<button>Estimate it</button></form>'
            '<p class="muted"><small>Or add a photo from the card. You can fix any estimate afterwards.</small></p>'
            '<h3>A session on another day</h3><form data-post="life/past" class="field-form">'
            '<label class="field">Activity<select name="kind">'
            + "".join(f'<option value="{e(k)}">{e(a["emoji"])} {e(a["label"])}</option>' for k, a in rules(cfg)["acts"].items())
            + '</select></label>'
            f'<label class="field">Day<input type="date" name="day" value="{today - timedelta(days=1)}" '
            f'min="{today - timedelta(days=60)}" max="{today}"></label><button>Log it</button></form>')


def _goals_sheet(cfg, arg):
    g = goals(cfg)
    fields = "".join(f'<label class="field">{e(label)}{f" ({unit})" if unit else ""}'
                     f'<input type="number" inputmode="numeric" min="0" step="1" name="{k}" '
                     f'value="{"" if g.get(k) is None else f"{g[k]:g}"}" placeholder="not set"></label>'
                     for k, label, unit in rules(cfg)["goal_fields"])
    return ('<p class="muted"><small>Daily targets score Nutrition: a day at 90 % of the protein goal, and a finished day '
            'within 10 % of the calorie goal. Leave one blank to skip it. Changing a goal rescores your whole history.</small></p>'
            f'<form data-post="life/goals">{fields}<button>Save goals</button></form>')


def _sleep_sheet(cfg, arg):
    today = date.today()
    return ('<p class="muted"><small>One night, by the day you woke up. It replaces what that day had, an import'
            ' included. Recovery scores 7 and 8 hours; the sleep debt and HP use your last 7 days.</small></p>'
            '<form data-post="life/sleep" class="field-form">'
            f'<label class="field">Woke up on<input type="date" name="day" value="{today}" '
            f'min="{today - timedelta(days=SLEEP_DAYS)}" max="{today}"></label>'
            '<label class="field">Hours slept<input type="number" inputmode="decimal" min="0.25" max="16" step="0.25" '
            'name="hours" placeholder="7.5"></label><button>Save the night</button></form>')


def _history_sheet(cfg, arg):
    R, g, today = rules(cfg), goals(cfg), date.today()
    acts = _q("SELECT * FROM activity WHERE day >= ? ORDER BY ts DESC", ((today - timedelta(days=42)).isoformat(),))
    meals = _q("SELECT * FROM meal WHERE day >= ? ORDER BY ts DESC", ((today - timedelta(days=14)).isoformat(),))
    out = ["<h3>Weeks</h3>"]
    for i in range(6):
        mon = _monday(today) - timedelta(days=7 * i)
        days = {(mon + timedelta(days=j)).isoformat() for j in range(7)}
        n = {k: sum(1 for a in acts if a["kind"] == k and a["day"] in days) for k in R["acts"]}
        out.append(row(f'{mon:%b %d}' + (" (this week)" if i == 0 else ""),
                       " ".join(pill("ok" if n[k] >= (g.get(k) or 0) else "dim", f'{a["emoji"] or a["label"]} {n[k]}/{g.get(k) or 0:g}')
                                for k, a in R["acts"].items())))
    out.append("<h3>Sessions</h3>")
    for a in acts[:30]:
        spec = R["acts"].get(a["kind"], {"emoji": "", "label": a["kind"].title()})
        out.append(row(f'{spec["emoji"]} {spec["label"]} · {date.fromisoformat(a["day"]):%a %d %b}'.strip(),
                       f'<button class="mini" data-action="life/forget/a{a["id"]}" data-confirm="Remove this session?">remove</button>'))
    if not acts:
        out.append('<p class="muted">None yet.</p>')
    out.append("<h3>Meals, last two weeks</h3>")
    for m in meals:
        out.append(f'<button class="agent" data-sheet="life/meal/{m["id"]}"><i class="dot ok"></i><span class="who">'
                   f'<b>{e(m["title"] or "Meal")}</b><small>{m["kcal"] or 0:.0f} kcal · {m["protein"] or 0:.0f} g protein</small></span>'
                   f'<span class="when"><small>{time.strftime("%a %H:%M", time.localtime(m["ts"]))}</small></span></button>')
    if not meals:
        out.append('<p class="muted">None yet.</p>')
    return "".join(out)


# --- Actions ---

def log(cfg, arg, body):
    spec = rules(cfg)["acts"].get(arg)
    if not spec:
        raise ValueError("Unknown activity.")
    recent = _q("SELECT ts FROM activity WHERE kind = ? AND ts > ?", (arg, time.time() - 600))
    if recent:
        return f"{spec['label']} was already logged {ago(time.time() - recent[0]['ts'])} ago."
    return _add_session(cfg, arg, date.today())


def past(cfg, arg, body):
    kind, today = body.get("kind"), date.today()
    if kind not in rules(cfg)["acts"]:
        raise ValueError("Unknown activity.")
    day = date.fromisoformat(str(body.get("day")))
    if not today - timedelta(days=60) <= day <= today:
        raise ValueError("Pick a day in the last 60 days.")
    return _add_session(cfg, kind, day)


def _add_session(cfg, kind, day):
    R = rules(cfg)
    before = game(cfg)["level"]
    ts = time.time() if day == date.today() else datetime.combine(day, datetime.min.time()).timestamp() + 12 * 3600
    _w("INSERT INTO activity(ts, day, kind) VALUES(?, ?, ?)", (ts, day.isoformat(), kind))
    s = game(cfg)
    spec = R["acts"][kind]
    q = next((q for q in s["quests"] if q["key"] == kind), None)
    msg = f"{spec['label']} logged · +{spec['xp']} {dict((k[0], k[1]) for k in R['skills'])[spec['skill']]}"
    if q and day >= _monday(date.today()):
        msg += f" · {q['have']}/{q['need']} this week" + (" ✓" if q["done"] else "")
    if s["level"] > before:
        msg += f". Level up: {s['level']} · {s['title']}!"
    return msg


def meal(cfg, arg, body):
    said = str(body.get("said") or "").strip()[:500]
    image = str(body.get("image") or "")
    data = None
    if image:
        head, _, b64 = image.partition(",")
        if head != "data:image/jpeg;base64":
            raise ValueError("Send the photo as a JPEG.")
        data = base64.b64decode(b64, validate=True)
        if not data.startswith(b"\xff\xd8"):
            raise ValueError("That isn't a JPEG.")
    if not data and not said:
        raise ValueError("Add a photo or say what you ate.")
    now = time.time()
    mid = _w("INSERT INTO meal(ts, day, said, photo, status) VALUES(?, ?, ?, ?, 'estimating')",
             (now, date.today().isoformat(), said, 1 if data else 0))
    if data:
        fd = os.open(_photo(mid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    _start(cfg, mid)
    return "Meal saved. Estimating it…"


def fix(cfg, arg, body):
    text = str(body.get("text") or "").strip()[:500]
    m = _q("SELECT fixes, status FROM meal WHERE id = ?", (int(arg),))
    if not m or not text:
        raise ValueError("Nothing to fix.")
    if m[0]["status"] == "estimating":
        return "Still estimating. Try again in a moment."
    fixes = json.loads(m[0]["fixes"]) + [{"text": text, "when": time.strftime("%Y-%m-%d %H:%M")}]
    _w("UPDATE meal SET fixes = ? WHERE id = ?", (json.dumps(fixes, ensure_ascii=False), int(arg)))
    _start(cfg, int(arg), text)
    return "Re-estimating with your correction…"


def retry(cfg, arg, body):
    _start(cfg, int(arg))
    return "Estimating again…"


def forget(cfg, arg, body):
    kind, rid = arg[0], int(arg[1:])
    if kind == "a":
        _w("DELETE FROM activity WHERE id = ?", (rid,))
        return "Session removed."
    _w("DELETE FROM meal WHERE id = ?", (rid,))
    try:
        os.remove(_photo(rid))
    except FileNotFoundError:
        pass
    return "Meal removed."


def set_goals(cfg, arg, body):
    for k, label, _ in rules(cfg)["goal_fields"]:
        v = str(body.get(k, "")).strip()
        if not v:
            _w("DELETE FROM goal WHERE key = ?", (k,))
            continue
        n = float(v)
        if not 0 <= n <= 20000:
            raise ValueError(f"{label}: {v} is out of range.")
        _w("INSERT INTO goal(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, n))
    return "Goals saved. Your history was rescored."


def log_sleep(cfg, arg, body):
    # An XP source (Recovery, and the sleep bars) that anyone with the hub token or at the Switch can reach:
    # an accepted risk on a home panel. The 7-day limit keeps it from rewriting old history.
    today = date.today()
    day = date.fromisoformat(str(body.get("day") or today.isoformat()))
    if not today - timedelta(days=SLEEP_DAYS) <= day <= today:
        raise ValueError(f"Pick a day in the last {SLEEP_DAYS} days.")
    try:
        hours = float(body.get("hours"))
    except (TypeError, ValueError):
        raise ValueError("Say how many hours you slept.") from None
    if not 0 < hours <= 16:
        raise ValueError("Hours slept go from 0 to 16.")
    minutes = round(hours * 60)
    # Typed by hand wins: it replaces the night an import (or an earlier entry) put on that day.
    _w("INSERT INTO sleep(day, minutes, source) VALUES(?, ?, 'manual') ON CONFLICT(day) DO UPDATE SET"
       " minutes = excluded.minutes, deep = NULL, rem = NULL, score = NULL, source = 'manual'", (day.isoformat(), minutes))
    return f"Slept {minutes // 60} h {minutes % 60:02d} · night before {day:%a %d %b} saved."


def equip(cfg, arg, body):
    """Wear an unlocked title or cosmetic (slot: title, hat, cape, aura); an empty item takes it off."""
    if arg not in rpg.SLOTS:
        raise ValueError("Unknown slot.")
    item = str(body.get("item") or "").strip()
    s = game(cfg)
    if item and not rpg.unlocked(arg, item, s["titles"], s["cosmetics"]):
        raise ValueError("That isn't unlocked yet.")
    _meta(f"equip.{arg}", item)
    if not item:
        return f"Took off the {arg}."
    name = next(x["name"] for x in s["titles"] + s["cosmetics"] if x["key"] == item)
    return f"Title: {name}." if arg == "title" else f"Wearing the {name.lower()}."


def sprite_file(cfg, arg):
    R = rules(cfg)
    if arg != "casual" and arg not in R["acts"]:
        raise ValueError("No such outfit.")
    with open(os.path.join(R["sprites"], f"{arg}.png"), "rb") as f:
        return "image/png", f.read(), True  # the URL carries the file's version


def photo_file(cfg, arg):
    with open(_photo(int(arg)), "rb") as f:
        return "image/jpeg", f.read(), True  # a meal's photo never changes


def _party():
    """The desk's agents as a party (modules/bridge.py), while fresh: STATE["party"] = {"at", "members"}."""
    p = STATE.get("party")
    if not isinstance(p, dict) or not isinstance(p.get("members"), list):
        return []
    at = p.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= time.time() - at < PARTY_FRESH_S:
        return []
    return [m for m in p["members"] if isinstance(m, dict)][:20]


def _coop():
    """The co-op board modules/coop.py keeps in STATE["coop"], already in its final shape {"at", "board", "goal",
    "total", "rows": [{"name", "level", "title", "done", "need", "boss_defeated", "me"}]} with your row first.
    None without friends (coop removes the key), or when the board is older than COOP_FRESH_S."""
    c = STATE.get("coop")
    if not isinstance(c, dict) or not isinstance(c.get("rows"), list):
        return None
    at = c.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= time.time() - at < COOP_FRESH_S:
        return None
    return {"board": c.get("board"), "goal": c.get("goal"), "total": c.get("total"),
            "rows": [r for r in c["rows"] if isinstance(r, dict)]}


def state_file(cfg, arg):
    s = game(cfg)
    return "application/json", json.dumps({**s, "party": _party(), "coop": _coop()}).encode()


# The full-screen game: the docked Switch on a TV (Joy-Cons), or any browser. It reads the state
# through whichever panel serves it (/page/<module>/game, e.g. pi.life on the Switch). Read on every
# request, so editing the file needs no restart; app.py fills in %%KIOSK_JS%%.
GAME_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "life", "game.html")


def game_page(cfg, arg):
    try:
        with open(GAME_HTML, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Life RPG</title>'
                '<p style="font-family:monospace">The game page is missing (admin/life/game.html).'
                ' <a href="/">Back to the panel</a></p></html>')


ACTIONS = {"log": log, "past": past, "meal": meal, "fix": fix, "retry": retry, "forget": forget, "goals": set_goals,
           "sleep": log_sleep, "equip": equip}
SHEETS = {"meal": _meal_sheet, "add": _add_sheet, "goals": _goals_sheet, "history": _history_sheet, "sleep": _sleep_sheet}
FILES = {"photo": photo_file, "state": state_file, "sprite": sprite_file}
PAGES = {"game": game_page}

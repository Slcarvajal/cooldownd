"""Life RPG: sessions of any sport, meals and sleep logged as events, and a game built from them.

Only events are stored (~/.local/share/homelab-admin/life.db, 600). XP, levels, streaks and quests are
computed from them on every read (the rules below), so changing a rule rescores the whole history.

- Activities (config "life.activities"; gym and volleyball by default): one tap on the card, or the
  Joy-Cons on the game screen. Each one feeds a skill, and its sessions a week make a weekly quest.
- Meals: a photo or a few words. Claude Haiku 5.5 estimates the items and macros in the background;
  Sonnet 5.5 takes over when Haiku is unsure, and for every correction. Corrections are kept and sent
  with later estimates, so it learns your portions. The key is ~/.config/homelab-admin/anthropic-key (600).
- Sleep: the `sleep` table (one row per night), filled by a data source such as a Fitbit import; the
  Recovery skill scores it.
- PAGES["game"] is the full-screen game. The Switch shows it when docked to a TV (config "game").
- Phone alerts (modules/notify.py) on a level up, and on Friday evening when a weekly quest is behind.

Goals (calorie and macro targets, sessions a week) are set from the card and kept in the database.

config "life" (every key optional):
  "name"          shown in the game, and in the meal prompt ("Ana")
  "food_context"  where the meals are, for ambiguous dishes ("Mexico City: tacos, chilaquiles, pozole…")
  "activities"    {kind: {"label", "emoji", "skill", "xp", "per_week", "quest_bonus"}}
  "skills"        {skill: {"label", "color", "perks": [5 names]}}; Nutrition and Recovery are built in
  "sprites"       folder of avatar strips (life/make_sprites.py); without one, a drawn pixel avatar"""
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

from modules import notify
from modules.common import ago, bar, e, pill, row

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
}
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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meal(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, said TEXT NOT NULL DEFAULT '',
  photo INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, started REAL, title TEXT, items TEXT, kcal REAL, protein REAL,
  carbs REAL, fat REAL, confidence REAL, note TEXT, model TEXT, error TEXT, fixes TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sleep(day TEXT PRIMARY KEY, minutes INTEGER NOT NULL, deep INTEGER, rem INTEGER, score INTEGER,
  source TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS goal(key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS activity_day ON activity(day);
CREATE INDEX IF NOT EXISTS meal_day ON meal(day);
"""
_ready = threading.Lock()
_made = []
_ver = [0]  # bumped on every write here; with the file stamps, the computed game is cached until a change
_cache = {}

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
            with closing(sqlite3.connect(DB_PATH)) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA_SQL)
            os.chmod(DB_PATH, 0o600)
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


def rules(cfg):
    """The game's shape from config "life": activities, skills (activity skills first, then the built-in
    Nutrition and Recovery), weekly quests and goals."""
    c = (cfg or {}).get("life") or {}
    acts = {}
    for kind, a in (c.get("activities") or DEFAULT_ACTIVITIES).items():
        acts[kind] = {"label": kind.replace("_", " ").title(), "emoji": "", "skill": kind, "xp": 50, "per_week": 0,
                      "quest_bonus": 0, **a}
    keys = list(dict.fromkeys([a["skill"] for a in acts.values()] + ["nutrition", "recovery"]))
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
            "sprites": os.path.expanduser(c.get("sprites") or SPRITES), "key": json.dumps(c, sort_keys=True)}


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
    R = rules(cfg)
    today = date.today()
    key = (_ver[0], _stamp(), today, R["key"])
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
    for s in _q("SELECT day, minutes FROM sleep"):
        xp = sum(x for need, x in XP_SLEEP if s["minutes"] >= need)
        if xp:
            events.append((datetime.fromisoformat(s["day"] + "T07:00").timestamp(), "recovery", xp,
                           f"Slept {s['minutes'] // 60} h {s['minutes'] % 60:02d}"))
    for monday, got in weeks.items():
        for q, (skill, xp) in R["bonus"].items():
            need = int(g.get(q) or 0)
            if need and xp and len(got.get(q, [])) >= need:
                events.append((sorted(got[q])[need - 1], skill, xp, f"Quest: {R['quests'][q]} ×{need}"))

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
                       "reward": f"+{xp} {names[skill]}" if xp else ""})
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
        "feed": [{"when": time.strftime("%a %H:%M", time.localtime(ts)), "text": text, "xp": xp, "skill": names[skill]}
                 for ts, skill, xp, text in sorted(events, key=lambda ev: -ev[0])[:10]],
    }
    did = [a["kind"] for a in acts if a["day"] == t]
    out["outfit"] = did[-1] if did and sprite(R, did[-1]) else "casual"
    out["sprite"] = sprite(R, out["outfit"])
    out["avatar"] = None if out["sprite"] else avatar(out)
    _cache.update(key=key, game=out)
    return out


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
            "W": "#ef4444", "G": "#f59e0b", "C": "#b91c1c", "Y": "#facc15"}


def avatar(state):
    top = max(state["skills"], key=lambda k: k["xp"])
    grid = [list(r) for r in _SPRITE]
    if state["level"] >= 10:  # a cape behind the body
        for y in range(8, 15):
            for x in (2, 13):
                if grid[y][x] == ".":
                    grid[y][x] = "C"
    if state["level"] >= 3:
        grid[2] = list("....WWWWWWWW....")
    if state["level"] >= 5:
        grid[12] = list("....GGGGGGGG....")
    if state["level"] >= 20:
        grid[0] = list(".....Y.YY.Y.....")
    colors = {**_PALETTE, "B": top["color"] if top["xp"] else "#6d4fd8"}
    rects = []
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
    now = datetime.now()
    behind = [q for q in s["quests"] if q["key"] != "protein_days" and not q["done"]]
    if now.weekday() == 4 and now.hour >= 18 and behind and _meta("nudged") != s["week"]:
        _meta("nudged", s["week"])
        notify.send(cfg, "The weekend is left", " · ".join(f"{q['name'].split(' ×')[0]} {q['have']}/{q['need']}" for q in behind)
                    + ". Finish them to keep your streaks.", url=panel + "page/life/game", tag="life week", urgency="normal")


# --- Card and sheets ---

def _fmt(v, unit=""):
    return f"{v:,.0f}{unit}"


def card(cfg):
    s = game(cfg)
    face = f'<img src="{s["sprite"]["url"]}" alt="">' if s["sprite"] else s["avatar"]
    out = [f'<div class="life-top"><span class="av">{face}</span><div><b>Level {s["level"]} · {e(s["title"])}</b>'
           f'{bar(s["into"] / s["span"])}<small>{s["into"]}/{s["span"]} XP to level {s["level"] + 1}</small></div></div>']
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
               + '<button data-photo="life/meal">📷 Meal photo</button>'
               '<button class="alt" data-sheet="life/add/meal"><b>✍️ Describe a meal</b></button></div>')
    for m in t["meals"]:
        dot = {"estimating": "busy", "failed": "bad"}.get(m["status"], "ok")
        sub = ("Estimating…" if m["status"] == "estimating" else "Couldn't estimate. Tap to retry." if m["status"] == "failed"
               else f'{m["kcal"]} kcal · {m["protein"]} g protein')
        out.append(f'<button class="agent" data-sheet="life/meal/{m["id"]}"><i class="dot {dot}"></i>'
                   f'<span class="who"><b>{e(m["title"])}</b><small>{e(sub)}</small></span>'
                   f'<span class="when"><small>{m["time"]}</small></span></button>')
    out.append('<div class="foot"><span><button class="mini" data-sheet="life/history/all"><b>History</b></button> '
               '<button class="mini" data-sheet="life/goals/all"><b>Goals</b></button></span>'
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


def sprite_file(cfg, arg):
    R = rules(cfg)
    if arg != "casual" and arg not in R["acts"]:
        raise ValueError("No such outfit.")
    with open(os.path.join(R["sprites"], f"{arg}.png"), "rb") as f:
        return "image/png", f.read(), True  # the URL carries the file's version


def photo_file(cfg, arg):
    with open(_photo(int(arg)), "rb") as f:
        return "image/jpeg", f.read(), True  # a meal's photo never changes


def state_file(cfg, arg):
    return "application/json", json.dumps(game(cfg)).encode()


def game_page(cfg, arg):
    return GAME_PAGE


ACTIONS = {"log": log, "past": past, "meal": meal, "fix": fix, "retry": retry, "forget": forget, "goals": set_goals}
SHEETS = {"meal": _meal_sheet, "add": _add_sheet, "goals": _goals_sheet, "history": _history_sheet}
FILES = {"photo": photo_file, "state": state_file, "sprite": sprite_file}
PAGES = {"game": game_page}

# The full-screen game: the docked Switch on a TV (Joy-Cons), or any browser. It reads the state
# through whichever panel serves it (/page/<module>/game, e.g. pi.life on the Switch).
GAME_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1022"><link rel="apple-touch-icon" href="/icon.png">
<title>Life RPG</title><style>
:root{--bg:#0d1022;--panel:#161a33;--panel2:#1d2244;--line:#2c3366;--fg:#eef0ff;--mute:#9aa0c8;--gold:#f5c542;
--mono:ui-monospace,"DejaVu Sans Mono","SF Mono",Menlo,monospace}
*{box-sizing:border-box}
html{font-size:clamp(14px,1.05vw,22px)}
html,body{margin:0;background:var(--bg);color:var(--fg);font:1rem/1.4 var(--mono);overflow-x:hidden}
body{min-height:100vh;padding:calc(env(safe-area-inset-top) + 1.2rem) 16px calc(env(safe-area-inset-bottom) + 5.5rem);
background-image:linear-gradient(rgba(44,51,102,.35) 1px,transparent 1px),linear-gradient(90deg,rgba(44,51,102,.35) 1px,transparent 1px);background-size:3rem 3rem}
a.back{color:var(--mute);text-decoration:none;font-size:.85rem}
main{display:grid;gap:1.1rem;grid-template-columns:minmax(0,1fr);max-width:1800px;margin:.6rem auto 0}
@media (min-width:1000px){main{grid-template-columns:minmax(0,.9fr) minmax(0,1.3fr) minmax(0,1.1fr)}}
section{background:var(--panel);border:2px solid var(--line);border-radius:6px;padding:1.1rem;box-shadow:0 0 0 2px #090b18,6px 6px 0 #090b18;min-width:0}
h2{font-size:.8rem;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);margin:0 0 .8rem}
h2:not(:first-child){margin-top:1.4rem}
.hero{text-align:center}
#avatar{width:min(17rem,70vw);aspect-ratio:166/161;margin:0 auto;background:radial-gradient(ellipse 45% 7% at 50% 94%,rgba(0,0,0,.45),transparent),var(--panel2);
border:2px solid var(--line);border-radius:6px;position:relative;overflow:hidden}
#drawn svg{position:absolute;inset:10%;width:80%;height:80%;animation:breathe 2.6s ease-in-out infinite}
#logs{display:contents}
#avatar i{position:absolute;inset:5%;background-repeat:no-repeat;background-size:1600% 100%;image-rendering:pixelated;animation:breathe 2.6s ease-in-out infinite}
@keyframes breathe{50%{transform:translateY(-1.2%) scaleY(1.01)}}
h1{font-size:1.6rem;margin:.6rem 0 .1rem;letter-spacing:.04em}
#lvl{font-size:1.15rem;color:var(--gold)}
.xp{height:1rem;background:#090b18;border:2px solid var(--line);margin:.6rem 0 .3rem;position:relative}
.xp i{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,var(--gold));transition:width .6s}
small,.mute{color:var(--mute);font-size:.8rem}
.streaks{display:grid;gap:.4rem;margin-top:1rem;text-align:left}
.streak{display:flex;justify-content:space-between;background:var(--panel2);padding:.45rem .7rem;border-radius:4px}
.skill{margin-bottom:1.1rem}
.skill header{display:flex;justify-content:space-between;align-items:baseline;font-weight:700}
.skill .xp{height:.7rem;margin:.35rem 0}
.tree{display:flex;align-items:flex-start;margin-top:.45rem}
.node{flex:1;min-width:0;padding:0 .15rem;overflow-wrap:anywhere;display:grid;justify-items:center;gap:.3rem;text-align:center;position:relative;font-size:.7rem;color:var(--mute)}
.node::before{content:"";position:absolute;top:.65rem;left:-50%;width:100%;height:2px;background:var(--line);z-index:0}
.node:first-child::before{display:none}
.node i{width:1.3rem;height:1.3rem;border:2px solid var(--line);background:#090b18;transform:rotate(45deg);z-index:1}
.node.on{color:var(--fg)}.node.on i{background:var(--c);border-color:var(--c);box-shadow:0 0 .6rem var(--c)}
.node.on::before{background:var(--c)}
.quest{display:grid;grid-template-columns:1.6rem 1fr auto;gap:.6rem;align-items:center;background:var(--panel2);padding:.55rem .7rem;border-radius:4px;margin-bottom:.45rem}
.quest .box{width:1.2rem;height:1.2rem;border:2px solid var(--mute);display:grid;place-items:center;font-size:.8rem}
.quest.done .box{border-color:#4ade80;color:#4ade80}.quest.done b{text-decoration:line-through;color:var(--mute)}
.fuel{display:grid;grid-template-columns:5.5rem 1fr 6.5rem;gap:.5rem;align-items:center;margin-bottom:.35rem;font-size:.85rem}
.fuel .xp{margin:0;height:.7rem}.fuel span:last-child{text-align:right}
.feed div{display:grid;grid-template-columns:5.5rem 1fr auto;gap:.5rem;padding:.3rem 0;border-top:1px dashed var(--line);font-size:.85rem}
.feed div:first-child{border-top:0}.feed b{color:var(--gold)}
footer{position:fixed;left:0;right:0;bottom:0;display:flex;gap:.8rem;align-items:center;justify-content:center;flex-wrap:wrap;
padding:.8rem 16px calc(env(safe-area-inset-bottom) + .8rem);background:rgba(13,16,34,.92);border-top:2px solid var(--line)}
footer button{font:700 1rem var(--mono);color:var(--fg);background:var(--panel2);border:2px solid var(--line);border-radius:4px;padding:.7rem 1.2rem;cursor:pointer}
footer button.sel{border-color:var(--gold);box-shadow:0 0 0 2px var(--gold)}
footer button.armed{background:var(--gold);color:#1a1300;border-color:var(--gold)}
#hint{flex-basis:100%;text-align:center}
#toast{position:fixed;left:50%;top:1.2rem;transform:translateX(-50%);background:var(--gold);color:#1a1300;font-weight:700;padding:.8rem 1.4rem;
border-radius:4px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9;max-width:90vw;text-align:center}
#toast.show{opacity:1}
#levelup{position:fixed;inset:0;display:grid;place-items:center;background:rgba(9,11,24,.85);font-size:3rem;font-weight:800;color:var(--gold);
letter-spacing:.1em;opacity:0;pointer-events:none;transition:opacity .4s;text-align:center;z-index:8}
#levelup.show{opacity:1}
</style></head><body>
<a class="back" id="back" href="/">← Control center</a>
<main>
  <section class="hero"><div id="avatar"><i></i><span id="drawn"></span></div><h1 id="name"></h1><div id="lvl">Loading…</div>
    <div class="xp"><i id="xpbar" style="width:0"></i></div><small id="xp"></small>
    <div class="streaks" id="streaks"></div></section>
  <section><h2>Skill trees</h2><div id="skills"></div></section>
  <section><h2>This week's quests</h2><div id="quests"></div><h2>Today's fuel</h2><div id="fuel"></div>
    <h2>Recent XP</h2><div class="feed" id="feed"></div></section>
</main>
<footer><div id="logs"></div>
  <small id="hint">Tap twice to log</small></footer>
<div id="toast"></div><div id="levelup"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const MOD = location.pathname.split('/')[2] || 'life';  // "pi.life" when the Switch's panel serves it
const KIOSK = new URLSearchParams(location.search).get('kiosk');  // "dock" when opened because it was docked
%%KIOSK_JS%%
let lastLevel = null;
function toast(m) { const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 3500) }
const pct = (a, b) => Math.max(0, Math.min(100, b ? a / b * 100 : 0)).toFixed(1) + '%';

function draw(S) {
  avatar(S); logs(S.activities);
  $('#name').textContent = S.name || 'Player';
  $('#lvl').textContent = `Level ${S.level} · ${S.title}`;
  $('#xpbar').style.width = pct(S.into, S.span);
  $('#xp').textContent = `${S.into} / ${S.span} XP · ${S.xp} total`;
  $('#streaks').innerHTML = S.streaks.map(s => `<div class="streak"><span>${esc(s.name)}</span><b>${s.n ? '🔥 ' + s.n : '—'}</b></div>`).join('');
  $('#skills').innerHTML = S.skills.map(k => `<div class="skill" style="--c:${k.color}">
    <header><span style="color:${k.color}">${esc(k.name.toUpperCase())}</span><span>Lv ${k.level} <small>${k.into}/${k.span}</small></span></header>
    <div class="xp"><i style="width:${pct(k.into, k.span)};background:${k.color}"></i></div>
    <div class="tree">${k.perks.map(p => `<div class="node${p.on ? ' on' : ''}"><i></i>${esc(p.name)}<small>Lv ${p.at}</small></div>`).join('')}</div></div>`).join('');
  $('#quests').innerHTML = S.quests.map(q => `<div class="quest${q.done ? ' done' : ''}"><span class="box">${q.done ? '✓' : ''}</span>
    <span><b>${esc(q.name)}</b><br><small>${esc(q.reward)}</small></span><span>${q.have}/${q.need}</span></div>`).join('')
    || '<small>Set your goals on the control center.</small>';
  const t = S.today, rows = [['Calories', 'kcal', ''], ['Protein', 'protein', ' g'], ['Carbs', 'carbs', ' g'], ['Fat', 'fat', ' g']];
  $('#fuel').innerHTML = rows.map(([label, k, u]) => `<div class="fuel"><span>${label}</span><div class="xp"><i style="width:${pct(t[k], t.goals[k])};background:#4ade80"></i></div>
    <span>${t[k]}${t.goals[k] ? ' / ' + Math.round(t.goals[k]) : ''}${u}</span></div>`).join('')
    + (t.meals.length ? '' : '<small>No meals logged today.</small>');
  $('#feed').innerHTML = S.feed.map(f => `<div><small>${esc(f.when)}</small><span>${esc(f.text)}</span><b>+${f.xp} ${esc(f.skill.slice(0, 3).toUpperCase())}</b></div>`).join('')
    || '<small>Log a session or a meal to start.</small>';
  if (lastLevel !== null && S.level > lastLevel) { cheer(); const l = $('#levelup'); l.innerHTML = `LEVEL UP<br>${S.level} · ${esc(S.title)}`; l.classList.add('show'); setTimeout(() => l.classList.remove('show'), 4000) }
  lastLevel = S.level;
}
async function load() {
  try { const r = await fetch('/file/' + MOD + '/state/now', {cache: 'no-store'}); if (!r.ok) throw 0; draw(await r.json()) }
  catch (e) { $('#lvl').textContent = "Can't reach the panel" }
}
load(); setInterval(load, 5000);

// The avatar: one 16-frame strip per outfit (0-3 front: stand, step, step, its move; 15 celebrating).
// Mostly standing and breathing; now and then it shifts its weight, and every few beats does its move.
let sprite = null, frame = 0, busyUntil = 0;
function show(f) { frame = f; const i = $('#avatar i'); i.style.backgroundPosition = (f / 15 * 100) + '% 0' }
function avatar(S) {
  if (!S.sprite) {  // no sprite strips: the drawn pixel avatar
    if (sprite !== S.avatar) { sprite = S.avatar; $('#avatar i').style.backgroundImage = 'none'; $('#drawn').innerHTML = S.avatar }
    return;
  }
  if (sprite === S.sprite.url) return;
  $('#drawn').innerHTML = '';
  sprite = S.sprite.url; $('#avatar i').style.backgroundImage = `url("/${S.sprite.url.replace(/^file\/life\//, 'file/' + MOD + '/')}")`; show(0);
}
function play(seq, ms) { busyUntil = Date.now() + seq.length * ms; seq.forEach((f, k) => setTimeout(() => show(f), k * ms)) }
function cheer() { play([15, 15, 15, 15, 15, 0], 500) }
let beat = 0;
setInterval(() => {
  if (Date.now() < busyUntil) return;
  beat++;
  if (beat % 4 === 0) play([3, 3, 3, 0], 450);                 // its move: the coffee, the dumbbell, the ball
  else if (beat % 2 === 0) play([1, 0, 2, 0], 220);            // a small shift of weight
}, 3500);

// Logging takes two presses (A twice, or two taps) so a stray one never counts.
let buttons = [], shown = '', sel = 0, armed = null;
function logs(list) {  // one button per activity in config "life.activities"
  const key = JSON.stringify(list); if (key === shown) return; shown = key;
  $('#logs').innerHTML = list.map(a => `<button data-kind="${esc(a.kind)}" data-label="${esc(a.label.toLowerCase())}">${esc(a.emoji)} Log ${esc(a.label.toLowerCase())}</button>`).join('');
  buttons = [...document.querySelectorAll('#logs button')]; sel = Math.min(sel, Math.max(0, buttons.length - 1));
  buttons.forEach(b => b.onclick = () => press(b)); mark();
}
function mark() { buttons.forEach((b, i) => { b.classList.toggle('sel', KIOSK && i === sel); b.classList.toggle('armed', b === armed) }) }
async function press(b) {
  if (armed !== b) { armed = b; clearTimeout(press.t); press.t = setTimeout(() => { armed = null; mark() }, 4000); mark(); return toast((KIOSK ? 'Press A' : 'Tap') + ' again to log ' + b.dataset.label) }
  armed = null; mark();
  const r = await fetch('/action/' + MOD + '/log/' + b.dataset.kind, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json().catch(() => ({message: 'Request failed'}));
  toast(d.message); if (r.ok && / logged/.test(d.message)) cheer(); load();
}

if (KIOSK) {
  $('#back').hidden = true;
  $('#hint').textContent = 'Joy-Con: ↑↓ choose · A twice to log · + control center';
  mark();
  let prev = {};
  setInterval(() => {
    const gp = [...(navigator.getGamepads ? navigator.getGamepads() : [])].find(g => g && g.connected), map = gp && padMap(gp);
    if (!map) return;
    for (const [name, i] of Object.entries(map)) {
      const down = !!(gp.buttons[i] && gp.buttons[i].pressed);
      if (down && !prev[name]) {
        if (name === 'up' || name === 'down') { sel = (sel + (name === 'down' ? 1 : buttons.length - 1)) % buttons.length; armed = null; mark() }
        else if (name === 'a' && buttons[sel]) press(buttons[sel]);
        else if (name === 'b') { armed = null; mark() }
        else if (name === 'plus') location.href = '/?stay=1';
      }
      prev[name] = down;
    }
  }, 40);
  // undocked again: back to the touchscreen panel
  if (KIOSK === 'dock') setInterval(() => { if (screen.width < DOCK_WIDTH) location.href = '/' }, 2000);
  // an agent that starts waiting opens on the panel, as it would there (agents already waiting don't)
  let seen = null;
  setInterval(async () => {
    try {
      const d = await (await fetch('/live', {cache: 'no-store'})).json(), keys = (d.pager || []).map(a => a.key);
      const fresh = seen && (d.pager || []).find(a => !seen.includes(a.key));
      seen = keys;
      if (fresh) location.href = '/?stay=1#' + new URLSearchParams({sheet: fresh.sheet, title: '🔔 ' + fresh.title + ' · ' + fresh.project});
    } catch (e) {}
  }, 5000);
}
</script></body></html>"""

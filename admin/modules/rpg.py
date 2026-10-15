"""Life RPG rules that go beyond sessions, meals and sleep: bars, mood, the weekly boss, achievements,
titles, cosmetics, bedtime nights, sleep debt, the daily standup, and the XP that the desk's bridge
events earn. Not a card: modules/life.py calls these on every read, so like the rest of the game nothing
here is stored and a changed number rescores the history.

Every function is pure: the day or the moment comes in as a parameter (today, now), never from the clock.
Bridge rows are dicts {"ts", "kind", "data"} from life's `bridge` table (events pulled from the desk's
outbox by modules/bridge.py). The XP they give, and the boss reward, only count from the first day the
bridge delivered anything, so turning these rules on never rescores the past.

- An agent answered while away from the desk: 5 Balance XP, the first 10 a day.
- A break after a sit of 25 min or more: 10 Balance XP, the first 8 a day; 15 more when the agents were
  all busy (the desk's nudge window).
- A night on time (no desk activity between bedtime and wake, config "night", modules/night.py): 15
  Recovery XP. A night is judged only after its wake, once the desk has reported back (see nights()), and
  only from the night of the first bridge data.
- A weekly boss, picked by ISO week: every weekly quest done in that week (Monday to Sunday 23:59)
  defeats it, for 150 XP that feed no skill."""
from datetime import date, datetime, timedelta

from modules import night

XP_AWAY, AWAY_PER_DAY = 5, 10
XP_BREAK, BREAKS_PER_DAY, BREAK_SIT_MIN = 10, 8, 25
XP_NUDGE = 15
XP_NIGHT = 15
NIGHT_WAIT_S = 6 * 3600  # a night the desk hasn't reported back on is judged anyway this long after its wake
XP_BOSS = 150
TEXT_AWAY = "Answered an agent from afar"
TEXT_BREAK = "Break after a long sit"
TEXT_NUDGE = "Break while the agents worked"
TEXT_NIGHT = "In bed on time"

BOSSES = [("Couch Golem", "🛋️"), ("Sugar Wraith", "🍩"), ("Procrastination Hydra", "🐉"), ("Doomscroll Kraken", "🦑"),
          ("Snooze Ogre", "😴"), ("Slouch Troll", "🧌"), ("Takeout Dragon", "🥡"), ("Burnout Lich", "💀")]

# (key, name, about). life.game() works out when each was first earned.
ACHIEVEMENTS = [
    ("first_session", "First steps", "Log your first session."),
    ("food_log", "Food logger", "Get your first meal estimated."),
    ("well_rested", "Well rested", "Sleep 8 hours in one night."),
    ("protein_week", "Protein week", "Hit the protein goal 7 days in a row."),
    ("boss_slayer", "Boss slayer", "Finish every weekly quest and defeat the week's boss."),
    ("level_5", "Level 5", "Reach level 5."),
    ("level_10", "Level 10", "Reach level 10."),
    ("remote", "Remote control", "Answer an agent while away from the desk."),
    ("stretch", "Limber", "Take 10 breaks after long sits."),
    ("lights_out", "Lights out", "Be in bed on time 7 nights in a row."),
]
# Titles earned with achievements, after the level titles: achievement key -> (title key, name)
ACHIEVEMENT_TITLES = {"boss_slayer": ("slayer", "Boss Slayer"), "protein_week": ("paladin", "Protein Paladin"),
                      "remote": ("remote", "Remote Commander"), "stretch": ("limber", "Limber"),
                      "lights_out": ("warden", "Night Warden")}

# Cosmetics: (key, slot, name, unlock). unlock is ("level", n) or ("achievement", key). Every one draws on
# the pixel avatar (HAT_ART, CAPE_ART, AURA_ART); on sprite strips the game page only draws auras.
COSMETICS = [
    ("headband", "hat", "Headband", ("level", 3)),
    ("crown", "hat", "Crown", ("level", 20)),
    ("nightcap", "hat", "Night cap", ("achievement", "lights_out")),
    ("horns", "hat", "Boss horns", ("achievement", "boss_slayer")),
    ("cape", "cape", "Red cape", ("level", 10)),
    ("royal", "cape", "Royal cape", ("level", 16)),
    ("leaf", "cape", "Leaf cape", ("achievement", "protein_week")),
    ("spark", "aura", "Spark aura", ("level", 8)),
    ("calm", "aura", "Calm aura", ("achievement", "lights_out")),
    ("ember", "aura", "Ember aura", ("achievement", "boss_slayer")),
    ("zen", "aura", "Zen aura", ("achievement", "stretch")),
]
SLOTS = ("title", "hat", "cape", "aura")
# Pixel art for the drawn 16×16 avatar (life._SPRITE): rows replaced by a hat, the cape's color letter,
# the aura's color. Letters are colors in life._PALETTE.
HAT_ART = {"headband": {2: "....WWWWWWWW...."}, "crown": {0: ".....Y.YY.Y....."},
           "nightcap": {0: ".........Nw.....", 1: "....NNNNNNN.....", 2: "....NNNNNNNN...."},
           "horns": {0: "...R........R...", 1: "....RHHHHHHR...."}}
CAPE_ART = {"cape": "C", "royal": "V", "leaf": "L"}
AURA_ART = {"spark": "#facc15", "calm": "#a78bfa", "ember": "#f97316", "zen": "#2dd4bf"}
# Mood faces on the drawn avatar: rows replaced (4 is the eyes, 6 the mouth, 7 the chin).
MOODS = {"great": ("😄", "Great", {6: ".....SmmmmS....."}),
         "ok": ("🙂", "Fine", {}),
         "tired": ("😴", "Tired", {4: "....SSmSSmSS...."}),
         "hungry": ("🍽️", "Hungry", {7: "......SmmS......"}),
         "restless": ("🥱", "Restless", {6: ".....SmSSmS....."})}

DAILY = [("session", "Log a session", 1), ("meal", "Log 3 meals", 3), ("protein", "Hit your protein goal", 1),
         ("break", "Take 3 breaks from long sits", 3)]


def _day(ts):
    return date.fromtimestamp(ts).isoformat()


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _pct(frac):
    return int(round(max(0.0, min(1.0, frac)) * 100))


# --- Nights and sleep ---

def nights(rows, cfg, since, now):
    """Bedtime nights from the night of `since` (a date or "YYYY-MM-DD") to the last one already judged at
    `now`. rows are bridge rows; a night is on time when no desk presence span {"start", "end"} overlaps it.
    -> {"on_time": [evening, ...], "broken": [evening, ...], "streak": n, "seven": wake ts of the night the
    streak first reached 7, or None}. Without config "night" or `since`, no nights.

    A night is judged once the desk has reported back on it: a presence or break row with a ts after its
    wake, or NIGHT_WAIT_S after its wake, whichever comes first. Until then a span open over the night (the
    desk suspended, or used through it) may still arrive and break it, which would flip an on-time night
    and take back its XP, a level or "Lights out". Only the desk's presence sampler (modules/presence.py)
    writes these two kinds, in order, and a presence row's ts is its span's end: a later one means every
    span that touched the night is closed and already pulled. Answers, sits and nudges don't count, since
    they come while a span is still open. A span lost to a panel restart never arrives (presence.py)."""
    out = {"on_time": [], "broken": [], "streak": 0, "seven": None}
    if since is None or not night.window(cfg):
        return out
    if isinstance(since, str):
        since = date.fromisoformat(since)
    spans, heard = [], 0
    for r in rows:
        if r.get("kind") in ("presence", "break"):
            heard = max(heard, _num(r.get("ts")))
        if r.get("kind") != "presence":
            continue
        d = r.get("data") or {}
        start, end = _num(d.get("start")), _num(d.get("end"))
        if start and end >= start:
            spans.append((start, end))
    evening, last = since, date.fromtimestamp(now)
    while evening <= last:
        start, end = night.bounds(evening, cfg)
        if end > now or (heard <= end and now < end + NIGHT_WAIT_S):  # not over, or the desk not back yet
            break
        if any(s < end and e > start for s, e in spans):
            out["broken"].append(evening.isoformat())
            out["streak"] = 0
        else:
            out["on_time"].append(evening.isoformat())
            out["streak"] += 1
            if out["streak"] == 7 and out["seven"] is None:
                out["seven"] = end
        evening += timedelta(days=1)
    return out


def sleep_debt(rows, target_min, today):
    """Minutes short of `target_min` a night over the last 7 days (rows {"day", "minutes"}, the day you
    woke up), never below 0; a long night pays back a short one. None without a night in those 7 days."""
    lo, hi = (today - timedelta(days=6)).isoformat(), today.isoformat()
    got = [int(r["minutes"]) for r in rows if lo <= r["day"] <= hi]
    if not got:
        return None
    return max(0, sum(target_min - m for m in got))


# --- Bridge XP ---

def bridge_events(rows, cfg, now):
    """XP events (ts, skill, xp, text) from the desk's bridge rows, with the daily caps, plus the nights on
    time since the first row."""
    rows = sorted(rows, key=lambda r: r["ts"])
    out, count = [], {}

    def take(key, cap):
        count[key] = count.get(key, 0) + 1
        return count[key] <= cap

    for r in rows:
        d, day = r.get("data") or {}, _day(r["ts"])
        if r["kind"] == "answer" and d.get("away") is True:
            if take(("away", day), AWAY_PER_DAY):
                out.append((r["ts"], "balance", XP_AWAY, TEXT_AWAY))
        elif r["kind"] == "break" and _num(d.get("span_min")) >= BREAK_SIT_MIN:
            if take(("break", day), BREAKS_PER_DAY):
                nudged = d.get("nudge") is True
                out.append((r["ts"], "balance", XP_BREAK + (XP_NUDGE if nudged else 0), TEXT_NUDGE if nudged else TEXT_BREAK))
    if rows:
        n = nights(rows, cfg, _day(rows[0]["ts"]), now)
        for evening in n["on_time"]:
            out.append((night.bounds(evening, cfg)[1], "recovery", XP_NIGHT, TEXT_NIGHT))
    return out


# --- Bars and mood ---

def bars(debt_min, target_min, fuel, quests):
    """{"hp", "energy", "stamina"}, each 0-100.
    hp: sleep, 100 with no debt and 0 at two nights' worth; None without sleep data (debt_min None).
    energy: today's calories and protein against their goals; without goals, a third per meal eaten.
    stamina: this week's activity quests (not protein days); 50 with no activity quest set."""
    hp = None if debt_min is None else _pct(1 - debt_min / (2 * max(1, target_min)))
    goals = fuel.get("goals") or {}
    parts = [min(1.0, (fuel.get(k) or 0) / goals[k]) for k in ("kcal", "protein") if goals.get(k)]
    if parts:
        energy = _pct(sum(parts) / len(parts))
    else:
        energy = min(100, 34 * sum(1 for m in fuel.get("meals") or [] if m.get("status") == "done"))
    moves = [q for q in quests if q["key"] != "protein_days" and q["need"]]
    stamina = _pct(sum(min(1.0, q["have"] / q["need"]) for q in moves) / len(moves)) if moves else 50
    return {"hp": hp, "energy": energy, "stamina": stamina}


def mood(b):
    """{"key", "emoji", "name"} from the bars: the lowest one under 35 sets it, all at 70+ is great."""
    if b["hp"] is not None and b["hp"] < 35:
        key = "tired"
    elif b["energy"] < 35:
        key = "hungry"
    elif b["stamina"] < 35:
        key = "restless"
    elif min(v for v in b.values() if v is not None) >= 70:
        key = "great"
    else:
        key = "ok"
    emoji, name, _ = MOODS[key]
    return {"key": key, "emoji": emoji, "name": name}


# --- The weekly boss ---

def _monday_of(week):
    week = date.fromisoformat(week) if isinstance(week, str) else week
    return week - timedelta(days=week.weekday())


def boss(week, quests):
    """The boss of the week holding `week` (a date or "YYYY-MM-DD"), always the same one for an ISO week.
    Its HP is what's left of this week's quests (dicts with "have", "need", "done").
    -> {"name", "emoji", "hp", "max", "defeated", "week": Monday "YYYY-MM-DD"}"""
    monday = _monday_of(week)
    y, w, _ = monday.isocalendar()
    name, emoji = BOSSES[(y * 53 + w) % len(BOSSES)]
    top = sum(q["need"] for q in quests)
    return {"name": name, "emoji": emoji, "hp": top - sum(min(q["have"], q["need"]) for q in quests), "max": top,
            "defeated": bool(quests) and all(q["done"] for q in quests), "week": monday.isoformat()}


def boss_events(weeks, needs, xp=XP_BOSS):
    """(ts, "", xp, text) for every week whose quests were all done. weeks is {monday: {quest: [ts, ...]}}
    (each ts in that Monday-to-Sunday week), needs {quest: count needed}. The ts is the moment the last quest
    was done."""
    out = []
    if not needs:
        return out
    for monday, got in weeks.items():
        if all(len(got.get(q, [])) >= n for q, n in needs.items()):
            ts = max(sorted(got[q])[n - 1] for q, n in needs.items())
            out.append((ts, "", xp, f"Boss defeated: {boss(monday, [])['name']}"))
    return out


# --- Achievements, titles, cosmetics ---

def achievements(facts):
    """facts is {achievement key: ts first earned, or None}. -> [{"key", "name", "about", "on", "when"}],
    "when" being "YYYY-MM-DD" or None."""
    out = []
    for key, name, about in ACHIEVEMENTS:
        ts = facts.get(key)
        out.append({"key": key, "name": name, "about": about, "on": ts is not None,
                    "when": None if ts is None else datetime.fromtimestamp(ts).strftime("%Y-%m-%d")})
    return out


def titles(level, achieved, ladder):
    """Titles to wear: the level ladder ((level, name), ...) then those that achievements earn.
    -> [{"key", "name", "on"}]"""
    on = {a["key"] for a in achieved if a["on"]}
    out = [{"key": name.lower(), "name": name, "on": level >= at} for at, name in ladder]
    out += [{"key": k, "name": n, "on": ach in on} for ach, (k, n) in ACHIEVEMENT_TITLES.items()]
    return out


def cosmetics(level, achieved):
    """-> [{"key", "slot", "name", "on"}], "on" when unlocked."""
    on = {a["key"] for a in achieved if a["on"]}
    return [{"key": k, "slot": slot, "name": name, "on": level >= need if how == "level" else need in on}
            for k, slot, name, (how, need) in COSMETICS]


def unlocked(slot, item, title_list, cosmetic_list):
    """True if `item` is an unlocked title (slot "title") or cosmetic of that slot."""
    if slot == "title":
        return any(t["key"] == item and t["on"] for t in title_list)
    return any(c["key"] == item and c["slot"] == slot and c["on"] for c in cosmetic_list)


# --- The daily standup ---

def daily_standup(rows, today, protein=True, bridge=False):
    """Today's small goals. rows are (kind, ts) of what was done: "session", "meal", "protein", "break".
    The protein goal shows only when there is one, the breaks only with the desk's bridge.
    -> [{"key", "name", "have", "need", "done"}]"""
    t = today.isoformat()
    out = []
    for key, name, need in DAILY:
        if (key == "protein" and not protein) or (key == "break" and not bridge):
            continue
        have = sum(1 for kind, ts in rows if kind == key and _day(ts) == t)
        out.append({"key": key, "name": name, "have": have, "need": need, "done": have >= need})
    return out

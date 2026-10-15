import unittest
from datetime import date, datetime

from modules import life, rpg

LATE = {"night": {"bedtime": "23:30", "wake": "06:30"}}
EARLY = {"night": {"bedtime": "01:00", "wake": "07:00"}}


def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi).timestamp()


def presence(start, end):
    return {"ts": end, "kind": "presence", "data": {"start": start, "end": end}}


def quest(key, have, need):
    return {"key": key, "have": have, "need": need, "done": have >= need}


class Nights(unittest.TestCase):
    def test_across_midnight(self):
        rows = [presence(ts(2026, 10, 11, 0, 30), ts(2026, 10, 11, 0, 40)),  # up past bedtime on the 10th
                presence(ts(2026, 10, 12, 22, 0), ts(2026, 10, 12, 23, 29))]  # done before bedtime on the 12th
        n = rpg.nights(rows, LATE, "2026-10-10", ts(2026, 10, 13, 12, 30))
        self.assertEqual(n["broken"], ["2026-10-10"])
        self.assertEqual(n["on_time"], ["2026-10-11", "2026-10-12"])
        self.assertEqual(n["streak"], 2)

    def test_judged_only_after_wake(self):
        back = [{"ts": ts(2026, 10, 11, 6, 29), "kind": "break", "data": {}}]
        self.assertEqual(rpg.nights(back, LATE, "2026-10-10", ts(2026, 10, 11, 6, 29))["on_time"], [])
        self.assertEqual(rpg.nights([], LATE, "2026-10-10", ts(2026, 10, 11, 12, 30))["on_time"], ["2026-10-10"])

    def test_waits_for_the_desk_to_report_back(self):
        # Up at the desk from 01:00; the desk slept with that span open and wakes at 08:00.
        late = presence(ts(2026, 10, 11, 1, 0), ts(2026, 10, 11, 1, 30))
        sit = {"ts": ts(2026, 10, 11, 7, 0), "kind": "sit", "data": {}}
        answer = {"ts": ts(2026, 10, 11, 7, 5), "kind": "answer", "data": {"away": True}}
        for now in (ts(2026, 10, 11, 6, 30), ts(2026, 10, 11, 9, 0), ts(2026, 10, 11, 12, 29)):
            n = rpg.nights([sit, answer], LATE, "2026-10-10", now)  # sits and answers don't vouch for the night
            self.assertEqual((n["on_time"], n["broken"]), ([], []), now)
        # The desk's first presence after the night (the open span, closed at its last input) settles it.
        n = rpg.nights([late, sit, answer], LATE, "2026-10-10", ts(2026, 10, 11, 8, 0))
        self.assertEqual((n["on_time"], n["broken"]), ([], []))  # its ts is before wake: still waiting
        back = {"ts": ts(2026, 10, 11, 8, 1), "kind": "break", "data": {}}
        n = rpg.nights([late, sit, answer, back], LATE, "2026-10-10", ts(2026, 10, 11, 8, 1))
        self.assertEqual(n["broken"], ["2026-10-10"])
        n = rpg.nights([back], LATE, "2026-10-10", ts(2026, 10, 11, 8, 1))
        self.assertEqual(n["on_time"], ["2026-10-10"])
        # Never heard from: judged 6 h after wake.
        self.assertEqual(rpg.nights([sit], LATE, "2026-10-10", ts(2026, 10, 11, 12, 30))["on_time"], ["2026-10-10"])
        ev = rpg.bridge_events([sit, answer], LATE, ts(2026, 10, 11, 9, 0))
        self.assertEqual([e for e in ev if e[3] == rpg.TEXT_NIGHT], [])

    def test_bedtime_after_midnight(self):
        rows = [presence(ts(2026, 10, 11, 0, 30), ts(2026, 10, 11, 0, 50))]  # before a 01:00 bedtime: fine
        n = rpg.nights(rows, EARLY, date(2026, 10, 10), ts(2026, 10, 11, 13, 0))
        self.assertEqual(n["on_time"], ["2026-10-10"])
        n = rpg.nights([presence(ts(2026, 10, 11, 1, 10), ts(2026, 10, 11, 1, 20))], EARLY, "2026-10-10", ts(2026, 10, 11, 13, 0))
        self.assertEqual(n["broken"], ["2026-10-10"])

    def test_no_night_config(self):
        self.assertEqual(rpg.nights([], {}, "2026-10-10", ts(2026, 10, 20, 12))["on_time"], [])
        self.assertEqual(rpg.nights([], LATE, None, ts(2026, 10, 20, 12))["on_time"], [])

    def test_seven_in_a_row(self):
        n = rpg.nights([], LATE, "2026-10-01", ts(2026, 10, 9, 12, 30))
        self.assertEqual(n["streak"], 8)
        self.assertEqual(n["seven"], ts(2026, 10, 8, 6, 30))

    def test_no_points_before_the_first_bridge_data(self):
        # The bridge starts on the 12th: earlier nights, quiet as they were, earn nothing.
        rows = [{"ts": ts(2026, 10, 12, 10), "kind": "sit", "data": {"active_min": 50}}]
        ev = rpg.bridge_events(rows, LATE, ts(2026, 10, 14, 12, 30))
        nights = [e for e in ev if e[3] == rpg.TEXT_NIGHT]
        self.assertEqual([e[0] for e in nights], [ts(2026, 10, 13, 6, 30), ts(2026, 10, 14, 6, 30)])
        self.assertTrue(all(e[1] == "recovery" and e[2] == rpg.XP_NIGHT for e in nights))
        self.assertEqual(rpg.bridge_events([], LATE, ts(2026, 10, 14, 12)), [])


class DailyCaps(unittest.TestCase):
    def test_away_answers(self):
        rows = [{"ts": ts(2026, 10, 13, 9, i), "kind": "answer", "data": {"away": True}} for i in range(12)]
        rows += [{"ts": ts(2026, 10, 13, 15), "kind": "answer", "data": {"away": False}},
                 {"ts": ts(2026, 10, 14, 9), "kind": "answer", "data": {"away": True}}]
        ev = rpg.bridge_events(rows, {}, ts(2026, 10, 14, 12))
        self.assertEqual(sum(e[2] for e in ev if e[0] < ts(2026, 10, 14, 0)), rpg.AWAY_PER_DAY * rpg.XP_AWAY)
        self.assertEqual(len([e for e in ev if e[0] >= ts(2026, 10, 14, 0)]), 1)  # a new day, a new cap
        self.assertTrue(all(e[1] == "balance" for e in ev))

    def test_breaks(self):
        rows = [{"ts": ts(2026, 10, 13, 8 + i), "kind": "break", "data": {"span_min": 30, "nudge": i == 0}} for i in range(10)]
        rows.append({"ts": ts(2026, 10, 13, 20), "kind": "break", "data": {"span_min": 20}})  # too short a sit
        ev = rpg.bridge_events(rows, {}, ts(2026, 10, 14, 12))
        self.assertEqual(len(ev), rpg.BREAKS_PER_DAY)
        self.assertEqual(ev[0][2], rpg.XP_BREAK + rpg.XP_NUDGE)
        self.assertEqual(ev[0][3], rpg.TEXT_NUDGE)
        self.assertEqual({e[2] for e in ev[1:]}, {rpg.XP_BREAK})

    def test_bad_data_scores_nothing(self):
        rows = [{"ts": ts(2026, 10, 13, 9), "kind": "break", "data": {"span_min": "60"}},
                {"ts": ts(2026, 10, 13, 9), "kind": "answer", "data": {"away": "yes"}}]
        self.assertEqual(rpg.bridge_events(rows, {}, ts(2026, 10, 14, 12)), [])


class Boss(unittest.TestCase):
    NEEDS = {"gym": 2, "volleyball": 1}

    def test_same_boss_all_week(self):
        a = rpg.boss(date(2026, 10, 12), [])
        self.assertEqual(rpg.boss("2026-10-18", [])["name"], a["name"])
        self.assertEqual(a["week"], "2026-10-12")
        names = {rpg.boss(date(2026, 1, 5) + (date(2026, 1, 12) - date(2026, 1, 5)) * i, [])["name"] for i in range(8)}
        self.assertEqual(len(names), len(rpg.BOSSES))

    def test_hp(self):
        b = rpg.boss(date(2026, 10, 14), [quest("gym", 1, 3), quest("volleyball", 2, 1)])
        self.assertEqual((b["hp"], b["max"], b["defeated"]), (2, 4, False))
        self.assertTrue(rpg.boss(date(2026, 10, 14), [quest("gym", 3, 3)])["defeated"])
        self.assertFalse(rpg.boss(date(2026, 10, 14), [])["defeated"])

    def test_defeated_on_sunday_not_monday(self):
        mon = date(2026, 10, 12)
        sunday = {mon: {"gym": [ts(2026, 10, 12, 8), ts(2026, 10, 14, 8)], "volleyball": [ts(2026, 10, 18, 23, 58)]}}
        ev = rpg.boss_events(sunday, self.NEEDS)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][:3], (ts(2026, 10, 18, 23, 58), "", rpg.XP_BOSS))
        # The same game played on Monday counts for the next week, so this week's boss stands.
        monday = {mon: {"gym": sunday[mon]["gym"]}, date(2026, 10, 19): {"volleyball": [ts(2026, 10, 19, 0, 1)]}}
        self.assertEqual(rpg.boss_events(monday, self.NEEDS), [])

    def test_no_quests_no_boss_xp(self):
        self.assertEqual(rpg.boss_events({date(2026, 10, 12): {"gym": [1.0]}}, {}), [])


class SleepAndBars(unittest.TestCase):
    TODAY = date(2026, 10, 14)
    FUEL = {"kcal": 1100, "protein": 150, "goals": {"kcal": 2200, "protein": 150}, "meals": []}

    def test_hp_hidden_without_sleep(self):
        self.assertIsNone(rpg.sleep_debt([], 480, self.TODAY))
        self.assertIsNone(rpg.sleep_debt([{"day": "2026-10-07", "minutes": 300}], 480, self.TODAY))  # 8 days ago
        b = rpg.bars(None, 480, self.FUEL, [])
        self.assertIsNone(b["hp"])
        self.assertIn(rpg.mood(b)["key"], rpg.MOODS)

    def test_debt(self):
        rows = [{"day": "2026-10-13", "minutes": 420}, {"day": "2026-10-12", "minutes": 360}, {"day": "2026-10-08", "minutes": 540}]
        self.assertEqual(rpg.sleep_debt(rows, 480, self.TODAY), 60 + 120 - 60)
        self.assertEqual(rpg.sleep_debt([{"day": "2026-10-14", "minutes": 600}], 480, self.TODAY), 0)
        self.assertEqual(rpg.bars(120, 480, self.FUEL, [])["hp"], 88)
        self.assertEqual(rpg.bars(960, 480, self.FUEL, [])["hp"], 0)

    def test_energy_and_stamina(self):
        b = rpg.bars(0, 480, self.FUEL, [quest("gym", 1, 2), quest("protein_days", 0, 5)])
        self.assertEqual((b["energy"], b["stamina"]), (75, 50))
        no_goals = {"kcal": 0, "protein": 0, "goals": {}, "meals": [{"status": "done"}, {"status": "failed"}]}
        self.assertEqual(rpg.bars(None, 480, no_goals, [])["energy"], 34)

    def test_mood(self):
        self.assertEqual(rpg.mood({"hp": 20, "energy": 90, "stamina": 90})["key"], "tired")
        self.assertEqual(rpg.mood({"hp": None, "energy": 10, "stamina": 90})["key"], "hungry")
        self.assertEqual(rpg.mood({"hp": 90, "energy": 90, "stamina": 80})["key"], "great")
        self.assertEqual(rpg.mood({"hp": 60, "energy": 90, "stamina": 80})["key"], "ok")


class Unlocks(unittest.TestCase):
    def facts(self, **on):
        return rpg.achievements({k: on.get(k) for k, _, _ in rpg.ACHIEVEMENTS})

    def test_achievements(self):
        a = self.facts(boss_slayer=ts(2026, 10, 18, 20))
        slayer = next(x for x in a if x["key"] == "boss_slayer")
        self.assertEqual((slayer["on"], slayer["when"]), (True, "2026-10-18"))
        self.assertEqual(sum(x["on"] for x in a), 1)
        self.assertEqual(set(a[0]), {"key", "name", "about", "on", "when"})

    def test_titles_and_cosmetics(self):
        a = self.facts(lights_out=1.0)
        t = rpg.titles(3, a, life.TITLES)
        self.assertEqual([x["key"] for x in t if x["on"]], ["rookie", "apprentice", "warden"])
        c = rpg.cosmetics(3, a)
        self.assertEqual([x["key"] for x in c if x["on"]], ["headband", "nightcap", "calm"])
        self.assertTrue(rpg.unlocked("hat", "nightcap", t, c))
        self.assertFalse(rpg.unlocked("hat", "crown", t, c))
        self.assertFalse(rpg.unlocked("cape", "nightcap", t, c))  # right item, wrong slot
        self.assertTrue(rpg.unlocked("title", "warden", t, c))

    def test_daily(self):
        today = date(2026, 10, 14)
        rows = [("session", ts(2026, 10, 14, 8)), ("meal", ts(2026, 10, 14, 9)), ("meal", ts(2026, 10, 13, 20)),
                ("break", ts(2026, 10, 14, 11))]
        d = {x["key"]: x for x in rpg.daily_standup(rows, today, protein=False, bridge=True)}
        self.assertEqual(list(d), ["session", "meal", "break"])
        self.assertTrue(d["session"]["done"])
        self.assertEqual((d["meal"]["have"], d["meal"]["need"], d["meal"]["done"]), (1, 3, False))
        self.assertEqual([x["key"] for x in rpg.daily_standup(rows, today)], ["session", "meal", "protein"])


class CosmeticsWithoutSprites(unittest.TestCase):
    """Every cosmetic and mood draws on the pixel avatar, the one shown when there are no sprite strips."""

    def state(self, **worn):
        return {"level": 1, "skills": [{"xp": 0, "color": "#fff"}], "equipped": worn, "mood": {"key": "ok"}}

    def test_art_fits_the_grid(self):
        rows = [line for art in rpg.HAT_ART.values() for line in art.values()]
        rows += [line for _, _, art in rpg.MOODS.values() for line in art.values()]
        for line in rows:
            self.assertEqual(len(line), 16, line)
            self.assertTrue(set(line) <= set(life._PALETTE) | {"."}, line)
        self.assertTrue(set(rpg.CAPE_ART.values()) <= set(life._PALETTE))

    def test_every_cosmetic_draws(self):
        art = {"hat": rpg.HAT_ART, "cape": rpg.CAPE_ART, "aura": rpg.AURA_ART}
        bare = life.avatar(self.state())
        for key, slot, _, _ in rpg.COSMETICS:
            self.assertIn(key, art[slot], key)
            svg = life.avatar(self.state(**{slot: key}))
            self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
            self.assertNotEqual(svg, bare, key)

    def test_moods_draw(self):
        for key in rpg.MOODS:
            st = self.state()
            st["mood"] = {"key": key}
            self.assertTrue(life.avatar(st).startswith("<svg"))

    def test_level_gear_unchanged_without_equipment(self):
        st = {"level": 20, "skills": [{"xp": 0, "color": "#fff"}]}  # the state shape from before the RPG rules
        svg = life.avatar(st)
        for color in ("#ef4444", "#facc15", "#b91c1c", "#f59e0b"):  # headband, crown, cape, belt
            self.assertIn(color, svg)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from modules import life
from modules.common import STATE

TODAY = date(2026, 10, 14)  # a Wednesday


class FakeDate(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def at(d, h, m=0):
    return datetime(d.year, d.month, d.day, h, m).timestamp()


class LifeDB(unittest.TestCase):
    """Every test gets its own life.db; the real one is never touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="life-test-")
        self._db_path = life.DB_PATH
        life.DB_PATH = os.path.join(self.tmp, "life.db")
        life._made.clear()
        life._cache.clear()
        self.cfg = {"life": {"sprites": os.path.join(self.tmp, "no-sprites")}}
        for k in ("party", "coop"):
            STATE.pop(k, None)

    def tearDown(self):
        life.DB_PATH = self._db_path
        life._made.clear()
        life._cache.clear()
        for k in ("party", "coop"):
            STATE.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, sql, args=()):
        return [dict(r) for r in life._q(sql, args)]


class Record(LifeDB):
    def test_dedupe_by_src_and_ref(self):
        now = time.time()
        self.assertTrue(life.record("desk:aa", "1", "answer", now, {"away": True}))
        self.assertFalse(life.record("desk:aa", "1", "answer", now, {"away": True}))
        self.assertFalse(life.record("desk:aa", "1", "break", now + 5, {"span_min": 40}))  # same id, kept once
        self.assertTrue(life.record("desk:bb", "1", "answer", now, {"away": True}))  # a new desk epoch
        self.assertEqual(len(self.rows("SELECT * FROM bridge")), 2)
        self.assertEqual(self.rows("SELECT day FROM bridge")[0]["day"], date.fromtimestamp(now).isoformat())

    def test_unknown_kind_refused(self):
        now = time.time()
        self.assertFalse(life.record("desk:aa", "1", "finished", now, {"recap": "x"}))
        self.assertFalse(life.record("desk:aa", "2", "answer", now, ["not", "a", "dict"]))
        self.assertFalse(life.record("desk:aa", "3", "answer", "soon", {}))
        self.assertFalse(life.record("", "4", "answer", now, {}))
        self.assertEqual(self.rows("SELECT * FROM bridge"), [])

    def test_data_cut_to_2kb(self):
        self.assertTrue(life.record("desk:aa", "1", "answer", time.time(), {"title": "x" * 5000, "away": True, "deep": {"a": 1}}))
        data = self.rows("SELECT data FROM bridge")[0]["data"]
        self.assertLessEqual(len(data.encode()), life.RECORD_MAX)
        self.assertEqual(json.loads(data), {"title": "x" * 100, "away": True})

    def test_record_rescores_the_game(self):
        before = life.game(self.cfg)["xp"]
        life.record("desk:aa", "1", "answer", time.time(), {"away": True})
        g = life.game(self.cfg)
        self.assertEqual(g["xp"], before + 5)
        self.assertEqual(next(s for s in g["skills"] if s["key"] == "balance")["xp"], 5)
        self.assertEqual(g["feed"][0]["skill"], "Balance")


class Equip(LifeDB):
    def test_locked_item_refused(self):
        with self.assertRaises(ValueError):
            life.equip(self.cfg, "hat", {"item": "crown"})
        with self.assertRaises(ValueError):
            life.equip(self.cfg, "hat", {"item": "no-such-hat"})
        with self.assertRaises(ValueError):
            life.equip(self.cfg, "shoes", {"item": "headband"})
        self.assertEqual(life.game(self.cfg)["equipped"], {"title": None, "hat": None, "cape": None, "aura": None})

    def test_unlocked_item_worn_and_taken_off(self):
        for back in range(6):  # 6 gym sessions: 300 XP, level 3, the headband
            d = date.today() - timedelta(days=back)
            life._w("INSERT INTO activity(ts, day, kind) VALUES(?, ?, 'gym')", (at(d, 7), d.isoformat()))
        self.assertEqual(life.game(self.cfg)["level"], 3)
        life.equip(self.cfg, "hat", {"item": "headband"})
        life.equip(self.cfg, "title", {"item": "apprentice"})
        self.assertEqual(life.game(self.cfg)["equipped"], {"title": "apprentice", "hat": "headband", "cape": None, "aura": None})
        life.equip(self.cfg, "hat", {"item": ""})
        self.assertIsNone(life.game(self.cfg)["equipped"]["hat"])


class ManualSleep(LifeDB):
    def test_manual_entry_replaces(self):
        day = (date.today() - timedelta(days=1)).isoformat()
        life._w("INSERT INTO sleep(day, minutes, deep, rem, score, source) VALUES(?, 400, 60, 80, 70, 'fitbit')", (day,))
        life.log_sleep(self.cfg, None, {"day": day, "hours": "6"})
        self.assertEqual(self.rows("SELECT * FROM sleep"),
                         [{"day": day, "minutes": 360, "deep": None, "rem": None, "score": None, "source": "manual"}])
        life.log_sleep(self.cfg, None, {"day": day, "hours": 7.5})
        self.assertEqual(self.rows("SELECT minutes FROM sleep"), [{"minutes": 450}])
        s = life.game(self.cfg)["sleep"]
        self.assertEqual((s["debt_min"], s["last"]["minutes"]), (30, 450))

    def test_only_the_last_7_days(self):
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        life.log_sleep(self.cfg, None, {"day": week_ago, "hours": 7})
        self.assertEqual(self.rows("SELECT day FROM sleep"), [{"day": week_ago}])
        for days in (8, 30, 59):
            with self.assertRaisesRegex(ValueError, "last 7 days"):
                life.log_sleep(self.cfg, None, {"day": (date.today() - timedelta(days=days)).isoformat(), "hours": 7})
        self.assertEqual(len(self.rows("SELECT day FROM sleep")), 1)

    def test_db_is_created_600(self):
        real, modes = life.sqlite3.connect, []

        def connect(path, *a, **kw):
            modes.append(os.stat(path).st_mode & 0o777)  # the file exists, 600, before SQLite opens it
            return real(path, *a, **kw)

        old = os.umask(0o022)
        try:
            with mock.patch.object(life.sqlite3, "connect", side_effect=connect):
                life._q("SELECT 1")
        finally:
            os.umask(old)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {0o600})

    def test_bad_entries(self):
        for body in ({"hours": "0"}, {"hours": "20"}, {"hours": "nan"}, {"hours": "lots"},
                     {"day": (date.today() + timedelta(days=1)).isoformat(), "hours": 7}):
            with self.assertRaises(ValueError, msg=body):
                life.log_sleep(self.cfg, None, body)
        self.assertEqual(self.rows("SELECT * FROM sleep"), [])
        self.assertIsNone(life.game(self.cfg)["bars"]["hp"])


class State(LifeDB):
    KEYS = {"bars", "mood", "sleep", "boss", "achievements", "titles", "equipped", "cosmetics", "daily", "party", "coop"}

    def state(self):
        kind, body = life.state_file(self.cfg, "now")
        self.assertEqual(kind, "application/json")
        return json.loads(body)

    def test_contract_keys(self):
        s = self.state()
        self.assertLessEqual(self.KEYS, set(s))
        self.assertEqual(set(s["bars"]), {"hp", "energy", "stamina"})
        self.assertEqual(set(s["sleep"]), {"debt_min", "last", "nights_on_time", "bedtime_streak"})
        self.assertEqual(set(s["boss"]), {"name", "emoji", "hp", "max", "defeated", "week"})
        self.assertEqual(set(s["equipped"]), {"title", "hat", "cape", "aura"})
        self.assertEqual(set(s["cosmetics"][0]), {"key", "slot", "name", "on"})
        self.assertEqual(set(s["titles"][0]), {"key", "name", "on"})

    def test_stale_party_left_out(self):
        member = {"key": "a", "title": "Fix", "project": "p", "agent": "claude", "status": "working", "level": 2, "done": 3, "forgotten": False}
        STATE["party"] = {"at": time.time() - 300, "members": [member]}
        self.assertEqual(self.state()["party"], [])
        STATE["party"] = {"at": time.time() - 10, "members": [member, "junk"]}
        self.assertEqual(self.state()["party"], [member])

    def test_coop(self):
        self.assertIsNone(self.state()["coop"])
        from modules import coop  # its summary() calls life.game(): no import cycle
        ana = {"v": 1, "name": "Ana", "level": 4, "title": "Regular", "week": coop._week(),
               "quests": [{"name": "Gym ×3", "have": 3, "need": 3, "done": True}], "boss": None}
        cfg = {**self.cfg, "life": {**self.cfg["life"], "coop": {"board": "Our board", "party_goal": 9}}}
        with mock.patch.object(coop, "_read_json", return_value={"Ana": {"url": "https://ana.x.ts.net/", "token": "t"}}), \
                mock.patch.object(coop, "fetch", return_value=ana):
            coop.sample(cfg)
        c = self.state()["coop"]
        self.assertEqual((c["board"], c["goal"], c["total"]), ("Our board", 9, 1))
        self.assertEqual([(r["name"], r["done"], r["me"]) for r in c["rows"]], [("Player", 0, True), ("Ana", 1, False)])
        self.assertEqual(set(c), {"board", "goal", "total", "rows"})
        self.assertEqual(set(c["rows"][0]), {"name", "level", "title", "done", "need", "boss_defeated", "me"})
        STATE["coop"]["at"] = time.time() - 2 * life.COOP_FRESH_S  # coop stopped refreshing it
        self.assertIsNone(self.state()["coop"])
        with mock.patch.object(coop, "_read_json", return_value={}):  # no friends: coop drops the key
            coop.sample(cfg)
        self.assertIsNone(self.state()["coop"])


class Achievements(LifeDB):
    def test_told_once(self):
        cfg = {**self.cfg, "notify": {"panel": "https://pi/"}}
        with mock.patch.object(life.notify, "send") as send:
            life.sample(cfg)  # the first time only notes what's already earned
            life._w("INSERT INTO activity(ts, day, kind) VALUES(?, ?, 'gym')", (time.time(), date.today().isoformat()))
            life.sample(cfg)
            life.sample(cfg)
        told = [c for c in send.call_args_list if c.kwargs.get("tag") == "life achievement"]
        self.assertEqual(len(told), 1)
        self.assertIn("First steps", told[0].args[1])


class GamePage(LifeDB):
    def test_reads_the_file_each_time(self):
        path = os.path.join(self.tmp, "game.html")
        with mock.patch.object(life, "GAME_HTML", path):
            self.assertIn("missing", life.game_page(self.cfg, None))
            with open(path, "w") as f:
                f.write("<p>v1</p>%%KIOSK_JS%%")
            self.assertEqual(life.game_page(self.cfg, None), "<p>v1</p>%%KIOSK_JS%%")
            with open(path, "w") as f:
                f.write("<p>v2</p>")
            self.assertEqual(life.game_page(self.cfg, None), "<p>v2</p>")

    def test_game_page_block_is_gone(self):
        self.assertFalse(hasattr(life, "GAME_PAGE"))


def seed():
    """Three weeks of sessions, meals and sleep, with no bridge data."""
    for k, v in {"kcal": 2200, "protein": 150, "gym": 3, "volleyball": 1, "protein_days": 2}.items():
        life._w("INSERT INTO goal(key, value) VALUES(?, ?)", (k, v))
    for back, kind in [(23, "gym"), (22, "volleyball"), (21, "gym"), (19, "gym"), (16, "gym"), (14, "gym"),
                       (12, "gym"), (11, "volleyball"), (9, "gym"), (8, "gym"), (6, "gym"), (5, "volleyball"),
                       (2, "gym"), (1, "gym"), (0, "gym"), (3, "swim")]:
        d = TODAY - timedelta(days=back)
        life._w("INSERT INTO activity(ts, day, kind) VALUES(?, ?, ?)", (at(d, 18), d.isoformat(), kind))
    for back in range(0, 10):
        d = TODAY - timedelta(days=back)
        for h, kcal, prot in [(8, 600, 40), (13, 900, 60 if back % 3 else 30), (20, 700 + 50 * back, 45)]:
            life._w("INSERT INTO meal(ts, day, said, photo, status, title, items, kcal, protein, carbs, fat)"
                    " VALUES(?, ?, '', 0, 'done', ?, '[]', ?, ?, 100, 30)", (at(d, h), d.isoformat(), f"M{back}{h}", kcal, prot))
    life._w("INSERT INTO meal(ts, day, said, photo, status) VALUES(?, ?, 'x', 0, 'failed')", (at(TODAY, 9), TODAY.isoformat()))
    for back, minutes in [(1, 490), (2, 400), (3, 430), (9, 300)]:
        d = TODAY - timedelta(days=back)
        life._w("INSERT INTO sleep(day, minutes, source) VALUES(?, ?, 'fitbit')", (d.isoformat(), minutes))


def legacy(g):
    """The parts of the game that existed before the RPG rules (the new Balance skill left out)."""
    keep = ("level", "title", "xp", "into", "span", "name", "quests", "activities", "streaks", "week", "today", "feed",
            "outfit", "sprite")
    out = {k: g[k] for k in keep}
    out["skills"] = [s for s in g["skills"] if s["key"] != "balance"]
    return out


class SameResultsWithoutBridge(LifeDB):
    """The RPG rules must not rescore anyone without bridge data. The expected numbers and the hash are those
    of the life.py before the RPG rules (git show 22118e6:admin/modules/life.py) run on this same fixture."""
    HASH = "7a21b09cbb63ce30b9b891d7d07c67dfd5241c4d1f512cb648999fe409427464"

    def setUp(self):
        super().setUp()
        self.patch = mock.patch.object(life, "date", FakeDate)
        self.patch.start()
        seed()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_identical(self):
        g = legacy(life.game(self.cfg))
        self.assertEqual((g["level"], g["xp"], g["title"]), (6, 1935, "Regular"))
        self.assertEqual([(s["key"], s["xp"]) for s in g["skills"]],
                         [("strength", 1000), ("volleyball", 330), ("nutrition", 530), ("recovery", 75)])
        self.assertEqual([(q["key"], q["have"], q["need"]) for q in g["quests"]], [("gym", 3, 3), ("volleyball", 0, 1), ("protein_days", 2, 2)])
        self.assertEqual([s["n"] for s in g["streaks"]], [4, 3, 2])
        self.assertEqual(hashlib.sha256(json.dumps(g, sort_keys=True).encode()).hexdigest(), self.HASH)

    def test_night_config_changes_nothing(self):
        g = legacy(life.game({**self.cfg, "night": {"bedtime": "23:30", "wake": "06:30"}}))
        self.assertEqual(hashlib.sha256(json.dumps(g, sort_keys=True).encode()).hexdigest(), self.HASH)

    def test_bridge_does_not_rescore_the_past(self):
        # The fixture defeated earlier weeks' bosses; once the bridge starts, those weeks still earn nothing.
        base = life.game(self.cfg)
        self.assertTrue(next(a for a in base["achievements"] if a["key"] == "boss_slayer")["on"])
        life.record("desk:aa", "1", "answer", at(TODAY, 10), {"away": True})
        g = life.game(self.cfg)
        self.assertEqual(g["xp"], base["xp"] + 5)
        self.assertEqual([s["xp"] for s in g["skills"] if s["key"] != "balance"], [s["xp"] for s in base["skills"] if s["key"] != "balance"])


if __name__ == "__main__":
    unittest.main()

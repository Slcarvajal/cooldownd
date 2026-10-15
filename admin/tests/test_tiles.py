"""Home tiles: each module's tiles() (shape, tints, tap targets), and app.py's grid (validation, escaping, order,
the kiosk's fit, the phone's waiting-agents tile) and remote.py's forwarding (prefixed targets)."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import tests  # noqa: F401  (isolation first)
import app
from modules import agenthealth, agentlog, agents, ha, life, overview, pi, power, presence, remote
from modules.common import ICONS, STATE

KEYS = {"key", "label", "value", "tint", "icon", "open", "span", "order"}


def agent(status, handle="t1", title="Fix login", project="api-server"):
    return {"handle": handle, "title": title, "project": project, "agent": "claude", "status": status,
            "summary": "Which one?", "age": 1.0, "host": "desk", "branch": "main"}


class Shapes(unittest.TestCase):
    def check(self, name, tiles):
        for t in tiles:
            self.assertTrue(KEYS <= set(t), t)
            self.assertIn(t["tint"], app.TINTS + (None,))
            self.assertTrue(t["icon"] is None or t["icon"] in ICONS, t["icon"])
            self.assertIsNotNone(app.clean_tile(t, name))
            self.assertEqual(app.clean_tile(t, name)["open"] is None, t["open"] is None, t)  # every target passes the checks
        return tiles


class AgentsTiles(Shapes):
    def setUp(self):
        self.addCleanup(STATE.pop, "agents", None)

    def test_no_sample_or_orca_off(self):
        STATE.pop("agents", None)
        self.assertEqual(agents.tiles({}), [])
        STATE["agents"] = {"error": "Orca isn't running", "items": []}
        self.assertEqual(agents.tiles({}), [])

    def test_waiting_opens_the_first(self):
        STATE["agents"] = {"items": [agent("working", "t0"), agent("waiting", "t1"), agent("waiting", "t2", "Other")]}
        [t] = self.check("agents", agents.tiles({}))
        self.assertEqual((t["tint"], t["value"]), ("need", "2 need you"))
        self.assertEqual(t["open"], {"sheet": "agents/view/t1", "title": "🔔 Fix login · api-server"})

    def test_one_waiting_and_working_and_idle(self):
        STATE["agents"] = {"items": [agent("waiting")]}
        self.assertEqual(agents.tiles({})[0]["value"], "1 needs you")
        STATE["agents"] = {"items": [agent("working"), agent("stuck", "t2"), agent("idle", "t3")]}
        [t] = self.check("agents", agents.tiles({}))
        self.assertEqual((t["tint"], t["value"], t["open"]), ("busy", "2 working", {"tab": "agents"}))
        STATE["agents"] = {"items": [agent("idle")]}
        self.assertEqual((agents.tiles({})[0]["tint"], agents.tiles({})[0]["value"]), (None, "All idle"))


class AgentHealthTiles(Shapes):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=tests.HOME)
        orig = agentlog.DB_PATH
        agentlog.DB_PATH = os.path.join(self.dir, "agents.db")
        self.addCleanup(setattr, agentlog, "DB_PATH", orig)
        self.addCleanup(STATE.pop, "agents", None)
        STATE["agents"] = {"items": []}

    def task(self, title="T", dismissed=None, ts=None):
        return agentlog.w("INSERT INTO task(ts, day, handle, project, title, agent, recap, dismissed) "
                          "VALUES(?, '2026-10-14', 'h', 'api-server', ?, 'claude', 'r', ?)",
                          (ts or time.time(), title, dismissed))

    def test_nothing(self):
        self.assertEqual(agenthealth.tiles({}), [])

    def test_one_ready_opens_it(self):
        tid = self.task("Rate limiter")
        self.task("Old", ts=time.time() - 8 * 86400)
        self.task("Seen", dismissed=time.time())
        [t] = self.check("agenthealth", agenthealth.tiles({}))
        self.assertEqual((t["key"], t["tint"], t["value"]), ("ready", "ok", "1 to review"))
        self.assertEqual(t["open"], {"sheet": f"agenthealth/ready/{tid}", "title": "Rate limiter"})

    def test_many_ready_open_the_list(self):
        for i in range(3):
            self.task(f"T{i}")
        [t] = agenthealth.tiles({})
        self.assertEqual((t["value"], t["open"]["sheet"]), ("3 to review", "agenthealth/ready/all"))
        sheet = agenthealth._ready_sheet({}, "all")
        self.assertEqual(sheet.count('data-sheet="agenthealth/ready/'), 3)
        self.assertIn("agenthealth/dismiss/all", sheet)

    def test_stuck_comes_first_and_is_bad(self):
        STATE["agents"] = {"items": [agent("stuck", "t9", "<b>x</b>")]}
        self.task()
        stuck, ready = self.check("agenthealth", agenthealth.tiles({}))
        self.assertEqual((stuck["tint"], stuck["open"]["sheet"]), ("bad", "agenthealth/view/t9"))  # its own module's sheet
        self.assertLess(stuck["order"], ready["order"])

    def test_empty_list_sheet(self):
        self.assertIn("Nothing to review", agenthealth._ready_sheet({}, "all"))


class PresenceTiles(Shapes):
    def setUp(self):
        self.addCleanup(STATE.pop, "presence", None)

    def snap(self, mins, window=None, nudged=False, span=True):
        now = 10_000.0
        STATE["presence"] = {"at": now, "span_start": now - mins * 60 if span else None, "last_input": now,
                             "active_min": mins if span else 0, "window": window, "nudged": nudged, "idle_s": 1.0}

    def test_none_without_idle(self):
        STATE.pop("presence", None)
        self.assertEqual(presence.tiles({}), [])
        STATE["presence"] = {"idle_s": None}
        self.assertEqual(presence.tiles({}), [])

    def test_thresholds(self):
        cfg = {"presence": {"sit_min": 50}}
        for mins, tint, value in ((10, None, "10 min"), (40, "warn", "40 min · stand up soon"), (55, "bad", "55 min · stand up")):
            self.snap(mins)
            [t] = self.check("presence", presence.tiles(cfg))
            self.assertEqual((t["key"], t["tint"], t["value"]), ("desk", tint, value))
        self.snap(0, span=False)
        self.assertEqual(presence.tiles(cfg)[0]["value"], "Away")

    def test_move_while_the_window_is_open(self):
        self.snap(10, window=10_000.0 - 12 * 60)
        desk, move = self.check("presence", presence.tiles({}))
        self.assertEqual((move["key"], move["tint"], move["value"]), ("move", "busy", "Agents busy 12 min"))
        self.snap(10, window=10_000.0 - 25 * 60, nudged=True)
        self.assertIn("take a break", presence.tiles({})[1]["value"])


def life_state(sprite=None, quests=None, kcal=1120, goal=2300):
    return {"level": 9, "title": "Athlete", "into": 315, "span": 900, "sprite": sprite, "avatar": "<svg>drawn</svg>",
            "quests": quests if quests is not None else [
                {"key": "gym", "name": "Gym ×3", "have": 2, "need": 3, "done": False},
                {"key": "volleyball", "name": "Volleyball ×1", "have": 1, "need": 1, "done": True}],
            "today": {"kcal": kcal, "goals": {"kcal": goal}}}


class LifeTiles(Shapes):
    def test_avatar_quest_meal(self):
        with mock.patch.object(life, "game", return_value=life_state()):
            av, quest, meal = self.check("life", life.tiles({}))
        self.assertEqual((av["span"], av["label"], av["value"], av["open"]), (2, "Level 9 · Athlete", "315 / 900 XP", {"page": "life/game"}))
        self.assertAlmostEqual(av["progress"], 315 / 900)
        self.assertEqual(av["avatar_html"], "<svg>drawn</svg>")
        self.assertEqual((quest["label"], quest["value"], quest["tint"], quest["icon"]), ("Gym", "2 of 3 this week", "on", "dumbbell"))
        self.assertEqual((meal["value"], meal["tint"], meal["open"]["sheet"]), ("1,120 / 2,300 kcal", None, "life/add/meal"))

    def test_sprite_frame_all_done_and_over_goal(self):
        sp = {"url": "file/life/sprite/gym?v=5", "w": 199, "h": 193, "frames": 16}
        done = [{"key": "protein_days", "name": "Protein ×5", "have": 5, "need": 5, "done": True}]
        with mock.patch.object(life, "game", return_value=life_state(sp, done, kcal=2600)):
            av, quest, meal = life.tiles({})
        self.assertIn('style="aspect-ratio:199/193"', av["avatar_html"])
        self.assertIn('src="file/life/sprite/gym?v=5"', av["avatar_html"])
        self.assertEqual((quest["tint"], quest["value"], quest["icon"]), ("ok", "Done this week", "flag"))
        self.assertEqual(meal["tint"], "warn")

    def test_no_quests_no_goal(self):
        with mock.patch.object(life, "game", return_value=life_state(quests=[], goal=None)):
            tiles = life.tiles({})
        self.assertEqual([t["key"] for t in tiles], ["avatar", "meal"])
        self.assertEqual(tiles[1]["value"], "1,120 kcal today")


class HaTiles(Shapes):
    STATES = [{"entity_id": "light.living", "state": "on", "attributes": {"friendly_name": "Living room"}},
              {"entity_id": "switch.fan", "state": "off", "attributes": {"friendly_name": "Fan"}},
              {"entity_id": "light.porch", "state": "unavailable", "attributes": {}},
              {"entity_id": "alarm_control_panel.home", "state": "armed_away", "attributes": {}},
              {"entity_id": "camera.door", "state": "idle", "attributes": {}},
              {"entity_id": "siren.s", "state": "off", "attributes": {}}]

    def test_without_token_or_reachable_ha(self):
        with mock.patch.object(ha, "_token", return_value=None):
            self.assertEqual(ha.tiles({}), [])
        with mock.patch.object(ha, "_token", return_value="t"), mock.patch.object(ha, "_states", side_effect=OSError("down")):
            self.assertEqual(ha.tiles({}), [])

    def test_lights_switches_alarm(self):
        with mock.patch.object(ha, "_token", return_value="t"), mock.patch.object(ha, "_states", return_value=self.STATES):
            got = {t["key"]: t for t in self.check("ha", ha.tiles({}))}
        self.assertEqual(set(got), {"light.living", "switch.fan", "light.porch", "alarm_control_panel.home"})
        self.assertEqual((got["light.living"]["tint"], got["light.living"]["open"]), ("on", {"action": "ha/toggle/light.living"}))
        self.assertEqual((got["switch.fan"]["tint"], got["switch.fan"]["value"], got["switch.fan"]["icon"]), (None, "Off", "power"))
        self.assertEqual(got["switch.fan"]["open"], {"action": "ha/toggle/switch.fan", "confirm": "Turn on Fan?"})  # a light is one tap
        self.assertEqual((got["light.porch"]["value"], got["light.porch"]["open"]), ("Offline", None))
        self.assertEqual((got["alarm_control_panel.home"]["tint"], got["alarm_control_panel.home"]["value"]), ("ok", "Armed away"))
        for state, tint, value in (("disarmed", None, "Off"), ("triggered", "bad", "Triggered"), ("pending", "warn", "Pending")):
            with mock.patch.object(ha, "_token", return_value="t"), \
                    mock.patch.object(ha, "_states", return_value=[{"entity_id": "alarm_control_panel.a", "state": state, "attributes": {}}]):
                [t] = ha.tiles({})
            self.assertEqual((t["tint"], t["value"]), (tint, value))


class MachineTiles(Shapes):
    def setUp(self):
        self.addCleanup(STATE.pop, "machine", None)

    def test_desktop(self):
        STATE.pop("machine", None)
        self.assertEqual(overview.tiles({}), [])
        STATE["machine"] = {"wall": 74.4, "psu_load": 0.2, "temps": {"cpu": 48.2}}
        [t] = self.check("overview", overview.tiles({}))
        self.assertEqual((t["value"], t["tint"], t["open"]), ("74 W · 48°", "busy", {"tab": "machine"}))
        STATE["machine"] = {"wall": 500, "psu_load": 0.9, "temps": {"cpu": 50}}
        self.assertEqual(overview.tiles({})[0]["tint"], "bad")
        STATE["machine"] = {"wall": 80, "psu_load": 0.1, "temps": {"cpu": 82}}
        self.assertEqual(overview.tiles({})[0]["tint"], "warn")

    def test_pi(self):
        with mock.patch.object(pi, "_temp", return_value=None):
            self.assertEqual(pi.tiles({}), [])
        for temp, tint, value in ((52.4, None, "52° · all good"), (72, "warn", "72° · warm"), (85, "bad", "85° · hot")):
            with mock.patch.object(pi, "_temp", return_value=temp):
                [t] = self.check("pi", pi.tiles({}))
            self.assertEqual((t["tint"], t["value"], t["open"]), (tint, value, None))

    def test_power_wakes_only_sleeping_others(self):
        cfg = {"machines": [{"id": "pc", "name": "Desktop · RTX", "tailnet": "desk"},
                            {"id": "me", "name": "Pi", "tailnet": "pi"}]}
        with mock.patch.object(power, "self_host", return_value="pi"), mock.patch.object(power, "peer", return_value={"Online": False}):
            [t] = self.check("power", power.tiles(cfg))
        self.assertEqual((t["label"], t["open"]), ("Desktop", {"action": "power/wake/pc"}))
        with mock.patch.object(power, "self_host", return_value="pi"), mock.patch.object(power, "peer", return_value={"Online": True}):
            self.assertEqual(power.tiles(cfg), [])
        with mock.patch.object(power, "self_host", return_value=""):  # no tailnet status
            self.assertEqual(power.tiles(cfg), [])


class Mod:
    TITLE, TAB = "M", "home"

    def __init__(self, name, tiles):
        self.NAME, self._tiles = name, tiles

    def card(self, cfg):
        return "<p>card</p>"

    def tiles(self, cfg):
        if isinstance(self._tiles, Exception):
            raise self._tiles
        return self._tiles


def tile(key, order=100, **kw):
    return {"key": key, "label": key.title(), "value": "v", "tint": None, "icon": None, "open": None,
            "span": 1, "order": order, **kw}


class CleanTile(unittest.TestCase):
    def test_drops_what_is_off(self):
        self.assertIsNone(app.clean_tile({"label": "no key"}, "m"))
        self.assertIsNone(app.clean_tile("junk", "m"))
        t = app.clean_tile({"key": "k", "tint": "pink", "icon": "nope", "span": 3, "order": True, "progress": 7,
                            "open": {"sheet": "../etc/passwd/x"}}, "..")
        self.assertEqual((t["tint"], t["icon"], t["span"], t["order"], t["progress"], t["open"]), (None, None, 1, 100, 1.0, None))

    def test_targets(self):
        ok = ("life", {"tab": "agents"}), ("life", {"sheet": "life/add/meal", "title": "Add"}), \
            ("ha", {"action": "ha/toggle/light.x"}), ("agenthealth", {"action": "agenthealth/dismiss"}), \
            ("pi.life", {"page": "pi.life/game"}), ("desk.agents", {"sheet": "desk.agents/view/t1", "title": ""}), \
            ("desk.agents", {"sheet": "desk.agents/view/", "title": ""}), ("ha", {"action": "ha/toggle/a:b@c-d_1.e"})
        for name, o in ok:
            self.assertEqual(app.clean_tile({"key": "k", "open": o}, name)["open"], o, o)
        for o in ({"tab": "nowhere"}, {"sheet": "life/add"}, {"page": "life/game/x"}, {"action": "a b/c"}, "life/x", {"sheet": 3}):
            self.assertIsNone(app.clean_tile({"key": "k", "open": o}, "life")["open"], o)
        self.assertEqual(app.clean_tile({"key": "k", "open": {"action": "power/sleep/pc", "confirm": "Sure?"}}, "power")["open"]["confirm"], "Sure?")

    def test_targets_that_climb_out(self):
        # a browser turns "\" into "/" and %2e%2e into "..": each of these would reach another module, or this
        # panel's own /action/power/sleep from a remote's tile
        for name, o in (("x", {"action": "x/y/a\\%2e%2e\\%2e%2e\\%2e%2e\\power\\sleep"}),
                        ("x", {"action": "x/y/a\\power"}), ("x", {"action": "x/y/%2e%2e"}), ("x", {"sheet": "x/y/%2E%2E"}),
                        ("x", {"sheet": "x/y/a%2fb"}), ("x", {"action": "x/y/.."}), ("x", {"action": "x/y/."}),
                        ("x", {"sheet": "./x/y/z"}), ("x", {"action": "x/./y"}), ("x", {"page": "./y"}),
                        ("x", {"action": "x/y/a\n"}), ("x.", {"page": "x./y"}), (".x", {"page": ".x/y"}),
                        ("life", {"action": "ha/toggle/light.x"}), ("life", {"sheet": "agents/view/t1"}),
                        ("life", {"page": "lifex/game"}), ("pi.life", {"action": "pi.ha/toggle/light.x"}),
                        ("pi.life", {"page": "life/game"}), ("life", {"page": "pi.life/game"})):
            self.assertIsNone(app.clean_tile({"key": "k", "open": o}, name)["open"], (name, o))

    def test_tab_must_be_shown_here(self):
        self.assertEqual(app.clean_tile({"key": "k", "open": {"tab": "agents"}}, "m", ["home", "agents"])["open"], {"tab": "agents"})
        self.assertIsNone(app.clean_tile({"key": "k", "open": {"tab": "machine"}}, "m", ["home", "agents"])["open"])
        mods = [Mod("a", [tile("t", open={"tab": "machine"})])]
        with mock.patch("modules.night.quiet", return_value=False):
            d = app.live_payload({}, mods)  # Mod's tab is home: no Machine tab on this panel
        self.assertIsNone(d["tiles"]["a"][0]["open"])
        self.assertEqual(app.shown_tabs({}, mods), ["home"])

    def test_broken_module_gives_no_tiles(self):
        with mock.patch.object(app.traceback, "print_exc") as printed:
            self.assertEqual(app.tiles_of(Mod("x", RuntimeError("boom")), {}), [])
        printed.assert_called_once()
        self.assertEqual(app.tiles_of(Mod("x", "not a list"), {}), [])
        self.assertEqual(app.tiles_of(agents.__class__("plain"), {}), [])  # a module without tiles()


class Grid(unittest.TestCase):
    def grid(self, mods, pager=(), screen=False):
        return app.grid_html(mods, {m.NAME: app.tiles_of(m, {}) for m in mods}, pager, screen)

    def test_escapes_and_targets(self):
        m = Mod("evil", [tile("x", label='<img src=x onerror=alert(1)>', value='"><script>',
                              open={"sheet": "evil/s/a", "title": '"><b>'})])
        g = self.grid([m])
        self.assertNotIn("<img src=x", g)
        self.assertNotIn("<script>", g)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", g)
        self.assertIn('data-sheet="evil/s/a" data-title="&quot;&gt;&lt;b&gt;"', g)

    def test_tap_attributes(self):
        m = Mod("m", [tile("a", open={"tab": "agents"}), tile("b", open={"action": "m/do/1", "confirm": "Sure?"}),
                      tile("c", open={"page": "m/game"}), tile("d")])
        g = self.grid([m])
        for attr in ('data-tab="agents"', 'data-action="m/do/1" data-confirm="Sure?"', 'data-page="m/game"', 'data-goto="m"'):
            self.assertIn(attr, g)

    def test_order_clock_first_and_ties_by_module(self):
        a = Mod("a", [tile("late", 90), tile("tie1", 50)])
        b = Mod("b", [tile("first", 5), tile("tie2", 50)])
        g = self.grid([a, b])
        self.assertTrue(g.startswith('<div class="ht ht-clock" data-tile="clock">'))
        pos = [g.index(f'data-tile="{k}"') for k in ("b:first", "a:tie1", "b:tie2", "a:late")]
        self.assertEqual(pos, sorted(pos))

    def test_avatar_tile(self):
        m = Mod("life", [tile("avatar", span=2, progress=0.35, avatar_html='<svg id="me"></svg>', open={"page": "life/game"})])
        g = self.grid([m])
        self.assertIn('class="ht span2 ht-av"', g)
        self.assertIn('<svg id="me"></svg>', g)
        self.assertIn('style="width:35%"', g)

    def test_kiosk_fit(self):
        self.assertEqual(app.kiosk_fit([1] * 20), list(range(15)))
        # a 2-row tile that would start on the last row is left out; a later 1-row tile still fits
        self.assertEqual(app.kiosk_fit([1] * 11 + [2, 1]), list(range(11)) + [12])
        # a 2-row tile on row 2 takes that column on row 3 too: 1 + 5 + 2 + 3 = 15 cells, all shown
        self.assertEqual(app.kiosk_fit([1] * 5 + [2] + [1] * 8), list(range(14)))
        self.assertEqual(len(app.kiosk_fit([1] * 5 + [2] + [1] * 9)), 14)

    def test_kiosk_drops_the_rest(self):
        m = Mod("m", [tile(f"t{i}", i) for i in range(20)])
        self.assertEqual(self.grid([m], screen=True).count('data-tile="m:'), 14)  # plus the clock: 15 cells
        self.assertEqual(self.grid([m]).count('data-tile="m:'), 20)

    def test_phone_banner_lists_waiting_agents(self):
        pager = [{"key": "k", "sheet": "desk.agents/view/t1", "title": "Rate <limiter>", "project": "api", "summary": "Which?"}]
        m = Mod("desk.agents", [tile("agents", 10)])
        g = self.grid([m], pager)
        self.assertIn('<div class="ht-banner" data-tile="banner">', g)
        self.assertIn('1 agent needs you', g)
        self.assertIn('data-sheet="desk.agents/view/t1" data-title="🔔 Rate &lt;limiter&gt; · api"', g)
        self.assertIn('ht-agents', g)  # hidden on a phone while the banner shows
        self.assertNotIn("ht-banner", self.grid([m], pager, screen=True))
        self.assertNotIn("ht-banner", self.grid([m]))

    def test_every_child_has_a_stable_key(self):
        # the page replaces only the tiles whose HTML changed, matched by data-tile (gridUpdate in PAGE)
        import re
        pager = [{"key": "k", "sheet": "agents/view/t1", "title": "T", "project": "p", "summary": ""}]
        m = Mod("m", [tile("a", 1), tile("b", 2, span=2), tile("c", 3)])
        g = self.grid([m], pager)
        tops = re.findall(r'<(?:div|button) class="(?:ht|ht-banner)\b[^"]*" data-tile="([^"]+)"', g)
        self.assertEqual(tops, ["banner", "clock", "m:a", "m:b", "m:c"])
        self.assertIn("function gridUpdate(", app.PAGE)
        self.assertIn("gridUpdate(grid, d.grid)", app.PAGE)
        self.assertNotIn("grid.innerHTML = d.grid", app.PAGE)
        # a card re-rendered for its "ago" text keeps the drawn creatures whose attributes didn't change
        self.assertIn("const keep = crKeep(el); el.innerHTML = html; critters(el, keep)", app.PAGE)
        self.assertIn("function crKeep(", app.KIOSK_JS)


class RemoteTiles(unittest.TestCase):
    CFG = {"modules": ["desk.agents", "pi.life", "pi.ha"],
           "remotes": {"desk": {"url": "https://desk.example.ts.net:8443"}, "pi": {"url": "https://pi.example.ts.net:8443"}}}

    def setUp(self):
        remote._live.clear()
        self.addCleanup(remote._live.clear)
        self.answers = {
            "desk": {"cards": {"agents": "<p>a</p>"}, "pager": [], "notices": [],
                     "tiles": {"agents": [tile("agents", 10, open={"sheet": "agents/view/t1", "title": "Fix"})],
                               "agenthealth": [tile("ready", 20, open={"tab": "agents"})]}},
            "pi": {"cards": {"life": "<p>l</p>"}, "pager": [], "notices": [],
                   "tiles": {"life": [tile("avatar", 50, span=2, open={"page": "life/game"},
                                           avatar_html='<span class="frame"><img src="file/life/sprite/casual?v=1" alt=""></span>'),
                                      tile("meal", 100, open={"sheet": "life/add/meal", "title": "Add a meal"}),
                                      tile("foreign", 30, open={"action": "ha/toggle/light.x"}),
                                      tile("climb", 31, open={"action": "life/y/a\\%2e%2e\\%2e%2e\\%2e%2e\\power\\sleep"}),
                                      "junk"],
                            "ha": [tile("light.x", 30, open={"action": "ha/toggle/light.x"})]}},
        }

        def request(cfg, rid, path, body=None, timeout=remote.TIMEOUT):
            return json.dumps(self.answers[rid]).encode()

        p = mock.patch.object(remote, "_request", request)
        p.start()
        self.addCleanup(p.stop)

    def test_prefixed_like_cards(self):
        desk = remote.Remote("desk.agents", self.CFG).tiles(self.CFG)
        self.assertEqual(desk[0]["open"], {"sheet": "desk.agents/view/t1", "title": "Fix"})
        av, meal, foreign, climb = remote.Remote("pi.life", self.CFG).tiles(self.CFG)
        self.assertEqual(av["open"], {"page": "pi.life/game"})
        self.assertIn('src="file/pi.life/sprite/casual?v=1"', av["avatar_html"])
        self.assertEqual(meal["open"]["sheet"], "pi.life/add/meal")
        self.assertEqual(foreign["open"], {})  # life's tile can't fire ha's toggle: the target is dropped
        [light] = remote.Remote("pi.ha", self.CFG).tiles(self.CFG)  # ha's own tile can
        self.assertEqual(light["open"], {"action": "pi.ha/toggle/light.x"})

    def test_tab_stays_local_and_other_modules_stay_out(self):
        [t] = remote.Remote("desk.agenthealth", self.CFG).tiles(self.CFG)
        self.assertEqual(t["open"], {"tab": "agents"})
        self.assertEqual(remote.Remote("desk.overview", self.CFG).tiles(self.CFG), [])

    def test_unreachable_or_older_panel(self):
        del self.answers["pi"]["tiles"]
        self.assertEqual(remote.Remote("pi.life", self.CFG).tiles(self.CFG), [])
        remote._live.clear()
        with mock.patch.object(remote, "_request", side_effect=OSError("down")):
            self.assertEqual(remote.Remote("desk.agents", self.CFG).tiles(self.CFG), [])

    def test_through_the_app(self):
        mods = [remote.Remote(n, self.CFG) for n in self.CFG["modules"]]
        with mock.patch("modules.night.quiet", return_value=False):
            d = app.live_payload(self.CFG, mods)
        json.dumps(d)
        self.assertEqual([t["key"] for t in d["tiles"]["pi.life"]], ["avatar", "meal", "foreign", "climb"])
        opens = {t["key"]: t["open"] for t in d["tiles"]["pi.life"]}
        self.assertEqual((opens["foreign"], opens["climb"]), (None, None))  # a tap scrolls to the card instead
        self.assertEqual(d["tiles"]["pi.ha"][0]["open"], {"action": "pi.ha/toggle/light.x"})
        self.assertNotIn("power", d["grid"])
        self.assertNotIn("%2e", d["grid"])
        self.assertIn('data-sheet="desk.agents/view/t1"', d["grid"])
        self.assertIn('data-page="pi.life/game"', d["grid"])
        self.assertIn('data-action="pi.ha/toggle/light.x"', d["grid"])
        self.assertIn('data-goto="pi.life"', app.grid_html(mods, {"pi.life": [app.clean_tile(tile("x"), "pi.life")]}))


class Caches(unittest.TestCase):
    """One /live poll renders the card and the tiles of a module (and three cards read the levels): the slow
    reads behind them run once per ~2 s, whichever thread asks."""

    def setUp(self):
        for d in (ha._fresh, agents._levels):
            d.clear()
            self.addCleanup(d.clear)
        life._cache.pop("fresh", None)
        self.addCleanup(life._cache.pop, "fresh", None)

    def test_ha_states_read_once(self):
        states = [{"entity_id": "light.a", "state": "on", "attributes": {}},
                  {"entity_id": "light.hidden", "state": "on", "attributes": {}}]
        api = mock.Mock(return_value=(json.dumps(states).encode(), "application/json"))
        cfg = {"ha": {"exclude": ["light.hidden"]}}
        with mock.patch.object(ha, "_api", api), mock.patch.object(ha, "_token", return_value="t"):
            ha.card(cfg)
            self.assertEqual([t["key"] for t in ha.tiles(cfg)], ["light.a"])
            self.assertEqual(api.call_count, 1)
            self.assertEqual(len(ha._states({"ha": {}})), 2)  # another config is another read
            self.assertEqual(api.call_count, 2)
            ha.ACTIONS["toggle"](cfg, "light.a", {})  # a toggle drops the cache: the next poll shows it
            ha._states(cfg)
            self.assertEqual(api.call_count, 4)
            with mock.patch.object(ha, "FRESH", 0):
                ha._states(cfg)
            self.assertEqual(api.call_count, 5)

    def test_ha_failure_is_kept_too(self):
        api = mock.Mock(side_effect=OSError("timed out"))
        with mock.patch.object(ha, "_api", api), mock.patch.object(ha, "_token", return_value="t"):
            self.assertIn("unreachable", ha.card({}))
            self.assertEqual(ha.tiles({}), [])
        self.assertEqual(api.call_count, 1)  # one 5 s timeout per poll, not one per caller

    def test_ha_threads_share_one_read(self):
        import threading

        def slow(cfg, path, body=None, timeout=5):
            time.sleep(0.05)
            return b"[]", ""
        api = mock.Mock(side_effect=slow)
        with mock.patch.object(ha, "_api", api):
            ts = [threading.Thread(target=ha._states, args=({},)) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(api.call_count, 1)

    def test_life_game_once_per_poll(self):
        computed = mock.Mock(side_effect=lambda cfg: {"n": computed.call_count})
        cfg = {"life": {"name": "Ana"}}
        with mock.patch.object(life, "_game", computed):
            self.assertIs(life.game(cfg), life.game(dict(cfg)))  # the card and the tiles of one poll
            self.assertEqual(computed.call_count, 1)
            life.game({"life": {"name": "Bo"}})  # another config
            self.assertEqual(computed.call_count, 2)
            life._ver[0] += 1  # a write here shows at once
            life.game(cfg)
            self.assertEqual(computed.call_count, 3)
            with mock.patch.object(life, "FRESH", 0):
                life.game(cfg)
            self.assertEqual(computed.call_count, 4)

    def test_levels_once_per_poll(self):
        read = mock.Mock(return_value={("p", "claude"): 3})
        with mock.patch.object(agenthealth, "levels", read):
            for _ in range(3):  # agents, overview and agenthealth cards
                self.assertEqual(agents.levels(), {("p", "claude"): 3})
            self.assertEqual(read.call_count, 1)
            with mock.patch.object(agents, "LEVELS_FRESH", 0):
                agents.levels()
            self.assertEqual(read.call_count, 2)

if __name__ == "__main__":
    unittest.main()

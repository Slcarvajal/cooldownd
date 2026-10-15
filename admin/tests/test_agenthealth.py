import json
import os
import tempfile
import unittest

import tests
from modules import agenthealth, agentlog
from modules.common import STATE

DAY = "2026-10-10"
START, END = (int(t) for t in agenthealth._bounds(DAY))
C = {"stuck_min": 10, "repeat": 3, "finish_after_s": 45, "forgotten_days": 2}


def item(status, handle="t1", **kw):
    return {"handle": handle, "title": "Fix login", "project": "api-server", "agent": "claude", "status": status,
            "summary": kw.pop("summary", "Done: login fixed."), "age": kw.pop("age", 1.0), **kw}


def spinner(secs, extra=()):
    return ["● Running the test suite", *extra, f"✻ Twisting… ({secs}s · thinking with high effort)"]


def kinds(events, kind):
    return [d for k, d in events if k == kind]


class Stuck(unittest.TestCase):
    def setUp(self):
        self.w = agenthealth.Watch(C)

    def step(self, status, body, now, **kw):
        a = item(status, **kw)
        ev = self.w.observe([a], {"t1": body}, now)
        return a, ev

    def test_spinner_timer_still_counts_as_unchanged(self):
        out = []
        for i in range(0, 12 * 60 + 1, 60):
            a, ev = self.step("working", spinner(7 + i), i)
            out += kinds(ev, "stuck")
            if i < 600:
                self.assertEqual(a["status"], "working", i)
        self.assertEqual(a["status"], "stuck")
        self.assertEqual(len(out), 1)  # one event for the whole episode
        self.assertEqual(out[0]["why"], "unchanged")
        self.assertEqual(out[0]["line"], "● Running the test suite")
        self.assertEqual(set(out[0]), {"handle", "title", "project", "why", "line"})

    def test_episode_ends_when_screen_changes(self):
        for i in range(0, 11 * 60, 60):
            self.step("working", spinner(i), i)
        a, ev = self.step("working", spinner(1, ["● Edited app.py"]), 700)
        self.assertEqual((a["status"], kinds(ev, "stuck")), ("working", []))
        stuck = []
        for i in range(760, 760 + 11 * 60, 60):
            a, ev = self.step("working", spinner(i, ["● Edited app.py"]), i)
            stuck += kinds(ev, "stuck")
        self.assertEqual(len(stuck), 1)  # a new episode, a new event

    def test_leaving_working_ends_the_episode(self):
        for i in range(0, 11 * 60, 60):
            self.step("working", spinner(i), i)
        a, _ = self.step("waiting", ["Do you want to proceed?"], 700)
        self.assertEqual(a["status"], "waiting")
        a, ev = self.step("working", spinner(1), 760)
        self.assertEqual((a["status"], kinds(ev, "stuck")), ("working", []))

    def test_background_agents_are_exempt(self):
        body = ["● Waiting for 2 background agents", "  ⎿ still running"]
        for i in range(0, 20 * 60, 60):
            a, ev = self.step("working", body, i)
            self.assertEqual(kinds(ev, "stuck"), [])
        self.assertEqual(a["status"], "working")

    def test_repeated_error_marks_stuck(self):
        err = "FAILED tests/test_login.py::test_ok - AssertionError: {} != 200"
        screen, out = ["● Bash(pytest)"], []
        for n in range(3):
            screen = screen + [err.format(400 + n), f"● Trying fix number {n}"]
            a, ev = self.step("working", screen, n * 120)
            out += kinds(ev, "stuck")
        self.assertEqual(a["status"], "stuck")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["why"], "repeating")
        a, ev = self.step("working", screen + ["● One more try", err.format(404)], 400)
        self.assertEqual((a["status"], kinds(ev, "stuck")), ("stuck", []))  # same episode, no second event

    def test_error_that_just_stays_on_screen_is_not_stuck(self):
        screen = ["● Bash(pytest)", "FAILED tests/test_x.py::test_a - AssertionError", "● Fixing it"]
        for n in range(6):
            a, ev = self.step("working", screen + [f"● Step {'abcdef'[n]} done"], n * 60)
            self.assertEqual(kinds(ev, "stuck"), [])
        self.assertEqual(a["status"], "working")

    def test_repeats_outside_30_min_dont_count(self):
        err = "Traceback (most recent call last):"
        for n in range(4):
            a, ev = self.step("working", ["● Run it", err, f"● Attempt {'abcd'[n]}"], n * 1000)
            self.step("working", [f"● Fresh screen {'abcd'[n]}"], n * 1000 + 60)  # the error leaves the screen
            self.assertEqual(kinds(ev, "stuck"), [])


class Finished(unittest.TestCase):
    def setUp(self):
        self.w = agenthealth.Watch(C)

    def run_(self, timeline, **kw):
        out = []
        for now, status in timeline:
            out += kinds(self.w.observe([item(status, **kw)], {"t1": []}, now), "finished")
        return out

    def test_stable_idle_after_a_minute_of_work(self):
        out = self.run_([(0, "working"), (30, "working"), (70, "working"), (80, "idle"), (110, "idle"), (125, "idle"),
                         (200, "idle")], summary="x" * 400)
        self.assertEqual(len(out), 1)
        self.assertLessEqual(len(out[0]["recap"]), 220)
        self.assertEqual(out[0]["handle"], "t1")

    def test_flapping_under_45_s_makes_no_task(self):
        timeline = []
        for k in range(6):
            timeline += [(k * 40, "working"), (k * 40 + 10, "idle"), (k * 40 + 30, "idle")]
        self.assertEqual(self.run_(timeline), [])

    def test_short_work_makes_no_task(self):
        self.assertEqual(self.run_([(0, "working"), (30, "working"), (40, "idle"), (100, "idle"), (200, "idle")]), [])

    def test_a_question_is_not_finished(self):
        self.assertEqual(self.run_([(0, "working"), (90, "working"), (100, "waiting"), (120, "idle"), (300, "idle")]), [])

    def test_forgotten(self):
        a = item("idle", age=3 * 86400)
        self.w.observe([a, item("working", handle="t2", age=3 * 86400)], {}, 0)
        self.assertTrue(a["forgotten"])
        b = item("idle", age=86400)
        self.w.observe([b], {}, 1)
        self.assertFalse(b["forgotten"])


class Limit(unittest.TestCase):
    def setUp(self):
        self.w = agenthealth.Watch(C)

    def step(self, status, body, now):
        return kinds(self.w.observe([item(status)], {"t1": body}, now), "limit")

    def test_diff_that_mentions_rate_limit_does_not_fire(self):
        diff = ["● Update(client.py)", "  ⎿  Updated client.py with 3 additions",
                "      41 +    # back off when we get rate limited", "      42 +    if resp.status == 429:",
                "      43 -    raise RateLimitError('rate limit hit')", "● Added the rate limit handling."]
        self.assertEqual(self.step("idle", diff, 0), [])

    def test_banner_fires_once_per_episode(self):
        banner = ["● Working on the parser", "  ⎿  Claude usage limit reached. Your limit will reset at 3pm."]
        first = self.step("idle", banner, 0)
        self.assertEqual(len(first), 1)
        self.assertEqual(set(first[0]), {"handle", "title", "project", "line"})
        self.assertEqual(self.step("idle", banner, 60), [])
        self.assertEqual(self.step("idle", ["● Back to it"], 120), [])
        self.assertEqual(len(self.step("idle", ["● x", "You've hit your weekly limit · resets Mon"], 180)), 1)

    def test_banner_far_up_or_while_working_does_not_fire(self):
        old = ["API Error: 429 rate_limit_error"] + [f"● line {c}" for c in "abcdef"]
        self.assertEqual(self.step("idle", old, 0), [])
        self.assertEqual(self.step("working", ["  ⎿  API Error: 429"], 60), [])


class Blocked(unittest.TestCase):
    def test_crosses_midnight_and_respects_offline(self):
        def r(ts, handle, status):
            return {"ts": ts, "handle": handle, "status": status, "title": handle.upper(), "project": "p"}

        rows = [r(START - 600, "h1", "waiting"),                            # carried over from the night before
                r(START + 600, "h1", "working"),                            # 10 min today
                r(START + 3600, "h3", "waiting"), r(START + 4800, "*", "offline"),   # 20 min, then the desk slept
                r(START + 7200, "h3", "atpc"), r(START + 7800, "h3", "gone"),         # 10 min
                r(END - 300, "h2", "atpc")]                                 # 5 min, cut at midnight
        b = agenthealth.blocked(rows, START, END, until=END + 3000)
        self.assertEqual(b["total"], 45)
        self.assertEqual({x["handle"]: x["min"] for x in b["by"]}, {"h1": 10, "h2": 5, "h3": 30})
        self.assertEqual(b["by"][0]["handle"], "h3")
        self.assertEqual(agenthealth.sessions(rows, START, END, END), {"h1"})

    def test_open_interval_runs_until_the_last_sample(self):
        rows = [{"ts": START + 60, "handle": "h", "status": "waiting", "title": "", "project": ""}]
        self.assertEqual(agenthealth.blocked(rows, START, END, until=START + 60 + 900)["total"], 15)


class Glue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=tests.HOME)
        self._orig = agentlog.DB_PATH
        agentlog.DB_PATH = os.path.join(self.dir, "agents.db")
        agentlog._made.clear()
        agenthealth._watch = agenthealth.Watch()
        self._state = STATE.pop("agents", None)

    def tearDown(self):
        agentlog.DB_PATH = self._orig
        agentlog._made.clear()
        STATE.pop("agents", None)
        if self._state is not None:
            STATE["agents"] = self._state

    def observe(self, status, now, **kw):
        a = item(status, **kw)
        agenthealth.observe({}, [a], {a["handle"]: []}, now)
        return a

    def test_seen_only_on_change_and_offline_after_a_gap(self):
        t0 = START + 3600
        for t in range(t0, t0 + 601, 60):
            self.observe("waiting", t)
        for t in range(t0 + 1800, t0 + 2401, 60):  # the desk slept for 20 min in between
            self.observe("waiting" if t < t0 + 2400 else "idle", t)
        rows = [(r["handle"], r["status"]) for r in agentlog.q("SELECT * FROM seen ORDER BY id")]
        self.assertEqual(rows, [("t1", "waiting"), ("*", "offline"), ("t1", "waiting"), ("t1", "idle")])
        self.assertEqual(agentlog.q("SELECT ts FROM seen WHERE handle = '*'")[0]["ts"], t0 + 600)
        self.assertEqual(agenthealth.blocked_minutes(DAY)["total"], 20)

    def test_gone(self):
        self.observe("idle", START + 10)
        agenthealth.observe({}, [], {}, START + 20)
        self.assertEqual([r["status"] for r in agentlog.q("SELECT status FROM seen ORDER BY id")], ["idle", "gone"])

    def test_finished_task_and_event(self):
        t = START + 100
        for dt, status in ((0, "working"), (30, "working"), (60, "working"), (90, "idle"), (120, "idle"), (140, "idle")):
            self.observe(status, t + dt, summary="※ " + "y" * 300)
        tasks = agentlog.q("SELECT * FROM task")
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["day"], tasks[0]["project"], tasks[0]["agent"]), (DAY, "api-server", "claude"))
        ev = agentlog.outbox(0)["events"]
        self.assertEqual([x["kind"] for x in ev], ["finished"])
        self.assertEqual(set(ev[0]["data"]), {"task", "handle", "title", "project", "recap"})
        self.assertEqual(ev[0]["data"]["task"], tasks[0]["id"])
        self.assertLessEqual(len(ev[0]["data"]["recap"]), 220)

    def test_dismiss_one_and_all(self):
        ids = [agentlog.w("INSERT INTO task(ts, day, handle, project, title, agent, recap) VALUES(?, ?, 'h', 'p', 'T', 'claude', 'r')",
                          (START + i, DAY)) for i in range(3)]
        self.assertIn("reviewed", agenthealth.dismiss({}, str(ids[0]), {}))
        self.assertIsNotNone(agentlog.q("SELECT dismissed FROM task WHERE id = ?", (ids[0],))[0]["dismissed"])
        self.assertEqual(agenthealth.dismiss({}, "all", {}), "2 finished tasks marked as reviewed.")
        self.assertEqual(agentlog.q("SELECT count(*) AS n FROM task WHERE dismissed IS NULL")[0]["n"], 0)
        with self.assertRaises(ValueError):
            agenthealth.dismiss({}, "999", {})
        self.assertIn("Reviewed", agenthealth._ready_sheet({}, str(ids[0])))

    def test_files_and_sheets(self):
        for _ in range(4):
            agentlog.w("INSERT INTO task(ts, day, handle, project, title, agent, recap) VALUES(?, ?, 'h', 'api-server', 'T', 'claude', 'r')",
                       (START + 5, DAY))
        STATE["agents"] = {"at": 123.0, "items": [item("stuck", forgotten=False), item("idle", handle="t2", agent="codex")]}
        ctype, body = agenthealth.party_file({}, "now")
        p = json.loads(body)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(p["at"], 123.0)
        self.assertEqual([(m["key"], m["level"], m["done"], m["status"]) for m in p["members"]],
                         [("t1", 3, 4, "stuck"), ("t2", 1, 0, "idle")])
        self.assertEqual(set(p["members"][0]), {"key", "title", "project", "agent", "status", "level", "done", "forgotten"})
        eid = agentlog.emit("stuck", {"handle": "t1"}, ts=START + 50)
        out = json.loads(agenthealth.outbox_file({}, "0")[1])
        self.assertEqual((out["last"], out["events"][0]["kind"]), (eid, "stuck"))
        report = agenthealth._report_sheet({"agenthealth": {"tokens": False}}, DAY)
        self.assertIn("agenthealth/report/2026-10-09", report)
        self.assertIn("agenthealth/report/2026-10-11", report)
        self.assertIn("Finished (4)", report)
        html = agenthealth.card({"agenthealth": {"tokens": False}})
        self.assertIn("Stuck (1)", html)
        with self.assertRaises(ValueError):
            agenthealth._report_sheet({}, "not-a-day")


if __name__ == "__main__":
    unittest.main()

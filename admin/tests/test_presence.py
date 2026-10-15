import unittest
from unittest import mock

import tests  # isolation first
from modules import presence
from modules.common import STATE

assert tests.HOME  # the isolation ran: temp HOME, no network, no commands

C = {"sit_min": 50, "break_min": 3, "active_idle_s": 60, "busy_after_min": 20, "busy_min_working": 2}
STEP = presence.SAMPLE_EVERY
T0 = 1_000_000.0
BUSY = [{"status": "working"}, {"status": "stuck"}, {"status": "idle"}]
CALM = [{"status": "working"}, {"status": "idle"}]


def kinds(events, kind):
    return [d for k, d, _ in events if k == kind]


class Run:
    """Feeds a Desk one sample every SAMPLE_EVERY seconds."""

    def __init__(self, c=C):
        self.desk, self.now, self.events = presence.Desk(c), T0, []
        self.last_input = T0

    def active(self, secs, agents=()):
        end = self.now + secs
        while self.now < end:
            self.last_input = self.now - 5
            self.events += self.desk.step(5, self.now, list(agents))
            self.now += STEP

    def away(self, secs, agents=()):
        end = self.now + secs
        while self.now < end:
            self.events += self.desk.step(self.now - self.last_input, self.now, list(agents))
            self.now += STEP


class Sitting(unittest.TestCase):
    def test_fifty_minutes_give_one_sit(self):
        r = Run()
        r.active(80 * 60)
        sits = kinds(r.events, "sit")
        self.assertEqual(len(sits), 1)
        self.assertEqual(sits[0]["active_min"], 50)
        self.assertEqual(kinds(r.events, "break"), [])

    def test_short_pauses_stay_in_the_span(self):
        r = Run()
        r.active(30 * 60)
        r.away(2 * 60)  # under break_min
        r.active(25 * 60)
        self.assertEqual(len(kinds(r.events, "sit")), 1)
        self.assertEqual(kinds(r.events, "presence"), [])

    def test_three_idle_minutes_give_a_break_after_sit(self):
        r = Run()
        r.active(55 * 60)
        r.away(4 * 60)
        breaks, spans = kinds(r.events, "break"), kinds(r.events, "presence")
        self.assertEqual(len(breaks), 1)
        self.assertEqual(len(spans), 1)
        b = breaks[0]
        self.assertTrue(b["after_sit"])
        self.assertFalse(b["nudge"])
        self.assertGreaterEqual(b["idle_s"], 180)
        self.assertEqual(b["span_min"], 54)  # T0 - 5 to the last input, 15 s before the 55th minute
        self.assertEqual(spans[0]["start"], round(T0 - 5, 1))
        self.assertAlmostEqual(spans[0]["end"], r.last_input, delta=1)

    def test_break_before_sit_isnt_after_sit(self):
        r = Run()
        r.active(20 * 60)
        r.away(4 * 60)
        self.assertFalse(kinds(r.events, "break")[0]["after_sit"])

    def test_a_new_span_can_sit_again(self):
        r = Run()
        r.active(55 * 60)
        r.away(5 * 60)
        r.active(55 * 60)
        self.assertEqual(len(kinds(r.events, "sit")), 2)

    def test_suspend_closes_the_span_at_the_last_input(self):
        r = Run()
        r.active(20 * 60)
        last = r.last_input
        r.now += 2 * 3600  # the desk slept
        r.active(60)
        spans = kinds(r.events, "presence")
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0]["end"], last, delta=0.1)
        self.assertEqual(kinds(r.events, "break"), [])
        self.assertIsNotNone(r.desk.span_start)  # a new span started on wake
        self.assertGreater(r.desk.span_start, last)

    def test_no_idle_reading_changes_nothing(self):
        d = presence.Desk(C)
        self.assertEqual(d.step(None, T0, BUSY), [])
        self.assertIsNone(d.span_start)


class Nudge(unittest.TestCase):
    def test_window_nudges_once(self):
        r = Run()
        r.active(45 * 60, BUSY)
        nudges = kinds(r.events, "nudge")
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]["window"], int(T0))

    def test_needs_enough_agents_and_none_waiting(self):
        r = Run()
        r.active(30 * 60, CALM)
        r.active(30 * 60, BUSY + [{"status": "waiting"}])
        self.assertEqual(kinds(r.events, "nudge"), [])

    def test_only_while_the_desk_is_active(self):
        r = Run()
        r.active(60, BUSY)
        r.away(25 * 60, BUSY)  # agents busy, nobody at the desk
        self.assertEqual(kinds(r.events, "nudge"), [])
        r.active(60, BUSY)
        self.assertEqual(len(kinds(r.events, "nudge")), 1)

    def test_break_answers_the_nudge_once(self):
        r = Run()
        r.active(25 * 60, BUSY)
        r.away(4 * 60, BUSY)
        r.active(10 * 60, BUSY)
        r.away(4 * 60, BUSY)
        breaks = kinds(r.events, "break")
        self.assertEqual([b["nudge"] for b in breaks], [True, False])
        self.assertEqual(len(kinds(r.events, "nudge")), 1)

    def test_grace_after_the_window_closes(self):
        r = Run()
        r.active(25 * 60, BUSY)
        r.active(2 * 60, CALM)  # window closes
        r.away(4 * 60, CALM)
        self.assertTrue(kinds(r.events, "break")[0]["nudge"])
        r2 = Run()
        r2.active(25 * 60, BUSY)
        r2.active(10 * 60, CALM)  # closed long before the break
        r2.away(4 * 60, CALM)
        self.assertFalse(kinds(r2.events, "break")[0]["nudge"])

    def test_new_window_nudges_again(self):
        r = Run()
        r.active(25 * 60, BUSY)
        r.active(60, CALM)
        r.active(25 * 60, BUSY)
        self.assertEqual(len(kinds(r.events, "nudge")), 2)


class Notices(unittest.TestCase):
    def test_sit_and_nudge_notices_have_global_ids(self):
        r = Run()
        r.active(25 * 60, BUSY)
        self.assertEqual([n["id"] for n in r.desk.notices(r.now)], [f"desk:nudge:{int(T0)}"])
        r.active(26 * 60, CALM)
        self.assertEqual([n["id"] for n in r.desk.notices(r.now)], [f"desk:sit:{int(T0 - 5)}"])
        for n in r.desk.notices(r.now):
            self.assertGreater(n["until"], r.now)
            self.assertTrue(n["text"])

    def test_notices_expire(self):
        r = Run()
        r.active(51 * 60)
        self.assertEqual(len(r.desk.notices(r.now)), 1)
        r.active(presence.NOTICE_S + 60)
        self.assertEqual(r.desk.notices(r.now), [])

    def test_break_clears_notices(self):
        r = Run()
        r.active(51 * 60)
        r.away(4 * 60)
        self.assertEqual(r.desk.notices(r.now), [])


class Glue(unittest.TestCase):
    def setUp(self):
        presence._desk.clear()
        self.addCleanup(presence._desk.clear)
        self.addCleanup(STATE.pop, "notices", None)
        self.addCleanup(STATE.pop, "presence", None)
        self.addCleanup(STATE.pop, "agents", None)

    def test_sample_emits_and_publishes_notices(self):
        cfg = {"presence": {"sit_min": 1}}
        STATE["agents"] = {"items": BUSY}
        sent = []
        clock = iter([T0, T0 + 15, T0 + 30, T0 + 45, T0 + 60, T0 + 75])
        with mock.patch.object(presence, "_emit", lambda *a: sent.append(a)), \
                mock.patch.object(presence.idle, "idle_seconds", return_value=1.0), \
                mock.patch.object(presence.time, "time", lambda: next(clock)):
            for _ in range(6):
                presence.sample(cfg)
        self.assertEqual([k for k, _, _ in sent], ["sit"])
        self.assertEqual([n["id"] for n in STATE["notices"]], [f"desk:sit:{int(T0 - 1)}"])
        self.assertIn("Sitting", presence.card(cfg))

    def test_card_without_idle(self):
        STATE["presence"] = {"idle_s": None}
        self.assertIn("isn't available", presence.card({}))


if __name__ == "__main__":
    unittest.main()

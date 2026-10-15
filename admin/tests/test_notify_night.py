import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from modules import bridge, notify
from tests.test_bridge import Desk, ev

CFG = {"notify": {"panel": "https://pi.example.ts.net:8443/", "after_seconds": 60},
       "night": {"bedtime": "23:30", "wake": "06:30"}}


def at(d, h, mi=0):
    return datetime(2026, 10, d, h, mi).timestamp()


def agent(key, title):
    return {"key": key, "sheet": f"agents/view/{key}", "title": title, "project": "api-server", "summary": "Allow edit?"}


class Night(unittest.TestCase):
    def setUp(self):
        notify._seen.clear()
        notify._sent.clear()
        notify._held.clear()
        notify._overnight.update(n=0, detail=[])
        self.waiting = []
        patches = [mock.patch.object(notify.webpush, "subscriptions", return_value=[{"endpoint": "x", "label": "Phone", "added": "2026-10-01"}]),
                   mock.patch.object(notify.webpush, "public_key", return_value="key"),
                   mock.patch.object(notify.webpush, "send_all", return_value=(1, [])),
                   mock.patch.object(notify, "_waiting", side_effect=lambda cfg: list(self.waiting))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.send_all = notify.webpush.send_all

    def tick(self, now, cfg=CFG):
        with mock.patch("time.time", return_value=now):
            notify.sample(cfg)

    def titles(self):
        return [c.args[0]["title"] for c in self.send_all.call_args_list]

    def test_held_overnight_then_one_summary(self):
        self.waiting = [agent("a", "Alpha"), agent("b", "Beta")]
        self.tick(at(15, 2, 0))
        self.tick(at(15, 2, 5))
        self.assertEqual(self.titles(), [])
        self.assertEqual(sorted(notify._held), ["a", "b"])
        self.assertIn("Held for the morning", notify.card(CFG))
        self.tick(at(15, 6, 31))
        self.assertEqual(self.titles(), ["2 agents waited overnight"])
        self.assertEqual(self.send_all.call_args.args[0]["body"], "Alpha, Beta")
        self.assertEqual(notify._held, {})
        self.tick(at(15, 6, 40))  # still waiting: already counted as sent
        self.assertEqual(len(self.titles()), 1)

    def test_answered_overnight_alerts_again_later(self):
        self.waiting = [agent("a", "Alpha")]
        self.tick(at(15, 2, 0))
        self.tick(at(15, 2, 5))
        self.waiting = []  # answered from the desk at 3 am
        self.tick(at(15, 3, 0))
        self.tick(at(15, 6, 31))
        self.assertEqual(self.titles(), ["1 agent waited overnight"])
        self.assertNotIn("a", notify._sent)
        self.waiting = [agent("a", "Alpha")]  # waits again in the morning: alerts on its own
        self.tick(at(15, 9, 0))
        self.tick(at(15, 9, 2))
        self.assertEqual(self.titles(), ["1 agent waited overnight", "Alpha needs you"])

    def test_daytime_unchanged(self):
        self.waiting = [agent("a", "Alpha")]
        self.tick(at(15, 12, 0))
        self.tick(at(15, 12, 0) + 30)
        self.assertEqual(self.titles(), [])
        self.tick(at(15, 12, 2))
        self.assertEqual(self.titles(), ["Alpha needs you"])
        self.assertEqual(notify._held, {})

    def test_no_night_config_never_holds(self):
        cfg = {"notify": CFG["notify"]}
        self.waiting = [agent("a", "Alpha")]
        self.tick(at(15, 2, 0), cfg)
        self.tick(at(15, 2, 5), cfg)
        self.assertEqual(self.titles(), ["Alpha needs you"])


class OneWakePush(unittest.TestCase):
    """Agents that waited overnight (notify) and desk updates held overnight (bridge) make one push."""
    CFG = {**CFG, "bridge": {"remote": "desk", "fresh_s": 600}, "remotes": {"desk": {"url": "https://desk.example.ts.net:8443"}}}
    tick, titles = Night.tick, Night.titles

    def setUp(self):
        Night.setUp(self)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for p in (mock.patch.object(bridge, "PATH", os.path.join(self.dir, "bridge.json")),
                  mock.patch.object(bridge, "_record", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.desk = Desk("e1", [ev(1, "finished", at(15, 2, 0), task=1, title="Gamma"),
                                ev(2, "stuck", at(15, 2, 1), handle="h", title="Delta", why="unchanged")])

    def bridge_at(self, now):
        with mock.patch("time.time", return_value=now), mock.patch.object(bridge.remote, "_fetch", self.desk.fetch):
            bridge.sample(self.CFG)

    def night(self):
        self.waiting = [agent("a", "Alpha"), agent("b", "Beta")]
        self.tick(at(15, 2, 0), self.CFG)
        self.bridge_at(at(15, 2, 2))
        self.tick(at(15, 2, 5), self.CFG)
        self.assertEqual(self.titles(), [])
        self.assertEqual(sorted(notify._held), ["a", "b"])
        self.assertTrue(bridge.holding(self.CFG))

    def check_one(self):
        self.assertEqual(self.titles(), ["2 agents waited overnight · 2 agent updates"])
        body = self.send_all.call_args.args[0]["body"]
        for name in ("Alpha", "Beta", "Gamma", "Delta"):
            self.assertIn(name, body)
        self.tick(at(15, 6, 33), self.CFG)
        self.bridge_at(at(15, 6, 34))
        self.tick(at(15, 6, 40), self.CFG)
        self.assertEqual(len(self.titles()), 1)

    def test_notify_waits_for_the_bridge(self):
        self.night()
        self.tick(at(15, 6, 31), self.CFG)  # the bridge still holds its updates: nothing yet, not even one by one
        self.assertEqual(self.titles(), [])
        self.bridge_at(at(15, 6, 31) + 5)
        self.assertEqual(self.titles(), [])  # handed over, not pushed
        self.tick(at(15, 6, 31) + 10, self.CFG)
        self.check_one()

    def test_bridge_first(self):
        self.night()
        self.bridge_at(at(15, 6, 31))
        self.tick(at(15, 6, 31) + 10, self.CFG)
        self.check_one()

    def test_only_desk_updates(self):
        self.bridge_at(at(15, 2, 2))
        self.bridge_at(at(15, 6, 31))
        self.tick(at(15, 6, 31) + 10, self.CFG)
        self.assertEqual(self.titles(), ["Overnight: 2 agent updates"])


if __name__ == "__main__":
    unittest.main()

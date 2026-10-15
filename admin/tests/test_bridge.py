import json
import os
import shutil
import stat
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest import mock

from modules import bridge
from modules.common import STATE

DAY = datetime(2026, 10, 14, 15, 0).timestamp()
NIGHT = datetime(2026, 10, 15, 2, 0).timestamp()
MORNING = datetime(2026, 10, 15, 7, 0).timestamp()
CFG = {"bridge": {"remote": "desk", "fresh_s": 600}, "remotes": {"desk": {"url": "https://desk.example.ts.net:8443"}},
       "notify": {"panel": "https://pi.example.ts.net:8443/"}}
NIGHT_CFG = {**CFG, "night": {"bedtime": "23:30", "wake": "06:30"}}


def ev(i, kind, ts, **data):
    return {"id": i, "ts": ts, "kind": kind, "data": data}


class Desk:
    """A fake desktop: outbox pages by cursor, per epoch, plus the party."""

    def __init__(self, epoch, events, party=None):
        self.epoch, self.events, self.party, self.down, self.paths = epoch, events, party, False, []

    def fetch(self, cfg, rid, path, body=None, timeout=4):
        self.paths.append(path)
        if self.down:
            raise urllib.error.URLError("asleep")
        if path == "file/agenthealth/party/now":
            return "application/json", json.dumps(self.party or {"at": 0, "members": []}).encode()
        since = int(path.rsplit("/", 1)[1])
        page = [x for x in self.events if x["id"] > since][:2]  # small pages, so paging is exercised
        return "application/json", json.dumps({"epoch": self.epoch, "last": page[-1]["id"] if page else since,
                                               "events": page}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patches = [mock.patch.object(bridge, "PATH", os.path.join(self.dir, "sub", "bridge.json")),
                   mock.patch.object(bridge, "_record", return_value=True),
                   mock.patch.object(bridge, "_push", return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.record, self.push = bridge._record, bridge._push
        bridge._log.update(pull=None, error=None, events=0, pushed=0)
        STATE.pop("party", None)

    def run_at(self, now, desk, cfg=CFG):
        with mock.patch("time.time", return_value=now), mock.patch.object(bridge.remote, "_fetch", desk.fetch):
            bridge.sample(cfg)
        return bridge._load()


class Cursor(Base):
    def test_pages_cursor_and_file_mode(self):
        desk = Desk("e1", [ev(1, "answer", DAY - 5, handle="h", away=True, idle_s=300),
                           ev(2, "finished", DAY - 5, task=7, handle="h", title="Fix", project="api-server", recap="All green"),
                           ev(3, "presence", DAY - 5, start=1, end=2)])
        st = self.run_at(DAY, desk)
        self.assertEqual((st["epoch"], st["cursor"]), ("e1", 3))
        self.assertEqual([c.args[:3] for c in self.record.call_args_list],
                         [("desk:e1", "1", "answer"), ("desk:e1", "3", "presence")])
        self.assertEqual(self.push.call_count, 1)
        title, body = self.push.call_args.args[1:3]
        self.assertEqual((title, body), ("Fix finished", "api-server: All green"))
        self.assertIn("desk.agenthealth%2Fready%2F7", self.push.call_args.kwargs["url"])
        self.assertEqual(stat.S_IMODE(os.stat(bridge.PATH).st_mode), 0o600)
        self.assertIsNone(bridge._log["error"])
        # nothing new: nothing recorded or pushed again
        self.run_at(DAY + 20, desk)
        self.assertEqual((self.record.call_count, self.push.call_count), (2, 1))

    def test_new_epoch_starts_over(self):
        self.run_at(DAY, Desk("e1", [ev(1, "break", DAY, idle_s=200), ev(2, "break", DAY, idle_s=200)]))
        st = self.run_at(DAY + 20, Desk("e2", [ev(1, "nudge", DAY - 9999, window="w")]))
        self.assertEqual((st["epoch"], st["cursor"]), ("e2", 1))
        self.assertEqual(self.record.call_args.args[:3], ("desk:e2", "1", "nudge"))

    def test_desk_down_keeps_cursor(self):
        desk = Desk("e1", [ev(1, "answer", DAY, away=False)])
        self.run_at(DAY, desk)
        desk.down = True
        desk.events.append(ev(2, "answer", DAY, away=True))
        st = self.run_at(DAY + 20, desk)
        self.assertEqual(st["cursor"], 1)
        self.assertIn("URLError", bridge._log["error"])
        desk.down = False
        self.assertEqual(self.run_at(DAY + 40, desk)["cursor"], 2)

    def test_failing_record_is_retried(self):
        desk = Desk("e1", [ev(1, "answer", DAY), ev(2, "break", DAY)])
        self.record.side_effect = [True, RuntimeError("database is locked")]
        self.assertEqual(self.run_at(DAY, desk)["cursor"], 1)
        self.assertIn("database is locked", bridge._log["error"])
        self.record.side_effect = None
        self.assertEqual(self.run_at(DAY + 20, desk)["cursor"], 2)
        self.assertEqual(self.record.call_args.args[:3], ("desk:e1", "2", "break"))


class Pushes(Base):
    def test_old_event_records_but_never_pushes(self):
        desk = Desk("e1", [ev(1, "finished", DAY - 3600, task=1, title="Old"),
                           ev(2, "sit", DAY - 3600, active_min=55, span_start=0)])
        self.run_at(DAY, desk)
        self.push.assert_not_called()
        self.assertEqual([c.args[2] for c in self.record.call_args_list], ["sit"])

    def test_recap_is_cut(self):
        self.run_at(DAY, Desk("e1", [ev(1, "finished", DAY, task=1, title="T", project="p", recap="x" * 500)]))
        body = self.push.call_args.args[2]
        self.assertLessEqual(len(body), len("p: ") + bridge.CUT)
        self.assertTrue(body.endswith("…"))

    def test_night_holds_then_one_summary(self):
        desk = Desk("e1", [ev(1, "finished", NIGHT - 10, task=1, title="Alpha"),
                           ev(2, "stuck", NIGHT - 10, handle="h", title="Beta", why="unchanged"),
                           ev(3, "sit", NIGHT - 10, active_min=60, span_start=0),
                           ev(4, "nudge", NIGHT - 10, window="w")])
        st = self.run_at(NIGHT, desk, NIGHT_CFG)
        self.push.assert_not_called()
        self.assertEqual([h["kind"] for h in st["held"]], ["finished", "stuck"])
        self.assertEqual([c.args[2] for c in self.record.call_args_list], ["sit", "nudge"])
        self.run_at(NIGHT + 600, desk, NIGHT_CFG)  # still night: nothing
        self.push.assert_not_called()
        self.assertTrue(bridge.holding(NIGHT_CFG))
        with mock.patch.object(bridge.notify, "overnight") as handed:
            st = self.run_at(MORNING, desk, NIGHT_CFG)
            self.push.assert_not_called()  # notify owns the one morning summary
            handed.assert_called_once()
            n, detail = handed.call_args.args
            self.assertEqual(n, 2)
            self.assertIn("Alpha", detail)
            self.assertEqual(st["held"], [])
            self.assertFalse(bridge.holding(NIGHT_CFG))
            self.run_at(MORNING + 20, desk, NIGHT_CFG)
            handed.assert_called_once()

    def test_summary_goes_out_with_the_desk_asleep(self):
        desk = Desk("e1", [ev(1, "limit", NIGHT - 5, handle="h", title="Gamma", line="usage limit reached")])
        self.run_at(NIGHT, desk, NIGHT_CFG)
        desk.down = True
        with mock.patch.object(bridge.notify, "overnight") as handed:
            self.run_at(MORNING, desk, NIGHT_CFG)
        handed.assert_called_once()


class Party(Base):
    def test_party_is_cleaned_into_state(self):
        party = {"at": 1, "members": [{"key": "api-server/claude", "title": "Fix <b>", "project": "api-server", "agent": "claude",
                                       "status": "working", "level": 3, "done": 4, "forgotten": False, "extra": "x"},
                                      "junk", {"key": 5, "level": "9"}]}
        self.run_at(DAY, Desk("e1", [], party))
        p = STATE["party"]
        self.assertEqual(p["at"], DAY)
        self.assertEqual(len(p["members"]), 2)
        self.assertNotIn("extra", p["members"][0])
        self.assertEqual(p["members"][1], {"key": "", "title": "", "project": "", "agent": "", "status": "",
                                           "level": 0, "done": 0, "forgotten": False})

    def test_card_renders(self):
        self.run_at(DAY, Desk("e1", []))
        html = bridge.card(CFG)
        self.assertIn("Last pull", html)
        self.assertIn("epoch e1", html)


if __name__ == "__main__":
    unittest.main()

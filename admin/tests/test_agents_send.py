import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import tests
from modules import agenthealth, agentlog, agents
from modules.common import STATE


def agent(status):
    return {"handle": "t1", "host": "desk", "project": "api-server", "title": "Fix login", "agent": "claude",
            "status": status, "summary": "Want me to push?", "age": 3.0, "writable": True}


class Send(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=tests.HOME)
        self._orig = agentlog.DB_PATH
        agentlog.DB_PATH = os.path.join(self.dir, "agents.db")
        agentlog._made.clear()
        self._state = STATE.get("agents")
        agents._recent.clear()

    def tearDown(self):
        agentlog.DB_PATH = self._orig
        agentlog._made.clear()
        agents._recent.clear()
        STATE["agents"] = self._state
        if self._state is None:
            STATE.pop("agents")

    def send(self, status, body, result=("sent", ""), idle_s=300.0):
        STATE["agents"] = {"at": 0, "items": [agent(status)]}
        with mock.patch.object(agents, "orca_send", return_value=result) as sent, \
                mock.patch("modules.idle.idle_seconds", return_value=idle_s):
            try:
                msg = agents.send({}, "t1", body)
            except RuntimeError as ex:
                msg = str(ex)
        return msg, sent, [ev for ev in agentlog.outbox(0)["events"] if ev["kind"] == "answer"]

    def test_stuck_agent_refuses_text_and_answers(self):
        for body in ({"text": "carry on"}, {"key": "yes"}, {"key": "1"}, {"key": "enter"}):
            msg, sent, answers = self.send("stuck", body)
            self.assertIn("busy", msg)
            sent.assert_not_called()
            self.assertEqual(answers, [])

    def test_stuck_agent_can_still_be_stopped(self):
        msg, sent, answers = self.send("stuck", {"key": "esc"})
        sent.assert_called_once_with("t1", "\x1b", False)
        self.assertEqual(answers, [])  # stopping isn't answering

    def test_away_when_desk_idle(self):
        msg, sent, answers = self.send("waiting", {"key": "yes"}, idle_s=300.0)
        self.assertEqual(msg, "Pressed yes in Fix login.")
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["data"], {"handle": "t1", "title": "Fix login", "project": "api-server",
                                              "away": True, "idle_s": 300})

    def test_at_desk_when_recently_active(self):
        _, _, answers = self.send("waiting", {"text": "Go with option 2"}, idle_s=12.5)
        self.assertEqual((answers[0]["data"]["away"], answers[0]["data"]["idle_s"]), (False, 12))

    def test_idle_unknown_is_not_away(self):
        _, _, answers = self.send("waiting", {"key": "2"}, idle_s=None)
        self.assertEqual((answers[0]["data"]["away"], answers[0]["data"]["idle_s"]), (False, 0))

    def test_threshold_from_config(self):
        STATE["agents"] = {"at": 0, "items": [agent("waiting")]}
        with mock.patch("modules.idle.idle_seconds", return_value=60.0):
            agenthealth.answered({"agenthealth": {"away_idle_s": 30}}, agent("waiting"), "no")
            agenthealth.answered({}, agent("waiting"), "no")
        self.assertEqual([ev["data"]["away"] for ev in agentlog.outbox(0)["events"]], [True, False])

    def test_typed_counts_refused_and_unknown_do_not(self):
        _, _, answers = self.send("idle", {"text": "next task"}, result=("typed", "the agent hasn't started on it yet"))
        self.assertEqual(len(answers), 1)
        agents._recent.clear()
        msg, _, answers = self.send("idle", {"text": "other"}, result=("refused", "no terminal"))
        self.assertIn("Not sent", msg)
        self.assertEqual(len(answers), 1)  # still only the typed one
        _, _, answers = self.send("idle", {"text": "third"}, result=("unknown", "Orca didn't answer in 30 s"))
        self.assertEqual(len(answers), 1)

    def test_ignored_keys(self):
        for key in ("esc", "ctrl-c", "up", "down"):
            agents._recent.clear()
            self.send("waiting", {"key": key})
        self.assertEqual(self.send("waiting", {"key": "enter"})[2][0]["data"]["away"], True)
        self.assertEqual(len(agentlog.outbox(0)["events"]), 1)

    def test_defer_does_not_answer(self):
        STATE["agents"] = {"at": 0, "items": [agent("waiting")]}
        agents.defer({}, "t1", {})
        self.assertEqual(agentlog.outbox(0)["events"], [])

    def test_answered_never_raises(self):
        with mock.patch("modules.idle.idle_seconds", side_effect=OSError("xprintidle gone")):
            agenthealth.answered({}, agent("waiting"), "yes")
        agenthealth.answered("not a config", None, "yes")
        agenthealth.answered({}, None, ["unhashable"])

    def test_log_failure_after_delivery_still_reports_sent(self):
        with mock.patch.object(agentlog, "emit", side_effect=RuntimeError("disk full")):
            msg, sent, _ = self.send("waiting", {"text": "Go with option 2"})
        sent.assert_called_once()
        self.assertEqual(msg, "Sent to Fix login.")


def _orca(*args):
    if args[:2] == ("terminal", "list"):
        return {"terminals": [{"handle": "t1", "agentIdentity": "claude", "title": "Fix login", "writable": True,
                               "worktreePath": "/w/api-server", "lastOutputAt": time.time() * 1000}]}
    if args[:2] == ("terminal", "read"):
        return {"terminal": {"tail": ["Do you want to proceed?", "❯ 1. Yes", "  2. No"]}}
    return {"hosts": []}


class Sample(unittest.TestCase):
    def setUp(self):
        self._state = STATE.pop("agents", None)

    def tearDown(self):
        STATE.pop("agents", None)
        if self._state is not None:
            STATE["agents"] = self._state

    def test_health_failure_still_refreshes_agents(self):
        STATE["agents"] = {"at": 1.0, "items": [agent("waiting")]}  # stale: the agent is busy by now
        with mock.patch.object(agents, "orca", side_effect=_orca), \
                mock.patch.object(agenthealth._watch, "observe", side_effect=KeyError("boom")):
            agents.sample({})
        s = STATE["agents"]
        self.assertGreater(s["at"], time.time() - 60)
        self.assertEqual([a["handle"] for a in s["items"]], ["t1"])

    def test_observe_runs_one_sample_at_a_time(self):
        inside, most = [0], [0]

        def slow(items, bodies, now):
            inside[0] += 1
            most[0] = max(most[0], inside[0])
            time.sleep(0.05)
            inside[0] -= 1
            return []

        with mock.patch.object(agenthealth._watch, "observe", side_effect=slow), \
                mock.patch.object(agentlog, "meta", return_value=None), mock.patch.object(agenthealth, "_record"):
            ts = [threading.Thread(target=agenthealth.observe, args=({}, [], {}, time.time())) for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(most[0], 1)


if __name__ == "__main__":
    unittest.main()

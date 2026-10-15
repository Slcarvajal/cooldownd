import os
import stat
import tempfile
import threading
import unittest

import tests
from modules import agentlog


class AgentLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=tests.HOME)
        self.path = os.path.join(self.dir, "data", "agents.db")
        self._orig = agentlog.DB_PATH
        agentlog.DB_PATH = self.path
        agentlog._made.clear()

    def tearDown(self):
        agentlog.DB_PATH = self._orig
        agentlog._made.clear()

    def test_default_path_under_home(self):
        self.assertEqual(self._orig, os.path.join(tests.HOME, ".local/share/homelab-admin/agents.db"))

    def test_file_is_600(self):
        agentlog.emit("sit", {"active_min": 50})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_emit_and_page(self):
        ids = [agentlog.emit("finished", {"task": i, "title": "é"}, ts=1000 + i) for i in range(5)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 5)
        p1 = agentlog.outbox(0, limit=2)
        self.assertEqual([ev["id"] for ev in p1["events"]], ids[:2])
        self.assertEqual(p1["last"], ids[1])
        self.assertEqual(p1["events"][0], {"id": ids[0], "ts": 1000.0, "kind": "finished",
                                           "data": {"task": 0, "title": "é"}})
        p2 = agentlog.outbox(p1["last"], limit=2)
        self.assertEqual([ev["id"] for ev in p2["events"]], ids[2:4])
        p3 = agentlog.outbox(p2["last"])
        self.assertEqual([ev["id"] for ev in p3["events"]], ids[4:])
        empty = agentlog.outbox(p3["last"])
        self.assertEqual((empty["events"], empty["last"]), ([], p3["last"]))
        self.assertEqual(p1["epoch"], agentlog.meta("epoch"))

    def test_prune_never_reuses_ids(self):
        old = [agentlog.emit("break", {}, ts=1000) for _ in range(3)]
        self.assertEqual(agentlog.prune(now=1000 + 31 * 86400, keep_days=30), 3)
        self.assertEqual(agentlog.outbox(0)["events"], [])
        new = agentlog.emit("break", {}, ts=1000 + 31 * 86400)
        self.assertGreater(new, max(old))
        self.assertEqual(agentlog.prune(now=1000 + 31 * 86400, keep_days=30), 0)

    def test_epoch_stable(self):
        epoch = agentlog.meta("epoch")
        self.assertRegex(epoch, r"^[0-9a-f]{16}$")
        agentlog.emit("nudge", {"window": "x"})
        agentlog._made.clear()  # as after a restart
        self.assertEqual(agentlog.meta("epoch"), epoch)
        self.assertEqual(agentlog.outbox(0)["epoch"], epoch)

    def test_epoch_changes_when_file_deleted(self):
        epoch = agentlog.meta("epoch")
        os.remove(self.path)
        for side in ("-wal", "-shm"):
            if os.path.exists(self.path + side):
                os.remove(self.path + side)
        self.assertNotEqual(agentlog.meta("epoch"), epoch)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_meta_upsert(self):
        self.assertIsNone(agentlog.meta("last_sample"))
        agentlog.meta("last_sample", 12.5)
        agentlog.meta("last_sample", 13)
        self.assertEqual(agentlog.meta("last_sample"), "13")

    def test_q_and_w(self):
        rid = agentlog.w("INSERT INTO seen(ts, day, handle, status) VALUES(?, ?, ?, ?)", (1.0, "2026-10-10", "t1", "working"))
        rows = agentlog.q("SELECT handle, status, project FROM seen WHERE id = ?", (rid,))
        self.assertEqual([tuple(r) for r in rows], [("t1", "working", "")])

    def test_threads(self):
        out = []

        def burst():
            out.extend(agentlog.emit("presence", {"start": 1, "end": 2}) for _ in range(25))

        threads = [threading.Thread(target=burst) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(out)), 100)
        self.assertEqual(len(agentlog.outbox(0, limit=1000)["events"]), 100)


if __name__ == "__main__":
    unittest.main()

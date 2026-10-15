import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

import tests
from modules import agentlog, tokens

SECRET = "the client's private plan"


def stamp(d, hour=12):
    """A transcript timestamp (UTC, with Z) for a local hour on day d."""
    return datetime.combine(d, dtime(hour)).astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def line(mid, rid, d, inp=10, out=5, read=100, write=7, hour=12):
    return json.dumps({"type": "assistant", "timestamp": stamp(d, hour), "requestId": rid, "sessionId": "s",
                       "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": SECRET}],
                                   "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                             "cache_creation_input_tokens": write, "service_tier": "standard"}}})


class Tokens(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=tests.HOME)
        self._root, tokens.ROOT = tokens.ROOT, os.path.join(self.root, "projects")
        self._db, agentlog.DB_PATH = agentlog.DB_PATH, os.path.join(self.root, "agents.db")
        agentlog._made.clear()
        for x in (tokens._offsets, tokens._days, tokens._scanned):
            x.clear()
        self.today = date.today()
        self.now = datetime.combine(self.today, dtime(12, 30)).timestamp()

    def tearDown(self):
        tokens.ROOT, agentlog.DB_PATH = self._root, self._db
        agentlog._made.clear()

    def write(self, rel, lines, end="\n", mode="w"):
        path = os.path.join(tokens.ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write("\n".join(lines) + end)
        return path

    def test_default_root_is_under_home(self):
        self.assertEqual(self._root, os.path.join(tests.HOME, ".claude/projects"))

    def test_none_before_a_scan_and_without_transcripts(self):
        self.assertIsNone(tokens.today())
        tokens.scan(self.now)
        self.assertIsNone(tokens.today())

    def test_dedupe_mixed_days_and_broken_lines(self):
        yesterday = self.today - timedelta(days=1)
        self.write("-home-alex-code-api-server/s1.jsonl", [
            json.dumps({"type": "user", "timestamp": stamp(self.today), "message": {"role": "user", "content": SECRET}}),
            line("m1", "r1", self.today),
            line("m1", "r1", self.today),            # the same response on another line
            line("m1", "r2", self.today, inp=1, out=1, read=0, write=0),  # same message, another request
            line("m0", "r0", yesterday, inp=999),    # another day
            '{"type": "assistant", "message": {"usage": {"input_tokens": 5',  # cut short
            json.dumps({"type": "assistant", "timestamp": stamp(self.today), "message": {"usage": {"input_tokens": 3}}}),
            json.dumps({"timestamp": stamp(self.today), "message": {"id": "m9", "usage": {"input_tokens": "lots"}}}),
            "not json at all \"usage\"",
        ])
        self.write("-home-alex-code-api-server/s1/subagents/agent-a1.jsonl", [line("m2", "r3", self.today, inp=20, out=2, read=0, write=0)])
        tokens.scan(self.now)
        self.assertEqual(tokens.today(), {"input": 31, "output": 8, "cache_read": 100, "cache_write": 7, "messages": 3})

    def test_incremental_and_partial_lines(self):
        path = self.write("p/s.jsonl", [line("m1", "r1", self.today)])
        tokens.scan(self.now)
        half = line("m2", "r2", self.today, inp=40)
        with open(path, "a") as f:
            f.write(half[:30])
        tokens.scan(self.now)
        self.assertEqual(tokens.today()["messages"], 1)  # the half-written line waits
        with open(path, "a") as f:
            f.write(half[30:] + "\n" + line("m1", "r1", self.today) + "\n")
        tokens.scan(self.now)
        self.assertEqual((tokens.today()["messages"], tokens.today()["input"]), (2, 50))
        self.assertEqual(tokens._offsets[path][1], os.path.getsize(path))

    def test_old_files_are_skipped(self):
        path = self.write("p/old.jsonl", [line("m1", "r1", self.today)])
        old = time.time() - 3 * 86400
        os.utime(path, (old, old))
        tokens.scan(self.now)
        self.assertEqual(tokens.today()["messages"], 0)

    def test_day_turn_stores_totals_without_text(self):
        path = self.write("p/s.jsonl", [line("m1", "r1", self.today, hour=23)])
        tokens.scan(self.now)
        with open(path, "a") as f:  # written before midnight, read after it
            f.write(line("m2", "r2", self.today, inp=1, out=1, read=0, write=0, hour=23) + "\n")
        tomorrow = self.today + timedelta(days=1)
        tokens.scan(datetime.combine(tomorrow, dtime(0, 5)).timestamp())
        stored = json.loads(agentlog.meta(f"tokens:{self.today}"))
        self.assertEqual(stored, {"input": 11, "output": 6, "cache_read": 100, "cache_write": 7, "messages": 2})
        self.assertEqual(list(tokens._days), [tomorrow.isoformat()])
        self.assertNotIn(SECRET, repr(tokens._days) + repr(tokens._offsets))
        self.assertNotIn(SECRET, json.dumps([tuple(r) for r in agentlog.q("SELECT * FROM meta")]))
        agentlog.meta("tokens:2026-01-02", json.dumps({"input": 1, "output": 2, "cache_read": 0, "cache_write": 0, "messages": 1}))
        self.assertEqual(tokens.day("2026-01-02")["output"], 2)
        self.assertIsNone(tokens.day("2026-01-03"))


if __name__ == "__main__":
    unittest.main()

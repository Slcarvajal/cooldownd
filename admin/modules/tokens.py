"""Claude Code tokens used on this desktop, read from its transcripts. Not a card.

Claude Code writes one JSONL line per message part to ~/.claude/projects/<project>/<session>.jsonl, and its
subagents to <project>/<session>/subagents/*.jsonl. A line with "usage" carries the token counts. The same
response repeats on several lines, so it's counted once per (message.id, requestId), on the local date of the
line's timestamp. Each file is read from where the last scan stopped, and only files touched on a day that's
still open are read at all.

Only numbers are kept, never the text of a line, and nothing is logged. Agents on other hosts leave no
transcript here, so these totals are "this desktop only".

- scan(now=None): reads what's new and, once the day has turned, stores the finished day's totals in the
  agent log's meta as "tokens:<YYYY-MM-DD>" (JSON)
- today() -> {"input", "output", "cache_read", "cache_write", "messages"}, or None before the first scan or
  when this machine has no transcripts
- day(d) -> the same for a date ("YYYY-MM-DD"): today from memory, earlier days from meta, else None"""
import glob
import json
import os
import threading
import time
from datetime import date, datetime

from modules import agentlog

ROOT = os.path.expanduser("~/.claude/projects")
PATTERNS = ("*/*.jsonl", "*/*/subagents/*.jsonl")
FIELDS = (("input", "input_tokens"), ("output", "output_tokens"),
          ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens"))

_lock = threading.Lock()
_offsets = {}  # path -> (inode, bytes already read)
_days = {}     # open days: "YYYY-MM-DD" -> {(message id, request id): (input, output, cache_read, cache_write)}
_scanned = []  # [True] once a scan found the transcripts folder


def _local_day(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _add(line, today):
    """Count one transcript line. Reads only the ids, the timestamp and the usage numbers."""
    try:
        d = json.loads(line)
        msg = d.get("message") or {}
        usage, mid = msg.get("usage"), msg.get("id")
        day = _local_day(d.get("timestamp"))
        nums = tuple(int(usage.get(k) or 0) for _, k in FIELDS)
    except (ValueError, TypeError, AttributeError):
        return  # a broken line, or one without usage numbers
    if not mid or day is None or (day != today and day not in _days):
        return  # no id to dedupe on, or a day that's already closed
    _days.setdefault(day, {})[(mid, d.get("requestId"))] = nums


def _read(path, today):
    try:
        st = os.stat(path)
    except OSError:
        return
    ino, done = _offsets.get(path, (st.st_ino, 0))
    if ino != st.st_ino or st.st_size < done:
        done = 0  # replaced or truncated: read it again (the dedupe keeps counts right)
    if st.st_size == done:
        return
    try:
        with open(path, "rb") as f:
            f.seek(done)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written waits for the next scan
                done += len(raw)
                if b'"usage"' in raw:
                    _add(raw, today)
    except OSError:
        return
    _offsets[path] = (st.st_ino, done)


def _totals(bucket):
    sums = [sum(v[i] for v in bucket.values()) for i in range(len(FIELDS))]
    return {**{name: n for (name, _), n in zip(FIELDS, sums)}, "messages": len(bucket)}


def scan(now=None):
    now = time.time() if now is None else now
    today = date.fromtimestamp(now).isoformat()
    with _lock:
        _days.setdefault(today, {})
        found = os.path.isdir(ROOT)
        if found:
            oldest = min(_days)  # a file last touched before every open day has nothing new to count
            for pattern in PATTERNS:
                for path in glob.glob(os.path.join(ROOT, pattern)):
                    try:
                        touched = date.fromtimestamp(os.stat(path).st_mtime).isoformat()
                    except OSError:
                        continue
                    if touched >= oldest:
                        _read(path, today)
        for d in sorted(x for x in _days if x < today):
            agentlog.meta(f"tokens:{d}", json.dumps(_totals(_days[d])))
            del _days[d]
        _scanned[:] = [found]


def today():
    with _lock:
        if not _scanned or not _scanned[0]:
            return None
        return _totals(_days.get(date.today().isoformat(), {}))


def day(d):
    if d == date.today().isoformat():
        return today()
    try:
        return json.loads(agentlog.meta(f"tokens:{d}") or "null")
    except ValueError:
        return None

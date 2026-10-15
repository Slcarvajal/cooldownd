"""The desk's agent log: an outbox of events for the Pi to pull, plus the agent history the cards read.

Not a card. One SQLite file, ~/.local/share/homelab-admin/agents.db, created 600 before SQLite opens it.

- outbox: events (kind, data as JSON) the Pi pulls by id. AUTOINCREMENT, so an id is never reused after
  prune(); the Pi's cursor relies on that.
- seen / task: agent status changes and finished tasks, written by the agent-health card.
- meta: key/value. "epoch" is a random hex written once when the file is made; if the file is deleted, the
  epoch changes and the Pi knows to restart its cursor.

- emit(kind, data, ts=None) -> id (int) of the new outbox event; ts defaults to now
- outbox(since, limit=200) -> {"epoch": str, "last": int, "events": [{"id", "ts", "kind", "data"}]}: events
  with id > since, oldest first. "last" is the last id returned (since, when none); a full page may have more.
- q(sql, args=()) -> list of sqlite3.Row; w(sql, args=()) -> lastrowid
- meta(key, value=None) -> the stored str (or None) when reading; upserts str(value) when writing
- prune(now=None, keep_days=30) -> outbox events deleted (older than keep_days). seen and task are kept.

Each call opens its own connection (WAL, 10 s busy timeout), so the panel's threads can share it."""
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import closing

DB_PATH = os.path.expanduser("~/.local/share/homelab-admin/agents.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, handle TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task(id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, handle TEXT NOT NULL,
  project TEXT NOT NULL, title TEXT NOT NULL, agent TEXT NOT NULL, recap TEXT NOT NULL, dismissed REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_ts ON outbox(ts);
CREATE INDEX IF NOT EXISTS seen_day ON seen(day);
CREATE INDEX IF NOT EXISTS task_day ON task(day);
"""
MAX_PAGE = 1000

_ready = threading.Lock()
_made = []


def _db():
    with _ready:
        # Also redone when the file vanished, so a deleted log comes back 600 with a new epoch.
        if not _made or not os.path.exists(DB_PATH):
            os.makedirs(os.path.dirname(DB_PATH), mode=0o700, exist_ok=True)
            # Create the file 600 before SQLite touches it: a chmod afterwards leaves a short open window.
            fd = os.open(DB_PATH, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            with closing(sqlite3.connect(DB_PATH)) as con, con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA_SQL)
                con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('epoch', ?)", (secrets.token_hex(8),))
            if not _made:
                _made.append(True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def q(sql, args=()):
    with closing(_db()) as con:
        return con.execute(sql, args).fetchall()


def w(sql, args=()):
    with closing(_db()) as con, con:
        return con.execute(sql, args).lastrowid


def meta(key, value=None):
    if value is None:
        r = q("SELECT value FROM meta WHERE key = ?", (key,))
        return r[0]["value"] if r else None
    w("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def emit(kind, data, ts=None):
    return w("INSERT INTO outbox(ts, kind, data) VALUES(?, ?, ?)",
             (time.time() if ts is None else float(ts), str(kind), json.dumps(data, ensure_ascii=False)))


def outbox(since, limit=200):
    since, limit = int(since), max(1, min(int(limit), MAX_PAGE))
    rows = q("SELECT id, ts, kind, data FROM outbox WHERE id > ? ORDER BY id LIMIT ?", (since, limit))
    events = [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]
    return {"epoch": meta("epoch"), "last": events[-1]["id"] if events else since, "events": events}


def prune(now=None, keep_days=30):
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    with closing(_db()) as con, con:
        return con.execute("DELETE FROM outbox WHERE ts < ?", (cutoff,)).rowcount

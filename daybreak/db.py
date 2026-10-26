"""SQLite storage: the activity ledger, workstreams, briefs, meetings, nudges, artifacts, chats."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

-- Typed activity events produced by sensors (request / commitment / decision / blocker / context).
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,            -- gmail | jira | confluence | drive | slack | bitbucket | chat
    ref TEXT NOT NULL,               -- artifact the event belongs to, e.g. jira:PROJ-123
    kind TEXT NOT NULL,
    author TEXT DEFAULT '',
    target TEXT DEFAULT '',
    text TEXT NOT NULL,
    url TEXT DEFAULT '',
    ts REAL NOT NULL,                -- when it happened
    created REAL NOT NULL,
    routed INTEGER DEFAULT 0,
    fingerprint TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS events_routed ON events(routed, created);
CREATE INDEX IF NOT EXISTS events_ref ON events(ref);

-- Artifacts the events hang off (threads, issues, docs, PRs) at their latest known state.
CREATE TABLE IF NOT EXISTS items (
    ref TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT DEFAULT '',
    state TEXT DEFAULT '',
    url TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workstreams (
    slug TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    overview TEXT DEFAULT '',
    state TEXT DEFAULT 'active',     -- active | waiting | done | dormant
    open_loops TEXT DEFAULT '[]',    -- JSON list of {kind, who, owes, text}
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workstream_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    slug TEXT NOT NULL,
    ref TEXT NOT NULL,
    conf REAL NOT NULL,
    added REAL NOT NULL,
    PRIMARY KEY (slug, ref)
);
CREATE TABLE IF NOT EXISTS proposals (       -- new workstream slugs waiting for a second backer
    slug TEXT NOT NULL,
    ref TEXT NOT NULL,
    conf REAL NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (slug, ref)
);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    strength REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE TABLE IF NOT EXISTS surface (
    day TEXT PRIMARY KEY,
    slugs TEXT NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    day TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    painting TEXT DEFAULT '{}',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    attendees TEXT DEFAULT '[]',
    organizer TEXT DEFAULT '',
    location TEXT DEFAULT '',
    join_url TEXT DEFAULT '',
    description TEXT DEFAULT '',
    prep_artifact TEXT DEFAULT '',
    prep_state TEXT DEFAULT '',      -- '' | running | done | failed
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- meetingPrep | report | deck | recap | scheduled
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    created REAL NOT NULL,
    meta TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS nudges (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- meeting | brief | followup | scheduled | artifact
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    dedupe TEXT UNIQUE,
    deliver_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    delivered_at REAL,
    dismissed_at REAL,
    os_notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    spec TEXT NOT NULL,              -- "daily 09:00" | "weekdays 17:30" | "mon 10:00" | "every 2h"
    next_run REAL NOT NULL,
    last_run REAL,
    last_status TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT DEFAULT '{}',
    created REAL NOT NULL
);

-- Work the extension does for the helper inside the browser (e.g. read Slack through your browser's Slack session).
CREATE TABLE IF NOT EXISTS bridge_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',    -- queued | claimed | done | failed
    result TEXT,
    created REAL NOT NULL,
    claimed REAL,
    finished REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    status TEXT DEFAULT 'running',
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class DB:
    def __init__(self, path=None):
        config.ensure_dirs()
        self.conn = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, column, decl in (("todos", "source", "TEXT DEFAULT '{}'"),):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- generic helpers -------------------------------------------------
    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(r["value"]) if r else default

    def put(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, json.dumps(value)))

    # -- runs ------------------------------------------------------------
    def run_start(self, kind: str) -> int:
        return self.x("INSERT INTO runs(kind, started) VALUES(?, ?)", (kind, time.time())).lastrowid

    def run_end(self, run_id: int, status: str, detail: str = "") -> None:
        self.x("UPDATE runs SET finished = ?, status = ?, detail = ? WHERE id = ?",
               (time.time(), status, detail[:4000], run_id))

    # -- nudges ----------------------------------------------------------
    def add_nudge(self, kind: str, title: str, body: str = "", payload: dict | None = None, *,
                  deliver_at: float | None = None, ttl: float = 3600, dedupe: str | None = None) -> str | None:
        nid = new_id()
        at = deliver_at or time.time()
        try:
            self.x("INSERT INTO nudges(id, kind, title, body, payload, dedupe, deliver_at, expires_at) "
                   "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                   (nid, kind, title, body, json.dumps(payload or {}), dedupe, at, at + ttl))
        except sqlite3.IntegrityError:
            return None  # same dedupe key already queued
        return nid

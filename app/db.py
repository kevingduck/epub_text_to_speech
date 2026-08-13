"""SQLite schema and connection helper."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id           TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  author       TEXT,
  voice        TEXT NOT NULL DEFAULT 'af_heart',
  status       TEXT NOT NULL,
  progress     REAL NOT NULL DEFAULT 0.0,
  error        TEXT,
  duration_ms  INTEGER,
  size_bytes   INTEGER,
  created_at   TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
  book_id   TEXT NOT NULL,
  idx       INTEGER NOT NULL,
  title     TEXT,
  included  INTEGER NOT NULL DEFAULT 1,
  chars     INTEGER,
  start_ms  INTEGER,
  dur_ms    INTEGER,
  PRIMARY KEY (book_id, idx)
);

CREATE TABLE IF NOT EXISTS positions (
  book_id     TEXT PRIMARY KEY,
  position_ms INTEGER NOT NULL,
  updated_at  TEXT NOT NULL
);
"""

STATUSES = ("queued", "parsing", "synthesizing", "encoding", "done", "failed")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn

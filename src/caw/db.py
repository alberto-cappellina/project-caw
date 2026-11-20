"""SQLite database (Phase 2): index of archived emails and extracted offers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id            TEXT PRIMARY KEY,   -- file name without extension (YYYYMMDD-hash)
    path          TEXT NOT NULL,      -- relative to raw_dir
    msg_date      TEXT,               -- ISO 8601 UTC
    sender        TEXT,
    subject       TEXT,
    status        TEXT NOT NULL DEFAULT 'new',  -- new | parsed | unsupported | error
    parser        TEXT,
    n_jobs        INTEGER,
    search_query  TEXT,
    notes         TEXT,
    indexed_at    TEXT NOT NULL,
    labeled_at    TEXT                -- when it was marked in Gmail (read + label)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,     -- stable id from the platform (or a fallback hash)
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    company     TEXT,
    location    TEXT,
    work_mode   TEXT,                 -- in_person | hybrid | remote
    salary_min  INTEGER,
    salary_max  INTEGER,
    currency    TEXT,
    contract    TEXT,
    extras      TEXT,                 -- JSON: other labels of the ad
    url         TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    times_seen  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sightings (   -- in which email each offer appeared
    email_id      TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    job_id        TEXT NOT NULL REFERENCES jobs(job_id),
    rank          INTEGER,
    search_query  TEXT,
    PRIMARY KEY (email_id, job_id)
);

CREATE TABLE IF NOT EXISTS evaluations (   -- Phase 4 result for each offer
    job_id        TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    rule_status   TEXT NOT NULL,     -- ok | verify | excluded
    rule_reasons  TEXT,              -- JSON: reasons for exclusion or doubt
    flags         TEXT,              -- JSON: notes (e.g. "ral inferiore")
    profile_hash  TEXT,              -- profile/prompt used for the LLM scoring
    score         INTEGER,           -- 0-100, only if scored by the LLM
    llm_reason    TEXT,
    verdict       TEXT,              -- match | forse | escluso | da_valutare
    model         TEXT,
    evaluated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Migration for databases created before the labeled_at column.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(emails)")}
    if "labeled_at" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN labeled_at TEXT")
        conn.commit()
    return conn

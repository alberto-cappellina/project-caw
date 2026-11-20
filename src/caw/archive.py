"""Phase 2 - Index the .eml files in archive/raw into SQLite (idempotent)."""

from __future__ import annotations

import email
import email.policy
import email.utils
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _iso(date_header: str | None) -> str | None:
    try:
        when = email.utils.parsedate_to_datetime(date_header or "")
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def index_archive(conn: sqlite3.Connection, raw_dir: Path) -> int:
    """Add new emails to the index. Return how many were added."""
    known = {row[0] for row in conn.execute("SELECT id FROM emails")}
    added = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for path in sorted(raw_dir.rglob("*.eml")):
        if path.stem in known:
            continue
        msg = email.message_from_binary_file(path.open("rb"), policy=email.policy.default)
        conn.execute(
            "INSERT INTO emails (id, path, msg_date, sender, subject, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                path.stem,
                str(path.relative_to(raw_dir)),
                _iso(msg.get("Date")),
                str(msg.get("From", "")),
                str(msg.get("Subject", "")),
                now,
            ),
        )
        added += 1
    conn.commit()
    return added

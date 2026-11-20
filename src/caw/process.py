"""Phases 2-3: index the archive into SQLite and extract the offers from new emails.

    python -m caw.process            # process only emails not processed yet
    python -m caw.process --reparse  # process everything again (after improving a parser)
"""

from __future__ import annotations

import argparse
import email
import email.policy
import json
import sqlite3
from pathlib import Path

from .archive import index_archive
from .db import connect
from .fetch import Config
from .parsers import parse_email


def _upsert_job(conn: sqlite3.Connection, job) -> None:
    conn.execute(
        """
        INSERT INTO jobs (job_id, source, title, company, location, work_mode, salary_min,
                          salary_max, currency, contract, extras, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            title=excluded.title,
            company=COALESCE(excluded.company, jobs.company),
            location=excluded.location, work_mode=excluded.work_mode,
            salary_min=excluded.salary_min, salary_max=excluded.salary_max,
            currency=excluded.currency, contract=excluded.contract,
            extras=excluded.extras, url=excluded.url
        """,
        (
            job.job_id, job.source, job.title, job.company, job.location, job.work_mode,
            job.salary_min, job.salary_max, job.currency, job.contract,
            json.dumps(job.extras, ensure_ascii=False), job.url,
        ),
    )


def _refresh_job_stats(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE jobs SET
          first_seen = (SELECT MIN(e.msg_date) FROM sightings s JOIN emails e ON e.id = s.email_id
                        WHERE s.job_id = jobs.job_id),
          last_seen  = (SELECT MAX(e.msg_date) FROM sightings s JOIN emails e ON e.id = s.email_id
                        WHERE s.job_id = jobs.job_id),
          times_seen = (SELECT COUNT(*) FROM sightings s WHERE s.job_id = jobs.job_id)
        """
    )
    conn.execute(
        "DELETE FROM jobs WHERE job_id NOT IN (SELECT job_id FROM sightings)"
    )


def process(conn: sqlite3.Connection, raw_dir: Path, *, reparse: bool = False, log=print) -> dict:
    stats = {"indexed": index_archive(conn, raw_dir), "parsed": 0, "unsupported": 0,
             "error": 0, "jobs": 0}
    if reparse:
        conn.execute("DELETE FROM sightings")
        conn.execute("UPDATE emails SET status='new', parser=NULL, n_jobs=NULL, notes=NULL")

    # 'unsupported' emails are retried on every run: with a new parser they become processable.
    pending = conn.execute(
        "SELECT id, path FROM emails WHERE status IN ('new','unsupported') ORDER BY msg_date"
    ).fetchall()
    for row in pending:
        try:
            msg = email.message_from_binary_file(
                (raw_dir / row["path"]).open("rb"), policy=email.policy.default
            )
            res = parse_email(msg)
            if res is None:
                conn.execute("UPDATE emails SET status='unsupported' WHERE id=?", (row["id"],))
                stats["unsupported"] += 1
                continue
            for job in res.jobs:
                _upsert_job(conn, job)
                conn.execute(
                    "INSERT OR REPLACE INTO sightings (email_id, job_id, rank, search_query) "
                    "VALUES (?, ?, ?, ?)",
                    (row["id"], job.job_id, job.rank, res.search_query),
                )
            conn.execute(
                "UPDATE emails SET status='parsed', parser=?, n_jobs=?, search_query=?, notes=? "
                "WHERE id=?",
                (res.parser, len(res.jobs), res.search_query, "; ".join(res.notes) or None, row["id"]),
            )
            stats["parsed"] += 1
            stats["jobs"] += len(res.jobs)
            if res.notes:
                log(f"  ! {row['id']}: {'; '.join(res.notes)}")
        except Exception as exc:  # one broken email must not stop the others
            conn.execute(
                "UPDATE emails SET status='error', notes=? WHERE id=?", (repr(exc), row["id"])
            )
            stats["error"] += 1
            log(f"  ! {row['id']}: errore {exc!r}")
    _refresh_job_stats(conn)
    conn.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Index and process the archived emails.")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--reparse", action="store_true", help="process all emails again")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    conn = connect(cfg.db_file)
    stats = process(conn, cfg.raw_dir, reparse=args.reparse)
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(
        f"Indicizzate {stats['indexed']} | elaborate {stats['parsed']} "
        f"({stats['jobs']} offerte viste) | non supportate {stats['unsupported']} "
        f"| errori {stats['error']} | offerte uniche in DB: {total}"
    )
    return 1 if stats["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

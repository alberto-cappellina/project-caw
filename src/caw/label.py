"""Mark successfully processed emails in Gmail: "read" flag and `caw-processed` label.

    python -m caw.label --dry-run   # how many emails would be marked, change nothing
    python -m caw.label             # mark processed emails that are not marked yet

These are the only changes this project makes to the mailbox: it adds the \\Seen flag
and a label. It never moves or deletes emails and never removes labels.

Only emails with status `parsed` are marked (even with 0 offers, like profile
notifications). Emails with status `unsupported` or `error` stay unread and
unlabeled, so you notice them. An email is found in Gmail by its Message-ID, so this
also works for emails downloaded in the past. The result is stored in
`emails.labeled_at`: each email is marked only once.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db import connect
from .fetch import Config, connect as imap_connect, imap_quote, load_env


@dataclass
class LabelResult:
    marked: int = 0
    pending: int = 0
    not_found: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def message_id(path: Path) -> str | None:
    with path.open("rb") as fh:
        msg = email.message_from_binary_file(fh, policy=email.policy.default)
    value = (msg.get("Message-ID") or "").strip()
    return value or None


def _ensure_label(imap, label: str, log) -> None:
    """Create the label if it is missing (on Gmail, CREATE of a folder creates a label)."""
    typ, data = imap.create(imap_quote(label))
    if typ != "OK" and "ALREADYEXISTS" not in str(data).upper() and "EXISTS" not in str(data).upper():
        log(f"  ! creazione etichetta {label!r}: {typ} {data}")


def mark_processed(
    imap, conn: sqlite3.Connection, cfg: Config, *, dry_run: bool = False, log=print
) -> LabelResult:
    res = LabelResult()
    rows = conn.execute(
        "SELECT id, path FROM emails WHERE status='parsed' AND labeled_at IS NULL ORDER BY msg_date"
    ).fetchall()
    res.pending = len(rows)
    if not rows or dry_run or not (cfg.mark_read or cfg.processed_label):
        return res

    typ, data = imap.select(imap_quote(cfg.folder), readonly=False)
    if typ != "OK":
        raise RuntimeError(f"IMAP select {cfg.folder} fallito: {typ} {data}")
    if cfg.processed_label:
        _ensure_label(imap, cfg.processed_label, log)

    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    for row in rows:
        try:
            mid = message_id(cfg.raw_dir / row["path"])
            if not mid:
                conn.execute("UPDATE emails SET labeled_at='no-message-id' WHERE id=?", (row["id"],))
                res.not_found += 1
                continue
            typ, data = imap.uid("SEARCH", "HEADER", "Message-ID", imap_quote(mid))
            uids = (data[0] or b"").split() if typ == "OK" else []
            if typ != "OK":
                raise RuntimeError(f"search: {typ} {data}")
            if not uids:  # email deleted from Gmail or no longer in the folder: do not retry
                conn.execute("UPDATE emails SET labeled_at='not-found' WHERE id=?", (row["id"],))
                res.not_found += 1
                continue
            uid_set = b",".join(uids).decode()
            if cfg.mark_read:
                typ, data = imap.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Seen)")
                if typ != "OK":
                    raise RuntimeError(f"flag letto: {typ} {data}")
            if cfg.processed_label:
                typ, data = imap.uid("STORE", uid_set, "+X-GM-LABELS", f"({imap_quote(cfg.processed_label)})")
                if typ != "OK":
                    raise RuntimeError(f"etichetta: {typ} {data}")
            conn.execute("UPDATE emails SET labeled_at=? WHERE id=?", (now(), row["id"]))
            res.marked += 1
        except Exception as exc:  # one problematic email does not block the others: retry on the next run
            res.errors += 1
            res.notes.append(f"{row['id']}: {exc}")
            log(f"  ! {row['id']}: {exc}")
        conn.commit()
    conn.commit()
    return res


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Mark processed emails in Gmail.")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--dry-run", action="store_true", help="count without changing Gmail")
    args = parser.parse_args(argv)

    load_env(root / ".env")
    cfg = Config.load(args.config)
    conn = connect(cfg.db_file)
    pending = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE status='parsed' AND labeled_at IS NULL"
    ).fetchone()[0]
    if args.dry_run or not pending:
        print(f"Mail elaborate da segnare: {pending}" + (" (dry-run: nessuna modifica)" if args.dry_run else ""))
        return 0
    imap = imap_connect(cfg)
    try:
        res = mark_processed(imap, conn, cfg)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    print(
        f"Segnate {res.marked} | non trovate in Gmail {res.not_found} | errori {res.errors}"
    )
    return 1 if res.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

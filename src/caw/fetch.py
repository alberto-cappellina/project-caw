"""Phase 1 - Incremental download of emails from Gmail over IMAP.

- The folder is opened read-only (EXAMINE) and messages are read with BODY.PEEK[],
  so the download changes nothing in the mailbox. The "read" flag and the
  `caw-processed` label are applied later by `caw.label`, only to emails that
  were processed successfully.
- Each email is saved as is (.eml) in archive/raw/YYYY/MM/.
- The state (UIDVALIDITY + the UIDs already downloaded) allows incremental syncs.
  The file name comes from the Message-ID, so running again never creates duplicates.

Usage:
    python -m caw.fetch                 # incremental sync
    python -m caw.fetch --dry-run       # only count, do not download
    python -m caw.fetch --since 2026-01-01 --limit 20
    python -m caw.fetch --list-folders  # find the folder name
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UID_RE = re.compile(rb"UID (\d+)")
UIDVALIDITY_RE = re.compile(r"\[UIDVALIDITY (\d+)\]")


# --------------------------------------------------------------------------- config


def load_env(path: Path) -> None:
    """Load KEY=VALUE from a .env file without overriding variables that are already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    host: str
    port: int
    folder: str
    gmail_query: str
    initial_lookback_days: int
    batch_size: int
    raw_dir: Path
    state_file: Path
    db_file: Path = Path("data/caw.db")
    reports_dir: Path = Path("reports")
    stale_days: int = 30
    stale_review_days: int = 60
    mark_read: bool = True
    processed_label: str = "caw-processed"
    ignore_before: date | None = None  # emails received before this date are ignored

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        return cls(
            host=data["imap"]["host"],
            port=int(data["imap"]["port"]),
            folder=data["imap"]["folder"],
            gmail_query=data["fetch"]["gmail_query"],
            initial_lookback_days=int(data["fetch"]["initial_lookback_days"]),
            batch_size=int(data["fetch"]["batch_size"]),
            mark_read=bool(data["fetch"].get("mark_read", True)),
            processed_label=str(data["fetch"].get("processed_label", "caw-processed")),
            ignore_before=(
                date.fromisoformat(data["fetch"]["ignore_before"])
                if data["fetch"].get("ignore_before") else None
            ),
            raw_dir=base / data["paths"]["raw_dir"],
            state_file=base / data["paths"]["state_file"],
            db_file=base / data["paths"].get("db_file", "data/caw.db"),
            reports_dir=base / data["paths"].get("reports_dir", "reports"),
            stale_days=int(data.get("report", {}).get("stale_days", 30)),
            stale_review_days=int(data.get("report", {}).get("stale_review_days", 60)),
        )


# --------------------------------------------------------------------------- state


@dataclass
class State:
    folder: str = ""
    uidvalidity: int = 0
    last_uid: int = 0  # informational only (older versions used this as the checkpoint)
    query: str = ""
    # UIDs already downloaded. A "UID > n" checkpoint is not enough: an old email that gets
    # the label only now has a low UID and would be skipped.
    fetched_uids: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        os.replace(tmp, path)


# --------------------------------------------------------------------------- helpers


def imap_quote(value: str) -> str:
    """Quote a string for IMAP (ASCII only; literals are not handled)."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Il valore {value!r} contiene caratteri non ASCII: non supportato."
        ) from exc
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_gmail_query(base_query: str, after: date | None) -> str:
    query = base_query.strip()
    if after is not None:
        query = f"{query} after:{after:%Y/%m/%d}".strip()
    return query


def message_path(raw_dir: Path, raw: bytes) -> Path:
    """Stable storage path: archive/raw/YYYY/MM/YYYYMMDD-<hash>.eml."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    key = message_id or hashlib.sha256(raw).hexdigest()
    digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        when = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
    except (TypeError, ValueError):
        when = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return raw_dir / f"{when:%Y}" / f"{when:%m}" / f"{when:%Y%m%d}-{digest}.eml"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".eml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def parse_fetch_response(data: list) -> list[tuple[int, bytes]]:
    """Extract (uid, raw_message) from a UID FETCH (UID BODY.PEEK[]) response."""
    out = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        match = UID_RE.search(item[0])
        if match:
            out.append((int(match.group(1)), item[1]))
    return out


# --------------------------------------------------------------------------- core


@dataclass
class Result:
    found: int = 0
    saved: int = 0
    duplicates: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def _check(typ: str, data, what: str):
    if typ != "OK":
        raise RuntimeError(f"IMAP {what} fallito: {typ} {data}")
    return data


def fetch_new(
    imap,
    cfg: Config,
    state: State,
    *,
    since: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    log=print,
) -> Result:
    """Download the new emails. `imap` is an object compatible with imaplib.IMAP4_SSL."""
    result = Result()

    typ, data = imap.select(imap_quote(cfg.folder), readonly=True)
    _check(typ, data, f"select {cfg.folder}")

    typ, resp = imap.response("UIDVALIDITY")
    uidvalidity = int(resp[0]) if resp and resp[0] else 0
    had_state = bool(state.fetched_uids or state.last_uid)
    if state.folder != cfg.folder or (state.uidvalidity and state.uidvalidity != uidvalidity):
        if had_state:
            log("UIDVALIDITY o cartella cambiati: riparto da zero (i duplicati vengono saltati).")
        state.fetched_uids, state.last_uid = [], 0
    elif had_state and state.query != cfg.gmail_query:
        # The UIDs already downloaded only apply to the query they were chosen with.
        log("La query e' cambiata: riparto da zero (i duplicati vengono saltati).")
        state.fetched_uids, state.last_uid = [], 0
    state.folder, state.uidvalidity, state.query = cfg.folder, uidvalidity, cfg.gmail_query

    # Always search the whole window (not only "after the last UID") and download only
    # the UIDs not seen yet: this also catches emails that were labeled later.
    after = since or date.today() - timedelta(days=cfg.initial_lookback_days)
    if since is None and cfg.ignore_before and after < cfg.ignore_before:
        after = cfg.ignore_before  # an explicit --since has priority instead
    query = build_gmail_query(cfg.gmail_query, after)
    log(f"Query Gmail: {query!r}")

    typ, data = imap.uid("SEARCH", "X-GM-RAW", imap_quote(query))
    _check(typ, data, "search")
    seen = set(state.fetched_uids)
    uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) not in seen)
    result.found = len(uids)
    log(f"Mail da scaricare: {len(uids)}")

    if limit is not None:
        uids = uids[:limit]
    if dry_run or not uids:
        return result

    for i in range(0, len(uids), cfg.batch_size):
        batch = uids[i : i + cfg.batch_size]
        typ, data = imap.uid("FETCH", ",".join(map(str, batch)), "(UID BODY.PEEK[])")
        _check(typ, data, "fetch")
        got = dict(parse_fetch_response(data))
        for uid in batch:
            raw = got.get(uid)
            if raw is None:
                result.errors += 1
                result.notes.append(f"UID {uid}: nessun contenuto restituito")
                continue
            path = message_path(cfg.raw_dir, raw)
            if path.exists():
                result.duplicates += 1
            else:
                write_atomic(path, raw)
                result.saved += 1
        # Checkpoint only if the whole batch went well, so no email is lost.
        if all(uid in got for uid in batch):
            state.fetched_uids = sorted(seen.union(batch))
            seen = set(state.fetched_uids)
            state.last_uid = max(state.last_uid, batch[-1])
            state.save(cfg.state_file)
        log(f"  {min(i + cfg.batch_size, len(uids))}/{len(uids)}")
    return result


# --------------------------------------------------------------------------- cli


def connect(cfg: Config) -> imaplib.IMAP4_SSL:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not user or not password:
        sys.exit("Imposta GMAIL_USER e GMAIL_APP_PASSWORD nel file .env (vedi .env.example).")
    imap = imaplib.IMAP4_SSL(cfg.host, cfg.port)
    try:
        imap.login(user, password)
    except imaplib.IMAP4.error as exc:
        sys.exit(f"Login IMAP fallito ({exc}). Controlla utente e password per le app.")
    return imap


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Download job-alert emails from Gmail (IMAP).")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD: force the start date")
    parser.add_argument("--limit", type=int, help="maximum number of emails to download")
    parser.add_argument("--dry-run", action="store_true", help="count without downloading")
    parser.add_argument("--list-folders", action="store_true")
    args = parser.parse_args(argv)

    load_env(root / ".env")
    cfg = Config.load(args.config)
    imap = connect(cfg)
    try:
        if args.list_folders:
            typ, folders = imap.list()
            for f in folders:
                print(f.decode("utf-8", "replace"))
            return 0
        state = State.load(cfg.state_file)
        res = fetch_new(
            imap, cfg, state, since=args.since, limit=args.limit, dry_run=args.dry_run
        )
        print(
            f"Trovate {res.found} | salvate {res.saved} | duplicate {res.duplicates} "
            f"| errori {res.errors}"
        )
        for note in res.notes:
            print("  !", note)
        return 1 if res.errors else 0
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

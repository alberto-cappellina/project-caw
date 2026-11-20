from dataclasses import replace
from pathlib import Path

import pytest

from caw.db import connect
from caw.fetch import Config
from caw.label import mark_processed, message_id


def eml(msg_id: str | None) -> bytes:
    head = f"Message-ID: <{msg_id}>\r\n" if msg_id else ""
    return (head + "From: jobs@x.com\r\nSubject: s\r\nDate: Tue, 03 Nov 2026 10:00:00 +0000\r\n\r\nbody").encode()


class FakeImap:
    def __init__(self, known: dict[str, list[int]], fail_store: set[str] | None = None):
        self.known = known  # Message-ID -> UIDs present in Gmail
        self.fail_store = fail_store or set()
        self.calls: list[tuple] = []
        self.readonly = None
        self.created: list[str] = []

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def create(self, name):
        self.created.append(name)
        return "NO", [b"[ALREADYEXISTS] Duplicate folder name"]  # the label already exists

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            assert args[:2] == ("HEADER", "Message-ID")
            mid = args[2].strip('"')
            return "OK", [b" ".join(str(u).encode() for u in self.known.get(mid, []))]
        if command == "STORE":
            if any(str(u) in self.fail_store for u in args[0].split(",")):
                return "NO", [b"boom"]
            return "OK", [b""]
        raise AssertionError(command)

    def stores(self):
        return [c for c in self.calls if c[0] == "STORE"]


@pytest.fixture
def cfg(tmp_path):
    return Config(host="x", port=993, folder="[Google Mail]/All Mail", gmail_query="label:caw",
                  initial_lookback_days=30, batch_size=10, raw_dir=tmp_path / "raw",
                  state_file=tmp_path / "s.json", db_file=tmp_path / "db.sqlite")


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_file)
    cfg.raw_dir.mkdir()
    return c


def add(conn, cfg, key, status, msg_id):
    (cfg.raw_dir / f"{key}.eml").write_bytes(eml(msg_id))
    conn.execute(
        "INSERT INTO emails (id, path, msg_date, status, indexed_at) VALUES (?,?,?,?,'x')",
        (key, f"{key}.eml", "2026-11-03T10:00:00+00:00", status),
    )
    conn.commit()


def labeled(conn):
    return {r["id"]: r["labeled_at"] for r in conn.execute("SELECT id, labeled_at FROM emails")}


def quiet(*_a, **_k):
    pass


def test_marks_only_parsed_mails_read_and_labelled(conn, cfg):
    add(conn, cfg, "ok", "parsed", "a@x")
    add(conn, cfg, "unsup", "unsupported", "b@x")
    add(conn, cfg, "err", "error", "c@x")
    imap = FakeImap({"<a@x>": [7], "<b@x>": [8], "<c@x>": [9]})
    res = mark_processed(imap, conn, cfg, log=quiet)
    assert (res.marked, res.pending, res.errors) == (1, 1, 0)
    assert imap.readonly is False
    assert imap.stores() == [
        ("STORE", "7", "+FLAGS.SILENT", r"(\Seen)"),
        ("STORE", "7", "+X-GM-LABELS", '("caw-processed")'),
    ]
    got = labeled(conn)
    assert got["ok"] and got["unsup"] is None and got["err"] is None  # the others stay unread
    assert imap.created == ['"caw-processed"']


def test_each_mail_is_marked_only_once(conn, cfg):
    add(conn, cfg, "ok", "parsed", "a@x")
    imap = FakeImap({"<a@x>": [7]})
    mark_processed(imap, conn, cfg, log=quiet)
    again = FakeImap({"<a@x>": [7]})
    res = mark_processed(again, conn, cfg, log=quiet)
    assert res.pending == 0 and again.calls == []  # no useful connection


def test_dry_run_changes_nothing(conn, cfg):
    add(conn, cfg, "ok", "parsed", "a@x")
    imap = FakeImap({"<a@x>": [7]})
    res = mark_processed(imap, conn, cfg, dry_run=True, log=quiet)
    assert res.pending == 1 and res.marked == 0 and imap.calls == [] and labeled(conn)["ok"] is None


def test_mail_missing_in_gmail_is_not_retried(conn, cfg):
    add(conn, cfg, "gone", "parsed", "gone@x")
    add(conn, cfg, "noid", "parsed", None)
    imap = FakeImap({})
    res = mark_processed(imap, conn, cfg, log=quiet)
    assert (res.marked, res.not_found, res.errors) == (0, 2, 0)
    assert labeled(conn) == {"gone": "not-found", "noid": "no-message-id"}
    assert imap.stores() == []


def test_store_failure_leaves_mail_pending_for_retry(conn, cfg):
    add(conn, cfg, "bad", "parsed", "a@x")
    add(conn, cfg, "good", "parsed", "b@x")
    imap = FakeImap({"<a@x>": [1], "<b@x>": [2]}, fail_store={"1"})
    res = mark_processed(imap, conn, cfg, log=quiet)
    assert (res.marked, res.errors) == (1, 1)  # the problematic email does not block the other
    got = labeled(conn)
    assert got["bad"] is None and got["good"]
    retry = mark_processed(FakeImap({"<a@x>": [1]}), conn, cfg, log=quiet)
    assert retry.marked == 1 and labeled(conn)["bad"]


def test_options_can_be_disabled(conn, cfg):
    add(conn, cfg, "ok", "parsed", "a@x")
    only_read = replace(cfg, processed_label="")
    imap = FakeImap({"<a@x>": [7]})
    mark_processed(imap, conn, only_read, log=quiet)
    assert imap.stores() == [("STORE", "7", "+FLAGS.SILENT", r"(\Seen)")] and imap.created == []

    add(conn, cfg, "ok2", "parsed", "b@x")
    only_label = replace(cfg, mark_read=False)
    imap2 = FakeImap({"<b@x>": [8]})
    mark_processed(imap2, conn, only_label, log=quiet)
    assert imap2.stores() == [("STORE", "8", "+X-GM-LABELS", '("caw-processed")')]

    add(conn, cfg, "ok3", "parsed", "c@x")
    nothing = replace(cfg, processed_label="", mark_read=False)
    imap3 = FakeImap({"<c@x>": [9]})
    mark_processed(imap3, conn, nothing, log=quiet)
    assert imap3.calls == [] and labeled(conn)["ok3"] is None  # everything off: nothing to do


def test_duplicate_gmail_copies_all_marked(conn, cfg):
    add(conn, cfg, "ok", "parsed", "a@x")
    imap = FakeImap({"<a@x>": [3, 4]})
    mark_processed(imap, conn, cfg, log=quiet)
    assert imap.stores()[0] == ("STORE", "3,4", "+FLAGS.SILENT", r"(\Seen)")


def test_message_id_helper(tmp_path):
    p = tmp_path / "m.eml"
    p.write_bytes(eml("zzz@x"))
    assert message_id(p) == "<zzz@x>"
    p.write_bytes(eml(None))
    assert message_id(p) is None


def test_existing_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE emails (id TEXT PRIMARY KEY, path TEXT NOT NULL, msg_date TEXT, sender TEXT,"
                " subject TEXT, status TEXT NOT NULL DEFAULT 'new', parser TEXT, n_jobs INTEGER,"
                " search_query TEXT, notes TEXT, indexed_at TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect(path)
    assert "labeled_at" in {r["name"] for r in conn.execute("PRAGMA table_info(emails)")}

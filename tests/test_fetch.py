from datetime import date
from pathlib import Path

import pytest

from caw.fetch import (
    Config,
    State,
    build_gmail_query,
    fetch_new,
    imap_quote,
    message_path,
)


def make_eml(msg_id: str, day: str = "Mon, 03 Nov 2026 10:00:00 +0100", body="ciao") -> bytes:
    return (
        f"Message-ID: <{msg_id}>\r\nDate: {day}\r\nFrom: jobs@linkedin.com\r\n"
        f"Subject: Offerta {msg_id}\r\n\r\n{body}\r\n"
    ).encode()


class FakeImap:
    """Simulates the part of imaplib used by fetch_new."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 1):
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.selected_readonly = None
        self.searches: list[tuple] = []
        self.visible: set[int] | None = None

    def select(self, folder, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"%d" % len(self.messages)]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            # `visible` simulates the emails that match the query (e.g. a label applied later).
            uids = [u for u in self.messages if self.visible is None or u in self.visible]
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        if command == "FETCH":
            assert "BODY.PEEK[]" in args[1]  # never BODY[]: it must not mark the email as read
            out = []
            for u in map(int, args[0].split(",")):
                raw = self.messages[u]
                out.append((b"%d (UID %d BODY[] {%d}" % (u, u, len(raw)), raw))
                out.append(b")")
            return "OK", out
        raise AssertionError(command)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        host="x",
        port=993,
        folder="[Gmail]/All Mail",
        gmail_query="from:linkedin.com",
        initial_lookback_days=30,
        batch_size=2,
        raw_dir=tmp_path / "raw",
        state_file=tmp_path / "state.json",
    )


def quiet(*_a, **_k):
    pass


def test_downloads_all_and_opens_readonly(cfg):
    imap = FakeImap({i: make_eml(f"m{i}") for i in range(1, 6)})
    state = State()
    res = fetch_new(imap, cfg, state, log=quiet)
    assert imap.selected_readonly is True
    assert (res.found, res.saved, res.duplicates, res.errors) == (5, 5, 0, 0)
    assert len(list(cfg.raw_dir.rglob("*.eml"))) == 5
    assert state.last_uid == 5
    assert State.load(cfg.state_file).last_uid == 5
    # First run: the query contains the time limit.
    assert "after:" in imap.searches[0][-1]


def test_incremental_only_fetches_new(cfg):
    msgs = {i: make_eml(f"m{i}") for i in range(1, 4)}
    imap = FakeImap(msgs)
    state = State()
    fetch_new(imap, cfg, state, log=quiet)

    msgs[4] = make_eml("m4")
    res = fetch_new(imap, cfg, State.load(cfg.state_file), log=quiet)
    assert (res.found, res.saved) == (1, 1)
    assert imap.searches[-1][0] == "X-GM-RAW"


def test_no_new_mail_does_not_refetch_last(cfg):
    imap = FakeImap({1: make_eml("m1"), 2: make_eml("m2")})
    state = State()
    fetch_new(imap, cfg, state, log=quiet)
    res = fetch_new(imap, cfg, State.load(cfg.state_file), log=quiet)
    assert (res.found, res.saved) == (0, 0)


def test_mail_labelled_later_with_low_uid_is_fetched(cfg):
    """An old email (low UID) that enters the query after the first download."""
    msgs = {i: make_eml(f"m{i}") for i in range(1, 6)}
    imap = FakeImap(msgs)
    imap.visible = {3, 4, 5}
    fetch_new(imap, cfg, State(), log=quiet)
    assert len(list(cfg.raw_dir.rglob("*.eml"))) == 3

    imap.visible = {1, 3, 4, 5}  # the user applies the label to email 1
    res = fetch_new(imap, cfg, State.load(cfg.state_file), log=quiet)
    assert (res.found, res.saved, res.duplicates) == (1, 1, 0)
    assert State.load(cfg.state_file).fetched_uids == [1, 3, 4, 5]


def test_old_state_file_without_fetched_uids_still_loads(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"folder": "f", "uidvalidity": 1, "last_uid": 10, "query": "q"}')
    s = State.load(p)
    assert s.last_uid == 10 and s.fetched_uids == []


def test_same_message_id_is_deduplicated(cfg):
    imap = FakeImap({1: make_eml("same"), 2: make_eml("same", body="copia")})
    res = fetch_new(imap, cfg, State(), log=quiet)
    assert (res.saved, res.duplicates) == (1, 1)


def test_uidvalidity_change_resets_but_no_duplicates(cfg):
    msgs = {1: make_eml("a"), 2: make_eml("b")}
    state = State()
    fetch_new(FakeImap(msgs, uidvalidity=1), cfg, state, log=quiet)
    res = fetch_new(
        FakeImap(msgs, uidvalidity=2), cfg, State.load(cfg.state_file), log=quiet
    )
    assert (res.found, res.saved, res.duplicates) == (2, 0, 2)


def test_query_change_resets_checkpoint(cfg):
    msgs = {i: make_eml(f"m{i}") for i in range(1, 4)}
    fetch_new(FakeImap(msgs), cfg, State(), log=quiet)
    cfg.gmail_query = "label:caw"
    imap = FakeImap(msgs)
    res = fetch_new(imap, cfg, State.load(cfg.state_file), log=quiet)
    assert res.found == 3
    assert State.load(cfg.state_file).query == "label:caw"


def test_dry_run_and_limit(cfg):
    imap = FakeImap({i: make_eml(f"m{i}") for i in range(1, 6)})
    res = fetch_new(imap, cfg, State(), dry_run=True, log=quiet)
    assert res.found == 5 and not list(cfg.raw_dir.rglob("*.eml"))
    assert not cfg.state_file.exists()

    res = fetch_new(imap, cfg, State(), limit=3, log=quiet)
    assert res.saved == 3


def test_message_path_uses_message_date(tmp_path):
    p = message_path(tmp_path, make_eml("x", "Tue, 01 Sep 2026 23:30:00 -0500"))
    # 23:30 -0500 = 04:30 UTC the next day
    assert p.parent == tmp_path / "2026" / "09" and p.name.startswith("20260902-")


def test_message_path_without_headers_is_stable(tmp_path):
    raw = b"Subject: senza id\r\n\r\ncorpo"
    assert message_path(tmp_path, raw) == message_path(tmp_path, raw)


def test_query_helpers():
    assert build_gmail_query("from:a", date(2026, 1, 5)) == "from:a after:2026/01/05"
    assert build_gmail_query("from:a", None) == "from:a"
    assert imap_quote('a "b"') == '"a \\"b\\""'
    with pytest.raises(ValueError):
        imap_quote("città")


def test_ignore_before_raises_the_lower_bound(cfg):
    from dataclasses import replace
    imap = FakeImap({1: make_eml("m1")})
    fetch_new(imap, replace(cfg, ignore_before=date(2026, 11, 20)), State(), dry_run=True, log=quiet)
    assert imap.searches[-1][-1].endswith("after:2026/11/20" + '"')


def test_ignore_before_does_not_lower_a_recent_window(cfg):
    from dataclasses import replace
    imap = FakeImap({1: make_eml("m1")})
    cfg = replace(cfg, ignore_before=date(2000, 1, 1), initial_lookback_days=10)
    fetch_new(imap, cfg, State(), dry_run=True, log=quiet)
    assert "after:2000" not in imap.searches[-1][-1]  # the more recent window applies


def test_explicit_since_overrides_ignore_before(cfg):
    from dataclasses import replace
    imap = FakeImap({1: make_eml("m1")})
    fetch_new(imap, replace(cfg, ignore_before=date(2026, 11, 20)), State(),
              since=date(2026, 1, 5), dry_run=True, log=quiet)
    assert "after:2026/01/05" in imap.searches[-1][-1]

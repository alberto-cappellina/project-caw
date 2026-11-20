import json
from email.utils import formatdate
from pathlib import Path

import pytest

from caw.llm import LLMError
from caw.run import notify, run_pipeline
from test_process import make_email

ROOT = Path(__file__).resolve().parents[1]
RAW = make_email(date=formatdate()).as_bytes()  # today's date: not "expired"


class FakeImap:
    def __init__(self):
        self.stores = []
        self.created = []
        self.readonly_selects = []

    def select(self, folder, readonly=False):
        self.readonly_selects.append(readonly)
        return "OK", [b"1"]

    def create(self, name):
        self.created.append(name)
        return "OK", [b"created"]

    def response(self, name):
        return name, [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"1"]
        if command == "STORE":
            self.stores.append(args)
            return "OK", [b""]
        return "OK", [(b"1 (UID 1 BODY[] {%d}" % len(RAW), RAW), b")"]

    def logout(self):
        pass


class FakeLLM:
    name = "fake"

    def complete(self, system, prompt):
        ids = [json.loads(l)["id"] for l in prompt.splitlines() if l.startswith("{")]
        return json.dumps([{"id": i, "score": 90, "reason": "ottimo"} for i in ids])


@pytest.fixture
def project(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[imap]\nhost="x"\nport=993\nfolder="F"\n'
        '[fetch]\ngmail_query="label:caw"\ninitial_lookback_days=3650\nbatch_size=10\n'
        '[paths]\nraw_dir="archive/raw"\nstate_file="data/state.json"\n'
        'db_file="data/caw.db"\nreports_dir="reports"\n'
        '[llm]\nbackend="cli"\n'
    )
    return tmp_path, cfg


def run(cfg, **kw):
    kw.setdefault("imap_factory", lambda c: FakeImap())
    kw.setdefault("llm_factory", lambda: FakeLLM())
    return run_pipeline(cfg, ROOT / "profile.example.yaml", report_days=None, new_only=False,
                        log=lambda *a: None, **kw)


def test_full_pipeline(project):
    base, cfg = project
    s = run(cfg)
    assert s.ok and s.errors == [] and s.new_emails == 1
    assert (s.match, s.maybe, s.scored) == (1, 0, 1)  # Android in Milan dropped by the filters
    text = next((base / "reports").glob("*.md")).read_text()
    listed = text.split("## In linea con il profilo")[1]
    assert "Senior Flutter Developer" in listed
    assert "Android Developer" not in listed  # Milan on-site: dropped by the filters


def test_second_run_is_incremental_and_cached(project):
    _, cfg = project
    run(cfg)
    s = run(cfg)
    assert s.ok and s.new_emails == 0 and s.scored == 0  # nothing to pay the LLM for again


def test_fetch_failure_still_builds_report_from_archive(project):
    base, cfg = project
    run(cfg)  # fills the archive

    def broken(c):
        raise OSError("rete assente")

    s = run(cfg, imap_factory=broken)
    assert not s.ok and "download" in s.errors[0] and "rete assente" in s.errors[0]
    assert s.reports and all(p.exists() for p in s.reports)


def test_login_failure_exits_via_systemexit_is_handled(project):
    _, cfg = project

    def bad_login(c):
        raise SystemExit("Login IMAP fallito")

    s = run(cfg, imap_factory=bad_login)
    assert not s.ok and "Login IMAP fallito" in s.errors[0]


def test_llm_failure_keeps_pending_and_reports(project):
    base, cfg = project

    def no_llm():
        raise LLMError("chiave non valida")

    s = run(cfg, llm_factory=no_llm)
    assert not s.ok and "scoring" in s.errors[0]
    text = s.reports[0].read_text()
    assert "Non ancora valutate" in text
    assert run(cfg).ok  # on the next run the offers get scored


def test_no_fetch_skips_gmail(project):
    _, cfg = project

    def must_not_be_called(c):
        raise AssertionError("Gmail non va chiamato")

    s = run(cfg, imap_factory=must_not_be_called, do_fetch=False)
    assert s.ok and s.new_emails == 0


def test_notify_text_is_sanitized(monkeypatch):
    from caw import run as run_mod

    seen = {}
    monkeypatch.setattr(run_mod.sys, "platform", "darwin")
    monkeypatch.setattr(run_mod.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd))
    s = run_mod.RunSummary(ok=False, errors=['download: x" & do shell script "evil\\'])
    notify(s)
    script = seen["cmd"][-1]
    assert script.count('"') == 4 and "\\" not in script  # only the quotes of the command itself


def test_pipeline_marks_processed_mail_read_and_labelled(project):
    _, cfg = project
    imaps = []

    def factory(c):
        imaps.append(FakeImap())
        return imaps[-1]

    s = run(cfg, imap_factory=factory)
    assert s.ok
    download, marking = imaps  # one session for the download (read-only), one for marking
    assert download.readonly_selects == [True] and download.stores == []
    assert marking.readonly_selects == [False]
    assert marking.created == ['"caw-processed"']
    flags = [a for a in marking.stores]
    assert ("1", "+FLAGS.SILENT", r"(\Seen)") in flags
    assert ("1", "+X-GM-LABELS", '("caw-processed")') in flags

    # second run: nothing to mark, no write session
    imaps.clear()
    run(cfg, imap_factory=factory)
    assert all(not i.stores for i in imaps)


def test_marking_failure_is_reported_but_report_still_built(project):
    _, cfg = project

    class BrokenStore(FakeImap):
        def uid(self, command, *args):
            if command == "STORE":
                return "NO", [b"quota"]
            return super().uid(command, *args)

    s = run(cfg, imap_factory=lambda c: BrokenStore())
    assert not s.ok and any("etichette Gmail" in e for e in s.errors)
    assert s.reports and s.scored == 1  # the scoring and the report still run

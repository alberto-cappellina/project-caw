from email.message import EmailMessage
from pathlib import Path

from caw.db import connect
from caw.parsers import parse_email
from caw.process import process


def url(job_id: str) -> str:
    return (
        "https://www.jobleads.com/user/kuuid?m=1&redirectBack=https%3A%2F%2Fx%2F%3F"
        f"utm_content%3Djob_id%253D{job_id}%257Csearch_id%253Dabc%26job_rank%3D1"
    )


PLAIN = f"""Nuovi lavori per 24 maggioIn base alla tua ricerca salvataI risultati <span>'Senior Flutter Developer, Verona, ITA'</span>-----------------------------

Senior Flutter Developer
- Verona
- hybrid
- EUR 55.000 - 65.000
- full_time
- Equity participation
Mostra l’offerta di lavoro: {url("external-aaa111")}

Android Developer
- Milano
- in_person
- EUR 40.000
- full_time
Mostra l’offerta di lavoro: {url("external-bbb222")}
-----------------------------
Vedi tutti i risultati: https://x
-----------------------------
(c) JobLeads GmbH
"""

HTML = """<html><head><style>.a{}</style><title>t</title></head><body>
<p>Senior Flutter Developer</p><p>Acme S.p.A.</p><p>•</p><p>Verona | Ibrido</p>
<p>Android Developer</p><p>Beta &amp; C S.r.l.</p><p>•</p><p>Milano | In loco</p>
</body></html>"""


def make_email(subject="Ci sono 2 nuove offerte di lavoro", plain=PLAIN, html=HTML, date="Tue, 26 May 2026 10:00:00 +0000"):
    msg = EmailMessage()
    msg["From"] = "JobLeads <mailer@jobleads.com>"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{abs(hash((subject, date)))}@x>"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def test_parse_jobleads():
    res = parse_email(make_email())
    assert res.search_query == "Senior Flutter Developer, Verona, ITA"
    assert res.notes == []
    a, b = res.jobs
    assert (a.job_id, a.title, a.company) == ("jobleads:external-aaa111", "Senior Flutter Developer", "Acme S.p.A.")
    assert (a.location, a.work_mode, a.salary_min, a.salary_max, a.contract) == (
        "Verona", "hybrid", 55000, 65000, "full_time",
    )
    assert a.extras == ["Equity participation"]
    assert b.company == "Beta & C S.r.l." and b.salary_min == b.salary_max == 40000
    # link to the offer page (not the tracking redirect), without personal parameters
    assert a.url == "https://www.jobleads.com/it/job/senior-flutter-developer--verona--eaaa111"
    assert b.url == "https://www.jobleads.com/it/job/android-developer--milano--ebbb222"
    assert "kuuid" not in a.url


def test_unexpected_link_format_falls_back_with_note():
    plain = PLAIN.replace("external-aaa111", "boh").replace("external-bbb222", "boh2")
    res = parse_email(make_email(plain=plain))
    assert any("pagina generica" in n for n in res.notes)
    assert res.jobs[0].job_id.startswith("jobleads:fallback-")
    assert res.jobs[0].url.startswith("https://www.jobleads.com/user/kuuid")


def test_count_mismatch_is_reported():
    res = parse_email(make_email(subject="Ci sono 5 nuove offerte di lavoro"))
    assert any("annuncia 5" in n for n in res.notes)


def test_unknown_sender_is_unsupported():
    msg = make_email()
    del msg["From"]
    msg["From"] = "x@example.com"
    assert parse_email(msg) is None


def test_process_is_idempotent_and_dedups_jobs(tmp_path: Path):
    raw = tmp_path / "raw"
    for i, day in enumerate(["26 May", "27 May"]):
        d = raw / "2026" / "05"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"2026052{6 + i}-0000000{i}.eml").write_bytes(
            make_email(date=f"Tue, {day} 2026 10:00:00 +0000").as_bytes()
        )
    conn = connect(tmp_path / "t.db")

    s1 = process(conn, raw, log=lambda *a: None)
    assert (s1["indexed"], s1["parsed"], s1["jobs"]) == (2, 2, 4)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    row = conn.execute("SELECT * FROM jobs WHERE job_id='jobleads:external-aaa111'").fetchone()
    assert row["times_seen"] == 2 and row["first_seen"].startswith("2026-05-26")
    assert row["last_seen"].startswith("2026-05-27")

    s2 = process(conn, raw, log=lambda *a: None)  # nothing new
    assert (s2["indexed"], s2["parsed"]) == (0, 0)

    s3 = process(conn, raw, reparse=True, log=lambda *a: None)
    assert s3["parsed"] == 2
    assert conn.execute("SELECT SUM(times_seen) FROM jobs").fetchone()[0] == 4

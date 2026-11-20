from datetime import datetime, timezone
from pathlib import Path

from caw.db import connect
from caw.report import collect, fmt_salary, reason_category, render_html, render_markdown


def add(conn, job_id, title="Senior Android Developer", company="Acme", location="Verona",
        mode="hybrid", lo=55000, hi=65000, status="ok", score=None, verdict="da_valutare",
        reasons="[]", flags="[]", seen="2026-06-10T10:00:00+00:00", times=1, reason=None):
    conn.execute(
        "INSERT INTO jobs (job_id, source, title, company, location, work_mode, salary_min, salary_max,"
        " currency, contract, extras, url, first_seen, last_seen, times_seen)"
        " VALUES (?,?,?,?,?,?,?,?,'EUR','full_time','[]',?,?,?,?)",
        (job_id, "jobleads", title, company, location, mode, lo, hi, f"https://x/{job_id}", seen, seen, times),
    )
    conn.execute(
        "INSERT INTO evaluations (job_id, rule_status, rule_reasons, flags, score, llm_reason, verdict)"
        " VALUES (?,?,?,?,?,?,?)",
        (job_id, status, reasons, flags, score, reason, verdict),
    )


def make(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO emails (id, path, msg_date, status, n_jobs, indexed_at)"
        " VALUES ('e1','a','2026-06-10T10:00:00+00:00','parsed',6,'x')"
    )
    add(conn, "m1", score=90, verdict="match", reason="Ruolo perfetto")
    add(conn, "m2", title="Android Lead", score=75, verdict="match")
    add(conn, "f1", title="Mobile Dev | Pipe", score=50, verdict="forse")
    add(conn, "n1", title="Flutter Dev", lo=35000, hi=47000, flags='["ral inferiore"]')
    add(conn, "v1", location="Veneto", status="verify", reasons='["sede generica \'Veneto\'"]')
    add(conn, "x1", location="Vicenza", status="excluded", verdict="escluso",
        reasons='["sede fuori provincia di Verona: Vicenza (in_person)"]')
    add(conn, "x2", location="Brescia", status="excluded", verdict="escluso",
        reasons='["sede fuori provincia di Verona: Brescia (hybrid)", "RAL 20.000 sotto il minimo 30.000"]')
    conn.commit()
    return conn


NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


def test_sections_and_exclusion_counts(tmp_path):
    d = collect(make(tmp_path), now=NOW)
    assert [o.title for o in d.sections["match"]] == ["Senior Android Developer", "Android Lead"]
    assert len(d.sections["forse"]) == 1 and len(d.sections["da_valutare"]) == 2
    assert d.excluded == 2
    assert d.excluded_reasons["sede fuori provincia di Verona"] == 2
    assert d.excluded_reasons["RAL sotto il minimo"] == 1
    assert (d.emails, d.sightings, d.unique_jobs) == (1, 6, 7)


def test_low_score_counted_as_such_even_with_doubts(tmp_path):
    conn = make(tmp_path)
    add(conn, "low", title="Game Designer", status="verify", score=5, verdict="escluso",
        reasons='["sede generica \'Veneto\'"]')
    conn.commit()
    d = collect(conn, now=NOW)
    assert d.excluded_reasons["punteggio basso"] == 1
    assert not any("generica" in k for k in d.excluded_reasons)


def test_excluded_offers_are_not_listed(tmp_path):
    d = collect(make(tmp_path), now=NOW)
    md = render_markdown(d)
    assert "Vicenza" not in md.split("## Riepilogo")[1].split("## In linea")[1]
    assert "× sede fuori provincia di Verona" in md


def test_markdown_content(tmp_path):
    md = render_markdown(collect(make(tmp_path), now=NOW))
    assert "| 90 | Senior Android Developer | Acme | Verona · ibrido | 55–65k € |" in md
    assert "Ruolo perfetto" in md and "[apri](https://x/m1)" in md
    assert "Mobile Dev / Pipe" in md  # a | in the title does not break the table
    assert "ral inferiore" in md and "⚠ sede generica" in md
    assert "Non ancora valutate" in md


def test_days_filter(tmp_path):
    conn = make(tmp_path)
    add(conn, "old", title="Vecchia offerta", seen="2026-01-01T00:00:00+00:00", score=99, verdict="match")
    conn.commit()
    assert len(collect(conn, now=NOW).sections["match"]) == 3
    assert len(collect(conn, days=7, now=NOW).sections["match"]) == 2


def test_similar_offers_are_merged(tmp_path):
    conn = make(tmp_path)
    add(conn, "m1b", score=88, verdict="match", seen="2026-06-11T10:00:00+00:00")
    conn.commit()
    match = collect(conn, now=NOW).sections["match"]
    same = [o for o in match if o.title == "Senior Android Developer"]
    assert len(same) == 1 and same[0].copies == 2 and same[0].times_seen == 2


def test_html_is_escaped(tmp_path):
    conn = make(tmp_path)
    add(conn, "evil", title="<script>alert(1)</script>", company="A&B", score=80, verdict="match")
    conn.commit()
    html = render_html(collect(conn, now=NOW))
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "A&amp;B" in html


def test_helpers():
    assert fmt_salary(35000, 50000, "EUR") == "35–50k €"
    assert fmt_salary(40000, 40000, "EUR") == "40k €"
    assert fmt_salary(None, None, None) == "n/d"
    assert reason_category("RAL 20.000 sotto il minimo 30.000") == "RAL sotto il minimo"
    assert reason_category("settore escluso (casino)") == "settore escluso"


def test_new_only_uses_first_seen(tmp_path):
    conn = make(tmp_path)
    # first seen in January, back in June: not news
    add(conn, "again", title="Offerta ricorrente", score=95, verdict="match", seen="2026-06-10T10:00:00+00:00")
    conn.execute("UPDATE jobs SET first_seen='2026-01-05T10:00:00+00:00' WHERE job_id='again'")
    conn.commit()
    titles = lambda d: [o.title for o in d.sections.get("match", [])]  # noqa: E731
    assert "Offerta ricorrente" in titles(collect(conn, days=30, now=NOW))
    assert "Offerta ricorrente" not in titles(collect(conn, days=30, now=NOW, new_only=True))
    assert len(collect(conn, days=30, now=NOW, new_only=True).sections["match"]) == 2


def test_stale_offers_are_left_out_and_counted(tmp_path):
    conn = make(tmp_path)  # all seen on 2026-06-10
    add(conn, "fresh", title="Offerta recente", score=95, verdict="match", seen="2026-06-11T10:00:00+00:00")
    conn.commit()
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)  # the others: 40 days; "fresh": 39 days
    d = collect(conn, now=now, stale_days=39)
    assert [o.title for o in d.sections["match"]] == ["Offerta recente"]
    assert d.stale == 7 and d.unique_jobs == 1  # the 7 offers of the fixture are all old
    md = render_markdown(d)
    assert "Probabilmente scadute (non riviste da 39 giorni, non nel report): **7**" in md
    assert "Probabilmente scadute" in render_html(d)
    # without a threshold (--include-stale) everything comes back
    assert collect(conn, now=now).stale == 0


def test_last_seen_column(tmp_path):
    data = collect(make(tmp_path), now=NOW)
    assert "| 55–65k € | 2026-06-10 |" in render_markdown(data)
    assert "<th>Vista</th>" in render_html(data)


def test_old_strong_matches_go_to_review_section(tmp_path):
    conn = make(tmp_path)  # m1 (90) and m2 (75) are matches seen on 2026-06-10
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)  # 40 days later
    add(conn, "fresh", title="Offerta recente", score=95, verdict="match", seen="2026-07-15T10:00:00+00:00")
    add(conn, "ancient", title="Offerta antica", score=99, verdict="match", seen="2026-03-01T10:00:00+00:00")
    conn.commit()
    d = collect(conn, now=now, stale_days=30, review_days=60)
    assert [o.title for o in d.sections["match"]] == ["Offerta recente"]
    assert [o.title for o in d.sections["review"]] == ["Senior Android Developer", "Android Lead"]
    assert "forse" not in d.sections  # old "forse" (maybe) offers stay excluded
    assert d.stale >= 1  # the ancient offer (over 60 days) and the expired "forse" offers
    md = render_markdown(d)
    assert "## Forse ancora aperte, da verificare (2)" in md
    assert md.index("## In linea") < md.index("## Forse ancora aperte")
    assert "da verificare (vecchie): **2**" in md
    assert "Offerta antica" not in md
    assert "Forse ancora aperte" in render_html(d)


def test_review_disabled_without_review_days(tmp_path):
    d = collect(make(tmp_path), now=datetime(2026, 7, 20, tzinfo=timezone.utc), stale_days=30)
    assert "review" not in d.sections

from email.message import EmailMessage

from caw.parsers import parse_email
from caw.parsers.talent import parse_salary

LINK = (
    "https://it.talent.com/redirect?id&#x3D;{id}&amp;source&#x3D;talent&amp;context&#x3D;email"
    "&amp;user_id&#x3D;SECRET&amp;click_type&#x3D;em-jobredirect&amp;vcid&#x3D;955"
)


def card(job_id, title, place, company=None, extra=(), banner=None):
    """A card like the real ones: a wrapper link with a second link (the title) inside."""
    href = LINK.format(id=job_id)
    rows = [f'<tr><td>{banner}</td></tr>'] if banner else []
    rows.append(f'<tr><td><a href="{href}">{title}</a></td></tr>')
    rows.append(f"<tr><td>{place}</td></tr>")
    if company:
        rows.append(f"<tr><td>{company}</td></tr>")
    rows.append('<tr><td style="color:#0da0ba"></td></tr>')
    rows += [f"<tr><td><span>{e}</span></td></tr>" for e in extra]
    return f'<a href="{href}"><table>{"".join(rows)}</table></a>'


HTML = f"""<html><head><style>.x {{ color: red }}</style></head><body>
<div>Abbiamo scelto queste offerte apposta per te.</div>
<div>&zwnj; &zwnj; &zwnj;</div>
<h1>Il tuo avviso di lavoro giornaliero di Talent.com per Senior Platform Engineer</h1>
<h2>Verona</h2>
{card("aaa111", "Senior Engineer", "verona, veneto, it", "Ball Corporation",
      ["Migliore corrispondenza", "Vicino a te"], banner="Scelta di Talent.com")}
{card("bbb222", "Platform Engineer (Remote)", "Verona, Veneto, IT", "Swissblock",
      ["Periodo di candidatura quasi finito"])}
{card("ccc333", "Backend Developer", "verona", "Acme", ["€45.000 - €55.000 all'anno"])}
{card("aaa111", "Senior Engineer", "verona, veneto, it", "Ball Corporation")}
<a href="https://it.talent.com/en/jobs?click_type=em-more-jobs">Visualizza altre offerte di lavoro</a>
<p>Hai ricevuto questa e-mail perché hai mostrato interesse. Contattaci!</p>
</body></html>"""


def make(html=HTML):
    msg = EmailMessage()
    msg["From"] = '"Talent.com" <no-reply@alerts.talent.com>'
    msg["Subject"] = "Offerte Senior Platform Engineer da non perdere"
    msg["Date"] = "Fri, 20 Nov 2026 12:02:21 +0000"
    msg.set_content("Open this email in an HTML client.")
    msg.add_alternative(html, subtype="html")
    return msg


def test_parse_cards_fields_and_duplicates():
    res = parse_email(make())
    assert res.parser == "talent" and res.notes == []
    assert res.search_query == "Senior Platform Engineer"
    a, b, c = res.jobs  # the repeated card (same id) is counted once
    assert (a.job_id, a.title, a.company, a.location) == (
        "talent:aaa111", "Senior Engineer", "Ball Corporation", "Verona",
    )
    assert a.work_mode is None and a.salary_max is None
    assert a.extras == ["Scelta di Talent.com", "Migliore corrispondenza", "Vicino a te"]
    assert (b.title, b.work_mode, b.company) == ("Platform Engineer (Remote)", "remote", "Swissblock")
    assert b.extras == ["Periodo di candidatura quasi finito"]
    assert (c.salary_min, c.salary_max, c.currency) == (45000, 55000, "EUR")
    assert [j.rank for j in res.jobs] == [1, 2, 3]


def test_url_is_clean_public_link():
    res = parse_email(make())
    assert res.jobs[0].url == "https://it.talent.com/view?id=aaa111"
    assert all("SECRET" not in (j.url or "") for j in res.jobs)


def test_email_without_offers_is_reported_in_notes():
    res = parse_email(make("<html><body><h1>Nessuna offerta oggi</h1></body></html>"))
    assert res.jobs == [] and res.notes  # the format may have changed: leave a note


def test_unknown_extra_line_is_kept_and_reported():
    html = HTML.replace("€45.000 - €55.000 all'anno", "Qualcosa di nuovo")
    res = parse_email(make(html))
    c = res.jobs[2]
    assert c.salary_max is None and "Dato non interpretato: Qualcosa di nuovo" in c.extras
    assert res.notes == ["righe non interpretate: 1"]


def test_parse_salary_formats():
    assert parse_salary("€45.000 - €55.000 all'anno") == (45000, 55000, None)
    assert parse_salary("45k € / anno") == (45000, 45000, None)
    assert parse_salary("€ 1.800,50 al mese") == (23400, 23400, "RAL stimata da: € 1.800,50 al mese")
    assert parse_salary("€20 - €25 all'ora")[:2] == (35200, 44000)
    assert parse_salary("€50.000") is None  # no period: not interpreted
    assert parse_salary("Vicino a te") is None

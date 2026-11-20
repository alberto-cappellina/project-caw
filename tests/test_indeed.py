from email.message import EmailMessage

from caw.parsers import parse_email
from caw.parsers.indeed import parse_salary

ORGANIC = "https://it.indeed.com/rc/clk/dl?jk=abc123def456&from=ja&tk=zzz"
SPONSORED = "https://it.indeed.com/pagead/clk/dl?mo=r&ad=-6Nxx&jsa=5497"

PLAIN = f"""Indeed Job Alert
3 nuovi annunci di lavoro per mobile developer - Verona, Veneto

Jobs 1-3 of 3 new jobs
See matching results on Indeed: https://it.indeed.com/jobs?q=x


Senior Android Developer
Acme Spa - Verona, Veneto
45.000 € - 55.000 € all'anno
Candidati facilmente
Sviluppo app Android in Kotlin.
3 giorni fa
{ORGANIC}

Flutter Developer
Beta - San Giovanni Lupatoto, Veneto
2.000 € al mese
Appena pubblicato
{SPONSORED}

Rider
Gamma - Italia
Consegne serali.
4 giorni fa
https://it.indeed.com/rc/clk/dl?jk=ffff0000aaaa



Non condividere questa email
Questa email contiene link personalizzati.
© 2026 Indeed Ireland
"""


def make(subject="Android Developer presso Acme", plain=PLAIN):
    msg = EmailMessage()
    msg["From"] = "Indeed <donotreply@jobalert.indeed.com>"
    msg["Subject"] = subject
    msg["Date"] = "Sat, 15 Nov 2026 10:00:00 +0000"
    msg.set_content(plain)
    return msg


def test_parse_indeed_digest():
    res = parse_email(make())
    assert res.parser == "indeed" and res.notes == []
    assert res.search_query == "mobile developer - Verona, Veneto"
    a, b, c = res.jobs
    assert (a.job_id, a.title, a.company, a.location) == (
        "indeed:abc123def456", "Senior Android Developer", "Acme Spa", "Verona, Veneto",
    )
    assert (a.salary_min, a.salary_max, a.currency) == (45000, 55000, "EUR")
    assert a.extras == ["Sviluppo app Android in Kotlin."]  # no boilerplate and no "3 giorni fa"
    assert a.url == "https://it.indeed.com/viewjob?jk=abc123def456"  # no personal redirect
    # monthly salary: 2000 x 13, declared as an estimate
    assert (b.salary_min, b.salary_max) == (26000, 26000)
    assert b.extras == ["RAL stimata da: 2.000 € al mese"]
    assert b.job_id.startswith("indeed:fallback-")  # sponsored: no jk
    assert c.salary_max is None and c.location == "Italia"
    assert b.url == SPONSORED  # sponsored: no jk, the original link stays


def test_fallback_id_is_stable():
    first, second = parse_email(make()).jobs[1], parse_email(make()).jobs[1]
    assert first.job_id == second.job_id


def test_count_mismatch_reported():
    res = parse_email(make(plain=PLAIN.replace("3 nuovi annunci", "5 nuovi annunci")))
    assert any("annuncia 5" in n or "annuncia" in n for n in res.notes)


def test_parse_salary():
    assert parse_salary("22.000 € - 25.000 € all'anno") == (22000, 25000, None)
    assert parse_salary("60 € al giorno")[:2] == (13200, 13200)
    assert parse_salary("15 € all'ora")[:2] == (26400, 26400)
    assert parse_salary("Da 1.800,50 € al mese")[:2] == (23406, 23406)
    assert parse_salary("2.000 € boh") is None  # unknown period
    assert parse_salary("Sviluppo app") is None

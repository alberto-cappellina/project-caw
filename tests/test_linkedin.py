from email.message import EmailMessage

from caw.parsers import parse_email
from caw.parsers.linkedin import parse_salary

JOB = "https://www.linkedin.com/comm/jobs/view/{id}/?trackingId=SECRET&refId=X"

PLAIN = f"""----------------------------------------
Hiring trends for Android Developer roles
----------------------------------------

+33%

View job openings
https://www.linkedin.com/comm/jobs/search/?keywords=x

Companies with the most job openings in your role
----------------------------------------
ALTEN Italia
1 job opening
View jobs
https://www.linkedin.com/comm/jobs/search/?company=1
  

People with similar roles applied to these jobs
----------------------------------------
Senior Android Engineer
Qonto · Milan (Remote)
View
{JOB.format(id=111)}
  
App Developer (m/w/x)
Gunnebo · Verona
View
{JOB.format(id=222)}
  

Jobs where you are the top candidate
----------------------------------------
Analista funzionale Mid-Senior
Dale Consulting · Verona (Hybrid)
€45K-€50K / year
View
{JOB.format(id=333)}
  
Tech Lead
Acme · San Martino Buon Albergo (On-site)
€2,500 / month
View
{JOB.format(id=444)}

This email was intended for Jane Doe
"""


def make(plain=PLAIN, subject="1 new Android Developer openings at ALTEN Italia"):
    msg = EmailMessage()
    msg["From"] = "LinkedIn <messages-noreply@linkedin.com>"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 27 Oct 2026 14:16:15 +0000"
    msg.set_content(plain)
    return msg


def test_parse_linkedin_job_sections_only():
    res = parse_email(make())
    assert res.parser == "linkedin" and res.notes == []
    a, b, c, d = res.jobs  # the "trend" and "companies" sections are not offers
    assert (a.job_id, a.title, a.company, a.location, a.work_mode) == (
        "linkedin:111", "Senior Android Engineer", "Qonto", "Milan", "remote",
    )
    assert (b.location, b.work_mode, b.salary_max) == ("Verona", None, None)
    assert (c.work_mode, c.salary_min, c.salary_max, c.currency) == ("hybrid", 45000, 50000, "EUR")
    assert (d.work_mode, d.location) == ("in_person", "San Martino Buon Albergo")
    # monthly salary: 2500 x 13, declared as an estimate
    assert (d.salary_min, d.salary_max) == (32500, 32500)
    assert d.extras == ["RAL stimata da: €2,500 / month"]


def test_url_is_clean_public_link():
    job = parse_email(make()).jobs[0]
    assert job.url == "https://www.linkedin.com/jobs/view/111/"
    assert "SECRET" not in job.url


def test_mail_without_jobs_is_ok_and_silent():
    res = parse_email(make("Your profile is popular\n\nhttps://www.linkedin.com/comm/me/search-appearances/\n"))
    assert res.jobs == [] and res.notes == []


def test_unparsed_job_links_are_reported():
    broken = PLAIN.replace("Qonto · Milan (Remote)", "Qonto Milan")  # unreadable company line
    res = parse_email(make(broken))
    assert len(res.jobs) == 3 and any("link a offerte: 4" in n for n in res.notes)


def test_parse_salary():
    assert parse_salary("€45K-€50K / year") == (45000, 50000, None)
    assert parse_salary("€27K-€30K / year")[:2] == (27000, 30000)
    assert parse_salary("€2,500 / month")[:2] == (32500, 32500)
    assert parse_salary("Verona") is None
    assert parse_salary("€45K") is None  # unknown period

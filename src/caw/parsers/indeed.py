"""Parser for Indeed Job Alert emails (donotreply@jobalert.indeed.com).

In the plain text, each offer is a block separated by an empty line:
    Title
    Company - Location
    [Salary, may be monthly/daily/hourly]
    [Candidati facilmente]
    [description excerpt]
    N giorni fa
    <link>
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from email.message import EmailMessage

from . import Job, ParseResult

NAME = "indeed"
URL_LINE = re.compile(r"^https?://\S+$")
FOOTER_RE = re.compile(r"^(Non condividere questa email|Do not share this email|Questa email contiene)", re.M)
AGE_RE = re.compile(r"^(\d+\+?\s+(giorn[oi]|ore|ora|settiman[ae])\s+fa|pubblicat\w*|appena pubblicat\w*|oggi|ieri|attiv\w+)", re.I)
BOILERPLATE = {"candidati facilmente", "risposta rapida", "easily apply", "responsive employer"}
COUNT_RE = re.compile(r"(\d+)\s+nuov[oi]\s+annunc|(\d+)\s+new\s+jobs?", re.I)
QUERY_RE = re.compile(r"(?:per|for)\s+(.+?)\s+-\s+(.+)$", re.I)
NUM_RE = re.compile(r"\d[\d.]*(?:,\d+)?")

# Multipliers to turn the amount into a yearly salary (prudent estimates, declared in `extras`).
PERIODS = [
    (re.compile(r"all.anno|annu|/anno|per year|a year", re.I), 1, False),
    (re.compile(r"al mese|mensil|/mese|per month|a month", re.I), 13, True),
    (re.compile(r"al giorno|giornalier|per day|a day", re.I), 220, True),
    (re.compile(r"all.ora|orari|per hour|an hour", re.I), 1760, True),
]


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def parse_salary(line: str) -> tuple[int, int, str | None] | None:
    """(min, max, note) in euro/year, or None if the line is not a salary."""
    if "€" not in line:
        return None
    amounts = [_num(n) for n in NUM_RE.findall(line)]
    if not amounts:
        return None
    for pattern, factor, estimated in PERIODS:
        if pattern.search(line):
            lo, hi = round(min(amounts) * factor), round(max(amounts) * factor)
            return lo, hi, f"RAL stimata da: {line}" if estimated else None
    return None  # unknown period: better not to invent a value


def _job_key(url: str) -> str | None:
    key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("jk")
    return key[0] if key else None


def _job_id(url: str, company: str, title: str, location: str | None) -> str:
    key = _job_key(url)
    if key:
        return f"indeed:{key}"
    digest = hashlib.sha1(f"{company}|{title}|{location}".lower().encode()).hexdigest()[:16]
    return f"indeed:fallback-{digest}"  # sponsored ads: no stable id in the link


def _parse_block(block: str, rank: int) -> Job | None:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    if len(lines) < 3 or not URL_LINE.match(lines[-1]):
        return None
    title, url = lines[0], lines[-1]
    company, _, location = lines[1].rpartition(" - ")
    if not company:  # no " - ": company only
        company, location = lines[1], ""
    job = Job(source=NAME, job_id="", title=title, rank=rank, company=company.strip(),
              location=location.strip() or None, url=url)
    for line in lines[2:-1]:
        if line.lower() in BOILERPLATE or AGE_RE.match(line) and len(line) < 40:
            continue
        salary = parse_salary(line) if job.salary_max is None else None
        if salary:
            job.salary_min, job.salary_max, note = salary
            job.currency = "EUR"
            if note:
                job.extras.append(note)
        else:
            job.extras.append(line[:240])  # description excerpt
    if job.location and re.search(r"remot|telelavoro", job.location, re.I):
        job.work_mode = "remote"
    job.job_id = _job_id(url, job.company or "", title, job.location)
    key = _job_key(url)
    if key:  # public page of the ad, without the personal tracking redirect
        host = urllib.parse.urlparse(url).netloc
        job.url = f"https://{host}/viewjob?jk={key}"
    return job


def parse(msg: EmailMessage) -> ParseResult:
    result = ParseResult(parser=NAME, jobs=[])
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part else ""
    footer = FOOTER_RE.search(text)
    body = text[: footer.start()] if footer else text
    if "indeed" not in text.lower():
        result.notes.append("struttura inattesa: nessun riferimento a Indeed")
        return result

    for block in re.split(r"\n\s*\n", body):
        job = _parse_block(block, len(result.jobs) + 1)
        if job:
            result.jobs.append(job)

    # Header line: "N nuovi annunci di lavoro per <search> - <place>"
    header = next((ln for ln in body.split("\n")[:8] if COUNT_RE.search(ln)), "")
    query = QUERY_RE.search(header)
    result.search_query = f"{query.group(1)} - {query.group(2)}".strip() if query else None
    expected = COUNT_RE.search(header)
    if expected:
        n = int(expected.group(1) or expected.group(2))
        if n != len(result.jobs):
            result.notes.append(f"l'intestazione annuncia {n} offerte, estratte {len(result.jobs)}")
    return result

"""Parser for JobLeads digests (mailer@jobleads.com).

For each offer the plain text has: title, location, work mode, salary, contract and link.
The company name is only in the HTML: it is read from there, in order of appearance.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from email.message import EmailMessage
from html.parser import HTMLParser

from . import Job, ParseResult

NAME = "jobleads"
MODES = {"in_person", "hybrid", "remote"}
CONTRACT_RE = re.compile(r"^(full_time|part_time|contract|temporary|internship|freelance)\b", re.I)
SALARY_RE = re.compile(r"^([A-Z]{3})\s+([\d.,]+)(?:\s*-\s*([\d.,]+))?$")
URL_RE = re.compile(r"Mostra l.offerta di lavoro:\s*(\S+)")
EXTERNAL_ID_RE = re.compile(r"external-([0-9a-f]{6,})")
QUERY_RE = re.compile(r"'([^']+)'")
COUNT_RE = re.compile(r"Ci sono (\d+) nuov[ei]")


class _TextExtractor(HTMLParser):
    """Extract the lines of visible text from the HTML."""

    SKIP = {"style", "script", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.lines.append(" ".join(data.split()))


def _number(text: str) -> int:
    return int(re.sub(r"[.,]", "", text))


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def _part(msg: EmailMessage, kind: str) -> str:
    part = msg.get_body(preferencelist=(kind,))
    return part.get_content() if part else ""


def _companies(html: str, titles: list[str]) -> list[str | None]:
    """For each title (in order), look in the HTML for the next line, if it is the company."""
    extractor = _TextExtractor()
    extractor.feed(html)
    lines = extractor.lines
    out: list[str | None] = []
    pos = 0
    for title in titles:
        company = None
        for i in range(pos, len(lines)):
            if _norm(lines[i]) == _norm(title):
                # Structure: title, company, "•", "place | work mode", ...
                if i + 2 < len(lines) and lines[i + 2] == "•":
                    company = lines[i + 1]
                pos = i + 1
                break
        out.append(company)
    return out


def _slug(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _direct_url(title: str, location: str | None, external_id: str) -> str:
    """Public page of the offer, rebuilt from the id.

    In the emails the link is a tracking redirect that leads to a generic search page
    (and it holds personal identifiers). The offer page is instead
    /<language>/job/<title>--<place>--e<id>: the site finds it from the id alone
    (title and place in the path are ignored; the prefix "e" stands for "external").
    """
    return f"https://www.jobleads.com/it/job/{_slug(title)}--{_slug(location)}--e{external_id}"


def _parse_block(block: str, rank: int) -> Job | None:
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
    url_match = URL_RE.search(block)
    if len(lines) < 2 or not url_match:
        return None
    job = Job(source=NAME, job_id="", title=lines[0], rank=rank, url=url_match.group(1))
    for line in lines[1:]:
        if not line.startswith("- "):
            continue
        value = line[2:].strip()
        salary = SALARY_RE.match(value)
        if value in MODES:
            job.work_mode = value
        elif salary:
            job.currency = salary.group(1)
            job.salary_min = _number(salary.group(2))
            job.salary_max = _number(salary.group(3)) if salary.group(3) else job.salary_min
        elif CONTRACT_RE.match(value):
            job.contract = value
        elif job.location is None:
            job.location = value
        else:
            job.extras.append(value)
    found = EXTERNAL_ID_RE.search(job.url or "")
    if found:
        job.job_id = f"jobleads:external-{found.group(1)}"
        job.url = _direct_url(job.title, job.location, found.group(1))
    else:  # unexpected format: fallback id and tracking link unchanged
        job.job_id = "jobleads:fallback-" + hashlib.sha1(
            f"{job.title}|{job.location}".encode()
        ).hexdigest()[:16]
    return job


def parse(msg: EmailMessage) -> ParseResult:
    result = ParseResult(parser=NAME, jobs=[])
    sections = re.split(r"-{10,}", _part(msg, "plain"))
    if len(sections) < 2:
        result.notes.append("struttura inattesa: separatori non trovati")
        return result

    query = QUERY_RE.search(sections[0])
    result.search_query = query.group(1) if query else None

    for block in re.split(r"\n\s*\n", sections[1]):
        job = _parse_block(block, len(result.jobs) + 1)
        if job:
            result.jobs.append(job)

    html = _part(msg, "html")
    if any(j.job_id.startswith("jobleads:fallback-") for j in result.jobs):
        result.notes.append("id offerta non trovato in alcuni link: il link apre una pagina generica")

    companies = _companies(html, [j.title for j in result.jobs])
    for job, company in zip(result.jobs, companies):
        job.company = company
    if any(c is None for c in companies):
        result.notes.append("azienda non trovata per alcune offerte")

    expected = COUNT_RE.search(str(msg.get("Subject", "")))
    if expected and int(expected.group(1)) != len(result.jobs):
        result.notes.append(
            f"oggetto annuncia {expected.group(1)} offerte, estratte {len(result.jobs)}"
        )
    return result

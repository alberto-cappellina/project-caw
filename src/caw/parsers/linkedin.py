"""Parser for LinkedIn notification emails that contain offers
(messages-noreply@ / jobalerts-noreply@ linkedin.com).

The emails have several sections (trends, companies, "People with similar roles
applied...", "Jobs where you are the top candidate"). Only offers have this block:

    Title
    Company · Location [(Remote|Hybrid|On-site)]
    [€45K-€50K / year]
    View
    https://www.linkedin.com/comm/jobs/view/<id>/?...

Emails without offers (e.g. "Your profile is popular") give 0 offers and no error.
"""

from __future__ import annotations

import re
from email.message import EmailMessage

from . import Job, ParseResult

NAME = "linkedin"
JOB_URL_RE = re.compile(r"^https?://\S*/jobs/view/(\d+)\S*$")
MODE_RE = re.compile(r"\s*\((remote|hybrid|on-?site)\)\s*$", re.I)
MODES = {"remote": "remote", "hybrid": "hybrid", "onsite": "in_person", "on-site": "in_person"}
AMOUNT_RE = re.compile(r"(\d[\d.,]*)\s*([KkMm]?)")
SALARY_LINE_RE = re.compile(r"^[€$£]|^[A-Z]{3}\s*\d|/\s*(year|yr|month|mo|hour|hr)\b", re.I)
PERIODS = {"year": (1, False), "yr": (1, False), "month": (13, True), "mo": (13, True),
           "hour": (1760, True), "hr": (1760, True)}


def _amount(number: str, suffix: str) -> int:
    return round(float(number.replace(",", "")) * {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1))


def parse_salary(line: str) -> tuple[int, int, str | None] | None:
    """(min, max, note) in euro/year, or None if the line is not a salary."""
    if not SALARY_LINE_RE.search(line):
        return None
    amounts = [_amount(n, s) for n, s in AMOUNT_RE.findall(line)]
    period = re.search(r"/\s*(year|yr|month|mo|hour|hr)\b", line, re.I)
    if not amounts or not period:
        return None
    factor, estimated = PERIODS[period.group(1).lower()]
    lo, hi = round(min(amounts) * factor), round(max(amounts) * factor)
    return lo, hi, (f"RAL stimata da: {line}" if estimated else None)


def _parse_block(block: str, rank: int) -> Job | None:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not lines:
        return None
    match = JOB_URL_RE.match(lines[-1])
    if not match:
        return None
    try:  # company line: the last one with " · " before "View"
        idx = max(i for i, ln in enumerate(lines[:-1]) if " · " in ln)
    except ValueError:
        return None
    if idx < 1:
        return None
    company, _, place = lines[idx].partition(" · ")
    mode_match = MODE_RE.search(place)
    work_mode = None
    if mode_match:
        work_mode = MODES[mode_match.group(1).lower()]
        place = place[: mode_match.start()]
    job_id = match.group(1)
    job = Job(
        source=NAME, job_id=f"linkedin:{job_id}", title=lines[idx - 1], rank=rank,
        company=company.strip(), location=place.strip() or None, work_mode=work_mode,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",  # without personal parameters
    )
    following = lines[idx + 1] if idx + 1 < len(lines) - 1 else ""
    salary = parse_salary(following)
    if salary:
        job.salary_min, job.salary_max, note = salary
        job.currency = "EUR"
        if note:
            job.extras.append(note)
    return job


def parse(msg: EmailMessage) -> ParseResult:
    result = ParseResult(parser=NAME, jobs=[])
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part else ""
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", text):
        job = _parse_block(block, len(result.jobs) + 1)
        if job and job.job_id not in seen:
            seen.add(job.job_id)
            result.jobs.append(job)
    # How many offer links the email has: if more than the offers read, the format changed.
    n_links = len(set(re.findall(r"/jobs/view/(\d+)", text)))
    if n_links != len(result.jobs):
        result.notes.append(f"link a offerte: {n_links}, offerte estratte: {len(result.jobs)}")
    return result

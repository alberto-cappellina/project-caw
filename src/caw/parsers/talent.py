"""Parser for Talent.com job-alert emails (no-reply@alerts.talent.com).

The email is HTML only. Each offer is a card wrapped in a link to
`https://it.talent.com/redirect?id=<id>&...&click_type=em-jobredirect` (the title inside the
card is a second link with the same id). Text lines of a card, in order:

    [banner]            e.g. "Scelta di Talent.com", "ASSUNZIONE URGENTE"  (optional)
    Title               (the text of the inner link)
    Location            e.g. "verona, veneto, it"
    Company
    [salary]            (optional; format not seen yet, see `parse_salary`)
    [badges]            e.g. "Migliore corrispondenza", "Vicino a te"

Notes:
- The location is the area of the alert, not always the real place of the job (remote or
  national offers also show it). The report keeps it as is.
- `https://it.talent.com/view?id=<id>` redirects to the public page of the offer, so the
  link has no personal tracking parameters.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from email.message import EmailMessage

from . import Job, ParseResult

NAME = "talent"
JOB_HREF_RE = re.compile(r"/redirect\?(?:[^\"']*&)?id=([0-9a-f]+)(?:&|$)", re.I)
REMOTE_RE = re.compile(r"\bremot[eo]\b|\bsmart\s*working\b|\bfull\s*remote\b", re.I)
SALARY_LINE_RE = re.compile(r"[€$£]|\bEUR\b", re.I)
AMOUNT_RE = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d+)?\s*[kK]?")
PERIODS = [  # (regex, factor to euro/year, is estimate)
    (re.compile(r"\b(anno|annuo|annuale|year|yearly|annum)\b", re.I), 1, False),
    (re.compile(r"\b(mese|mensile|month|monthly)\b", re.I), 13, True),
    (re.compile(r"\b(ora|orario|hour|hourly)\b", re.I), 1760, True),
]


class _Cards(HTMLParser):
    """Collects, for each offer link, the id, the title and the text lines of its card."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self.headline: str | None = None  # "Il tuo avviso di lavoro ... per <query>"
        self._stack: list[str | None] = []  # job id of each open <a> (None if not an offer link)
        self._card: dict | None = None
        self._outer_depth = 0
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            href = html.unescape(dict(attrs).get("href") or "")
            match = JOB_HREF_RE.search(href) if "em-jobredirect" in href else None
            job_id = match.group(1) if match else None
            self._stack.append(job_id)
            if job_id and self._card is None:
                self._card = {"id": job_id, "title": None, "lines": []}
                self._outer_depth = len(self._stack)
                self.cards.append(self._card)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._stack:
            self._stack.pop()
            if self._card is not None and len(self._stack) < self._outer_depth:
                self._card = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split()).strip("‌ \xa0")
        if not text:
            return
        if self._card is None:
            if self.headline is None and "avviso di lavoro" in text.lower():
                self.headline = text
            return
        # Text inside the inner offer link (deeper than the card wrapper) is the title.
        if len(self._stack) > self._outer_depth and self._stack[-1] and not self._card["title"]:
            self._card["title"] = text
        else:
            self._card["lines"].append(text)


def _amount(token: str) -> int:
    """'45.000' / '45,000' -> 45000; '1.800,50' -> 1801; '45k' -> 45000; '2,5' -> 2 (rounded)."""
    token = token.strip()
    multiplier = 1
    if token[-1:] in "kK":
        multiplier, token = 1000, token[:-1].strip()
    if "." in token and "," in token:  # the last separator is the decimal one
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        token = token.replace(thousands, "").replace(decimal, ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):  # only thousands separators
        token = re.sub(r"[.,]", "", token)
    else:
        token = token.replace(",", ".")
    return round(float(token) * multiplier)


def parse_salary(line: str) -> tuple[int, int, str | None] | None:
    """(min, max, note) in euro/year, or None if the line is not a salary we understand.

    Without a period word (anno/mese/ora) the line is not interpreted: a wrong RAL would
    wrongly exclude or keep an offer.
    """
    if not SALARY_LINE_RE.search(line):
        return None
    amounts = [_amount(t) for t in AMOUNT_RE.findall(line)]
    if not amounts:
        return None
    for regex, factor, estimated in PERIODS:
        if regex.search(line):
            lo, hi = round(min(amounts) * factor), round(max(amounts) * factor)
            return lo, hi, (f"RAL stimata da: {line}" if estimated else None)
    return None


def _clean_place(raw: str) -> str | None:
    """'verona, veneto, it' -> 'Verona' (the first part: the city)."""
    city = raw.split(",")[0].strip()
    return city.title() if city.islower() else (city or None)


def _job(card: dict, rank: int) -> Job | None:
    title = card["title"]
    lines = card["lines"]
    if not title:
        return None
    # Lines before the title are banners; the card lines start after it. The inner-link text is
    # taken out of `lines`, so `lines` = banners + location + company + (salary) + badges.
    banners = {"scelta di talent.com", "assunzione urgente"}
    rest = [ln for ln in lines if ln.lower() not in banners]
    extras = [ln for ln in lines if ln.lower() in banners]
    badge_re = re.compile(
        r"corrispondenza|vicino a te|altamente raccomandato|periodo di candidatura", re.I
    )
    body = [ln for ln in rest if not badge_re.search(ln)]
    extras += [ln for ln in rest if badge_re.search(ln)]
    location = _clean_place(body[0]) if body else None
    company = body[1] if len(body) > 1 else None
    job = Job(
        source=NAME, job_id=f"talent:{card['id']}", title=title, rank=rank,
        company=company, location=location,
        work_mode="remote" if REMOTE_RE.search(title) else None,
        extras=extras,
        url=f"https://it.talent.com/view?id={card['id']}",
    )
    for line in body[2:]:
        salary = parse_salary(line)
        if salary:
            job.salary_min, job.salary_max, note = salary
            job.currency = "EUR"
            if note:
                job.extras.append(note)
        else:
            job.extras.append(f"Dato non interpretato: {line}")
    return job


def parse(msg: EmailMessage) -> ParseResult:
    result = ParseResult(parser=NAME, jobs=[])
    part = msg.get_body(preferencelist=("html",))
    content = part.get_content() if part else ""
    extractor = _Cards()
    extractor.feed(content)
    seen: set[str] = set()
    for card in extractor.cards:
        job = _job(card, len(result.jobs) + 1)
        if job and job.job_id not in seen:
            seen.add(job.job_id)
            result.jobs.append(job)
    # Cards with a link but no usable title: the layout probably changed.
    n_cards = len({c["id"] for c in extractor.cards})
    if n_cards != len(result.jobs) or not result.jobs:
        result.notes.append(f"schede trovate: {n_cards}, offerte estratte: {len(result.jobs)}")
    uninterpreted = sum(1 for j in result.jobs for e in j.extras if e.startswith("Dato non interpretato"))
    if uninterpreted:
        result.notes.append(f"righe non interpretate: {uninterpreted}")
    match = re.search(r"\bper (.+)$", extractor.headline or "")
    result.search_query = match.group(1).strip() if match else None
    return result

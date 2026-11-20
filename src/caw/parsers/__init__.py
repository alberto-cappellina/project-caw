"""Parsers by sender. Each one extracts the single offers from an email (digests included)."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass
class Job:
    source: str
    job_id: str
    title: str
    rank: int
    company: str | None = None
    location: str | None = None
    work_mode: str | None = None  # in_person | hybrid | remote
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    contract: str | None = None
    extras: list[str] = field(default_factory=list)
    url: str | None = None


@dataclass
class ParseResult:
    parser: str
    jobs: list[Job]
    search_query: str | None = None
    notes: list[str] = field(default_factory=list)


def parse_email(msg: EmailMessage) -> ParseResult | None:
    """Pick the parser from the sender; return None if the email is not supported."""
    from . import indeed, jobleads, linkedin, talent

    sender = str(msg.get("From", "")).lower()
    if "jobleads.com" in sender:
        return jobleads.parse(msg)
    if "indeed.com" in sender:
        return indeed.parse(msg)
    if "linkedin.com" in sender:
        return linkedin.parse(msg)
    if "talent.com" in sender:
        return talent.parse(msg)
    return None

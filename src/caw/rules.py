"""Phase 4a - Deterministic filters (free) applied to every offer.

Result:
  excluded  at least one reason to exclude (salary below minimum, place outside the province, ...)
  verify    cannot be decided by the rules (e.g. vague place "Veneto"): scoring or user decides
  ok        no problem
`flags` are notes that exclude nothing (e.g. "ral inferiore").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def norm(text: str | None) -> str:
    """Lowercase, without accents or punctuation."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def contains_word(haystack: str, needle: str) -> bool:
    """`needle` appears as a sequence of whole words in `haystack` (both normalized)."""
    return f" {needle} " in f" {haystack} "


def load_profile(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"{path.name} non trovato: copia profile.example.yaml in {path.name} e modificalo."
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class RuleResult:
    status: str = "ok"  # ok | verify | excluded
    reasons: list[str] = field(default_factory=list)  # reasons for exclusion or doubt
    flags: list[str] = field(default_factory=list)


class Rules:
    def __init__(self, profile: dict):
        flt = profile["filters"]
        sal = profile["salary_eur_ral"]
        self.exclude_below = sal["exclude_below"]
        self.flag_below = sal["flag_below"]
        self.flag_label = sal["flag_label"]
        self.compare_on = sal.get("compare_on", "max")
        self.junior = [norm(k) for k in flt["junior_keywords"]]
        self.excluded_kw = [norm(k) for k in flt["excluded_keywords"]]
        self.generic = {norm(k) for k in flt["generic_locations"]}
        self.remote_hints = [norm(k) for k in flt["remote_hints"]]
        self.places = [norm(p) for p in flt["verona_province_places"]]

    def _in_verona_province(self, location: str) -> bool:
        loc = norm(location)
        return any(contains_word(loc, p) for p in self.places)

    def evaluate(self, job) -> RuleResult:
        """`job` is a sqlite3.Row or a dict with the columns of the jobs table."""
        res = RuleResult()
        exclude: list[str] = []
        verify: list[str] = []
        title = norm(job["title"])
        extras = norm(job["extras"] or "")
        text = f"{title} {norm(job['company'])} {extras}"

        # 1. Seniority
        if "senior" not in title and any(contains_word(title, k) for k in self.junior):
            exclude.append("ruolo junior/stage")

        # 2. Excluded sectors (keywords; the rest is judged by the scoring)
        for kw in self.excluded_kw:
            if kw in text:
                exclude.append(f"settore escluso ({kw})")
                break

        # 3. Salary (RAL)
        value = job["salary_max"] if self.compare_on == "max" else job["salary_min"]
        if value is None:
            res.flags.append("RAL non indicata")
        elif value < self.exclude_below:
            exclude.append(f"RAL {value:,} sotto il minimo {self.exclude_below:,}".replace(",", "."))
        elif value < self.flag_below:
            res.flags.append(self.flag_label)

        # 4. Place and work mode
        mode, location = job["work_mode"], job["location"]
        if mode == "remote":
            res.flags.append("full remote")
        else:
            remote_in_text = any(contains_word(f"{title} {extras}", h) for h in self.remote_hints)
            if not location:
                verify.append("sede non indicata")
            elif self._in_verona_province(location):
                if mode == "in_person":
                    res.flags.append("in sede (provincia di Verona)")
            elif norm(location) in self.generic:
                verify.append(f"sede generica '{location}'")
            elif remote_in_text:
                label = {"in_person": "in sede", "hybrid": "ibrido"}.get(mode, mode)
                verify.append(f"titolo parla di remoto ma modalità {label} a {location}")
            else:
                exclude.append(
                    f"sede fuori provincia di Verona: {location}" + (f" ({mode})" if mode else "")
                )

        res.reasons = exclude or verify
        res.status = "excluded" if exclude else ("verify" if verify else "ok")
        return res

"""Phase 5 - Summary report of the offers that match the profile.

    python -m caw.report                 # reports/YYYY-MM-DD.md and .html, all offers
    python -m caw.report --days 7        # only offers seen in the last 7 days
    python -m caw.report --format md     # Markdown only

Offers dropped by the filters are not listed: the report only shows their count and the
main reasons. Offers not scored by the LLM yet are in a separate section.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect
from .fetch import Config
from .rules import norm

MODES = {"in_person": "in sede", "hybrid": "ibrido", "remote": "remoto"}


@dataclass
class Offer:
    title: str
    company: str
    location: str
    mode: str
    salary: str
    score: int | None
    reason: str
    flags: list[str]
    doubts: list[str]
    url: str
    first_seen: str
    last_seen: str
    times_seen: int
    copies: int = 1  # offers with the same company/title/place but a different id


@dataclass
class ReportData:
    generated: str
    period: str
    emails: int
    sightings: int
    unique_jobs: int
    excluded: int
    excluded_reasons: Counter
    sections: dict[str, list[Offer]] = field(default_factory=dict)
    stale: int = 0  # offers that are probably expired (not in the report)
    stale_days: int | None = None


def fmt_salary(lo, hi, currency) -> str:
    if not hi:
        return "n/d"
    cur = "€" if currency == "EUR" else (currency or "")
    if lo == hi or not lo:
        return f"{hi / 1000:g}k {cur}"
    return f"{lo / 1000:g}–{hi / 1000:g}k {cur}"


def reason_category(reason: str) -> str:
    if reason.startswith("sede fuori"):
        return "sede fuori provincia di Verona"
    if reason.startswith("RAL"):
        return "RAL sotto il minimo"
    return re.sub(r"\s*\(.*\)$", "", reason)


def collect(
    conn: sqlite3.Connection, days: int | None = None, now: datetime | None = None,
    new_only: bool = False, stale_days: int | None = None, review_days: int | None = None,
) -> ReportData:
    """`days`: window in days. With `new_only` it uses the first time the offer appeared
    (only the news), otherwise the last time (all offers still around)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat() if days else ""
    column = "first_seen" if new_only else "last_seen"
    rows = conn.execute(
        "SELECT j.*, e.rule_status, e.rule_reasons, e.flags, e.score, e.llm_reason, e.verdict "
        f"FROM jobs j JOIN evaluations e USING(job_id) WHERE j.{column} >= ?",
        (since,),
    ).fetchall()
    mail_where = "WHERE msg_date >= ?" if since else "WHERE ? = ''"
    n_emails, n_sightings = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(n_jobs),0) FROM emails {mail_where} AND status='parsed'",
        (since,),
    ).fetchone()
    # An offer not seen in any email for `stale_days` days is almost surely expired on the
    # site (the links answer "ad no longer online"): it does not go in the report.
    stale_cut = (now - timedelta(days=stale_days)).isoformat() if stale_days else ""
    fresh = [r for r in rows if (r["last_seen"] or "") >= stale_cut]
    # Exception: an offer that is "expired by date" but has a high score and was seen not too
    # long ago (e.g. on LinkedIn offers stay open longer) goes in a separate section to verify.
    review_cut = (now - timedelta(days=review_days)).isoformat() if review_days else ""
    review = [
        r for r in rows
        if r not in fresh and r["verdict"] == "match" and (r["last_seen"] or "") >= review_cut
    ] if review_days else []
    n_stale = len(rows) - len(fresh) - len(review)
    review_ids = {r["job_id"] for r in review}
    rows = fresh
    dates = [r["first_seen"] for r in rows if r["first_seen"]]
    period = f"{min(dates)[:10]} → {max(r['last_seen'] for r in rows)[:10]}" if dates else "-"

    data = ReportData(
        generated=now.astimezone().strftime("%Y-%m-%d %H:%M"), period=period, emails=n_emails,
        sightings=n_sightings, unique_jobs=len(rows), excluded=0, excluded_reasons=Counter(),
        stale=n_stale, stale_days=stale_days,
    )
    rows = rows + review
    best: dict[tuple, Offer] = {}
    section_of: dict[tuple, str] = {}
    for r in rows:
        reasons = json.loads(r["rule_reasons"] or "[]")
        if r["rule_status"] == "excluded" or r["verdict"] == "escluso":
            data.excluded += 1
            if r["rule_status"] == "excluded":
                for reason in reasons:
                    data.excluded_reasons[reason_category(reason)] += 1
            else:  # passed the filters (even with doubts): dropped by the LLM score
                data.excluded_reasons["punteggio basso"] += 1
            continue
        section = r["verdict"] if r["verdict"] in ("match", "forse") else "da_valutare"
        if r["job_id"] in review_ids:
            section = "review"
        offer = Offer(
            title=r["title"], company=r["company"] or "?", location=r["location"] or "?",
            mode=MODES.get(r["work_mode"], r["work_mode"] or "?"),
            salary=fmt_salary(r["salary_min"], r["salary_max"], r["currency"]),
            score=r["score"], reason=r["llm_reason"] or "",
            flags=json.loads(r["flags"] or "[]"),
            doubts=reasons if r["rule_status"] == "verify" else [],
            url=r["url"] or "", first_seen=(r["first_seen"] or "")[:10],
            last_seen=(r["last_seen"] or "")[:10], times_seen=r["times_seen"],
        )
        key = (norm(offer.company), norm(offer.title), norm(offer.location))
        if key in best:
            old = best[key]
            keep, drop = (offer, old) if offer.last_seen > old.last_seen else (old, offer)
            keep.copies = old.copies + 1
            keep.times_seen += drop.times_seen
            best[key], section_of[key] = keep, section if keep is offer else section_of[key]
        else:
            best[key], section_of[key] = offer, section

    for key, offer in best.items():
        data.sections.setdefault(section_of[key], []).append(offer)
    for name, offers in data.sections.items():
        if name == "da_valutare":  # without a score: remote/hybrid first, then the highest salary
            offers.sort(key=lambda o: (o.mode == "in sede", -_salary_key(o.salary), o.title))
        else:
            offers.sort(key=lambda o: (-(o.score or 0), o.company))
    return data


def _salary_key(text: str) -> float:
    nums = re.findall(r"[\d.]+(?=k)", text)
    return float(nums[-1]) if nums else 0.0


TITLES = {
    "match": "In linea con il profilo",
    "forse": "Forse",
    "review": "Forse ancora aperte, da verificare",
    "da_valutare": "Non ancora valutate dall'LLM",
}
BLURB = {
    "match": "Punteggio alto: ruolo, stack e sede coerenti.",
    "forse": "Compatibilità parziale o titolo ambiguo: da guardare a mano.",
    "review": "Punteggio alto, ma non riviste in nessuna mail da molto tempo: potrebbero essere "
    "scadute. Controlla il link prima di candidarti.",
    "da_valutare": "Hanno superato i filtri (RAL, sede, seniority) ma manca il punteggio: "
    "esegui `python -m caw.score`.",
}


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def render_markdown(d: ReportData) -> str:
    out = [f"# Report offerte di lavoro", "", f"Generato il {d.generated} · periodo {d.period}", ""]
    shown = sum(len(v) for v in d.sections.values())
    out += [
        "## Riepilogo",
        "",
        f"- Mail analizzate: **{d.emails}** ({d.sightings} offerte viste, **{d.unique_jobs}** uniche)",
        f"- In linea con il profilo: **{len(d.sections.get('match', []))}** · "
        f"forse: **{len(d.sections.get('forse', []))}** · "
        f"da verificare (vecchie): **{len(d.sections.get('review', []))}** · "
        f"da valutare: **{len(d.sections.get('da_valutare', []))}**",
        f"- Escluse (non nel report): **{d.excluded}** su {d.unique_jobs}",
    ]
    for reason, n in d.excluded_reasons.most_common():
        out.append(f"  - {n} × {reason}")
    if d.stale:
        out.append(
            f"- Probabilmente scadute (non riviste da {d.stale_days} giorni, non nel report): **{d.stale}**"
        )
    out.append("")
    for name in ("match", "forse", "review", "da_valutare"):
        offers = d.sections.get(name, [])
        if not offers:
            continue
        out += [f"## {TITLES[name]} ({len(offers)})", "", f"_{BLURB[name]}_", ""]
        out += ["| Punti | Ruolo | Azienda | Sede | RAL | Vista | Note | Link |", "|---|---|---|---|---|---|---|---|"]
        for o in offers:
            notes = list(o.flags)
            if o.doubts:
                notes.append("⚠ " + "; ".join(o.doubts))
            if o.reason:
                notes.append(o.reason)
            if o.copies > 1:
                notes.append(f"{o.copies} annunci simili")
            link = f"[apri]({o.url})" if o.url else ""
            out.append(
                f"| {o.score if o.score is not None else '–'} | {_cell(o.title)} | {_cell(o.company)} "
                f"| {_cell(o.location)} · {o.mode} | {o.salary} | {o.last_seen} | {_cell('; '.join(notes))} | {link} |"
            )
        out.append("")
    if not shown:
        out += ["_Nessuna offerta da mostrare._", ""]
    return "\n".join(out)


def render_html(d: ReportData) -> str:
    e = htmllib.escape
    parts = [
        "<!doctype html><html lang='it'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Report offerte</title><style>"
        ":root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--line:#d0d7de;--acc:#0969da;--head:#f6f8fa}"
        "@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;"
        "--line:#30363d;--acc:#58a6ff;--head:#161b22}}"
        "body{font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);"
        "max-width:1100px;margin:0 auto;padding:16px}"
        "table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}"
        "th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}"
        "th{background:var(--head)}a{color:var(--acc)}.mut{color:var(--mut)}"
        ".sc{font-weight:600;text-align:center}</style></head><body>",
        f"<h1>Report offerte di lavoro</h1><p class='mut'>Generato il {e(d.generated)} · periodo {e(d.period)}</p>",
        "<h2>Riepilogo</h2><ul>",
        f"<li>Mail analizzate: <b>{d.emails}</b> ({d.sightings} offerte viste, <b>{d.unique_jobs}</b> uniche)</li>",
        f"<li>In linea: <b>{len(d.sections.get('match', []))}</b> · forse: <b>{len(d.sections.get('forse', []))}</b>"
        f" · da verificare (vecchie): <b>{len(d.sections.get('review', []))}</b>"
        f" · da valutare: <b>{len(d.sections.get('da_valutare', []))}</b></li>",
        f"<li>Escluse (non nel report): <b>{d.excluded}</b> su {d.unique_jobs}<ul>",
        *[f"<li>{n} × {e(r)}</li>" for r, n in d.excluded_reasons.most_common()],
        "</ul></li>",
        *([f"<li>Probabilmente scadute (non riviste da {d.stale_days} giorni, non nel report): "
           f"<b>{d.stale}</b></li>"] if d.stale else []),
        "</ul>",
    ]
    for name in ("match", "forse", "review", "da_valutare"):
        offers = d.sections.get(name, [])
        if not offers:
            continue
        parts += [f"<h2>{e(TITLES[name])} ({len(offers)})</h2><p class='mut'>{e(BLURB[name])}</p>",
                  "<table><tr><th>Punti</th><th>Ruolo</th><th>Azienda</th><th>Sede</th><th>RAL</th>"
                  "<th>Vista</th><th>Note</th><th>Link</th></tr>"]
        for o in offers:
            notes = list(o.flags) + (["⚠ " + "; ".join(o.doubts)] if o.doubts else [])
            if o.reason:
                notes.append(o.reason)
            if o.copies > 1:
                notes.append(f"{o.copies} annunci simili")
            link = f"<a href='{e(o.url, quote=True)}' rel='noopener'>apri</a>" if o.url.startswith("http") else ""
            parts.append(
                f"<tr><td class='sc'>{o.score if o.score is not None else '–'}</td><td>{e(o.title)}</td>"
                f"<td>{e(o.company)}</td><td>{e(o.location)} · {e(o.mode)}</td><td>{e(o.salary)}</td>"
                f"<td>{e(o.last_seen)}</td><td>{e('; '.join(notes))}</td><td>{link}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def write_reports(
    conn: sqlite3.Connection, out_dir: Path, *, days: int | None = None, new_only: bool = False,
    fmt: str = "both", name: str | None = None, stale_days: int | None = None,
    review_days: int | None = None,
) -> list[Path]:
    """Write the report in out_dir and return the files created."""
    data = collect(conn, days, new_only=new_only, stale_days=stale_days, review_days=review_days)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = name or datetime.now().strftime("%Y-%m-%d")
    written = []
    if fmt in ("md", "both"):
        written.append(out_dir / f"{stem}.md")
        written[-1].write_text(render_markdown(data), encoding="utf-8")
    if fmt in ("html", "both"):
        written.append(out_dir / f"{stem}.html")
        written[-1].write_text(render_html(data), encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Generate the offers report.")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--days", type=int, help="only offers seen in the last N days")
    parser.add_argument("--new", action="store_true",
                        help="with --days: only offers that appeared for the first time in that period")
    parser.add_argument("--name", help="file name (default: today's date)")
    parser.add_argument("--include-stale", action="store_true",
                        help="also include offers that are probably expired")
    parser.add_argument("--format", choices=["md", "html", "both"], default="both")
    parser.add_argument("--out-dir", type=Path, default=root / "reports")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    conn = connect(cfg.db_file)
    if not conn.execute("SELECT 1 FROM evaluations LIMIT 1").fetchone():
        print("Nessuna valutazione: esegui prima `python -m caw.score`.")
        return 1
    for p in write_reports(conn, args.out_dir, days=args.days, new_only=args.new,
                           fmt=args.format, name=args.name,
                           stale_days=None if args.include_stale else cfg.stale_days,
                           review_days=cfg.stale_review_days):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

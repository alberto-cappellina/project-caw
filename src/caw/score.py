"""Phase 4 - Deterministic filters + LLM scoring against the profile.

    python -m caw.score --no-llm     # filters only (free), show what would be excluded
    python -m caw.score              # filters + LLM scoring of the remaining offers
    python -m caw.score --audit      # list exclusions and places, to check the rules
    python -m caw.score --rescore    # score again even offers already scored

The scoring is cached: each offer is scored only once per profile/prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .db import connect
from .fetch import Config, load_env
from .llm import LLMError, extract_json_array, make_llm
from .rules import Rules, load_profile

PROMPT_VERSION = "1"

SYSTEM = """Sei un recruiter tecnico che valuta annunci di lavoro per UNA persona, descritta nel profilo.
Per ogni annuncio assegni un punteggio 0-100 di compatibilita' con il profilo.
Criteri, in ordine di peso: (1) coerenza del ruolo con i ruoli di interesse e con la seniority;
(2) competenze/stack (Android, Flutter, mobile security, architettura, AI engineering);
(3) tipo di azienda e settore preferiti; (4) componente tecnica del ruolo (non solo manageriale);
(5) priorita' del profilo (flessibilita', stabilita', crescita).
Regole:
- Hai SOLO titolo, azienda, sede, modalita' e RAL: non inventare dettagli che non vedi. Se il titolo e'
  ambiguo, scrivilo nel motivo e usa un punteggio intermedio.
- Ruoli lontani dal profilo (es. game designer, embedded, DevOps puro, analista funzionale, sales)
  prendono punteggi bassi anche se la sede e la RAL vanno bene.
- NON penalizzare la RAL (e' gia' gestita altrove) e NON penalizzare la sede, tranne se la nota
  "dubbi" lo richiede.
- Settori da escludere (gambling, armi, adulti, crypto/trading speculativo, tabacco, sorveglianza
  invasiva): punteggio 0.
Rispondi SOLO con un array JSON, un oggetto per annuncio, nello stesso ordine:
[{"id": "<id>", "score": <0-100>, "reason": "<massimo 20 parole, in italiano>"}]"""


def load_settings(config_path: Path) -> dict:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    llm = data.get("llm", {})
    return {
        "backend": llm.get("backend", "auto"),
        "cli_model": llm.get("cli_model", "haiku"),
        "api_model": llm.get("api_model", "claude-haiku-5-5"),
        "openrouter_model": llm.get("openrouter_model", "anthropic/claude-haiku-5.5"),
        "batch_size": int(llm.get("batch_size", 10)),
    }


def verdict_for(score: int, profile: dict) -> str:
    if score >= profile["scoring"]["match"]:
        return "match"
    if score >= profile["scoring"]["maybe"]:
        return "forse"
    return "escluso"


def build_prompt(profile: dict, jobs: list[sqlite3.Row], notes: dict[str, list[str]]) -> str:
    lines = [
        "PROFILO",
        profile["summary"].strip(),
        "Ruoli di interesse (in ordine): " + "; ".join(r["title"] for r in profile["roles"]),
        "Settori preferiti: " + ", ".join(profile["companies"]["preferred_sectors"]),
        "Tipi di azienda preferiti: " + ", ".join(profile["companies"]["preferred_types"]),
        "",
        "ANNUNCI",
    ]
    for job in jobs:
        salary = (
            f"{job['salary_min']}-{job['salary_max']} {job['currency']}" if job["salary_max"] else "n/d"
        )
        entry = {
            "id": job["job_id"],
            "titolo": job["title"],
            "azienda": job["company"],
            "sede": job["location"],
            "modalita": job["work_mode"],
            "ral": salary,
            "etichette": json.loads(job["extras"] or "[]"),
        }
        if notes.get(job["job_id"]):
            entry["dubbi"] = notes[job["job_id"]]
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines)


def profile_hash(profile_path: Path) -> str:
    payload = profile_path.read_bytes() + SYSTEM.encode() + PROMPT_VERSION.encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def apply_rules(conn: sqlite3.Connection, rules: Rules) -> Counter:
    """Recompute the filters for all offers (fast); keep the existing LLM scores."""
    counts: Counter = Counter()
    for job in conn.execute("SELECT * FROM jobs").fetchall():
        res = rules.evaluate(job)
        counts[res.status] += 1
        conn.execute(
            """
            INSERT INTO evaluations (job_id, rule_status, rule_reasons, flags)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                rule_status=excluded.rule_status,
                rule_reasons=excluded.rule_reasons, flags=excluded.flags
            """,
            (
                job["job_id"], res.status,
                json.dumps(res.reasons, ensure_ascii=False), json.dumps(res.flags, ensure_ascii=False),
            ),
        )
    conn.commit()
    return counts


def llm_score(
    conn, profile, llm, phash, *, batch_size, limit=None, rescore=False, log=print
) -> dict:
    stats = {"scored": 0, "missing": 0, "batch_errors": 0}
    where = "e.rule_status IN ('ok','verify')"
    if not rescore:
        where += " AND (e.profile_hash IS NOT ? OR e.score IS NULL)"
    rows = conn.execute(
        f"SELECT j.*, e.rule_reasons FROM jobs j JOIN evaluations e USING(job_id) WHERE {where} "
        "ORDER BY j.last_seen DESC, j.job_id",
        () if rescore else (phash,),
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    notes = {
        r["job_id"]: json.loads(r["rule_reasons"] or "[]") for r in rows
    }
    log(f"Offerte da valutare con l'LLM: {len(rows)}")

    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            answer = llm.complete(SYSTEM, build_prompt(profile, batch, notes))
            items = extract_json_array(answer)
        except LLMError as exc:
            stats["batch_errors"] += 1
            log(f"  ! batch {i // batch_size + 1}: {exc}")
            continue
        by_id = {str(it.get("id")): it for it in items if isinstance(it, dict)}
        for job in batch:
            item = by_id.get(job["job_id"])
            try:
                score = max(0, min(100, int(item["score"])))
            except (TypeError, KeyError, ValueError):
                stats["missing"] += 1
                continue
            conn.execute(
                "UPDATE evaluations SET score=?, llm_reason=?, verdict=?, profile_hash=?, "
                "model=?, evaluated_at=? WHERE job_id=?",
                (
                    score, str(item.get("reason", ""))[:300], verdict_for(score, profile),
                    phash, llm.name, now(), job["job_id"],
                ),
            )
            stats["scored"] += 1
        conn.commit()
        log(f"  {min(i + batch_size, len(rows))}/{len(rows)}")
    return stats


def finalize_verdicts(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE evaluations SET verdict='escluso' WHERE rule_status='excluded'")
    conn.execute(
        "UPDATE evaluations SET verdict='da_valutare', score=NULL, llm_reason=NULL "
        "WHERE rule_status!='excluded' AND score IS NULL"
    )
    conn.commit()


def audit(conn: sqlite3.Connection) -> None:
    reasons: Counter = Counter()
    places: dict[str, Counter] = {"sede": Counter()}
    for row in conn.execute(
        "SELECT e.rule_status, e.rule_reasons, j.location FROM evaluations e JOIN jobs j USING(job_id)"
    ):
        for reason in json.loads(row["rule_reasons"] or "[]"):
            key = reason.split(":")[0] if reason.startswith("sede fuori") else reason.split(" (")[0]
            reasons[(row["rule_status"], key)] += 1
        if row["rule_status"] == "excluded" and "sede fuori" in (row["rule_reasons"] or ""):
            places["sede"][row["location"]] += 1
    print("Motivi (stato, motivo): n")
    for (status, reason), n in reasons.most_common():
        print(f"  {n:4d}  {status:9s} {reason}")
    print("\nLuoghi scartati come 'fuori provincia di Verona' (controlla che non ci siano errori):")
    print("  " + ", ".join(f"{p} ({n})" for p, n in sorted(places["sede"].items())))


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Filters and scoring of the offers.")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--profile", type=Path, default=root / "profile.yaml")
    parser.add_argument("--no-llm", action="store_true", help="deterministic filters only")
    parser.add_argument("--limit", type=int, help="maximum number of offers to score with the LLM")
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--backend", choices=["auto", "openrouter", "api", "cli"])
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    settings = load_settings(args.config)
    profile = load_profile(args.profile)
    conn = connect(cfg.db_file)

    counts = apply_rules(conn, Rules(profile))
    print(
        f"Filtri: {counts['ok']} ok | {counts['verify']} da verificare | "
        f"{counts['excluded']} escluse (su {sum(counts.values())} offerte)"
    )
    if args.audit:
        audit(conn)
        return 0

    if not args.no_llm:
        # Create the backend after the filters, so --no-llm and --audit work even without access.
        load_env(root / ".env")
        llm = make_llm(
            args.backend or settings["backend"], settings["cli_model"],
            settings["api_model"], settings["openrouter_model"],
        )
        print(f"LLM: {llm.name}")
        stats = llm_score(
            conn, profile, llm, profile_hash(args.profile),
            batch_size=settings["batch_size"], limit=args.limit, rescore=args.rescore,
        )
        print(f"Valutate {stats['scored']} | senza risposta {stats['missing']} | batch falliti {stats['batch_errors']}")
        if stats["batch_errors"] or stats["missing"]:
            print("Rilancia il comando: le offerte non valutate vengono riprese.")
    finalize_verdicts(conn)
    for verdict, n in conn.execute("SELECT verdict, COUNT(*) FROM evaluations GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {verdict}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Full pipeline: download -> archive/parsing -> mark emails -> filters and scoring -> report.

    python -m caw.run                 # weekly run: report of the news of the last 7 days
    python -m caw.run --days 30       # news of the last month
    python -m caw.run --all           # report on all offers still around
    python -m caw.run --no-fetch      # skip Gmail (only process the local archive)
    python -m caw.run --notify        # macOS notification at the end

Every phase is isolated: if Gmail does not answer or the LLM fails, the next phases still
run with the data available and the exit code is 1, so the scheduler reports the problem.
A lock prevents two runs at the same time.
"""

from __future__ import annotations

import argparse
import fcntl
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import connect
from .fetch import Config, State, connect as imap_connect, fetch_new, load_env
from .label import mark_processed
from .llm import LLMError, make_llm
from .process import process
from .report import collect, write_reports
from .rules import Rules, load_profile
from .score import apply_rules, finalize_verdicts, llm_score, load_settings, profile_hash


@dataclass
class RunSummary:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    new_emails: int = 0
    scored: int = 0
    match: int = 0
    maybe: int = 0
    reports: list[Path] = field(default_factory=list)

    def fail(self, step: str, exc: BaseException, log) -> None:
        self.ok = False
        self.errors.append(f"{step}: {exc}")
        log(f"! {step} fallito: {exc}")


def run_pipeline(
    config_path: Path, profile_path: Path, *, report_days: int | None, new_only: bool = True,
    do_fetch: bool = True, imap_factory=None, llm_factory=None, log=print,
) -> RunSummary:
    summary = RunSummary()
    cfg = Config.load(config_path)
    settings = load_settings(config_path)
    profile = load_profile(profile_path)
    conn = connect(cfg.db_file)

    # 1. Download
    if do_fetch:
        log("== 1/5 Download mail")
        imap = None
        try:
            imap = (imap_factory or imap_connect)(cfg)
            state = State.load(cfg.state_file)
            res = fetch_new(imap, cfg, state, log=log)
            summary.new_emails = res.saved
            log(f"   salvate {res.saved}, duplicate {res.duplicates}, errori {res.errors}")
            if res.errors:
                summary.fail("download", RuntimeError(f"{res.errors} mail non scaricate"), log)
        except (Exception, SystemExit) as exc:  # connect() uses sys.exit for login/credentials errors
            summary.fail("download", exc, log)
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    # 2. Archive and parsing
    log("== 2/5 Archivio e parsing")
    try:
        stats = process(conn, cfg.raw_dir, log=log)
        log(f"   indicizzate {stats['indexed']}, elaborate {stats['parsed']}, "
            f"non supportate {stats['unsupported']}, errori {stats['error']}")
        if stats["error"]:
            summary.fail("parsing", RuntimeError(f"{stats['error']} mail con errore"), log)
    except Exception as exc:
        summary.fail("parsing", exc, log)

    # 3. Mark processed emails in Gmail (read + label). Before the scoring: "processed" means
    # the parsing worked, and an unreachable LLM must not leave them unread.
    if do_fetch and (cfg.mark_read or cfg.processed_label):
        log("== 3/5 Segno le mail elaborate in Gmail")
        imap = None
        try:
            imap = (imap_factory or imap_connect)(cfg)
            lab = mark_processed(imap, conn, cfg, log=log)
            log(f"   segnate {lab.marked}, non trovate {lab.not_found}, errori {lab.errors}")
            if lab.errors:
                summary.fail("etichette Gmail", RuntimeError(f"{lab.errors} mail non segnate"), log)
        except (Exception, SystemExit) as exc:
            summary.fail("etichette Gmail", exc, log)
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    pass

    # 4. Filters and scoring
    log("== 4/5 Filtri e scoring")
    try:
        apply_rules(conn, Rules(profile))
        try:
            llm = (llm_factory or (lambda: make_llm(
                settings["backend"], settings["cli_model"], settings["api_model"],
                settings["openrouter_model"])))()
            s = llm_score(conn, profile, llm, profile_hash(profile_path),
                          batch_size=settings["batch_size"], log=log)
            summary.scored = s["scored"]
            if s["batch_errors"] or s["missing"]:
                summary.fail("scoring", RuntimeError(
                    f"{s['batch_errors']} batch falliti, {s['missing']} senza risposta "
                    "(verranno ripresi alla prossima esecuzione)"), log)
        except LLMError as exc:
            summary.fail("scoring", exc, log)
        finalize_verdicts(conn)
    except Exception as exc:
        summary.fail("filtri", exc, log)

    # 5. Report
    log("== 5/5 Report")
    try:
        summary.reports = write_reports(
            conn, cfg.reports_dir, days=report_days, new_only=new_only, stale_days=cfg.stale_days,
            review_days=cfg.stale_review_days)
        data = collect(conn, report_days, new_only=new_only, stale_days=cfg.stale_days,
                       review_days=cfg.stale_review_days)
        summary.match = len(data.sections.get("match", []))
        summary.maybe = len(data.sections.get("forse", []))
        for p in summary.reports:
            log(f"   {p}")
    except Exception as exc:
        summary.fail("report", exc, log)
    return summary


def notify(summary: RunSummary) -> None:
    """macOS notification with counts only (no personal data)."""
    if sys.platform != "darwin":
        return
    if summary.ok:
        text = f"{summary.match} in linea, {summary.maybe} forse ({summary.new_emails} nuove mail)"
    else:
        text = f"Completato con errori: {summary.errors[0][:80]}"
    text = re.sub(r"[^\w\s.,:()/+-]", "", text)  # no quotes or backslashes in the AppleScript command
    script = f'display notification "{text}" with title "Report offerte di lavoro"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run the whole pipeline.")
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--profile", type=Path, default=root / "profile.yaml")
    parser.add_argument("--days", type=int, default=7, help="report window in days (default 7)")
    parser.add_argument("--all", action="store_true", help="report on all offers still around")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args(argv)

    load_env(root / ".env")
    cfg = Config.load(args.config)
    cfg.db_file.parent.mkdir(parents=True, exist_ok=True)
    lock = (cfg.db_file.parent / "run.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Un'altra esecuzione e' in corso: esco.")
        return 2

    def log(msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    log(f"Avvio pipeline ({datetime.now():%Y-%m-%d})")
    try:
        summary = run_pipeline(
            args.config, args.profile, report_days=None if args.all else args.days,
            new_only=not args.all, do_fetch=not args.no_fetch, log=log,
        )
    except Exception:
        traceback.print_exc()
        return 1
    log("Fine: " + ("ok" if summary.ok else "con errori: " + " | ".join(summary.errors)))
    if args.notify:
        notify(summary)
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

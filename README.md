# caw - job-alert email pipeline

caw reads the job-alert emails in your Gmail account, extracts every offer, drops the ones
that do not fit your profile, and writes a short report with the best matches.

It is built for people who receive many alert emails (JobLeads, Indeed, LinkedIn, Talent.com) and do not
want to read them one by one. Everything runs on your own computer. The only outside service
is an optional LLM that scores the offers.

```
Gmail  ->  download  ->  archive (.eml + SQLite)  ->  parse offers
       ->  filters + LLM score  ->  report (Markdown + HTML)  ->  mark emails in Gmail
```

## What it does

1. **Downloads** the alert emails you select with a Gmail label or sender (read-only IMAP).
2. **Archives** each email as an untouched `.eml` file and indexes it in SQLite.
3. **Parses** each digest into single offers: title, company, place, work mode, salary, link.
4. **Filters** offers with simple rules (salary, place, seniority, excluded sectors).
5. **Scores** the remaining offers from 0 to 100 against your profile with an LLM.
6. **Reports** the matches in a Markdown and an HTML file, best scores first.
7. **Marks** the processed emails in Gmail (read + a label), so you can see what is done.

It can run twice a week on its own (macOS `launchd` example included).

## Before you start: what to know

- **Gmail only.** It uses two Gmail IMAP extensions (`X-GM-RAW` search and `X-GM-LABELS`).
- **Italian by default.** The report text, the log messages and the LLM prompt are in Italian.
  The default place filter covers the province of Verona (Italy). Both are easy to change
  (see [Limits](#limits-and-things-to-adapt)).
- **Tested on** macOS and Python 3.13. The scheduling files are macOS only. The rest should
  run on any system with Python 3.11 or newer, but this is not tested.
- **The LLM sees little.** It gets the title, company, place, work mode and salary of each
  offer, plus a short profile summary. It never sees email text, links, or your salary.

## Quick start

You need Python 3.11+, a Gmail account with IMAP enabled, and (optional) an LLM key.

```bash
git clone https://github.com/<you>/project-caw.git
cd project-caw
python3 -m venv .venv
.venv/bin/pip install pyyaml pytest
```

**1. Credentials.** Copy `.env.example` to `.env` and fill it in.
Gmail needs an [app password](https://myaccount.google.com/apppasswords) (2-step
verification must be on). For scoring, add an [OpenRouter](https://openrouter.ai/keys) key
(`OPENROUTER_API_KEY`), or an Anthropic key, or log in to the `claude` CLI.

**2. Profile.** Copy `profile.example.yaml` to `profile.yaml` and edit it: roles, salary
limits, home area, sectors to avoid. `profile.yaml` is git-ignored because it is personal.

**3. Pick the emails.** In Gmail, create a label (the default is `caw`) and a filter that
applies it to your alert senders. Or change `gmail_query` in `config.toml`.

**4. Check the folder name.** It depends on your account language:

```bash
PYTHONPATH=src .venv/bin/python -m caw.fetch --list-folders
```

Put the name of the "All Mail" folder in `config.toml` (`[imap] folder`).

**5. Try it without changing anything.**

```bash
PYTHONPATH=src .venv/bin/python -m caw.fetch --dry-run   # how many emails would be downloaded
```

**6. Run the whole pipeline.**

```bash
PYTHONPATH=src .venv/bin/python -m caw.run
```

The report is written to `reports/YYYY-MM-DD.html` and `.md`.

## Commands

Each phase is also a command. Prefix them with `PYTHONPATH=src .venv/bin/python -m`.

| Command | What it does |
|---|---|
| `caw.run` | Runs everything. Report shows only offers that are new in the last 7 days. |
| `caw.run --all` | Same, but the report lists all offers that are still open. |
| `caw.run --no-fetch` | Skips Gmail and works on the local archive only. |
| `caw.fetch` | Downloads new emails. Add `--dry-run` to only count. |
| `caw.process` | Indexes the archive and parses new emails. `--reparse` redoes everything. |
| `caw.score --no-llm` | Runs only the free filters. `--audit` lists what was dropped and why. |
| `caw.score` | Filters, then scores the remaining offers with the LLM. |
| `caw.label --dry-run` | Counts the emails that would be marked in Gmail. |
| `caw.report` | Writes the report. `--days 7 --new` for news only, `--include-stale` for old offers. |

## How it works

**Download (`fetch.py`).** Opens the folder read-only and reads each email with `BODY.PEEK[]`,
so nothing changes in the mailbox. It saves the raw email as
`archive/raw/YYYY/MM/<date>-<hash>.eml`. The file name comes from the Message-ID, so a second
run never creates duplicates. It remembers the UIDs it has seen, so emails that get the label
later are still found.

**Parse (`parsers/`).** One parser per sender. Each one turns a digest email into single
offers. Emails from a sender without a parser are marked `unsupported` and tried again on
every run, so a new parser picks them up. If a parser finds fewer offers than the email
announces, it writes a note in the database.

**Filters (`rules.py`).** Rules read `profile.yaml`:
- salary: drop below a minimum, or keep with the label `ral inferiore` below a second limit;
- place: remote is always allowed; on-site or hybrid must be in the home province;
- drop junior roles and excluded sectors;
- vague places (for example "Veneto") are kept and marked "to verify".

**Score (`score.py`, `llm.py`).** The LLM gives each remaining offer a score and a short reason.
Scores are cached, so each offer is scored once per profile version. From 70 it is a `match`,
from 40 a `forse` (maybe). Backends: OpenRouter, the Anthropic API, or the `claude` CLI.

**Report (`report.py`).** Offers dropped by the filters or the score are not listed, only
counted with their reasons. By default, offers not seen in any email for 30 days are probably closed, so
they are left out. A strong match that is only a little old goes in a separate section
"to verify", with its last-seen date.

**Mark in Gmail (`label.py`).** After an email is parsed, it gets the "read" flag and the
label `caw-processed`. Emails that fail to parse stay unread, so you notice them.

## Data and privacy

| Where | What |
|---|---|
| On your computer | `archive/` (raw emails), `data/caw.db` (offers and scores), `reports/`, `logs/`. All are git-ignored. |
| Sent to the LLM | For each offer: title, company, place, work mode, salary range, labels. Plus the profile `summary`, roles, sectors and company types. |
| Never sent | Email text, links, your salary, your address. (The profile `summary` is free text: do not put personal data in it.) |
| Changed in Gmail | Only two things, after parsing: the "read" flag and the `caw-processed` label. No moves, no deletes. |

Links in the report open the public page of each offer. Tracking parameters from the emails
are removed, so a report does not contain your personal tracking ids. The one exception is a
sponsored Indeed ad: it has no public id, so its original tracking link is kept.

To undo the Gmail marks: search `label:caw-processed`, select all, "Mark as unread", and
delete the label if you want. You can switch the marks off with `mark_read = false` and
`processed_label = ""` in `config.toml`.

## Configuration

- `config.toml` - folder, Gmail query, date limit (`ignore_before`), LLM backend, report
  thresholds. Every key has a comment.
- `profile.yaml` - your profile (see `profile.example.yaml`).
- `.env` - passwords and keys. Never commit it.

## Schedule (macOS)

```bash
automation/install.sh render      # show the LaunchAgent file for this machine
automation/install.sh install     # install it (default: Tuesday and Friday 13:00)
automation/install.sh run-now     # start a test run
automation/install.sh uninstall   # remove it
```

The committed file is a template: it has no absolute paths. `install.sh` fills them in on
your machine. To change the day or time, edit `automation/local.caw.weekly.plist.template`.
Output goes to `logs/run.log`. If a step fails, the run exits with code 1 and shows a
notification; the other steps still run. On Linux you can use cron with the same command.

## Add a new email source

1. Download a few sample emails (label them, run `caw.fetch`).
2. Add `src/caw/parsers/<source>.py` with a `parse(msg)` function that returns a
   `ParseResult` with a list of `Job` objects. Use `parsers/indeed.py` as a model.
3. Register the sender in `src/caw/parsers/__init__.py`.
4. Add a test with a small fake email. Then run `caw.process`: emails that were
   `unsupported` are tried again automatically.

## Limits and things to adapt

- **Language.** Report headings, log lines and the scoring prompt are Italian. They are plain
  strings in `report.py`, `run.py` and `score.py` (`SYSTEM`).
- **Place rules.** The home province is a list of municipalities in `profile.yaml`
  (`verona_province_places`). Replace it with your own area.
- **Parser formats.** Parsers match the real wording of the emails (for example the Italian
  JobLeads and Indeed alerts and the English LinkedIn notifications). If a sender changes its
  layout, the parser must be updated.
- **Talent.com.** The alert shows the place of the search, not always the place of the job, so
  remote or national offers also show your city. Only a title with "remote" sets the work mode.
  The salary format was not seen in a real email yet: a line that is not understood is kept as
  a label and written in the email notes.
- **Little context.** Alert emails have no job description, so scores come from the title,
  company, place and salary only. Titles like "Software Engineer" often end as "maybe".
- **Closed offers.** Alerts are not removed when an offer closes. Old offers are hidden by
  `stale_days`, but a recent one can still be closed.
- **Job sites block scripts.** The project never opens the job pages. It only links them.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests use fake emails and a fake IMAP server and LLM. They need no network, no account and
no `profile.yaml` (they use `profile.example.yaml`).

## Project layout

```
src/caw/            pipeline code (fetch, process, score, report, label, run)
src/caw/parsers/    one parser per email source
tests/              unit tests with fake emails, IMAP and LLM
automation/         macOS weekly schedule (template + install script)
config.toml         settings           profile.example.yaml   profile template
.env.example        credentials template
```

## License

MIT. See [LICENSE](LICENSE).

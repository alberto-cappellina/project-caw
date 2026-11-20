# project-caw – pipeline mail di annunci di lavoro

Stato: **Fase 1 (download)** pronta. Le fasi 2–5 (archivio indicizzato, estrazione, scoring, report) seguono.

## Setup della Fase 1

1. Attiva la verifica in 2 passaggi sul tuo account Google e crea una *password per le app*:
   https://myaccount.google.com/apppasswords
2. Crea `.env` partendo da `.env.example` e inserisci tu stesso utente e password
   (il file è in `.gitignore`; non incollare la password in chat).
3. Controlla in Gmail che l'accesso IMAP sia attivo (Impostazioni → Inoltro e POP/IMAP).
4. Trova il nome giusto della cartella e correggi `imap.folder` in `config.toml` se serve:

```bash
PYTHONPATH=src .venv/bin/python -m caw.fetch --list-folders
```

5. Adatta `fetch.gmail_query` in `config.toml` ai mittenti da cui ricevi gli annunci.

## Uso

```bash
PYTHONPATH=src .venv/bin/python -m caw.fetch --dry-run          # quante mail verrebbero scaricate
PYTHONPATH=src .venv/bin/python -m caw.fetch --limit 20         # prova su 20 mail
PYTHONPATH=src .venv/bin/python -m caw.fetch                    # sync completo/incrementale
PYTHONPATH=src .venv/bin/python -m caw.fetch --since 2026-01-01 # backfill da una data
```

## Fasi 2-3: indice SQLite ed estrazione delle offerte

```bash
PYTHONPATH=src .venv/bin/python -m caw.process            # elabora le mail nuove
PYTHONPATH=src .venv/bin/python -m caw.process --reparse  # rielabora tutto
```

- Indicizza `archive/raw` in `data/caw.db` (tabelle `emails`, `jobs`, `sightings`).
- Il parser JobLeads (`src/caw/parsers/jobleads.py`) estrae da ogni digest titolo, azienda,
  sede, modalità (`in_person`/`hybrid`/`remote`), RAL min/max, contratto e link,
  senza usare LLM. L'offerta è identificata dall'id JobLeads, quindi le ripetizioni
  tra mail diverse aumentano `times_seen` invece di creare duplicati.
- Parser disponibili: JobLeads e Indeed (`src/caw/parsers/`). Le mail di altri mittenti (es. LinkedIn)
  restano `unsupported` e vengono riprovate a ogni esecuzione, quindi basta aggiungere un parser.
- Il profilo strutturato per i filtri è in `profile.yaml`.

## Fase 4: filtri e scoring

```bash
PYTHONPATH=src .venv/bin/python -m caw.score --no-llm   # solo filtri deterministici (gratis)
PYTHONPATH=src .venv/bin/python -m caw.score --audit    # motivi di esclusione e luoghi scartati
PYTHONPATH=src .venv/bin/python -m caw.score            # filtri + scoring LLM
```

- **Filtri** (`src/caw/rules.py`, parametri in `profile.yaml`): RAL massima < 30.000 esclusa,
  < 48.000 con etichetta "ral inferiore"; sede fuori provincia di Verona esclusa (remoto sempre
  ammesso); ruoli junior e settori esclusi scartati; sedi generiche o ambigue "da verificare".
- **Scoring LLM** (`src/caw/score.py`): punteggio 0-100 con motivazione per le offerte rimaste;
  >= 70 `match`, >= 40 `forse`, altrimenti `escluso`. Risultati in cache in `evaluations`.
  All'LLM vanno solo titolo, azienda, sede, modalita' e RAL: mai testo delle mail o link.
- **Backend**: OpenRouter (`OPENROUTER_API_KEY`), API Anthropic (`ANTHROPIC_API_KEY`) o CLI `claude -p`,
  da scegliere in `[llm]` di `config.toml` (con `auto` vince la prima chiave trovata nel `.env`;
  l'API Anthropic richiede anche `pip install anthropic`).

## Fase 5: report

```bash
PYTHONPATH=src .venv/bin/python -m caw.report             # reports/AAAA-MM-GG.md e .html
PYTHONPATH=src .venv/bin/python -m caw.report --days 7    # solo offerte viste nell'ultima settimana
```

- Sezioni: **In linea** (punteggio >= 70), **Forse** (40-69), **Non ancora valutate** (senza
  punteggio LLM). Le offerte escluse non compaiono: solo conteggio e motivi nel riepilogo.
- Le offerte con stessa azienda/titolo/sede ma id diverso sono unite ("N annunci simili").
- I link sono quelli JobLeads delle mail e contengono un identificativo personale: non
  condividere i report.

## Fase 6: automazione settimanale

```bash
PYTHONPATH=src .venv/bin/python -m caw.run              # download, parsing, scoring, report delle novita' (7 giorni)
PYTHONPATH=src .venv/bin/python -m caw.run --all        # report su tutte le offerte in circolazione
PYTHONPATH=src .venv/bin/python -m caw.run --no-fetch   # senza Gmail
```

- Ogni fase e' isolata: se Gmail o l'LLM falliscono le altre girano comunque, il codice di uscita
  e' 1 e l'errore finisce in `logs/run.log`. Le offerte non valutate vengono riprese alla volta dopo.
- Il report settimanale (`reports/AAAA-MM-GG.md/.html`) contiene solo le offerte comparse per la
  prima volta negli ultimi 7 giorni, senza ripetere quelle gia' viste. Quello completo si ottiene
  con `python -m caw.report --name tutte`.
- Schedulazione su macOS con un LaunchAgent (ogni martedi' alle 13:00, modificabile in
  `automation/local.caw.weekly.plist`):

```bash
automation/install.sh            # installa
automation/install.sh run-now    # prova subito
automation/install.sh uninstall  # rimuove
```

## Garanzie

- **Download in sola lettura**: cartella aperta con EXAMINE e mail lette con `BODY.PEEK[]`.
- **Unica modifica alla casella**: dopo l'elaborazione (`python -m caw.label`, eseguito anche da
  `caw.run`) le mail lette con successo ricevono il flag "letto" e l'etichetta `caw-processed`.
  Nessuno spostamento, nessuna cancellazione, nessuna etichetta rimossa. Le mail che il parser non
  riesce a leggere restano non lette e senza etichetta. Si spegne con `mark_read = false` e
  `processed_label = ""` in `config.toml`; `python -m caw.label --dry-run` conta senza modificare.
  Per annullare: in Gmail cerca `label:caw-processed`, seleziona tutto, "Segna come non letto"
  ed eventualmente elimina l'etichetta.
- **Idempotente**: il nome file deriva dal `Message-ID`; rieseguire non crea duplicati.
- **Incrementale**: `data/fetch_state.json` conserva UIDVALIDITY e ultimo UID scaricato,
  aggiornato dopo ogni batch riuscito.
- **Output**: `archive/raw/AAAA/MM/AAAAMMGG-<hash>.eml` (permessi 600).

## Test

```bash
.venv/bin/python -m pytest
```
# project-caw

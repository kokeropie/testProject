# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Two independent pipelines in one repo, sharing nothing but Python + this checkout:

1. **KEDP ingest/compile** (`consumer.py`, `compiler.py`, `utils.py`) — Kafka JSON messages -> daily `Compiled_Report_YYYY-MM-DD.xlsx`. Documented in `README.md`.
2. **Transform stage + Streamlit app** (`pipeline.py`, `rules_engine.py`, `rule_importer.py`, `app.py`, `scheduler.py`, `path_picker.py`) — takes a raw export (originally a manual, undocumented Qlik process) and derives 10 business columns via a first-match-wins rule engine, splits active/void, and produces 4 report-ready workbooks. Has a Streamlit UI for editing the rules, running the transform, and scheduling unattended runs via Windows Task Scheduler.

**`spec.txt` is the current, authoritative spec for the transform stage + app.** `prd.txt` (original PRD) and `process.txt` (log of one reference run) predate the rule-editor GUI and the Schedule feature — treat them as historical design notes where they disagree with `spec.txt`.

## Stack

| Layer | Library |
| --- | --- |
| Web UI | Streamlit (`app.py`) |
| Data transform | pandas |
| Excel I/O | openpyxl (`.xlsx`), xlrd (`.xls`) |
| Kafka consumer | `confluent-kafka` |
| Scheduling | Windows Task Scheduler (`schtasks`), via a generated `.bat` |

No database layer exists in this repo (no MSSQL/SQLAlchemy) — outputs are always `.xlsx` files.

## Setup

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running

```bash
streamlit run app.py                          # Transform stage UI (rule editor, Run Pipeline, Schedule)
python pipeline.py <input.xlsx> --outdir DIR   # Transform stage, no UI
python consumer.py                             # KEDP: Kafka -> JSON files (always-on)
python compiler.py                             # KEDP: JSON files -> daily compiled report (one-shot)
```

## Architecture

```text
consumer.py / compiler.py / utils.py   KEDP ingest + compile (see README.md)

pipeline.py                            Transform stage: 8-step derivation + CLI entry point,
                                        write_excel_overwrite() (force-overwrite on every write)
rules_engine.py                        Shared rule model (canonical DNF) + evaluator
rule_importer.py                       One-time parser: dataFilter/*.txt -> rules/*.json
                                        (re-running it overwrites rules/*.json, discarding
                                        any edits made through the app — see its docstring)
app.py                                 Streamlit UI: one rule-editor page per derived step,
                                        Run Pipeline, Schedule, Config
scheduler.py                           Schedule config I/O + schtasks/.bat generation
path_picker.py                         Cross-platform (macOS/Windows) file/folder browser
                                        widget used by the Schedule page
build_mgmt_report.py                   One-off script: adds mgmtRpt to an existing output_all.xlsx

dataFilter/*.txt                       Original Qlik-style rule scripts (nested If()/LOAD-WHERE) —
                                        source of the business logic, gitignored
rules/*.json                           Canonical rule storage pipeline.py actually reads at run
                                        time; editable live via app.py's rule-editor pages
rawData/, output/                      Sample input / generated output — gitignored
```

`app.py` never imports pandas directly — it only calls `pipeline.py` and hands DataFrames to Streamlit (`st.dataframe`, `st.download_button`).

## Rule engine

Which `businessUnit`/`productNew`/`Channels`/`Market`/`reportDate`/`mgmtRpt` a row gets is decided by first-match-wins conditions stored in `rules/*.json` (canonical DNF — see `spec.txt` section 4), not hardcoded in Python. Edit rules through the Streamlit rule editor, not by hand-editing `dataFilter/*.txt` or `rules/*.json` directly — `rule_importer.py` is a one-time importer, not something to re-run casually.

## Output files

`output.xlsx` / `output_active.xlsx` / `output_void.xlsx` / `output_all.xlsx` — see `spec.txt` sections 3 and 5 for the full 8-step derivation and reference row/column counts. Every write goes through `pipeline.write_excel_overwrite()`: an existing file at the target path is deleted before writing, so a re-run always fully replaces stale output — never merges, never silently fails on a locked-open file.

## Kafka specifics (KEDP ingest only — unrelated to the transform stage)

- Topic: `update-client` (prod) / `update-client-dev` (dev)
- `consumer.py` commits the Kafka offset only after the JSON file is flushed to disk — no data loss on crash
- `group.id` must stay stable across restarts so Kafka tracks consumer position

## Scheduling

The Schedule page in `app.py` configures a recurring `pipeline.py` run (daily/weekly/monthly/annually, with a start date and an optional end date) and generates `register_scheduled_pipeline_task.bat` — same `schtasks`-based pattern as `register_compiler_task.bat`/`register_consumer_task.bat`. Streamlit has no background job runner, so activating the schedule still requires manually running that `.bat` as Administrator on the Windows host.

## Open questions

See `spec.txt` section 10 (e.g. whether the transform stage should read `compiler.py`'s output directly, and how zero-match rows on no-default rule partitions should be handled).

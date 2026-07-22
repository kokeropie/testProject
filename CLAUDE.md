# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Two independent pipelines in one repo, sharing nothing but Python + this checkout:

1. **KEDP ingest/compile** (`consumer.py`, `compiler.py`, `utils.py`) — Kafka JSON messages -> daily `Compiled_Report_YYYY-MM-DD.xlsx`. Documented in `README.md`.
2. **Transform stage + Streamlit app** (`pipeline.py`, `rules_engine.py`, `rule_importer.py`, `app.py`, `scheduler.py`, `path_picker.py`) — takes a raw export (originally a manual, undocumented Qlik process) and derives 12 business columns (11 via a first-match-wins rule engine, plus `subClass` via a formula, see below; a 13th, `subClass2`, is computed as an intermediate and dropped), splits active/void, and produces 4 report-ready workbooks. Has a Streamlit UI for editing the rules, running the transform, and scheduling unattended runs via Windows Task Scheduler. Optionally loads any of the 4 output workbooks straight into a SQL Server table (`sql_import.py`, `sql_scheduler.py`) — its own step, menu, and schedule, independent of the xlsx outputs. Also optionally pulls raw daily transaction report CSVs from three external partner APIs (`report_fetch.py`, `report_fetch_scheduler.py`) into `rawData/` — its own step, menu, and schedule too, unrelated to the xlsx pipeline or Kafka.

**`spec.txt` is the current, authoritative spec for the transform stage + app.** `prd.txt` (original PRD) and `process.txt` (log of one reference run) predate the rule-editor GUI and the Schedule feature — treat them as historical design notes where they disagree with `spec.txt`.

## Stack

| Layer | Library |
| --- | --- |
| Web UI | Streamlit (`app.py`) |
| Data transform | pandas |
| Excel I/O | openpyxl (`.xlsx`), xlrd (`.xls`) |
| Kafka consumer | `confluent-kafka` |
| SQL Server import | `sqlalchemy` + `pyodbc` (`sql_import.py`) |
| Scheduling | Windows Task Scheduler (`schtasks`), via a generated `.bat` |

Outputs are always `.xlsx` files first; SQL Server is an optional downstream load of an already-written output workbook, not a datastore the pipeline reads from.

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
                                        write_excel_overwrite() (force-overwrite on every write),
                                        verify_outputs() (Step 9) + sanitize_commas() (Step 10)
rules_engine.py                        Shared rule model (canonical DNF) + evaluator
rule_importer.py                       One-time parser: dataFilter/*.txt -> rules/*.json
                                        (re-running it overwrites rules/*.json, discarding
                                        any edits made through the app — see its docstring)
app.py                                 Streamlit UI: one rule-editor page per derived step,
                                        Run Pipeline, Schedule, Config, Import to SQL Server,
                                        Schedule SQL Import, Fetch Daily Reports, Schedule
                                        Daily Reports
scheduler.py                           Schedule config I/O + schtasks/.bat generation for
                                        recurring pipeline.py runs
path_picker.py                         Cross-platform (macOS/Windows) file/folder browser
                                        widget used by the Schedule and SQL pages
sql_import.py                          Loads an output workbook into a SQL Server table
                                        (TRUNCATE+load or append) + CLI entry point; auto-caps
                                        pandas.to_sql's chunksize so wide tables (77-135 cols)
                                        stay under SQL Server's ~2100-param-per-statement limit
sql_scheduler.py                       Schedule config I/O + schtasks/.bat generation for
                                        recurring sql_import.py runs — mirrors scheduler.py,
                                        imports its shared constants rather than duplicating them
report_fetch.py                        Pulls daily transaction report CSVs (flight/train/hotel)
                                        from KATRINA / COBT MT PROD / COBT DANAMON PROD via
                                        HTTP Basic auth (ck/cs) + CLI entry point
report_fetch_scheduler.py              Schedule config I/O + schtasks/.bat generation for
                                        recurring report_fetch.py runs — mirrors scheduler.py,
                                        imports its shared constants rather than duplicating them
build_mgmt_report.py                   One-off script: adds mgmtRpt to an existing output_all.xlsx

dataFilter/*.txt                       Original Qlik-style rule scripts (nested If()/LOAD-WHERE) —
                                        source of the business logic, gitignored
rules/*.json                           Canonical rule storage pipeline.py actually reads at run
                                        time; editable live via app.py's rule-editor pages
rawData/, output/                      Sample input / generated output — gitignored
sql_config.json, sql_schedule_config.json,
register_scheduled_sql_import_task.bat SQL connection settings + SQL import schedule config +
                                        generated task-registration script — all gitignored;
                                        the password itself is never written to any of them
report_fetch_config.json,
report_fetch_schedule_config.json,
register_scheduled_report_fetch_task.bat,
report_fetch_secrets.txt               Per-source ck values + fetch schedule config + generated
                                        task-registration script + optional cs fallback file —
                                        all gitignored
```

`app.py` never imports pandas directly — it only calls `pipeline.py` and hands DataFrames to Streamlit (`st.dataframe`, `st.download_button`).

## Rule engine

Which `businessUnit`/`productNew`/`Channels`/`Market`/`reportDate`/`mgmtRpt`/`subClass2`/`subClass3` a row gets is decided by first-match-wins conditions stored in `rules/*.json` (canonical DNF — see `spec.txt` section 4), not hardcoded in Python. Edit rules through the Streamlit rule editor, not by hand-editing `dataFilter/*.txt` or `rules/*.json` directly — `rule_importer.py` is a one-time importer, not something to re-run casually (it also overwrites every file in its `steps` dict on each run, not just the one you care about — importing a newly-added `dataFilter/*.txt` should generate just that file's `rules/*.json`, e.g. via a one-off script, not a full `rule_importer.run()`).

`subClass2`/`subClass3` (`dataFilter/subClass2.txt`, `subClass3.txt`) are the one dialect-(a) pair whose `If()` conditions can be a compound AND (e.g. `AIRLINE = 'AF' and subClass = 'A'`), not just a single `column = literal` leaf — `rule_importer._parse_nested_if_chain_multi()` handles that by parsing each condition through the same tokenizer used for the LOAD/WHERE dialect (`parse_condition_dnf`), rather than the single-leaf regex the other nested-If files use.

`subClass` is the one derived column that's *not* rule-engine driven — it's a fixed formula on `SUMMARYROUTE` (`derive_sub_class()` in `pipeline.py`), so it has no `rules/*.json` file and no rule-editor page. `subClass2` is computed as an intermediate for `subClass3`'s default (column passthrough, not a literal) and dropped before the output is written — it's never a persisted column.

## Output files

`output.xlsx` / `output_active.xlsx` / `output_void.xlsx` / `output_all.xlsx` — see `spec.txt` sections 3 and 5 for the full 8-step derivation and reference row/column counts. Every write goes through `pipeline.write_excel_overwrite()`: an existing file at the target path is deleted before writing, so a re-run always fully replaces stale output — never merges, never silently fails on a locked-open file. Step 9 (`verify_outputs()`) sanity-checks row/column counts, per-column nulls, and active/void/all consistency, writing `verification_report.txt` alongside the 4 workbooks. Step 10 (`sanitize_commas()`) replaces `,` with ` ` in every text cell of all 4 workbooks.

Any of the 4 output workbooks can optionally be loaded into a SQL Server table afterward — see "SQL Server import" below.

## SQL Server import

`sql_import.py` loads one output `.xlsx` into a `schema.table` in SQL Server, in `replace` mode (`TRUNCATE TABLE` then insert — preserves the table's existing indexes/constraints, unlike pandas' destructive drop-and-recreate) or `append` mode. CLI: `python sql_import.py output/output_all.xlsx`. Streamlit: the "Import to SQL Server" page.

- **Connection settings** live in `sql_config.json` (gitignored) — server, database, driver, auth mode, table, write mode. The password is never written to it.
- **Password**: typed into the Streamlit form (used only for that run), or read from the `MSSQL_PASSWORD` environment variable — the Streamlit page falls back to it automatically whenever the Password field is left blank, and the CLI/scheduled path always reads it via `read_password()`.
- **Table name** is validated against a strict `schema.table` / `[schema].[table]` pattern (`validate_table_name()`) before being interpolated into any SQL statement.
- **Wide tables**: `pandas.to_sql(method="multi")` packs `chunksize` rows into one `INSERT`, so a naive `chunksize=1000` on a 77-135 column output workbook would produce 77,000+ bound parameters — SQL Server's hard limit is ~2100 per statement. `_safe_chunksize()` auto-caps the batch size to whatever fits the table's actual column count, so this can't silently insert 0 rows (the failure mode before this existed: the table's DDL commits fine, then the first batch insert is rejected, so it *looks* like "table created, but no data imported" rather than a clear error).

## Daily report fetch (KATRINA / COBT)

`report_fetch.py` pulls the "daily transaction report" for a given date from three external partner APIs — KATRINA, COBT MT PROD, COBT DANAMON PROD — each exposing `flight`/`train`/`hotel` endpoints that return a zip of CSV(s). CLI: `python report_fetch.py --outdir rawData/katrina_daily_reports`. Streamlit: the "Fetch Daily Reports" page. Unrelated to the KEDP Kafka pipeline or the transform stage — it only writes CSVs into a folder, nothing reads them automatically.

- **Auth** is HTTP Basic (`ck` as username, `cs` as password) — undocumented by the partners, found by probing the live APIs. Requests also need a browser-like `User-Agent`; Cloudflare (fronting all three) 403s urllib's default one outright.
- **Config** (`report_fetch_config.json`, gitignored) holds each source's `ck` only. `cs` is resolved at run time in order: typed into the Streamlit form for that run > a per-source environment variable (`KATRINA_CS` / `COBT_MT_CS` / `COBT_DANAMON_CS`) > a line in `report_fetch_secrets.txt` (gitignored, `<SOURCE>_CS=value` per line, saved from the "Save cs to report_fetch_secrets.txt" button on the Fetch Daily Reports page). The secrets file is a plaintext-on-disk convenience for CLI/scheduled runs so `cs` doesn't have to be retyped or set as a system env var every time — mirrors `sql_import.py`'s `MSSQL_PASSWORD` pattern but with a file-based option added since there's no SQL-auth-style "just use a different mode" escape hatch here.
- **Dates** are computed at run time via `--days-ago N` (default 1 = yesterday), not passed as a fixed date, so a recurring schedule always fetches the right day without editing the command.
- **`cobt.id` (no "www")** 301-redirects to `www.cobt.id` and the redirect drops the `Authorization` header, so `cobt_mt`'s base URL points straight at `www.cobt.id` to skip that hop.
- Each of the 9 source/product endpoints is fetched independently — one failing (bad creds, network blip) doesn't abort the rest of the batch; `fetch_all()` returns a per-endpoint status list instead of raising.
- Uses `certifi`'s CA bundle explicitly rather than trusting the OS/venv Python to have one configured (a python.org macOS install without `Install Certificates.command` run, or a bare Windows install, will otherwise fail every request with `CERTIFICATE_VERIFY_FAILED`).
- **Logging**: every run (CLI or scheduled) logs to console and, by default, to a rotating `report_fetch.log` next to the script (5 MB x 3 backups, gitignored via `*.log`) — `setup_logging()`, `--log-file` to override, empty string for console-only. This exists because a headless `schtasks`-launched run has no console for anything to land in; without a file, a silently-failing scheduled fetch (bad `cs`, network block, moved script) leaves no trace to check afterward. Unhandled exceptions are also logged before re-raising (`if __name__ == "__main__"` wraps `main()`).

## Kafka specifics (KEDP ingest only — unrelated to the transform stage)

- Topic: `update-client` (prod) / `update-client-dev` (dev)
- `consumer.py` commits the Kafka offset only after the JSON file is flushed to disk — no data loss on crash
- `group.id` must stay stable across restarts so Kafka tracks consumer position

## Scheduling

The Schedule page in `app.py` configures a recurring `pipeline.py` run (daily/weekly/monthly/annually, with a start date and an optional end date) and generates `register_scheduled_pipeline_task.bat` — same `schtasks`-based pattern as `register_compiler_task.bat`/`register_consumer_task.bat`. Streamlit has no background job runner, so activating the schedule still requires manually running that `.bat` as Administrator on the Windows host.

The Schedule SQL Import page is the same pattern for `sql_import.py` (task name `KEDP_ScheduledSQLImport`, generates `register_scheduled_sql_import_task.bat`) — a separate config/task from the pipeline's own schedule, so registering or deleting one never touches the other. It refuses to save a schedule that uses SQL Authentication, since a password can't be embedded in a Task Scheduler command line safely: scheduled SQL imports require Windows Authentication, or `MSSQL_PASSWORD` set as a *system* environment variable (`setx MSSQL_PASSWORD ... /M`, as Administrator).

The Schedule Daily Reports page is the same pattern for `report_fetch.py` (task name `KEDP_ScheduledReportFetch`, generates `register_scheduled_report_fetch_task.bat`) — again a separate config/task. Unlike the SQL page it doesn't refuse to save when a source's `cs` isn't resolvable, it only warns: before the task fires, each selected source's `cs` needs to be either saved to `report_fetch_secrets.txt` (Fetch Daily Reports page) or set as a *system* environment variable (`setx <SOURCE>_CS ... /M`, as Administrator) on the machine that runs the schedule, since it can't be embedded in the command line either way.

## Open questions

See `spec.txt` section 10 (e.g. whether the transform stage should read `compiler.py`'s output directly, and how zero-match rows on no-default rule partitions should be handled).

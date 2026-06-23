# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A web-based ETL tool built for personal + work use at a travel company in Jakarta. It reads data from multiple sources (MSSQL, CSV, Excel, Kafka), lets the user transform it, and outputs to CSV download or a database write. Built with Python — no frontend skills required.

The Kafka source is the work-critical path: consuming `update-client` events from iCore Studio's internal event messaging system and loading them to MSSQL.

Full project context is in the Obsidian vault at `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/aiaidede_vault/wiki/entities/my-app.md`.

## Stack

| Layer | Library |
| --- | --- |
| Web UI | Streamlit |
| Data transform | pandas |
| MSSQL connection | SQLAlchemy + pyodbc |
| Excel reading | openpyxl (pandas uses it automatically) |
| Kafka consumer | `confluent-kafka` (preferred) or `kafka-python` |

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
streamlit run app.py
```

## Architecture

```text
app.py                   # Streamlit entry point — UI + routing only
src/
  extract/
    csv_reader.py        # pd.read_csv wrapper
    excel_reader.py      # pd.read_excel wrapper
    mssql_reader.py      # SQLAlchemy connection + query → DataFrame
    kafka_consumer.py    # consume update-client topic → DataFrame
  transform/
    pipeline.py          # filter, select columns, rename — operates on DataFrames
  load/
    csv_writer.py        # st.download_button helper
    db_writer.py         # DataFrame → MSSQL via SQLAlchemy
```

`app.py` never imports pandas directly — it only calls `src/` modules and hands DataFrames to Streamlit (`st.dataframe`, `st.download_button`). Business logic lives in `src/`.

## Kafka specifics

- Topic: `update-client` (prod) / `update-client-dev` (dev)
- Payload is JSON; `updated_date` field is the incremental watermark
- Use `enable.auto.commit=False` — commit offset only after the row is written to MSSQL
- `group.id` must be stable across restarts so Kafka tracks your consumer position

## Build phases

- [ ] Phase 1: CSV → display → download
- [ ] Phase 2: Excel → display → download
- [ ] Phase 3: MSSQL → query → display
- [ ] Phase 4: Write results back to DB
- [ ] Phase 5: Transform UI (filter, select columns, rename)
- [ ] Phase 6: Kafka `update-client` → transform → load to MSSQL

## Open questions (decide before Phase 6)

- Kafka consumer: daemon (always running) vs on-demand trigger from UI?
- Where does the app get hosted — local only, or cloud?
- Which MSSQL table does `update-client` load into?

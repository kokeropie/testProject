"""
Import a transform-stage output workbook (output.xlsx / output_active.xlsx /
output_void.xlsx / output_all.xlsx) into a Microsoft SQL Server table.

New, standalone module — does not modify pipeline.py, app.py's existing
pages, or any other already-committed file's logic. Wired into app.py only
as an additive new sidebar page ("Import to SQL Server") and a new
Schedule-SQL-Import page; see sql_scheduler.py for the recurring-run half.

Config (sql_config.json, gitignored — see .gitignore) holds connection
settings but never a password: the password is supplied at run time, either
via the MSSQL_PASSWORD environment variable (CLI / scheduled runs) or typed
into the Streamlit form (interactive runs) — it is never written to disk.

Usage (CLI):
    python sql_import.py output/output_all.xlsx
    python sql_import.py output/output_all.xlsx --config sql_config.json --if-exists append
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).parent
SQL_CONFIG_PATH = PROJECT_DIR / "sql_config.json"

log = logging.getLogger("sql_import")

# dbo.TableName / [dbo].[Table Name] / TableName — deliberately conservative;
# reject anything else rather than interpolate it into raw SQL.
_VALID_TABLE_RE = re.compile(r"^(\[[^\]\[]+\]|\w+)(\.(\[[^\]\[]+\]|\w+))?$")


def default_sql_config() -> dict:
    return {
        "driver": "ODBC Driver 17 for SQL Server",
        "server": "",
        "database": "",
        "auth": "windows",  # "windows" (trusted connection) or "sql"
        "username": "",
        "table": "dbo.TransformOutput",
        "if_exists": "replace",  # "replace" (truncate then load) or "append"
        "chunksize": 1000,
    }


def load_sql_config() -> dict:
    if not SQL_CONFIG_PATH.exists():
        return default_sql_config()
    return {**default_sql_config(), **json.loads(SQL_CONFIG_PATH.read_text())}


def save_sql_config(config: dict) -> None:
    """Never persists a password — callers must not put one in `config`."""
    safe = {k: v for k, v in config.items() if k != "password"}
    SQL_CONFIG_PATH.write_text(json.dumps(safe, indent=2))


def validate_table_name(table: str) -> None:
    if not _VALID_TABLE_RE.match(table.strip()):
        raise ValueError(
            f"table name {table!r} doesn't look like a plain 'schema.table' or "
            f"'[schema].[table]' identifier — refusing to use it in a SQL statement"
        )


def validate_sql_config(config: dict) -> list[str]:
    """Returns a list of human-readable problems; empty means OK to use."""
    errors = []
    if not config.get("server"):
        errors.append("Server is required.")
    if not config.get("database"):
        errors.append("Database is required.")
    if not config.get("table"):
        errors.append("Table is required.")
    else:
        try:
            validate_table_name(config["table"])
        except ValueError as e:
            errors.append(str(e))
    if config.get("auth") == "sql" and not config.get("username"):
        errors.append("Username is required for SQL authentication.")
    if config.get("if_exists") not in ("replace", "append"):
        errors.append("if_exists must be 'replace' or 'append'.")
    return errors


def build_connection_url(config: dict, password: str | None = None) -> str:
    """sqlalchemy connection URL for the mssql+pyodbc dialect. pyodbc itself
    is only imported lazily by sqlalchemy when an engine is actually created,
    so building this string doesn't require pyodbc to be installed."""
    from urllib.parse import quote_plus

    driver = quote_plus(config["driver"])
    server = config["server"]
    database = config["database"]
    if config.get("auth") == "windows":
        return (
            f"mssql+pyodbc://@{server}/{database}"
            f"?driver={driver}&trusted_connection=yes"
        )
    username = quote_plus(config["username"])
    pwd = quote_plus(password or "")
    return f"mssql+pyodbc://{username}:{pwd}@{server}/{database}?driver={driver}"


def read_password(config: dict, cli_password: str | None = None) -> str | None:
    if config.get("auth") != "sql":
        return None
    if cli_password:
        return cli_password
    return os.environ.get("MSSQL_PASSWORD")


_MSSQL_MAX_PARAMS = 2100  # SQL Server's hard limit on bound parameters per statement
_MSSQL_PARAM_SAFETY_MARGIN = 100  # headroom below the hard limit


def _safe_chunksize(configured: int, n_columns: int) -> int:
    """pandas.to_sql(method="multi") packs `chunksize` rows into a single
    INSERT, so `chunksize x n_columns` bound parameters must stay under
    SQL Server's ~2100-parameter ceiling. A wide table (e.g. this repo's
    77-135 column output workbooks) blows past that with the default
    chunksize=1000 long before row count is the limiting factor — 1000
    rows x 77 columns = 77,000 parameters, so the very first batch insert
    is rejected outright (the table's already-committed DDL is why you'd
    see an empty table rather than an error at first glance). Cap
    chunksize to whatever actually fits this table's width."""
    max_rows = max(1, (_MSSQL_MAX_PARAMS - _MSSQL_PARAM_SAFETY_MARGIN) // max(n_columns, 1))
    return min(configured, max_rows)


def import_xlsx_to_sql(xlsx_path: Path, config: dict, password: str | None = None) -> dict:
    """Reads xlsx_path with pandas and loads it into config['table'].

    if_exists="replace": TRUNCATE TABLE then insert — mirrors
    write_excel_overwrite()'s "re-run always replaces stale output outright"
    semantics, but preserves the table's existing schema/indexes/constraints
    rather than pandas.to_sql's destructive drop-and-recreate.
    if_exists="append": insert on top of whatever's already there.

    Returns a dict of verification info (never raises on the SQL-side count
    check — that's a sanity check, not a code invariant); connection/import
    errors themselves do raise, since a failed import must not look silent.
    """
    from sqlalchemy import create_engine, text

    errors = validate_sql_config(config)
    if errors:
        raise ValueError("invalid SQL config: " + "; ".join(errors))
    table = config["table"].strip()

    df = pd.read_excel(xlsx_path, engine="openpyxl")
    log.info("loaded %s: %d rows x %d cols", Path(xlsx_path).name, *df.shape)

    engine = create_engine(build_connection_url(config, password))
    with engine.begin() as conn:
        if config.get("if_exists", "replace") == "replace":
            has_table = conn.execute(
                text(
                    "SELECT CASE WHEN OBJECT_ID(:table, 'U') IS NOT NULL THEN 1 ELSE 0 END"
                ),
                {"table": table},
            ).scalar()
            if has_table:
                conn.execute(text(f"TRUNCATE TABLE {table}"))
                log.info("truncated existing table %s before load", table)

    configured_chunksize = config.get("chunksize", 1000)
    chunksize = _safe_chunksize(configured_chunksize, len(df.columns))
    if chunksize < configured_chunksize:
        log.info(
            "reduced chunksize from %d to %d row(s)/batch to keep this %d-column "
            "table's INSERT statements under SQL Server's ~%d parameter limit",
            configured_chunksize, chunksize, len(df.columns), _MSSQL_MAX_PARAMS,
        )

    df.to_sql(
        _split_schema_table(table)[1],
        engine,
        schema=_split_schema_table(table)[0],
        if_exists="append",
        index=False,
        chunksize=chunksize,
        method="multi",
    )

    result = {"source_rows": len(df), "table": table}
    try:
        with engine.connect() as conn:
            result["table_row_count"] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    except Exception as e:  # pragma: no cover - best-effort verification only
        result["table_row_count"] = None
        result["count_check_error"] = str(e)
    log.info("import done: %s", result)
    return result


def _split_schema_table(table: str) -> tuple[str | None, str]:
    """'dbo.TransformOutput' -> ('dbo', 'TransformOutput'); 'TransformOutput'
    -> (None, 'TransformOutput'). Strips [brackets] from either part."""
    parts = table.split(".", 1)
    parts = [p.strip().strip("[]") for p in parts]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0]


def verify_import(result: dict) -> list[str]:
    """Same OK/WARN report style as pipeline.py's verify_outputs()."""
    checks = []
    if result.get("table_row_count") is None:
        checks.append(f"WARN could not verify row count in {result['table']}: "
                       f"{result.get('count_check_error', 'unknown error')}")
    elif result["table_row_count"] >= result["source_rows"]:
        checks.append(
            f"OK   {result['table']} has {result['table_row_count']} row(s) "
            f"(>= {result['source_rows']} just loaded)"
        )
    else:
        checks.append(
            f"WARN {result['table']} has {result['table_row_count']} row(s), "
            f"expected at least {result['source_rows']}"
        )
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path, help="output .xlsx file to import (e.g. output/output_all.xlsx)")
    parser.add_argument("--config", type=Path, default=SQL_CONFIG_PATH)
    parser.add_argument("--table", help="override the table from config")
    parser.add_argument("--if-exists", choices=["replace", "append"], help="override if_exists from config")
    parser.add_argument("--password", help="SQL auth password (prefer MSSQL_PASSWORD env var over this)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = {**default_sql_config(), **json.loads(args.config.read_text())} if args.config.exists() else default_sql_config()
    if args.table:
        config["table"] = args.table
    if args.if_exists:
        config["if_exists"] = args.if_exists

    password = read_password(config, args.password)
    result = import_xlsx_to_sql(args.xlsx, config, password)
    for line in verify_import(result):
        (log.warning if line.startswith("WARN") else log.info)(line)


if __name__ == "__main__":
    main()

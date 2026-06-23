import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from utils import ConfigError, check_dir_writable, load_config, setup_logging


def validate_config(cfg: dict) -> None:
    check_dir_writable(
        Path(cfg["paths"]["json_ingestion_dir"]), "JSON ingestion directory"
    )
    check_dir_writable(
        Path(cfg["paths"]["excel_output_dir"]), "Excel output directory"
    )


def ensure_archive_dirs(ingestion_dir: Path) -> tuple[Path, Path]:
    archive_dir = ingestion_dir / "Archive"
    errors_dir = archive_dir / "Errors"
    archive_dir.mkdir(exist_ok=True)
    errors_dir.mkdir(exist_ok=True)
    return archive_dir, errors_dir


def parse_json_files(
    files: list[Path], errors_dir: Path, log
) -> tuple[list[dict], list[Path], list[Path]]:
    rows = []
    good = []
    bad = []

    for f in files:
        try:
            data = json.loads(f.read_bytes())
            if isinstance(data, list):
                rows.extend(data)
            else:
                rows.append(data)
            good.append(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error(f"Skipped {f.name} — {e}")
            dest = errors_dir / f.name
            shutil.move(str(f), dest)
            bad.append(f)

    return rows, good, bad


def build_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df.reindex(sorted(df.columns), axis=1)
    return df


def write_excel(df: pd.DataFrame, output_dir: Path, run_date: str) -> Path:
    filename = f"Compiled_Report_{run_date}.xlsx"
    dest = output_dir / filename
    df.to_excel(dest, index=False, engine="openpyxl")
    return dest


def archive_good_files(files: list[Path], archive_dir: Path) -> None:
    for f in files:
        shutil.move(str(f), archive_dir / f.name)


def run() -> None:
    cfg = load_config()
    validate_config(cfg)
    log = setup_logging(cfg, "COMPILER")

    start = time.monotonic()
    run_date = datetime.now().strftime("%Y-%m-%d")

    ingestion_dir = Path(cfg["paths"]["json_ingestion_dir"])
    output_dir = Path(cfg["paths"]["excel_output_dir"])

    # Snapshot the file list at start — new arrivals during this run are excluded
    json_files = sorted(ingestion_dir.glob("*.json"))

    if not json_files:
        log.warning("No JSON files found in ingestion directory — nothing to compile")
        return

    log.info(f"Started. Found {len(json_files)} JSON file(s).")

    archive_dir, errors_dir = ensure_archive_dirs(ingestion_dir)
    rows, good_files, bad_files = parse_json_files(json_files, errors_dir, log)

    if not rows:
        log.warning("All files were corrupt — no Excel output generated")
        return

    df = build_dataframe(rows)
    output_path = write_excel(df, output_dir, run_date)
    archive_good_files(good_files, archive_dir)

    elapsed = time.monotonic() - start
    log.info(
        f"Done. Files processed: {len(good_files)}, errors: {len(bad_files)}, "
        f"rows: {len(df)}, output: {output_path.name}, elapsed: {elapsed:.1f}s"
    )


if __name__ == "__main__":
    try:
        run()
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}", file=sys.stderr)
        sys.exit(1)

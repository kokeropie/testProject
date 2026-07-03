"""
Adds the mgmtRpt column (rules/mgmtRpt.json, parsed from dataFilter/mgmtRpt.txt
by rule_importer.import_mgmt_rpt) to output/output_all.xlsx and saves the
result as output/output_mgmt.xlsx.

Usage:
    python build_mgmt_report.py
    python build_mgmt_report.py --source output/output_all.xlsx --out output/output_mgmt.xlsx
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from pipeline import (
    _apply_single_output_step,
    build_column_resolver,
    load_config,
    load_step,
    write_excel_overwrite,
)

log = logging.getLogger("build_mgmt_report")


def build_mgmt_report(source: Path, out: Path) -> Path:
    config = load_config()
    df = pd.read_excel(source, engine="openpyxl")
    aliases = build_column_resolver(df, config)
    stats: dict = {}
    df["mgmtRpt"] = _apply_single_output_step(df, load_step("mgmtRpt"), aliases, stats)
    unmatched = stats["mgmtRpt"].get(None, 0)
    if unmatched:
        log.warning("mgmtRpt: %d row(s) matched no rule and were left null", unmatched)
    write_excel_overwrite(df, out)
    log.info("wrote %s: %d rows x %d cols", out, *df.shape)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("output/output_all.xlsx"))
    parser.add_argument("--out", type=Path, default=Path("output/output_mgmt.xlsx"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_mgmt_report(args.source, args.out)


if __name__ == "__main__":
    main()

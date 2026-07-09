"""
Transform pipeline: input (csv/xls/xlsx) -> output.xlsx / output_active.xlsx /
output_void.xlsx / output_all.xlsx, per prd.txt / process.txt.

Steps 1-6 apply the rule-engine JSON in rules/*.json (edited via app.py) to
derive 10 new columns. A formula-based step then derives subClass from
SUMMARYROUTE (not rule-engine driven — see derive_sub_class()), followed by
two more rule-engine steps (rules/subClass2.json, rules/subClass3.json)
that derive subClass3 from subClass + AIRLINE, discarding the intermediate
subClass2 (see derive_sub_class2_and_3()). Step 7 splits active/void and
negates the configured monetary columns for void rows. Step 8 concatenates
the two into a union.

Usage (CLI):
    python pipeline.py rawData/original.xlsx
    python pipeline.py rawData/original.csv --outdir output
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from rules_engine import apply_step, resolve_column, MatchOutcome

RULES_DIR = Path(__file__).parent / "rules"

log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

def load_step(name: str) -> dict:
    return json.loads((RULES_DIR / f"{name}.json").read_text())


def load_config() -> dict:
    return json.loads((RULES_DIR / "config.json").read_text())


def _patch_voided_encoding(step_def: dict, config: dict) -> dict:
    """Rewrite 'Voided = <literal>' leaves to 'Voided <> 0' when the dataset
    encodes voided rows as nonzero (e.g. -1) rather than literal 1.
    See PRD 5.6 / process.txt Step 5."""
    if not config.get("voided_nonzero_encoding"):
        return step_def
    voided_col = config["voided_column"]
    literal = config["voided_literal_in_rules"]

    def patch_leaf(leaf: dict) -> dict:
        if leaf["column"] == voided_col and leaf.get("operator") == "=" and leaf.get("value") == literal:
            leaf = dict(leaf)
            leaf["operator"] = "<>"
            leaf["value"] = 0
        return leaf

    patched = json.loads(json.dumps(step_def))
    for rule in patched.get("rules", []):
        rule["condition"]["any"] = [
            [patch_leaf(leaf) for leaf in clause] for clause in rule["condition"]["any"]
        ]
    return patched


# ---------------------------------------------------------------------------
# Input reading (csv / xls / xlsx — see prd.txt section 4)
# ---------------------------------------------------------------------------

DATE_COLUMNS = ["DateOfCreated", "VoidDate", "DATEOFSERVICESTART", "DATEOFSERVICEEND"]
NUMERIC_COLUMNS_HINT = [
    "ADDFEE", "AGENTCHARGE", "NETFARE", "NIGHTS", "ORGAMT",
    "RealNETFARE", "RealTAX", "SUBTOTAL", "SUBTOTALORG", "TAX", "VAT", "Voided",
]


def _input_suffix(source) -> str:
    """source is a path (str/Path) or a file-like object with a .name
    attribute (e.g. Streamlit's UploadedFile)."""
    if isinstance(source, (str, Path)):
        return Path(source).suffix.lower()
    name = getattr(source, "name", None)
    if not name:
        raise ValueError("cannot determine input format: pass a path or a file-like object with .name")
    return Path(name).suffix.lower()


def drop_incomplete_rows(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Drop rows missing the configured required column (e.g. InvoiceID) —
    some exports (see rawData/Detail.xlsx) pad in fully-blank rows that
    carry no real transaction data. No-op if the column isn't configured
    or every row already has it."""
    required_col = config.get("required_nonnull_column")
    if not required_col or required_col not in df.columns:
        return df
    mask = df[required_col].notna()
    dropped = len(df) - mask.sum()
    if dropped:
        log.info("dropped %d row(s) with no %s (incomplete/blank export rows)", dropped, required_col)
    return df[mask].reset_index(drop=True)


def ensure_voided_column(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Some exports (see rawData/Detail.xlsx) don't carry the Voided flag
    the rule engine expects but do carry VoidDate — derive Voided (1/0)
    from VoidDate presence in that case. No-op if Voided is already there."""
    voided_col = config["voided_column"]
    if voided_col in df.columns:
        return df
    source_col = config.get("voided_fallback_source_column")
    if not source_col or source_col not in df.columns:
        return df
    df = df.copy()
    df[voided_col] = df[source_col].notna().astype(int)
    log.info("derived %s from %s (%d row(s) voided)", voided_col, source_col, df[voided_col].sum())
    return df


def read_input(source) -> pd.DataFrame:
    suffix = _input_suffix(source)
    if suffix == ".csv":
        df = pd.read_csv(source)
        for col in DATE_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        for col in NUMERIC_COLUMNS_HINT:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    elif suffix == ".xls":
        df = pd.read_excel(source, engine="xlrd")
    elif suffix == ".xlsx":
        df = pd.read_excel(source, engine="openpyxl")
    else:
        raise ValueError(f"unsupported input format: {suffix} (expected .csv, .xls, or .xlsx)")
    return df


# ---------------------------------------------------------------------------
# Steps 1-5: derived columns
# ---------------------------------------------------------------------------

def _apply_single_output_step(df: pd.DataFrame, step_def: dict, column_aliases: dict,
                               stats: dict | None = None) -> pd.Series:
    counts: dict[int | None, int] = {}
    values = []
    for _, row in df.iterrows():
        outcome: MatchOutcome = apply_step(step_def, row, column_aliases)
        counts[outcome.rule_id] = counts.get(outcome.rule_id, 0) + 1
        values.append(next(iter(outcome.values.values())))
    log.info("%s: match counts by rule id -> %s", step_def["step"], counts)
    if stats is not None:
        stats[step_def["step"]] = counts
    return pd.Series(values, index=df.index)


def _apply_multi_output_step(df: pd.DataFrame, step_def: dict, column_aliases: dict,
                              stats: dict | None = None) -> pd.DataFrame:
    counts: dict[int | None, int] = {}
    rows = []
    for _, row in df.iterrows():
        outcome = apply_step(step_def, row, column_aliases)
        counts[outcome.rule_id] = counts.get(outcome.rule_id, 0) + 1
        rows.append(outcome.values)
    unmatched = counts.get(None, 0)
    if unmatched and step_def.get("default") is None:
        log.warning("%s: %d row(s) matched no rule (no default defined)", step_def["step"], unmatched)
    log.info("%s: match counts by rule id -> %s", step_def["step"], counts)
    if stats is not None:
        stats[step_def["step"]] = counts
    return pd.DataFrame(rows, index=df.index)


def step1_business_unit(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    df = df.copy()
    df["businessUnit"] = _apply_single_output_step(df, load_step("businessUnit"), column_aliases, stats)
    df["subBusinessUnit"] = _apply_single_output_step(df, load_step("subBusinessUnit"), column_aliases, stats)
    return df


def step2_product(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    df = df.copy()
    product_step = load_step("product")
    for out_col, chain in product_step["chains"].items():
        sub_step = {
            "step": f"product.{out_col}",
            "output_columns": [out_col],
            "default": chain["default"],
            "rules": chain["rules"],
        }
        df[out_col] = _apply_single_output_step(df, sub_step, column_aliases, stats)
    return df


def step3_channel(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    df = df.copy()
    result = _apply_multi_output_step(df, load_step("channel"), column_aliases, stats)
    df[result.columns] = result
    return df


def step4_market(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    """Must run after step2 (needs subProduct) and step3 (needs subChannels)."""
    df = df.copy()
    result = _apply_multi_output_step(df, load_step("market"), column_aliases, stats)
    df[result.columns] = result
    return df


def step5_report_date(df: pd.DataFrame, column_aliases: dict, config: dict,
                       stats: dict | None = None) -> pd.DataFrame:
    """Must run after step2 (needs productNew)."""
    df = df.copy()
    step_def = _patch_voided_encoding(load_step("reportDate"), config)
    result = _apply_multi_output_step(df, step_def, column_aliases, stats)
    unmatched = result["reportDate"].isna().sum()
    if unmatched:
        log.warning("reportDate: %d row(s) matched no rule and were left null", unmatched)
    df["reportDate"] = result["reportDate"]
    return df


def step6_mgmt_rpt(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    """Must run after step1 (needs subBusinessUnit), step2 (needs subProduct),
    and step3 (needs Channels)."""
    df = df.copy()
    df["mgmtRpt"] = _apply_single_output_step(df, load_step("mgmtRpt"), column_aliases, stats)
    return df


def derive_sub_class(df: pd.DataFrame, column_aliases: dict) -> pd.DataFrame:
    """subClass = If(right(SUMMARYROUTE,1) = ')', left(right(SUMMARYROUTE,2),1),
    right(SUMMARYROUTE,1)) — the booking-class letter is the last character of
    SUMMARYROUTE, unless it's wrapped in parens (e.g. 'CGK DPS (I)' or a
    multi-segment route like 'DJB CGK (Y)/CGK SUB (Y)'), in which case it's
    the character just before the closing paren."""
    df = df.copy()
    col = resolve_column("SUMMARYROUTE", column_aliases)
    if col not in df.columns:
        df["subClass"] = None
        return df
    route = df[col].astype("string")
    last = route.str[-1]
    second_last = route.str[-2]
    sub_class = second_last.where(last == ")", last)
    # plain object/None, not pandas' nullable "string" dtype: downstream rule
    # evaluation (rules_engine._cmp_ok) does actual == expected under all(),
    # and pd.NA's __eq__ returns pd.NA rather than False, which bool() can't
    # resolve — None compares the same way regular Python values do.
    df["subClass"] = pd.Series(sub_class.to_numpy(dtype=object, na_value=None), index=df.index)
    return df


def derive_sub_class2_and_3(df: pd.DataFrame, column_aliases: dict, stats: dict | None = None) -> pd.DataFrame:
    """subClass2 (dataFilter/subClass2.txt): First/Business/Economy from
    subClass + AIRLINE. subClass3 (dataFilter/subClass3.txt): per-carrier
    Premium Economy exceptions on subClass + AIRLINE, falling back to the
    just-computed subClass2 value otherwise. Both are rule-engine driven
    (rules/subClass2.json, rules/subClass3.json), unlike subClass itself.
    Only subClass3 is kept in the output — subClass2 is an intermediate."""
    df = df.copy()
    df["subClass2"] = _apply_single_output_step(df, load_step("subClass2"), column_aliases, stats)
    df["subClass3"] = _apply_single_output_step(df, load_step("subClass3"), column_aliases, stats)
    df = df.drop(columns=["subClass2"])
    return df


def build_column_resolver(df: pd.DataFrame, config: dict) -> dict[str, str]:
    """Case-insensitive fallback for every input column (rule text was
    hand-written against a Qlik schema that doesn't always match this data's
    exact casing, e.g. 'clientSegment' vs 'ClientSegment') plus the explicit
    renames from config (e.g. Qlik's 'subChannels2' -> our 'subChannels')."""
    resolver = {c.lower(): c for c in df.columns}
    resolver.update(config.get("column_aliases", {}))
    # Fallback synonyms (e.g. Detail.xlsx has newPCID where dataFilter/
    # product.txt expects PCID) — only kicks in when the canonical column
    # is genuinely absent, so datasets that already have it are untouched.
    for canonical, fallback in config.get("column_fallbacks", {}).items():
        if canonical.lower() not in resolver and fallback.lower() in resolver:
            resolver[canonical.lower()] = resolver[fallback.lower()]
    return resolver


def run_derivation_steps(df: pd.DataFrame, config: dict, stats: dict | None = None) -> pd.DataFrame:
    aliases = build_column_resolver(df, config)
    df = step1_business_unit(df, aliases, stats)
    df = step2_product(df, aliases, stats)
    df = step3_channel(df, aliases, stats)
    df = step4_market(df, aliases, stats)
    df = step5_report_date(df, aliases, config, stats)
    df = step6_mgmt_rpt(df, aliases, stats)
    df = derive_sub_class(df, aliases)
    df = derive_sub_class2_and_3(df, aliases, stats)
    return df


# ---------------------------------------------------------------------------
# Step 7-8: active/void split + union
# ---------------------------------------------------------------------------

def step7_active_void_split(df: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    voided_col = config["voided_column"]
    active = df.copy()
    active["voidStatus"] = config["void_status_active_value"]

    if config.get("voided_nonzero_encoding"):
        void_mask = df[voided_col] != 0
    else:
        void_mask = df[voided_col] == config["voided_literal_in_rules"]

    void = df[void_mask].copy()
    void["voidStatus"] = config["void_status_void_value"]
    for col in config["negation_columns"]:
        if col in void.columns:
            void[col] = void[col] * -1
    log.info("step7: %d active row(s), %d void row(s)", len(active), len(void))
    return active, void


def step8_union(active: pd.DataFrame, void: pd.DataFrame) -> pd.DataFrame:
    if list(active.columns) != list(void.columns):
        raise ValueError("step8: active/void column schemas differ, refusing to concatenate")
    return pd.concat([active, void], ignore_index=True)


# ---------------------------------------------------------------------------
# Step 9 — data verification: sanity-checks on the derived output, run right
# before writing so a bad derivation is caught instead of silently shipped.
# ---------------------------------------------------------------------------

DERIVED_COLUMNS = [
    "businessUnit", "subBusinessUnit", "productNew", "subProduct",
    "Channels", "subChannels", "Market", "subMarket", "reportDate", "mgmtRpt",
    "subClass", "subClass3",
]


def verify_outputs(source_df: pd.DataFrame, output: pd.DataFrame, active: pd.DataFrame,
                    void: pd.DataFrame, all_rows: pd.DataFrame, config: dict) -> list[str]:
    """Returns a list of human-readable check results ('OK ...' / 'WARN ...').
    Never raises — a failed check is surfaced in the report, not a crash,
    since these are sanity checks on business data, not code invariants."""
    checks: list[str] = []

    def ok(msg: str) -> None:
        checks.append(f"OK   {msg}")

    def warn(msg: str) -> None:
        checks.append(f"WARN {msg}")

    expected_cols = len(source_df.columns) + len(DERIVED_COLUMNS)
    if len(output.columns) == expected_cols:
        ok(f"output.xlsx has {len(output.columns)} columns ({len(source_df.columns)} source + {len(DERIVED_COLUMNS)} derived)")
    else:
        warn(f"output.xlsx has {len(output.columns)} columns, expected {expected_cols}")

    if len(output) == len(source_df):
        ok(f"output.xlsx row count matches source ({len(output)} rows)")
    else:
        warn(f"output.xlsx has {len(output)} rows, source has {len(source_df)}")

    for col in DERIVED_COLUMNS:
        if col not in output.columns:
            warn(f"derived column {col!r} missing from output entirely")
            continue
        nulls = int(output[col].isna().sum())
        pct = (nulls / len(output) * 100) if len(output) else 0
        if nulls == 0:
            ok(f"{col}: 0 nulls")
        else:
            (warn if pct > 0 else ok)(f"{col}: {nulls} null(s) ({pct:.1f}% of rows)")

    voided_col = config["voided_column"]
    if voided_col in output.columns:
        if config.get("voided_nonzero_encoding"):
            expected_void = int((output[voided_col] != 0).sum())
        else:
            expected_void = int((output[voided_col] == config["voided_literal_in_rules"]).sum())
        if expected_void == len(void):
            ok(f"output_void.xlsx row count matches {voided_col}-derived voided rows ({len(void)})")
        else:
            warn(f"output_void.xlsx has {len(void)} rows, but {expected_void} row(s) look voided in {voided_col}")

    if len(active) == len(output):
        ok(f"output_active.xlsx row count matches output.xlsx ({len(active)} rows)")
    else:
        warn(f"output_active.xlsx has {len(active)} rows, output.xlsx has {len(output)}")

    if len(all_rows) == len(active) + len(void):
        ok(f"output_all.xlsx row count = active + void ({len(active)} + {len(void)} = {len(all_rows)})")
    else:
        warn(f"output_all.xlsx has {len(all_rows)} rows, expected {len(active) + len(void)} (active + void)")

    negation_cols = config.get("negation_columns", [])
    for col in negation_cols:
        if col not in void.columns or col not in active.columns:
            continue
        active_sum = active.loc[active.index.isin(void.index), col].abs().sum()
        void_sum = void[col].abs().sum()
        if len(void) and abs(active_sum - void_sum) > 1e-6:
            warn(f"{col}: void magnitude doesn't match the corresponding active row(s)")

    n_warn = sum(1 for c in checks if c.startswith("WARN"))
    checks.append(f"---\n{len(checks) - 1} check(s) run, {n_warn} warning(s)" if checks else "0 checks run")
    return checks


def write_verification_report(checks: list[str], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(checks) + "\n")


# ---------------------------------------------------------------------------
# Comma sanitization — some downstream/report tooling mis-parses commas
# inside cell text, so every output workbook has ',' replaced with ' ' in
# all text columns right before writing.
# ---------------------------------------------------------------------------

def sanitize_commas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in df.select_dtypes(include=["object", "str"]).columns:
        df[col] = df[col].map(lambda v: v.replace(",", " ") if isinstance(v, str) else v)
    return df


# ---------------------------------------------------------------------------
# Output writing — force-overwrite: an existing file at the target path is
# unlinked before writing, rather than relying on to_excel's implicit
# truncate-on-write, so a re-run always replaces stale output outright
# instead of silently failing on a locked/oddly-permissioned file.
# ---------------------------------------------------------------------------

def write_excel_overwrite(df: pd.DataFrame, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            raise OSError(
                f"Could not overwrite {path} — it may be open in another "
                f"program (e.g. Excel). Close it and try again. ({e})"
            ) from e
    df.to_excel(path, index=False, engine="openpyxl")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_pipeline(input_path: Path, outdir: Path) -> dict[str, Path]:
    config = load_config()
    df = read_input(input_path)
    log.info("loaded %s: %d rows x %d cols", input_path.name, *df.shape)

    df = drop_incomplete_rows(df, config)
    df = ensure_voided_column(df, config)

    output = run_derivation_steps(df, config)
    active, void = step7_active_void_split(output, config)
    all_rows = step8_union(active, void)

    checks = verify_outputs(df, output, active, void, all_rows, config)
    for line in checks:
        (log.warning if line.startswith("WARN") else log.info)(line)

    output = sanitize_commas(output)
    active = sanitize_commas(active)
    void = sanitize_commas(void)
    all_rows = sanitize_commas(all_rows)

    outdir.mkdir(parents=True, exist_ok=True)
    paths = {
        "output": outdir / "output.xlsx",
        "output_active": outdir / "output_active.xlsx",
        "output_void": outdir / "output_void.xlsx",
        "output_all": outdir / "output_all.xlsx",
        "verification_report": outdir / "verification_report.txt",
    }
    write_excel_overwrite(output, paths["output"])
    write_excel_overwrite(active, paths["output_active"])
    write_excel_overwrite(void, paths["output_void"])
    write_excel_overwrite(all_rows, paths["output_all"])
    write_verification_report(checks, paths["verification_report"])

    log.info("done. output=%d rows, active=%d, void=%d, all=%d",
              len(output), len(active), len(void), len(all_rows))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="input .csv/.xls/.xlsx file")
    parser.add_argument("--outdir", type=Path, default=Path("output"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_pipeline(args.input, args.outdir)


if __name__ == "__main__":
    main()

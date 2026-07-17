"""
Streamlit rule editor + run panel for the transform pipeline (see prd.txt /
process.txt).

Lets you view, add, edit, delete, and reorder the business rules for each
derived-column step (Business Unit, Sub Business Unit, Product, Channel,
Market, Report Date, Management Report) without hand-editing dataFilter/*.txt,
upload a CSV/XLS/XLSX file to run the full transform and download the 4
output files, and configure a recurring unattended run via Windows Task
Scheduler (see the Schedule page / scheduler.py).
Rule edits are saved straight to rules/*.json, which pipeline.py reads when
it actually runs the transform.

Run with:
    streamlit run app.py
"""
from __future__ import annotations

import io
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from path_picker import path_picker
from pipeline import (
    RULES_DIR,
    drop_incomplete_rows,
    ensure_voided_column,
    load_config as load_pipeline_config,
    read_input,
    run_derivation_steps,
    sanitize_commas,
    step7_active_void_split,
    step8_union,
    verify_outputs,
    write_excel_overwrite,
    write_verification_report,
)
from report_fetch import (
    SOURCES,
    cs_env_var,
    fetch_all,
    load_report_fetch_config,
    read_cs,
    save_report_fetch_config,
    summarize_results,
    validate_report_fetch_scope,
)
from report_fetch_scheduler import (
    ALL_PRODUCTS as FETCH_ALL_PRODUCTS,
    ALL_SOURCES as FETCH_ALL_SOURCES,
    FETCH_SCHEDULE_CONFIG_PATH,
    REGISTER_FETCH_BAT_PATH,
    TASK_NAME as FETCH_TASK_NAME,
    delete_fetch_schedule_config,
    fetch_schedule_warnings,
    format_fetch_schtasks_command,
    load_fetch_schedule_config,
    save_fetch_schedule_config,
    summarize_fetch_schedule,
    validate_fetch_schedule_config,
    write_fetch_register_bat,
)
from rules_engine import condition_to_text, parse_condition_dnf, result_to_text
from scheduler import (
    MONTHS,
    RECURRENCE_CHOICES,
    REGISTER_BAT_PATH,
    SCHEDULE_CONFIG_PATH,
    WEEKDAYS,
    delete_schedule_config,
    format_schtasks_command,
    load_schedule_config,
    save_schedule_config,
    summarize_schedule,
    validate_schedule_config,
    write_register_bat,
)
from sql_import import (
    import_xlsx_to_sql,
    load_sql_config,
    read_password,
    save_sql_config,
    validate_sql_config,
    verify_import,
)
from sql_scheduler import (
    REGISTER_SQL_BAT_PATH,
    SQL_SCHEDULE_CONFIG_PATH,
    TASK_NAME as SQL_TASK_NAME,
    delete_sql_schedule_config,
    format_sql_schtasks_command,
    load_sql_schedule_config,
    save_sql_schedule_config,
    summarize_sql_schedule,
    validate_sql_schedule_config,
    write_sql_register_bat,
)

SAMPLE_DATA_PATH = Path(__file__).parent / "rawData" / "original.xlsx"
OUTPUT_DIR = Path(__file__).parent / "output"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

STEP_PAGES = {
    "Run Pipeline": {"kind": "run"},
    "Schedule": {"kind": "schedule"},
    "Business Unit": {"file": "businessUnit.json", "kind": "single", "has_default": True},
    "Sub Business Unit": {"file": "subBusinessUnit.json", "kind": "single", "has_default": True},
    "Product": {"file": "product.json", "kind": "product", "has_default": True},
    "Channel": {"file": "channel.json", "kind": "single", "has_default": False},
    "Market": {"file": "market.json", "kind": "single", "has_default": False},
    "Report Date": {"file": "reportDate.json", "kind": "single", "has_default": False},
    "Management Report": {"file": "mgmtRpt.json", "kind": "single", "has_default": False},
    "Config": {"file": "config.json", "kind": "config", "has_default": None},
    "Import to SQL Server": {"kind": "sql_import"},
    "Schedule SQL Import": {"kind": "sql_schedule"},
    "Fetch Daily Reports": {"kind": "report_fetch"},
    "Schedule Daily Reports": {"kind": "report_fetch_schedule"},
}


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def renumber(rules: list[dict]) -> None:
    for i, rule in enumerate(rules, start=1):
        rule["id"] = i


# ---------------------------------------------------------------------------
# Rule list editor (works on any {"rules": [...], "default": {...}|None}
# container — businessUnit/subBusinessUnit/channel/market/reportDate at the
# top level of their file, or product's chains["productNew"/"subProduct"])
# ---------------------------------------------------------------------------

def blank_leaf(column: str) -> dict:
    return {"column": column, "operator": "=", "negate": False, "value": ""}


def blank_result(output_columns: list[str]) -> dict:
    return {col: {"type": "literal", "value": ""} for col in output_columns}


def render_result_editor(result: dict, output_columns: list[str], key_prefix: str) -> dict:
    """Renders literal/passthrough inputs for each output column and returns
    a callable-free dict of the *current widget values* (call resolve after
    form submit to turn it back into a result spec)."""
    widget_values = {}
    for out_col in output_columns:
        spec = result.get(out_col) or {"type": "literal", "value": ""}
        is_literal = spec.get("type", "literal") == "literal"
        c1, c2 = st.columns([1, 2])
        kind = c1.radio(
            f"{out_col}", ["Literal", "Copy column"],
            index=0 if is_literal else 1,
            key=f"{key_prefix}_kind_{out_col}", horizontal=True, label_visibility="visible",
        )
        default_val = spec.get("value", "") if is_literal else spec.get("column", "")
        val = c2.text_input(
            f"{out_col} value", value=str(default_val) if default_val is not None else "",
            key=f"{key_prefix}_val_{out_col}", label_visibility="collapsed",
        )
        widget_values[out_col] = (kind, val)
    return widget_values


def resolve_result_from_widgets(widget_values: dict) -> dict:
    result = {}
    for out_col, (kind, val) in widget_values.items():
        if kind == "Literal":
            result[out_col] = {"type": "literal", "value": val}
        else:
            result[out_col] = {"type": "column", "column": val}
    return result


def render_rule_container(container: dict, output_columns: list[str], key_columns: list[str],
                           key_prefix: str, save_fn, has_default: bool) -> None:
    rules = container.setdefault("rules", [])

    st.caption(f"Key column(s): {', '.join(key_columns)} — first matching rule wins, in the order below.")

    for idx, rule in enumerate(rules):
        title = f"{idx + 1}. {condition_to_text(rule['condition'])}  →  {result_to_text(rule['result'])}"
        with st.expander(title):
            bcols = st.columns([1, 1, 1, 6])
            if bcols[0].button("Move up", key=f"{key_prefix}_up_{rule['id']}", disabled=idx == 0):
                rules[idx - 1], rules[idx] = rules[idx], rules[idx - 1]
                renumber(rules)
                save_fn()
                st.rerun()
            if bcols[1].button("Move down", key=f"{key_prefix}_down_{rule['id']}", disabled=idx == len(rules) - 1):
                rules[idx + 1], rules[idx] = rules[idx], rules[idx + 1]
                renumber(rules)
                save_fn()
                st.rerun()
            if bcols[2].button("Delete", key=f"{key_prefix}_del_{rule['id']}"):
                rules.pop(idx)
                renumber(rules)
                save_fn()
                st.rerun()

            with st.form(key=f"{key_prefix}_form_{rule['id']}"):
                cond_text = st.text_area(
                    "Condition",
                    value=condition_to_text(rule["condition"]),
                    height=80,
                    help="Qlik-style boolean expression, e.g. "
                         "Divisi = 'CC01' and clientSegment <> 'SA'  —  "
                         "supports =, <>, like ('CIM*'), >, >=, <, <=, and, or, not(), parentheses.",
                    key=f"{key_prefix}_cond_{rule['id']}",
                )
                widget_values = render_result_editor(rule["result"], output_columns, f"{key_prefix}_r_{rule['id']}")
                if st.form_submit_button("Save rule"):
                    try:
                        new_condition = parse_condition_dnf(cond_text)
                    except Exception as e:
                        st.error(f"Could not parse condition: {e}")
                    else:
                        rule["condition"] = new_condition
                        rule["result"] = resolve_result_from_widgets(widget_values)
                        save_fn()
                        st.success("Saved.")
                        st.rerun()

    if st.button("+ Add rule", key=f"{key_prefix}_add"):
        rules.append({
            "id": len(rules) + 1,
            "condition": {"any": [[blank_leaf(key_columns[0])]]},
            "result": blank_result(output_columns),
        })
        save_fn()
        st.rerun()

    st.divider()
    st.subheader("Default (applied when no rule above matches)")
    if not has_default:
        st.info("This step has no default in the source rules — unmatched rows are left blank "
                "and flagged when the pipeline runs (channel/market/reportDate are meant to be "
                "exhaustive partitions).")
        return

    default = container.get("default")
    enabled = st.checkbox("Enable a default fallback", value=default is not None, key=f"{key_prefix}_default_on")
    if not enabled:
        if container.get("default") is not None:
            container["default"] = None
            save_fn()
        return

    with st.form(key=f"{key_prefix}_default_form"):
        widget_values = render_result_editor(default or {}, output_columns, f"{key_prefix}_default")
        if st.form_submit_button("Save default"):
            container["default"] = resolve_result_from_widgets(widget_values)
            save_fn()
            st.success("Saved.")
            st.rerun()


# ---------------------------------------------------------------------------
# Preview: run the full derivation chain against rawData/original.xlsx (if
# present) and show per-step match counts, so an edit's effect is visible
# without leaving the editor.
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _load_sample(mtime: float) -> pd.DataFrame:
    return read_input(SAMPLE_DATA_PATH)


def render_match_count_table(counts: dict) -> None:
    total = sum(counts.values())
    rows = [{"rule_id": str(rid) if rid is not None else "(unmatched/default)", "rows": n,
             "% of total": round(100 * n / total, 1)}
            for rid, n in sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0]))]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render_preview(step_key: str) -> None:
    st.divider()
    st.subheader("Preview")
    if not SAMPLE_DATA_PATH.exists():
        st.caption(f"No sample data at {SAMPLE_DATA_PATH} — preview unavailable.")
        return
    if st.button("Run against rawData/original.xlsx", key=f"preview_{step_key}"):
        df = _load_sample(SAMPLE_DATA_PATH.stat().st_mtime)
        config = load_pipeline_config()
        stats: dict = {}
        with st.spinner("Running derivation steps..."):
            run_derivation_steps(df, config, stats)
        matched_keys = [k for k in stats if k == step_key or k.startswith(f"{step_key}.")]
        if not matched_keys:
            st.warning("No stats captured for this step.")
            return
        for k in matched_keys:
            st.caption(k)
            render_match_count_table(stats[k])


# ---------------------------------------------------------------------------
# Run panel: upload/select an input file, run the full pipeline, preview and
# download the 4 output files.
# ---------------------------------------------------------------------------

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def render_run_page() -> None:
    st.header("Run Pipeline")
    st.caption("Upload a CSV/XLS/XLSX file, run the full transform (Steps 1-8), "
               "and preview or download the 4 output files.")

    sample_available = SAMPLE_DATA_PATH.exists()
    source_options = ["Upload a file"]
    if sample_available:
        source_options.append(f"Use {SAMPLE_DATA_PATH.relative_to(Path(__file__).parent)}")
    source = st.radio("Input source", source_options, horizontal=True)

    uploaded = None
    if source == "Upload a file":
        uploaded = st.file_uploader("Input file", type=["csv", "xls", "xlsx"])

    save_to_disk = st.checkbox(
        f"Also write outputs to {OUTPUT_DIR.name}/ (overwrites existing files there)",
        value=False,
    )

    if st.button("Run pipeline", type="primary"):
        try:
            if uploaded is not None:
                df = read_input(uploaded)
            elif sample_available:
                df = read_input(SAMPLE_DATA_PATH)
            else:
                st.error("Upload a file first.")
                return
        except Exception as e:
            st.error(f"Could not read input: {e}")
            return

        config = load_pipeline_config()
        stats: dict = {}
        with st.spinner("Running..."):
            try:
                df = drop_incomplete_rows(df, config)
                df = ensure_voided_column(df, config)
                output = run_derivation_steps(df, config, stats)
                active, void = step7_active_void_split(output, config)
                all_rows = step8_union(active, void)
                checks = verify_outputs(df, output, active, void, all_rows, config)
                output = sanitize_commas(output)
                active = sanitize_commas(active)
                void = sanitize_commas(void)
                all_rows = sanitize_commas(all_rows)
            except Exception as e:
                st.error(f"Pipeline failed: {e}")
                return

        st.session_state["run_result"] = {
            "output": output, "active": active, "void": void, "all": all_rows,
            "stats": stats, "checks": checks,
        }

        if save_to_disk:
            try:
                write_excel_overwrite(output, OUTPUT_DIR / "output.xlsx")
                write_excel_overwrite(active, OUTPUT_DIR / "output_active.xlsx")
                write_excel_overwrite(void, OUTPUT_DIR / "output_void.xlsx")
                write_excel_overwrite(all_rows, OUTPUT_DIR / "output_all.xlsx")
                write_verification_report(checks, OUTPUT_DIR / "verification_report.txt")
            except OSError as e:
                st.error(str(e))
            else:
                st.success(f"Also saved to {OUTPUT_DIR}/ (existing files there were overwritten)")

    result = st.session_state.get("run_result")
    if not result:
        return

    st.divider()
    st.subheader("Result")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("output.xlsx", f"{len(result['output'])} rows")
    c2.metric("output_active.xlsx", f"{len(result['active'])} rows")
    c3.metric("output_void.xlsx", f"{len(result['void'])} rows")
    c4.metric("output_all.xlsx", f"{len(result['all'])} rows")

    with st.expander("Per-step match counts"):
        for step_name, counts in result["stats"].items():
            st.caption(step_name)
            render_match_count_table(counts)

    with st.expander("Verification report", expanded=any(c.startswith("WARN") for c in result["checks"])):
        st.code("\n".join(result["checks"]), language=None)

    st.subheader("Preview")
    preview_choice = st.selectbox("File", ["output", "active", "void", "all"], key="run_preview_choice")
    st.dataframe(result[preview_choice].head(50), width="stretch")

    st.subheader("Download")
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button("output.xlsx", to_excel_bytes(result["output"]),
                        file_name="output.xlsx", mime=XLSX_MIME)
    d2.download_button("output_active.xlsx", to_excel_bytes(result["active"]),
                        file_name="output_active.xlsx", mime=XLSX_MIME)
    d3.download_button("output_void.xlsx", to_excel_bytes(result["void"]),
                        file_name="output_void.xlsx", mime=XLSX_MIME)
    d4.download_button("output_all.xlsx", to_excel_bytes(result["all"]),
                        file_name="output_all.xlsx", mime=XLSX_MIME)


# ---------------------------------------------------------------------------
# Schedule page: configure a recurring unattended run and hand off to
# Windows Task Scheduler via a generated register_*.bat (see scheduler.py).
# Streamlit itself has no background job runner, so this page only writes
# config + the registration script — actually registering the task still
# needs the manual "run as Administrator" step already used by
# register_compiler_task.bat / register_consumer_task.bat.
# ---------------------------------------------------------------------------

def render_schedule_page() -> None:
    st.header("Schedule")
    st.caption(
        "Configure a recurring, unattended run of the pipeline via Windows Task "
        "Scheduler. Scheduled runs can't upload a file interactively, so point "
        "'Input file' at a path that will exist on disk when the schedule fires."
    )

    config = load_schedule_config()

    recurrence = st.radio(
        "Run", RECURRENCE_CHOICES, horizontal=True,
        index=RECURRENCE_CHOICES.index(config["recurrence"]),
        key="sched_recurrence",
    )

    # start_date/has_end live outside the form: has_end must trigger an
    # immediate rerun to reveal the end-date picker, and end_date's
    # min_value needs start_date's current value — neither works with
    # widgets trapped inside st.form, which only reruns on submit.
    d1, d2 = st.columns(2)
    start_date = d1.date_input("Start date", value=datetime.fromisoformat(config["start_date"]).date())
    has_end = d2.checkbox("Set an end date for the auto-run", value=config.get("end_date") is not None)
    end_date = None
    if has_end:
        default_end = (datetime.fromisoformat(config["end_date"]).date()
                        if config.get("end_date") else start_date)
        end_date = st.date_input(
            "End date (schedule stops firing after this date)",
            value=max(default_end, start_date), min_value=start_date,
        )

    # Not wrapped in st.form: path_picker's Browse buttons need to rerun
    # immediately to navigate, and plain st.button isn't allowed inside
    # st.form (only form_submit_button is).
    interval = 1
    weekdays = config["weekdays"]
    day_of_month = config["day_of_month"]
    month = config["month"]

    if recurrence == "Daily":
        interval = st.number_input(
            "Every N day(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Daily" else 1,
        )
    elif recurrence == "Weekly":
        c1, c2 = st.columns(2)
        interval = c1.number_input(
            "Every N week(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Weekly" else 1,
        )
        weekdays = c2.multiselect("On", WEEKDAYS, default=weekdays or ["Monday"])
    elif recurrence == "Monthly":
        day_of_month = st.number_input("Day of month", min_value=1, max_value=31,
                                        value=int(day_of_month), step=1)
    else:  # Annually
        c1, c2 = st.columns(2)
        month = c1.selectbox("Month", MONTHS, index=MONTHS.index(month))
        day_of_month = c2.number_input("Day", min_value=1, max_value=31,
                                        value=int(day_of_month), step=1)

    run_time = st.time_input("Time of day", value=datetime.strptime(config["time"], "%H:%M").time())

    st.divider()
    p1, p2 = st.columns(2)
    with p1:
        input_path = path_picker(
            "sched_input", config["input_path"], mode="file",
            file_extensions=(".csv", ".xls", ".xlsx"), label="Input file",
        )
    with p2:
        outdir = path_picker("sched_outdir", config["outdir"], mode="dir", label="Output folder")
    st.caption(
        "Paths are resolved on whichever machine runs the schedule — Browse walks that "
        "machine's own filesystem (works the same on macOS or Windows)."
    )

    if st.button("Save schedule", type="primary"):
        new_config = {
            "recurrence": recurrence,
            "interval": int(interval),
            "weekdays": weekdays,
            "day_of_month": int(day_of_month),
            "month": month,
            "time": run_time.strftime("%H:%M"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date else None,
            "input_path": input_path,
            "outdir": outdir,
        }
        errors = validate_schedule_config(new_config)
        for e in errors:
            st.error(e)
        if not errors:
            save_schedule_config(new_config)
            bat_path = write_register_bat(new_config)
            st.success(f"Saved. Registration script refreshed at {bat_path.name}.")
            st.rerun()

    if not SCHEDULE_CONFIG_PATH.exists():
        st.info("No schedule saved yet.")
        return

    config = load_schedule_config()
    st.divider()
    st.subheader("Current schedule")
    st.write(summarize_schedule(config))
    st.caption(f"Input: `{config['input_path']}`  →  Output: `{config['outdir']}/`")

    st.subheader("Activate it")
    st.caption(
        f"Run **{REGISTER_BAT_PATH.name}** as Administrator on the Windows machine that "
        "should host this schedule — the same manual-approval step as "
        "register_compiler_task.bat / register_consumer_task.bat. Re-running it after "
        "editing the schedule above overwrites the existing task (`/F`)."
    )
    st.code(format_schtasks_command(config), language="bat")

    if REGISTER_BAT_PATH.exists():
        st.download_button(
            f"Download {REGISTER_BAT_PATH.name}",
            REGISTER_BAT_PATH.read_bytes(),
            file_name=REGISTER_BAT_PATH.name,
            mime="text/plain",
        )

    st.caption("Once registered, manage the task directly with schtasks:")
    st.code(
        'schtasks /Query  /TN "KEDP_ScheduledPipeline" /FO LIST /V\n'
        'schtasks /Run    /TN "KEDP_ScheduledPipeline"\n'
        'schtasks /Delete /TN "KEDP_ScheduledPipeline" /F',
        language="bat",
    )

    if st.button("Delete saved schedule"):
        delete_schedule_config()
        st.success("Schedule config deleted. This does not remove an already-registered "
                    "Windows task — use schtasks /Delete, shown above, for that.")
        st.rerun()


# ---------------------------------------------------------------------------
# Import to SQL Server page: load one of the transform stage's output .xlsx
# files into a Microsoft SQL Server table (see sql_import.py). New, additive
# page — doesn't touch Run Pipeline or Schedule above.
# ---------------------------------------------------------------------------

def render_sql_import_page() -> None:
    st.header("Import to SQL Server")
    st.caption(
        "Load one of the transform stage's output .xlsx files into a SQL Server "
        "table. Connection settings save to sql_config.json — the password never "
        "does; see sql_import.py for the CLI equivalent."
    )

    xlsx_path = path_picker(
        "sqlimp_xlsx", str(OUTPUT_DIR / "output_all.xlsx"), mode="file",
        file_extensions=(".xlsx",), label="Input .xlsx file",
    )

    config = load_sql_config()
    st.subheader("Connection")
    c1, c2 = st.columns(2)
    server = c1.text_input("Server", value=config["server"])
    database = c2.text_input("Database", value=config["database"])
    driver = st.text_input("ODBC driver", value=config["driver"])

    auth_options = ["windows", "sql"]
    auth = st.radio(
        "Authentication", auth_options, horizontal=True,
        index=auth_options.index(config.get("auth", "windows")),
        format_func=lambda v: "Windows Authentication" if v == "windows" else "SQL Authentication",
    )
    username = ""
    password_input = None
    if auth == "sql":
        c3, c4 = st.columns(2)
        username = c3.text_input("Username", value=config.get("username", ""))
        password_input = c4.text_input(
            "Password", type="password",
            help="Leave blank to use the MSSQL_PASSWORD environment variable instead "
                 "(set once at the OS level with 'setx MSSQL_PASSWORD ...' on Windows). "
                 "Whatever you type here is used only for this run and never written to disk.",
        )
        if not password_input:
            if os.environ.get("MSSQL_PASSWORD"):
                st.caption("Password field is blank — using MSSQL_PASSWORD from the environment.")
            else:
                st.caption("Password field is blank and MSSQL_PASSWORD isn't set — "
                           "the import will fail to authenticate.")

    st.subheader("Target")
    t1, t2 = st.columns(2)
    table = t1.text_input("Table (schema.table)", value=config["table"])
    if_exists_options = ["replace", "append"]
    if_exists = t2.radio(
        "Write mode", if_exists_options, horizontal=True,
        index=if_exists_options.index(config.get("if_exists", "replace")),
        format_func=lambda v: "Replace (truncate then load)" if v == "replace" else "Append",
    )

    new_config = {
        "driver": driver, "server": server, "database": database, "auth": auth,
        "username": username, "table": table, "if_exists": if_exists,
        "chunksize": config.get("chunksize", 1000),
    }

    b1, b2 = st.columns(2)
    if b1.button("Save connection settings"):
        errors = validate_sql_config(new_config)
        for e in errors:
            st.error(e)
        if not errors:
            save_sql_config(new_config)
            st.success("Saved (any password entered above was not written to disk).")

    if b2.button("Run import", type="primary"):
        errors = validate_sql_config(new_config)
        for e in errors:
            st.error(e)
        if not errors:
            password = read_password(new_config, password_input)
            try:
                with st.spinner("Importing..."):
                    result = import_xlsx_to_sql(Path(xlsx_path), new_config, password)
                    checks = verify_import(result)
            except Exception as e:
                st.error(f"Import failed: {e}")
            else:
                st.success(f"Loaded {result['source_rows']} row(s) into {result['table']}.")
                st.code("\n".join(checks), language=None)


# ---------------------------------------------------------------------------
# Schedule SQL Import page: configure a recurring, unattended sql_import.py
# run via Windows Task Scheduler (see sql_scheduler.py). Mirrors the Schedule
# page's pattern for pipeline.py — a separate config/task, so registering or
# deleting this schedule never touches the pipeline's own schedule.
# ---------------------------------------------------------------------------

def render_sql_schedule_page() -> None:
    st.header("Schedule SQL Import")
    st.caption(
        "Configure a recurring, unattended sql_import.py run via Windows Task "
        "Scheduler. Scheduled runs require Windows Authentication — a SQL auth "
        "password can't be embedded in a Task Scheduler command line safely "
        "(see sql_scheduler.py's docstring)."
    )

    config = load_sql_schedule_config()

    recurrence = st.radio(
        "Run", RECURRENCE_CHOICES, horizontal=True,
        index=RECURRENCE_CHOICES.index(config["recurrence"]),
        key="sqlsched_recurrence",
    )

    d1, d2 = st.columns(2)
    start_date = d1.date_input(
        "Start date", value=datetime.fromisoformat(config["start_date"]).date(), key="sqlsched_start",
    )
    has_end = d2.checkbox(
        "Set an end date for the auto-run", value=config.get("end_date") is not None, key="sqlsched_has_end",
    )
    end_date = None
    if has_end:
        default_end = (datetime.fromisoformat(config["end_date"]).date()
                        if config.get("end_date") else start_date)
        end_date = st.date_input(
            "End date (schedule stops firing after this date)",
            value=max(default_end, start_date), min_value=start_date, key="sqlsched_end",
        )

    interval = 1
    weekdays = config["weekdays"]
    day_of_month = config["day_of_month"]
    month = config["month"]

    if recurrence == "Daily":
        interval = st.number_input(
            "Every N day(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Daily" else 1,
            key="sqlsched_interval_daily",
        )
    elif recurrence == "Weekly":
        c1, c2 = st.columns(2)
        interval = c1.number_input(
            "Every N week(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Weekly" else 1,
            key="sqlsched_interval_weekly",
        )
        weekdays = c2.multiselect("On", WEEKDAYS, default=weekdays or ["Monday"], key="sqlsched_weekdays")
    elif recurrence == "Monthly":
        day_of_month = st.number_input(
            "Day of month", min_value=1, max_value=31,
            value=int(day_of_month), step=1, key="sqlsched_dom_monthly",
        )
    else:  # Annually
        c1, c2 = st.columns(2)
        month = c1.selectbox("Month", MONTHS, index=MONTHS.index(month), key="sqlsched_month")
        day_of_month = c2.number_input(
            "Day", min_value=1, max_value=31,
            value=int(day_of_month), step=1, key="sqlsched_dom_annual",
        )

    run_time = st.time_input(
        "Time of day", value=datetime.strptime(config["time"], "%H:%M").time(), key="sqlsched_time",
    )

    st.divider()
    p1, p2 = st.columns(2)
    with p1:
        xlsx_path = path_picker(
            "sqlsched_xlsx", config["xlsx_path"], mode="file",
            file_extensions=(".xlsx",), label="Input .xlsx file",
        )
    with p2:
        sql_config_path = path_picker(
            "sqlsched_sqlconfig", config["sql_config_path"], mode="file",
            file_extensions=(".json",), label="SQL connection config",
        )
    st.caption(
        "Paths are resolved on whichever machine runs the schedule — Browse walks that "
        "machine's own filesystem. Set up the connection config on the Import to SQL "
        "Server page first."
    )

    if st.button("Save schedule", type="primary", key="sqlsched_save"):
        new_config = {
            "recurrence": recurrence,
            "interval": int(interval),
            "weekdays": weekdays,
            "day_of_month": int(day_of_month),
            "month": month,
            "time": run_time.strftime("%H:%M"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date else None,
            "xlsx_path": xlsx_path,
            "sql_config_path": sql_config_path,
        }
        errors = validate_sql_schedule_config(new_config)
        for e in errors:
            st.error(e)
        if not errors:
            save_sql_schedule_config(new_config)
            bat_path = write_sql_register_bat(new_config)
            st.success(f"Saved. Registration script refreshed at {bat_path.name}.")
            st.rerun()

    if not SQL_SCHEDULE_CONFIG_PATH.exists():
        st.info("No SQL import schedule saved yet.")
        return

    config = load_sql_schedule_config()
    st.divider()
    st.subheader("Current schedule")
    st.write(summarize_sql_schedule(config))
    st.caption(f"Input: `{config['xlsx_path']}`  →  SQL config: `{config['sql_config_path']}`")

    st.subheader("Activate it")
    st.caption(
        f"Run **{REGISTER_SQL_BAT_PATH.name}** as Administrator on the Windows machine "
        "that should host this schedule — same manual-approval step as the other "
        "register_*.bat scripts. Re-running it after editing the schedule above "
        "overwrites the existing task (`/F`)."
    )
    try:
        st.code(format_sql_schtasks_command(config), language="bat")
    except ValueError as e:
        st.error(str(e))

    if REGISTER_SQL_BAT_PATH.exists():
        st.download_button(
            f"Download {REGISTER_SQL_BAT_PATH.name}",
            REGISTER_SQL_BAT_PATH.read_bytes(),
            file_name=REGISTER_SQL_BAT_PATH.name,
            mime="text/plain",
        )

    st.caption("Once registered, manage the task directly with schtasks:")
    st.code(
        f'schtasks /Query  /TN "{SQL_TASK_NAME}" /FO LIST /V\n'
        f'schtasks /Run    /TN "{SQL_TASK_NAME}"\n'
        f'schtasks /Delete /TN "{SQL_TASK_NAME}" /F',
        language="bat",
    )

    if st.button("Delete saved schedule", key="sqlsched_delete"):
        delete_sql_schedule_config()
        st.success("Schedule config deleted. This does not remove an already-registered "
                    "Windows task — use schtasks /Delete, shown above, for that.")
        st.rerun()


# ---------------------------------------------------------------------------
# Fetch Daily Reports page: pull KATRINA / COBT MT PROD / COBT DANAMON PROD
# daily transaction reports (flight/train/hotel) and extract them into a
# folder (see report_fetch.py). New, additive page - doesn't touch any
# existing page's logic.
# ---------------------------------------------------------------------------

def render_report_fetch_page() -> None:
    st.header("Fetch Daily Reports")
    st.caption(
        "Pulls daily transaction reports from KATRINA, COBT MT PROD, and COBT DANAMON PROD "
        "(flight/train/hotel each) and extracts the CSVs into a folder. Auth is HTTP Basic "
        "(ck/cs) - ck saves to report_fetch_config.json, cs never does; see report_fetch.py."
    )

    config = load_report_fetch_config()
    st.subheader("Credentials")
    cs_values: dict[str, str | None] = {}
    for key, source in SOURCES.items():
        c1, c2 = st.columns(2)
        config[key] = c1.text_input(f"{source['label']} ck", value=config.get(key, ""), key=f"fetch_ck_{key}")
        cs_input = c2.text_input(
            f"{source['label']} cs", type="password", key=f"fetch_cs_{key}",
            help=f"Leave blank to use the {cs_env_var(key)} environment variable instead. "
                 "Used only for this run, never written to disk.",
        )
        cs_values[key] = read_cs(key, cs_input)
        if not cs_input and not os.environ.get(cs_env_var(key)):
            c2.caption(f"cs is blank and {cs_env_var(key)} isn't set - this source will fail.")

    if st.button("Save ck values"):
        save_report_fetch_config(config)
        st.success("Saved (cs was not written to disk).")

    st.divider()
    st.subheader("What to fetch")
    c1, c2, c3 = st.columns(3)
    days_ago = c1.number_input("Days ago (0 = today)", min_value=0, step=1, value=1, key="fetch_days_ago")
    report_date = (date.today() - timedelta(days=int(days_ago))).isoformat()
    c2.text_input("Report date", value=report_date, disabled=True)
    sources = c3.multiselect("Sources", list(SOURCES.keys()), default=list(SOURCES.keys()),
                              format_func=lambda k: SOURCES[k]["label"], key="fetch_sources")
    products = st.multiselect("Products", ["flight", "train", "hotel"],
                               default=["flight", "train", "hotel"], key="fetch_products")

    outdir = path_picker("fetch_outdir", "rawData/katrina_daily_reports", mode="dir", label="Output folder")

    if st.button("Run fetch now", type="primary"):
        errors = validate_report_fetch_scope(config, sources, cs_values)
        for e in errors:
            st.error(e)
        if not errors:
            with st.spinner("Fetching..."):
                results = fetch_all(report_date, Path(outdir), config, cs_values,
                                     sources=sources, products=products)
            ok = sum(1 for r in results if r["status"] == "ok")
            if ok == len(results):
                st.success(f"{ok}/{len(results)} endpoint(s) OK. Saved to {outdir}/")
            else:
                st.warning(f"{ok}/{len(results)} endpoint(s) OK.")
            st.code(summarize_results(results), language=None)


# ---------------------------------------------------------------------------
# Schedule Daily Reports page: configure a recurring, unattended
# report_fetch.py run via Windows Task Scheduler (see
# report_fetch_scheduler.py). Mirrors the Schedule / Schedule SQL Import
# pages' pattern - a separate config/task, so registering or deleting this
# schedule never touches the other two.
# ---------------------------------------------------------------------------

def render_report_fetch_schedule_page() -> None:
    st.header("Schedule Daily Reports")
    st.caption(
        "Configure a recurring, unattended report_fetch.py run via Windows Task Scheduler. "
        "Each source's cs must be set as a system environment variable on the machine that "
        "runs the schedule - see report_fetch_scheduler.py's docstring."
    )

    config = load_fetch_schedule_config()

    recurrence = st.radio(
        "Run", RECURRENCE_CHOICES, horizontal=True,
        index=RECURRENCE_CHOICES.index(config["recurrence"]),
        key="fetchsched_recurrence",
    )

    d1, d2 = st.columns(2)
    start_date = d1.date_input(
        "Start date", value=datetime.fromisoformat(config["start_date"]).date(), key="fetchsched_start",
    )
    has_end = d2.checkbox(
        "Set an end date for the auto-run", value=config.get("end_date") is not None, key="fetchsched_has_end",
    )
    end_date = None
    if has_end:
        default_end = (datetime.fromisoformat(config["end_date"]).date()
                        if config.get("end_date") else start_date)
        end_date = st.date_input(
            "End date (schedule stops firing after this date)",
            value=max(default_end, start_date), min_value=start_date, key="fetchsched_end",
        )

    interval = 1
    weekdays = config["weekdays"]
    day_of_month = config["day_of_month"]
    month = config["month"]

    if recurrence == "Daily":
        interval = st.number_input(
            "Every N day(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Daily" else 1,
            key="fetchsched_interval_daily",
        )
    elif recurrence == "Weekly":
        c1, c2 = st.columns(2)
        interval = c1.number_input(
            "Every N week(s)", min_value=1, step=1,
            value=int(config.get("interval", 1)) if config["recurrence"] == "Weekly" else 1,
            key="fetchsched_interval_weekly",
        )
        weekdays = c2.multiselect("On", WEEKDAYS, default=weekdays or ["Monday"], key="fetchsched_weekdays")
    elif recurrence == "Monthly":
        day_of_month = st.number_input(
            "Day of month", min_value=1, max_value=31,
            value=int(day_of_month), step=1, key="fetchsched_dom_monthly",
        )
    else:  # Annually
        c1, c2 = st.columns(2)
        month = c1.selectbox("Month", MONTHS, index=MONTHS.index(month), key="fetchsched_month")
        day_of_month = c2.number_input(
            "Day", min_value=1, max_value=31,
            value=int(day_of_month), step=1, key="fetchsched_dom_annual",
        )

    run_time = st.time_input(
        "Time of day", value=datetime.strptime(config["time"], "%H:%M").time(), key="fetchsched_time",
    )

    st.divider()
    c1, c2 = st.columns(2)
    days_ago = c1.number_input("Days ago (0 = today)", min_value=0, step=1,
                                value=int(config.get("days_ago", 1)), key="fetchsched_days_ago")
    sources = c2.multiselect("Sources", FETCH_ALL_SOURCES, default=config.get("sources", FETCH_ALL_SOURCES),
                              format_func=lambda k: SOURCES[k]["label"], key="fetchsched_sources")
    products = st.multiselect("Products", FETCH_ALL_PRODUCTS, default=config.get("products", FETCH_ALL_PRODUCTS),
                               key="fetchsched_products")

    p1, p2 = st.columns(2)
    with p1:
        outdir = path_picker("fetchsched_outdir", config["outdir"], mode="dir", label="Output folder")
    with p2:
        report_fetch_config_path = path_picker(
            "fetchsched_config", config["report_fetch_config_path"], mode="file",
            file_extensions=(".json",), label="ck config file",
        )
    st.caption(
        "Paths are resolved on whichever machine runs the schedule - Browse walks that "
        "machine's own filesystem. Set ck for each source on the Fetch Daily Reports page first."
    )

    if st.button("Save schedule", type="primary", key="fetchsched_save"):
        new_config = {
            "recurrence": recurrence,
            "interval": int(interval),
            "weekdays": weekdays,
            "day_of_month": int(day_of_month),
            "month": month,
            "time": run_time.strftime("%H:%M"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat() if end_date else None,
            "days_ago": int(days_ago),
            "outdir": outdir,
            "report_fetch_config_path": report_fetch_config_path,
            "sources": sources,
            "products": products,
        }
        errors = validate_fetch_schedule_config(new_config)
        for e in errors:
            st.error(e)
        if not errors:
            save_fetch_schedule_config(new_config)
            bat_path = write_fetch_register_bat(new_config)
            st.success(f"Saved. Registration script refreshed at {bat_path.name}.")
            st.rerun()

    if not FETCH_SCHEDULE_CONFIG_PATH.exists():
        st.info("No report fetch schedule saved yet.")
        return

    config = load_fetch_schedule_config()
    st.divider()
    st.subheader("Current schedule")
    st.write(summarize_fetch_schedule(config))
    st.caption(f"Output: `{config['outdir']}/`  ·  Sources: {', '.join(config['sources'])}  ·  "
               f"Products: {', '.join(config['products'])}")

    for w in fetch_schedule_warnings(config):
        st.warning(w)

    st.subheader("Activate it")
    st.caption(
        f"Run **{REGISTER_FETCH_BAT_PATH.name}** as Administrator on the Windows machine "
        "that should host this schedule - same manual-approval step as the other "
        "register_*.bat scripts. Re-running it after editing the schedule above "
        "overwrites the existing task (`/F`)."
    )
    try:
        st.code(format_fetch_schtasks_command(config), language="bat")
    except ValueError as e:
        st.error(str(e))

    if REGISTER_FETCH_BAT_PATH.exists():
        st.download_button(
            f"Download {REGISTER_FETCH_BAT_PATH.name}",
            REGISTER_FETCH_BAT_PATH.read_bytes(),
            file_name=REGISTER_FETCH_BAT_PATH.name,
            mime="text/plain",
        )

    st.caption("Once registered, manage the task directly with schtasks:")
    st.code(
        f'schtasks /Query  /TN "{FETCH_TASK_NAME}" /FO LIST /V\n'
        f'schtasks /Run    /TN "{FETCH_TASK_NAME}"\n'
        f'schtasks /Delete /TN "{FETCH_TASK_NAME}" /F',
        language="bat",
    )

    if st.button("Delete saved schedule", key="fetchsched_delete"):
        delete_fetch_schedule_config()
        st.success("Schedule config deleted. This does not remove an already-registered "
                    "Windows task - use schtasks /Delete, shown above, for that.")
        st.rerun()


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------

def render_single_step_page(step_key: str, file_name: str, has_default: bool) -> None:
    path = RULES_DIR / file_name
    step_def = load_json(path)

    def save():
        save_json(path, step_def)

    st.header(step_def.get("label", step_key))
    render_rule_container(
        step_def, step_def["output_columns"], step_def["key_columns"],
        key_prefix=step_key, save_fn=save, has_default=has_default,
    )
    render_preview(step_def["step"])


def render_product_page() -> None:
    path = RULES_DIR / "product.json"
    step_def = load_json(path)

    def save():
        save_json(path, step_def)

    st.header("Product")
    st.caption(f"Key column: {', '.join(step_def['key_columns'])}")
    tab1, tab2 = st.tabs(["productNew", "subProduct"])
    for tab, out_col in [(tab1, "productNew"), (tab2, "subProduct")]:
        with tab:
            chain = step_def["chains"][out_col]
            render_rule_container(
                chain, [out_col], step_def["key_columns"],
                key_prefix=f"product_{out_col}", save_fn=save, has_default=True,
            )
            render_preview(f"product.{out_col}")


def render_config_page() -> None:
    path = RULES_DIR / "config.json"
    config = load_json(path)

    st.header("Config")
    st.caption("Pipeline-wide settings — not tied to any single rule file.")

    with st.form("config_form"):
        st.subheader("Voided-row encoding")
        st.caption("dataFilter/reportDate.txt was written against a literal "
                    "Voided = 1 convention; this dataset instead encodes voided "
                    "rows as a nonzero value (e.g. -1). When enabled, any rule "
                    "condition of the form 'Voided = <literal>' is evaluated as "
                    "'Voided <> 0' instead. See prd.txt section 5.6.")
        voided_column = st.text_input("Voided column name", value=config["voided_column"])
        voided_nonzero = st.checkbox("Treat Voided as nonzero-encoded", value=config["voided_nonzero_encoding"])
        voided_literal = st.number_input("Literal value used in the rule text (usually 1)",
                                          value=int(config["voided_literal_in_rules"]), step=1)

        st.subheader("Active/void split (Step 7)")
        negation_cols = st.text_area(
            "Columns to negate (×-1) on void rows, comma-separated",
            value=", ".join(config["negation_columns"]), height=80,
        )
        active_label = st.text_input("voidStatus value for active rows", value=config["void_status_active_value"])
        void_label = st.text_input("voidStatus value for void rows", value=config["void_status_void_value"])

        submitted = st.form_submit_button("Save config")
        if submitted:
            config["voided_column"] = voided_column
            config["voided_nonzero_encoding"] = voided_nonzero
            config["voided_literal_in_rules"] = int(voided_literal)
            config["negation_columns"] = [c.strip() for c in negation_cols.split(",") if c.strip()]
            config["void_status_active_value"] = active_label
            config["void_status_void_value"] = void_label
            save_json(path, config)
            st.success("Saved.")
            st.rerun()

    st.divider()
    st.subheader("Column aliases")
    st.caption("Maps a name used in rule text to the actual data column, e.g. Qlik's "
               "internal 'subChannels2' alias to this pipeline's 'subChannels' output. "
               "Case-insensitive matches (clientSegment -> ClientSegment) are handled "
               "automatically and don't need an entry here.")
    alias_df = pd.DataFrame(
        [{"rule text name": k, "actual column": v} for k, v in config.get("column_aliases", {}).items()]
    )
    edited = st.data_editor(alias_df, num_rows="dynamic", width="stretch", key="alias_editor")
    if st.button("Save aliases"):
        new_aliases = {
            row["rule text name"]: row["actual column"]
            for _, row in edited.iterrows()
            if row.get("rule text name") and row.get("actual column")
        }
        config["column_aliases"] = new_aliases
        save_json(path, config)
        st.success("Saved.")
        st.rerun()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Transform Rule Editor", layout="wide")
    st.sidebar.title("Transform Rule Editor")
    page = st.sidebar.radio("Step", list(STEP_PAGES.keys()))
    st.sidebar.caption("Edits save straight to rules/*.json — pipeline.py picks them "
                        "up next time it runs, no restart needed.")

    info = STEP_PAGES[page]
    if info["kind"] == "run":
        render_run_page()
    elif info["kind"] == "schedule":
        render_schedule_page()
    elif info["kind"] == "single":
        render_single_step_page(page, info["file"], info["has_default"])
    elif info["kind"] == "product":
        render_product_page()
    elif info["kind"] == "sql_import":
        render_sql_import_page()
    elif info["kind"] == "sql_schedule":
        render_sql_schedule_page()
    elif info["kind"] == "report_fetch":
        render_report_fetch_page()
    elif info["kind"] == "report_fetch_schedule":
        render_report_fetch_schedule_page()
    else:
        render_config_page()


if __name__ == "__main__":
    main()

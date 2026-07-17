"""
Config + Windows Task Scheduler (schtasks) plumbing for running
report_fetch.py on a recurring schedule, chosen from the Streamlit
"Schedule Daily Reports" page (app.py). Mirrors scheduler.py's pattern for
pipeline.py (and sql_scheduler.py's for sql_import.py) exactly, as a
separate, additive module - shared pieces (recurrence choices, weekday/month
codes, the /SC flag builder, the mm/dd/yyyy formatter) are imported from
scheduler.py rather than duplicated.

This module only builds config/commands - it never calls schtasks itself.
Registering the task still requires running the generated .bat as
Administrator, same as the other register_*.bat scripts in this repo.

Security note: schtasks command lines are visible to any local user via
`schtasks /Query /TN ... /FO LIST /V` and in the Task Scheduler UI, so a cs
(API secret) must never be embedded in one - see report_fetch.py's docstring.
Unattended/scheduled fetches read each source's cs from a per-source system
environment variable (setx <SOURCE>_CS ... /M, as Administrator) that must
already be set on the machine before the task fires; this module has no way
to verify that from here, so it only warns, it does not block registration.

Usage:
    from report_fetch_scheduler import load_fetch_schedule_config, \
        save_fetch_schedule_config, format_fetch_schtasks_command, \
        render_fetch_register_bat
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from report_fetch import (
    SOURCES,
    REPORT_FETCH_CONFIG_PATH,
    cs_env_var,
    load_report_fetch_config,
)
from scheduler import (
    MONTHS,
    RECURRENCE_CHOICES,
    WEEKDAYS,
    _mmddyyyy,
    _recurrence_flag_lines,
    validate_schedule_config,
)

PROJECT_DIR = Path(__file__).parent
FETCH_SCHEDULE_CONFIG_PATH = PROJECT_DIR / "report_fetch_schedule_config.json"
REGISTER_FETCH_BAT_PATH = PROJECT_DIR / "register_scheduled_report_fetch_task.bat"
REPORT_FETCH_SCRIPT = PROJECT_DIR / "report_fetch.py"

TASK_NAME = "KEDP_ScheduledReportFetch"

ALL_SOURCES = list(SOURCES.keys())
ALL_PRODUCTS = ["flight", "train", "hotel"]


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def default_fetch_schedule_config() -> dict:
    return {
        "recurrence": "Daily",
        "interval": 1,
        "weekdays": ["Monday"],
        "day_of_month": 1,
        "month": "January",
        "time": "03:00",
        "start_date": date.today().isoformat(),
        "end_date": None,
        "days_ago": 1,
        "outdir": "rawData/katrina_daily_reports",
        "report_fetch_config_path": str(REPORT_FETCH_CONFIG_PATH.name),
        "sources": list(ALL_SOURCES),
        "products": list(ALL_PRODUCTS),
    }


def load_fetch_schedule_config() -> dict:
    if not FETCH_SCHEDULE_CONFIG_PATH.exists():
        return default_fetch_schedule_config()
    return {**default_fetch_schedule_config(), **json.loads(FETCH_SCHEDULE_CONFIG_PATH.read_text())}


def save_fetch_schedule_config(config: dict) -> None:
    FETCH_SCHEDULE_CONFIG_PATH.write_text(json.dumps(config, indent=2))


def delete_fetch_schedule_config() -> None:
    if FETCH_SCHEDULE_CONFIG_PATH.exists():
        FETCH_SCHEDULE_CONFIG_PATH.unlink()


# ---------------------------------------------------------------------------
# Config -> schtasks
# ---------------------------------------------------------------------------

def validate_fetch_schedule_config(config: dict) -> list[str]:
    """Returns a list of human-readable problems; empty means OK to save/register."""
    errors = validate_schedule_config(config)
    if not config.get("outdir"):
        errors.append("An output folder is required.")
    if config.get("days_ago", 0) < 0:
        errors.append("Days ago must be 0 or more.")
    sources = config.get("sources") or []
    if not sources:
        errors.append("Pick at least one source to fetch.")
    if not config.get("products"):
        errors.append("Pick at least one product to fetch.")

    fetch_config_path = Path(config.get("report_fetch_config_path") or REPORT_FETCH_CONFIG_PATH)
    if not fetch_config_path.exists():
        errors.append(f"{fetch_config_path} doesn't exist yet - set ck for each source on "
                       f"the Fetch Daily Reports page first.")
    else:
        fetch_config = {**load_report_fetch_config(), **json.loads(fetch_config_path.read_text())}
        for key in sources:
            if key not in SOURCES:
                errors.append(f"Unknown source: {key!r}")
            elif not fetch_config.get(key):
                errors.append(f"{SOURCES[key]['label']}: ck is not set in {fetch_config_path.name}.")
    return errors


def fetch_schedule_warnings(config: dict) -> list[str]:
    """Non-blocking cautions, shown separately from hard validation errors."""
    warnings = []
    for key in config.get("sources") or []:
        if key in SOURCES:
            warnings.append(
                f"{SOURCES[key]['label']} needs its cs set as a SYSTEM environment variable "
                f"named {cs_env_var(key)} on the machine that runs this schedule "
                f"(setx {cs_env_var(key)} ... /M, as Administrator) - it can't be embedded "
                f"in the scheduled task."
            )
    return warnings


def _fetch_schtasks_flag_lines(config: dict, python_exe: str, report_fetch_script: str,
                                task_name: str = TASK_NAME) -> list[str]:
    errors = validate_fetch_schedule_config(config)
    if errors:
        raise ValueError("invalid fetch schedule config: " + "; ".join(errors))

    extra = "".join(f' \\"--source\\" \\"{s}\\"' for s in config["sources"])
    extra += "".join(f' \\"--product\\" \\"{p}\\"' for p in config["products"])
    tr_value = (f'"\\"{python_exe}\\" \\"{report_fetch_script}\\" '
                f'\\"--days-ago\\" \\"{config["days_ago"]}\\" '
                f'\\"--outdir\\" \\"{config["outdir"]}\\" '
                f'\\"--config\\" \\"{config["report_fetch_config_path"]}\\"{extra}"')

    lines = [f'/TN "{task_name}"', f"/TR {tr_value}"]
    lines += _recurrence_flag_lines(config)
    lines += [f'/ST {config["time"]}', f'/SD {_mmddyyyy(config["start_date"])}']
    if config.get("end_date"):
        lines.append(f'/ED {_mmddyyyy(config["end_date"])}')
    lines += ["/RL HIGHEST", "/F"]
    return lines


def format_fetch_schtasks_command(config: dict, python_exe: str = "python",
                                   report_fetch_script: Path = REPORT_FETCH_SCRIPT,
                                   task_name: str = TASK_NAME) -> str:
    """Single-line schtasks /Create command, for read-only display in the UI."""
    lines = _fetch_schtasks_flag_lines(config, python_exe, str(report_fetch_script), task_name)
    return "schtasks /Create " + " ".join(lines)


def summarize_fetch_schedule(config: dict) -> str:
    recurrence = config["recurrence"]
    if recurrence == "Daily":
        summary = f"every {config.get('interval', 1)} day(s) at {config['time']}"
    elif recurrence == "Weekly":
        days = ", ".join(config["weekdays"])
        summary = f"every {config.get('interval', 1)} week(s) on {days} at {config['time']}"
    elif recurrence == "Monthly":
        summary = f"monthly on day {config['day_of_month']} at {config['time']}"
    else:
        summary = f"annually on {config['month']} {config['day_of_month']} at {config['time']}"
    end_note = f", ending {config['end_date']}" if config.get("end_date") else ", no end date"
    days_ago_note = "today's" if config.get("days_ago", 1) == 0 else f"{config.get('days_ago', 1)}-day(s)-ago"
    return f"{summary}{end_note} - fetches {days_ago_note} report"


# ---------------------------------------------------------------------------
# .bat generation - same admin-check / python-detect / schtasks shape as
# register_compiler_task.bat / register_consumer_task.bat / scheduler.py's
# render_register_bat / sql_scheduler.py's render_sql_register_bat.
# ---------------------------------------------------------------------------

def render_fetch_register_bat(config: dict, task_name: str = TASK_NAME) -> str:
    schtasks_lines = _fetch_schtasks_flag_lines(config, "%PYTHON%", "%REPORTFETCH%", "%TASK_NAME%")
    schtasks_block = "schtasks /Create ^\n    " + " ^\n    ".join(schtasks_lines)
    summary = summarize_fetch_schedule(config)
    env_var_lines = "\n".join(
        f"echo        {cs_env_var(key)}  ({SOURCES[key]['label']})"
        for key in config["sources"] if key in SOURCES
    )

    return f"""@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  KEDP - Scheduled Daily Report Fetch Task Registration
::  Generated by app.py's Schedule Daily Reports page from
::  report_fetch_schedule_config.json. Run once as Administrator:
::    {summary}
::  Safe to re-run - /F overwrites the existing task.
:: ============================================================

:: -- Admin check ------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This script must be run as Administrator.
    echo.
    echo Right-click {REGISTER_FETCH_BAT_PATH.name} ^> "Run as administrator"
    pause
    exit /b 1
)

:: -- Resolve report_fetch.py path -----------------------------------
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "REPORTFETCH=%SCRIPT_DIR%\\report_fetch.py"

if not exist "%REPORTFETCH%" (
    echo [ERROR] report_fetch.py not found at: %REPORTFETCH%
    pause
    exit /b 1
)

:: -- Detect Python ------------------------------------------------
for /f "usebackq delims=" %%i in (`where python 2^>nul`) do (
    set "PYTHON=%%i"
    goto :python_found
)
echo [ERROR] Python not found in PATH.
echo Install Python and ensure it is added to PATH, then re-run.
pause
exit /b 1

:python_found

:: -- Timezone reminder ---------------------------------------------
echo.
echo [INFO] Task Scheduler uses the system clock with no timezone conversion.
echo        Confirm your system timezone before continuing:
echo.
tzutil /g
echo.
echo        Expected: (UTC+07:00) Bangkok, Hanoi, Jakarta
echo        If wrong, fix it in Settings ^> Time ^& Language before proceeding.
echo.
echo [INFO] Each source's cs must already be set as a SYSTEM environment
echo        variable (setx NAME ... /M, as Administrator) - it is never
echo        embedded in this script or the scheduled task:
{env_var_lines if env_var_lines else "echo        (no sources selected)"}
echo.
set /p "CONFIRM=Continue with registration? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo Aborted.
    pause
    exit /b 0
)

:: -- Register Task Scheduler job ------------------------------------
set "TASK_NAME={task_name}"

{schtasks_block}

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] schtasks failed. See message above for details.
    pause
    exit /b 1
)

:: -- Confirm ----------------------------------------------------------
echo.
echo [OK] Task "%TASK_NAME%" registered.
echo      Trigger : {summary}
echo      Script  : %REPORTFETCH%
echo      Python  : %PYTHON%
echo      Outdir  : {config['outdir']}
echo      Sources : {", ".join(config['sources'])}
echo      Products: {", ".join(config['products'])}
echo.
echo Useful commands:
echo   Query  : schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
echo   Run now: schtasks /Run   /TN "%TASK_NAME%"
echo   Remove : schtasks /Delete /TN "%TASK_NAME%" /F
echo.
pause
endlocal
"""


def write_fetch_register_bat(config: dict, task_name: str = TASK_NAME) -> Path:
    REGISTER_FETCH_BAT_PATH.write_text(render_fetch_register_bat(config, task_name))
    return REGISTER_FETCH_BAT_PATH


__all__ = [
    "RECURRENCE_CHOICES", "WEEKDAYS", "MONTHS", "ALL_SOURCES", "ALL_PRODUCTS",
    "default_fetch_schedule_config", "load_fetch_schedule_config", "save_fetch_schedule_config",
    "delete_fetch_schedule_config", "validate_fetch_schedule_config", "fetch_schedule_warnings",
    "format_fetch_schtasks_command", "summarize_fetch_schedule", "render_fetch_register_bat",
    "write_fetch_register_bat", "REGISTER_FETCH_BAT_PATH", "FETCH_SCHEDULE_CONFIG_PATH",
]

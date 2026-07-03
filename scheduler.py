"""
Config + Windows Task Scheduler (schtasks) plumbing for running pipeline.py
on a recurring schedule, chosen from the Streamlit "Schedule" page (app.py).

This module only builds config/commands — it never calls schtasks itself.
Registering the task still requires running the generated .bat as
Administrator, the same manual-approval pattern already used by
register_compiler_task.bat / register_consumer_task.bat.

Usage:
    from scheduler import load_schedule_config, save_schedule_config, \
        format_schtasks_command, render_register_bat
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
SCHEDULE_CONFIG_PATH = PROJECT_DIR / "schedule_config.json"
REGISTER_BAT_PATH = PROJECT_DIR / "register_scheduled_pipeline_task.bat"
PIPELINE_SCRIPT = PROJECT_DIR / "pipeline.py"

TASK_NAME = "KEDP_ScheduledPipeline"

RECURRENCE_CHOICES = ["Daily", "Weekly", "Monthly", "Annually"]

# schtasks /D weekday codes, in display order
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAY_CODES = {
    "Monday": "MON", "Tuesday": "TUE", "Wednesday": "WED", "Thursday": "THU",
    "Friday": "FRI", "Saturday": "SAT", "Sunday": "SUN",
}

# schtasks /M month codes, in display order
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_CODES = {
    "January": "JAN", "February": "FEB", "March": "MAR", "April": "APR",
    "May": "MAY", "June": "JUN", "July": "JUL", "August": "AUG",
    "September": "SEP", "October": "OCT", "November": "NOV", "December": "DEC",
}


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def default_schedule_config() -> dict:
    return {
        "recurrence": "Daily",
        "interval": 1,
        "weekdays": ["Monday"],
        "day_of_month": 1,
        "month": "January",
        "time": "01:00",
        "start_date": date.today().isoformat(),
        "end_date": None,
        "input_path": "rawData/original.xlsx",
        "outdir": "output",
    }


def load_schedule_config() -> dict:
    if not SCHEDULE_CONFIG_PATH.exists():
        return default_schedule_config()
    return {**default_schedule_config(), **json.loads(SCHEDULE_CONFIG_PATH.read_text())}


def save_schedule_config(config: dict) -> None:
    SCHEDULE_CONFIG_PATH.write_text(json.dumps(config, indent=2))


def delete_schedule_config() -> None:
    if SCHEDULE_CONFIG_PATH.exists():
        SCHEDULE_CONFIG_PATH.unlink()


# ---------------------------------------------------------------------------
# Config -> schtasks
# ---------------------------------------------------------------------------

def _mmddyyyy(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{m}/{d}/{y}"


def validate_schedule_config(config: dict) -> list[str]:
    """Returns a list of human-readable problems; empty means OK to save/register."""
    errors = []
    if config["recurrence"] not in RECURRENCE_CHOICES:
        errors.append(f"Unknown recurrence: {config['recurrence']!r}")
    if config["recurrence"] == "Weekly" and not config.get("weekdays"):
        errors.append("Pick at least one day of the week for a weekly schedule.")
    if config["recurrence"] in ("Monthly", "Annually"):
        dom = config.get("day_of_month")
        if not isinstance(dom, int) or not (1 <= dom <= 31):
            errors.append("Day of month must be between 1 and 31.")
    if config.get("interval", 1) < 1:
        errors.append("Interval must be at least 1.")
    end_date = config.get("end_date")
    if end_date and end_date < config["start_date"]:
        errors.append("End date can't be before the start date.")
    return errors


def _recurrence_flag_lines(config: dict) -> list[str]:
    recurrence = config["recurrence"]
    if recurrence == "Daily":
        return [f'/SC DAILY', f'/MO {config.get("interval", 1)}']
    if recurrence == "Weekly":
        days = ",".join(WEEKDAY_CODES[d] for d in config["weekdays"])
        return [f'/SC WEEKLY', f'/MO {config.get("interval", 1)}', f'/D {days}']
    if recurrence == "Monthly":
        return ["/SC MONTHLY", f'/D {config["day_of_month"]}']
    if recurrence == "Annually":
        return ["/SC MONTHLY", f'/D {config["day_of_month"]}',
                f'/M {MONTH_CODES[config["month"]]}']
    raise ValueError(f"unknown recurrence: {recurrence!r}")


def _schtasks_flag_lines(config: dict, python_exe: str, pipeline_script: str,
                          task_name: str = TASK_NAME) -> list[str]:
    """One `/FLAG value` string per schtasks argument, in the same order and
    /TR nested-quote style (\\"...\\" inside an outer "...") already used by
    register_compiler_task.bat / register_consumer_task.bat."""
    errors = validate_schedule_config(config)
    if errors:
        raise ValueError("invalid schedule config: " + "; ".join(errors))

    tr_value = (f'"\\"{python_exe}\\" \\"{pipeline_script}\\" '
                f'\\"{config["input_path"]}\\" --outdir \\"{config["outdir"]}\\""')

    lines = [f'/TN "{task_name}"', f"/TR {tr_value}"]
    lines += _recurrence_flag_lines(config)
    lines += [f'/ST {config["time"]}', f'/SD {_mmddyyyy(config["start_date"])}']
    if config.get("end_date"):
        lines.append(f'/ED {_mmddyyyy(config["end_date"])}')
    lines += ["/RL HIGHEST", "/F"]
    return lines


def format_schtasks_command(config: dict, python_exe: str = "python",
                             pipeline_script: Path = PIPELINE_SCRIPT,
                             task_name: str = TASK_NAME) -> str:
    """Single-line schtasks /Create command, for read-only display in the UI."""
    lines = _schtasks_flag_lines(config, python_exe, str(pipeline_script), task_name)
    return "schtasks /Create " + " ".join(lines)


def summarize_schedule(config: dict) -> str:
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
    return summary + end_note


# ---------------------------------------------------------------------------
# .bat generation — same shape as register_compiler_task.bat /
# register_consumer_task.bat: admin check, resolve script path, detect
# python, run schtasks /Create, print the useful follow-up commands.
# ---------------------------------------------------------------------------

def render_register_bat(config: dict, task_name: str = TASK_NAME) -> str:
    schtasks_lines = _schtasks_flag_lines(config, "%PYTHON%", "%PIPELINE%", "%TASK_NAME%")
    schtasks_block = "schtasks /Create ^\n    " + " ^\n    ".join(schtasks_lines)
    summary = summarize_schedule(config)

    return f"""@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  KEDP - Scheduled Pipeline Task Registration
::  Generated by app.py's Schedule page from schedule_config.json.
::  Run once as Administrator to wire up the job:
::    {summary}
::  Safe to re-run - /F overwrites the existing task.
:: ============================================================

:: -- Admin check ------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This script must be run as Administrator.
    echo.
    echo Right-click {REGISTER_BAT_PATH.name} ^> "Run as administrator"
    pause
    exit /b 1
)

:: -- Resolve pipeline path ---------------------------------------
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PIPELINE=%SCRIPT_DIR%\\pipeline.py"

if not exist "%PIPELINE%" (
    echo [ERROR] pipeline.py not found at: %PIPELINE%
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
echo      Script  : %PIPELINE%
echo      Python  : %PYTHON%
echo      Input   : {config['input_path']}
echo      Outdir  : {config['outdir']}
echo.
echo Useful commands:
echo   Query  : schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
echo   Run now: schtasks /Run   /TN "%TASK_NAME%"
echo   Remove : schtasks /Delete /TN "%TASK_NAME%" /F
echo.
pause
endlocal
"""


def write_register_bat(config: dict, task_name: str = TASK_NAME) -> Path:
    REGISTER_BAT_PATH.write_text(render_register_bat(config, task_name))
    return REGISTER_BAT_PATH

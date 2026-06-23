@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  KEDP – Daily Compiler Task Registration
::  Run once as Administrator to wire up the 01:00 AM job.
::  Safe to re-run – /F overwrites the existing task.
:: ============================================================

:: ── Admin check ─────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This script must be run as Administrator.
    echo.
    echo Right-click register_task.bat ^> "Run as administrator"
    pause
    exit /b 1
)

:: ── Resolve compiler path ────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash so path concatenation is clean
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "COMPILER=%SCRIPT_DIR%\compiler.py"

if not exist "%COMPILER%" (
    echo [ERROR] compiler.py not found at: %COMPILER%
    echo Make sure register_task.bat lives in the same folder as compiler.py.
    pause
    exit /b 1
)

:: ── Detect Python ────────────────────────────────────────────
for /f "usebackq delims=" %%i in (`where python 2^>nul`) do (
    set "PYTHON=%%i"
    goto :python_found
)
echo [ERROR] Python not found in PATH.
echo Install Python and ensure it is added to PATH, then re-run.
pause
exit /b 1

:python_found

:: ── Timezone reminder ────────────────────────────────────────
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

:: ── Register Task Scheduler job ──────────────────────────────
set "TASK_NAME=KEDP_DailyCompiler"

schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "\"%PYTHON%\" \"%COMPILER%\"" ^
    /SC DAILY ^
    /ST 01:00 ^
    /RL HIGHEST ^
    /F

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] schtasks failed. See message above for details.
    pause
    exit /b 1
)

:: ── Confirm ──────────────────────────────────────────────────
echo.
echo [OK] Task "%TASK_NAME%" registered.
echo      Trigger : daily at 01:00 AM system time
echo      Script  : %COMPILER%
echo      Python  : %PYTHON%
echo.
echo Useful commands:
echo   Query  : schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
echo   Run now: schtasks /Run   /TN "%TASK_NAME%"
echo   Remove : schtasks /Delete /TN "%TASK_NAME%" /F
echo.
pause
endlocal

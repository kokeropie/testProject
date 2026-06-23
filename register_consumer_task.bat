@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  KEDP – Kafka Consumer Task Registration
::  Run once as Administrator to start consumer.py on login.
::  Safe to re-run – /F overwrites the existing task.
:: ============================================================

:: ── Admin check ─────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This script must be run as Administrator.
    echo.
    echo Right-click register_consumer_task.bat ^> "Run as administrator"
    pause
    exit /b 1
)

:: ── Resolve consumer path ────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "CONSUMER=%SCRIPT_DIR%\consumer.py"

if not exist "%CONSUMER%" (
    echo [ERROR] consumer.py not found at: %CONSUMER%
    echo Make sure register_consumer_task.bat lives in the same folder as consumer.py.
    pause
    exit /b 1
)

:: ── Detect Python ────────────────────────────────────────────
:: Prefer pythonw.exe (silent background window) over python.exe
for /f "usebackq delims=" %%i in (`where pythonw 2^>nul`) do (
    set "PYTHON=%%i"
    goto :python_found
)
for /f "usebackq delims=" %%i in (`where python 2^>nul`) do (
    set "PYTHON=%%i"
    goto :python_found
)
echo [ERROR] Python not found in PATH.
echo Install Python and ensure it is added to PATH, then re-run.
pause
exit /b 1

:python_found
echo [OK] Python found at: %PYTHON%
echo [OK] Consumer script: %CONSUMER%

:: ── Register Task Scheduler job ──────────────────────────────
set "TASK_NAME=KEDP_KafkaConsumer"

:: /SC ONLOGON  – starts when the current user logs into Windows
:: /DELAY 0:30  – 30-second grace period for network to come up before Kafka connect
:: /RL HIGHEST  – elevated privileges for file system access
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "\"%PYTHON%\" \"%CONSUMER%\"" ^
    /SC ONLOGON ^
    /DELAY 0:30 ^
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
echo      Trigger : on user login (30-second startup delay)
echo      Script  : %CONSUMER%
echo      Python  : %PYTHON%
echo.
echo Useful commands:
echo   Query  : schtasks /Query /TN "%TASK_NAME%" /FO LIST /V
echo   Run now: schtasks /Run   /TN "%TASK_NAME%"
echo   Stop   : schtasks /End   /TN "%TASK_NAME%"
echo   Remove : schtasks /Delete /TN "%TASK_NAME%" /F
echo.

:: ── Offer to start immediately ───────────────────────────────
set /p "START=Start consumer now without waiting for next login? (Y/N): "
if /i "%START%"=="Y" (
    schtasks /Run /TN "%TASK_NAME%"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to start task. Try: schtasks /Run /TN "%TASK_NAME%"
    ) else (
        echo [OK] Consumer started. Check pipeline.log to confirm Kafka connection.
    )
)

echo.
pause
endlocal

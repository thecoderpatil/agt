@echo off
setlocal EnableDelayedExpansion
title AGT Scheduler Daemon - clientId=2
echo ============================================
echo   AGT Equities - Scheduler Daemon Boot
echo ============================================
echo.

cd /d "C:\AGT_Telegram_Bridge"

REM ============================================================
REM  NO git fetch/reset here. The bot (boot_desk.bat) owns the
REM  working-tree sync. Scheduler inherits whatever main the bot
REM  checked out. Running two competing hard-resets from two
REM  daemons would race and corrupt the tree.
REM ============================================================
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set _HEAD=%%i
echo [INFO] Working tree at !_HEAD!.
echo.

REM ============================================================
REM  Scheduler daemon cutover flag.
REM
REM  This launcher forces USE_SCHEDULER_DAEMON=1 for this process
REM  only (setlocal scope). The bot process reads its own env at
REM  its own startup; this does not affect it.
REM
REM  WARNING: during the 4-week observation window (before MR4
REM  flips the .env default to 1), running this launcher while
REM  the bot also has USE_SCHEDULER_DAEMON=0 will DOUBLE-EXECUTE
REM  the gated jobs (attested_sweeper, el_snapshot_writer,
REM  beta_cache_refresh, corporate_intel_refresh, flex_sync_eod,
REM  universe_monthly, conviction_weekly).
REM
REM  Operator discipline per .env.example:
REM    1. Set USE_SCHEDULER_DAEMON=1 in .env.
REM    2. Restart the bot (boot_desk.bat) so it skips gated jobs.
REM    3. Then run this launcher.
REM ============================================================
set "USE_SCHEDULER_DAEMON=1"
set "SCHEDULER_IB_CLIENT_ID=2"

if exist ".venv\Scripts\activate.bat" (
    echo [OK] Activating virtual environment...
    call .venv\Scripts\activate.bat
) else (
    echo [SKIP] No .venv found - using system Python.
)

echo [OK] Checking dependencies...
pip install --quiet --upgrade "apscheduler==3.11.2" "ib_async==2.1.0" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo [OK] apscheduler + ib_async verified.
) else (
    echo [WARN] pip install failed - scheduler may not start.
)

echo [OK] Launching agt_scheduler.py...
echo       USE_SCHEDULER_DAEMON=!USE_SCHEDULER_DAEMON!
echo       SCHEDULER_IB_CLIENT_ID=!SCHEDULER_IB_CLIENT_ID!
echo.
echo ----- smoke verify (from a second shell) -----
echo   python -c "import sqlite3; c=sqlite3.connect('file:agt_desk.db?mode=ro', uri=True); print(list(c.execute(\"SELECT daemon_name, last_beat_utc, pid, client_id FROM daemon_heartbeat WHERE daemon_name='agt_scheduler'\")))"
echo ----------------------------------------------
echo.
python agt_scheduler.py

echo.
echo ============================================
echo   Scheduler exited. Press any key to close.
echo ============================================
pause >nul
goto :eof

:halt
echo.
echo ============================================
echo   BOOT HALTED - Fatal error.
echo ============================================
pause >nul
exit /b 1

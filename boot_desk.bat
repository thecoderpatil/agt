@echo off
setlocal EnableDelayedExpansion
title AGT Desk Oracle - Telegram Bridge
echo ============================================
echo   AGT Equities - Desk Oracle Boot Sequence
echo ============================================
echo.

cd /d "C:\AGT_Telegram_Bridge"

REM ============================================================
REM  Auto-sync: always reset to origin/main before boot.
REM  Local edits are stale Cowork/API artifacts — GitLab is SSOT.
REM ============================================================
echo [SYNC] Fetching origin/main...

git rev-parse --is-inside-work-tree >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Not a git repo. Refusing to boot.
    goto :halt
)

git fetch origin main --quiet
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] git fetch origin main failed. Check network/credentials.
    goto :halt
)

REM Discard any local modifications and untracked build artifacts.
REM Tracked files reset to origin/main; untracked (.env, .gitlab-token,
REM agt_desk_cache/, .claude-cowork-notes.md, etc.) are preserved.
echo [SYNC] Resetting to origin/main...
git reset --hard origin/main --quiet
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] git reset --hard origin/main failed.
    goto :halt
)

for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do set _HEAD=%%i
echo [OK] Synced to origin/main ^(!_HEAD!^).
echo.

if exist ".venv\Scripts\activate.bat" (
    echo [OK] Activating virtual environment...
    call .venv\Scripts\activate.bat
) else (
    echo [SKIP] No .venv found - using system Python.
)

echo [OK] Checking dependencies...
pip install --quiet --upgrade "python-telegram-bot[job-queue]" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo [OK] python-telegram-bot[job-queue] verified.
) else (
    echo [WARN] pip install failed - JobQueue may not work.
)

echo [OK] Launching telegram_bot.py...
echo.
python telegram_bot.py

echo.
echo ============================================
echo   Process exited. Press any key to close.
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

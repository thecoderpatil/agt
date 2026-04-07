@echo off
title AGT Desk Oracle - Telegram Bridge
echo ============================================
echo   AGT Equities - Desk Oracle Boot Sequence
echo ============================================
echo.

cd /d "C:\AGT_Telegram_Bridge"

if exist ".venv\Scripts\activate.bat" (
    echo [OK] Activating virtual environment...
    call .venv\Scripts\activate.bat
) else (
    echo [SKIP] No .venv found - using system Python.
)

echo [OK] Checking dependencies...
pip install --quiet --upgrade "python-telegram-bot[job-queue]" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
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

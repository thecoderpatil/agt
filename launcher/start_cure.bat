@echo off
setlocal enabledelayedexpansion
title AGT Cure Console Launcher
cd /d C:\AGT_Telegram_Bridge

:: ── Verify Python is available ───────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Install Python 3.11+ and ensure it is in your system PATH.
    pause
    exit /b 1
)

:: ── Load .env for AGT_PAPER_MODE (display only) ─────────────────────
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "line=%%A"
        if not "!line:~0,1!"=="#" (
            if "%%A"=="AGT_PAPER_MODE" set "AGT_PAPER_MODE=%%B"
        )
    )
)

:: ── Check if deck already running on port 8787 ──────────────────────
set "DECK_RUNNING=0"
netstat -ano 2>nul | findstr /C:":8787 " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 set "DECK_RUNNING=1"

if "%DECK_RUNNING%"=="1" (
    echo Deck already running on port 8787.
    goto :read_token
)

:: ── Start deck in minimized window ───────────────────────────────────
echo Starting AGT Command Deck on port 8787...
if not exist logs mkdir logs
start /min "AGT Deck" cmd /c "cd /d C:\AGT_Telegram_Bridge && python -m agt_deck.main > logs\deck.log 2>&1"

:: ── Wait for port 8787 to accept connections (up to 10s) ────────────
set "ATTEMPTS=0"
:wait_port
if %ATTEMPTS% GEQ 10 (
    echo ERROR: Deck failed to start within 10 seconds.
    echo Check logs\deck.log for details.
    if exist logs\deck.log type logs\deck.log
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul 2>&1
netstat -ano 2>nul | findstr /C:":8787 " | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 (
    set /a ATTEMPTS+=1
    goto :wait_port
)
echo Deck started successfully.

:: ── Read token from .deck_token ──────────────────────────────────────
:read_token
set "ATTEMPTS=0"
:wait_token
if not exist .deck_token (
    if %ATTEMPTS% GEQ 10 (
        echo ERROR: .deck_token not found after 10 seconds.
        echo The deck may have failed to write the token file.
        echo Check logs\deck.log for details.
        pause
        exit /b 1
    )
    timeout /t 1 /nobreak >nul 2>&1
    set /a ATTEMPTS+=1
    goto :wait_token
)

set /p TOKEN=<.deck_token
if "%TOKEN%"=="" (
    echo ERROR: .deck_token file is empty.
    pause
    exit /b 1
)

:: ── Open browser ─────────────────────────────────────────────────────
if "%AGT_PAPER_MODE%"=="1" (
    echo [PAPER MODE] Opening Cure Console...
) else (
    echo Opening Cure Console...
)
start "" "http://127.0.0.1:8787/cure?t=%TOKEN%"
exit /b 0

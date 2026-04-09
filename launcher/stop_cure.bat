@echo off
setlocal enabledelayedexpansion
title AGT Cure Console — Stop
echo Stopping AGT Command Deck on port 8787...

:: ── Find PID listening on port 8787 ──────────────────────────────────
set "FOUND_PID="
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /C:":8787 " ^| findstr /C:"LISTENING"') do (
    set "FOUND_PID=%%P"
)

if "%FOUND_PID%"=="" (
    echo No process found listening on port 8787 — deck is not running.
    timeout /t 2 /nobreak >nul
    exit /b 0
)

echo Found deck process PID: %FOUND_PID%
taskkill /PID %FOUND_PID% /F >nul 2>&1
if errorlevel 1 (
    echo WARNING: Could not terminate PID %FOUND_PID%. May require admin rights.
    pause
    exit /b 1
)

echo Deck stopped (PID %FOUND_PID% terminated).
timeout /t 2 /nobreak >nul
exit /b 0

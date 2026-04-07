@echo off
echo ============================================
echo   Litestream Setup for AGT Equities
echo ============================================
echo.

REM Check if litestream binary exists
if exist "C:\AGT_Telegram_Bridge\litestream.exe" (
    echo [OK] Litestream binary found.
    C:\AGT_Telegram_Bridge\litestream.exe version 2>nul
    goto :start_replication
)

echo [INFO] Litestream binary not found.
echo [INFO] Download from: https://github.com/benbjohnson/litestream/releases
echo [INFO] Get: litestream-v0.3.13-windows-amd64.zip
echo [INFO] Extract litestream.exe to C:\AGT_Telegram_Bridge\
echo.
echo After downloading, run this script again.
pause
goto :eof

:start_replication
echo.
echo [INFO] Starting Litestream replication...
echo [INFO] Config: litestream.yml
echo [INFO] Source: agt_desk.db
echo [INFO] Target: R2 bucket agt-desk-backup
echo.
start /B C:\AGT_Telegram_Bridge\litestream.exe replicate -config C:\AGT_Telegram_Bridge\litestream.yml
echo [OK] Litestream started in background.
echo [INFO] To check status: litestream.exe generations -config litestream.yml C:\AGT_Telegram_Bridge\agt_desk.db
echo.
pause

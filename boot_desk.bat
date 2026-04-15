@echo off
setlocal EnableDelayedExpansion
title AGT Desk Oracle - Telegram Bridge
echo ============================================
echo   AGT Equities - Desk Oracle Boot Sequence
echo ============================================
echo.

cd /d "C:\AGT_Telegram_Bridge"

REM ============================================================
REM  Git guardrail: prod runs origin/main, clean tree, no drift.
REM  Refuses to boot on uncommitted mods or non-fast-forward state.
REM ============================================================
echo [CHECK] Verifying git state...

git rev-parse --is-inside-work-tree >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Not a git repo. Refusing to boot.
    goto :halt
)

git diff --quiet
set _DIRTY_UNSTAGED=!ERRORLEVEL!
git diff --cached --quiet
set _DIRTY_STAGED=!ERRORLEVEL!
if !_DIRTY_UNSTAGED! NEQ 0 goto :dirty_tree
if !_DIRTY_STAGED! NEQ 0 goto :dirty_tree

git fetch origin main --quiet
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] git fetch origin main failed. Check network/credentials.
    goto :halt
)

for /f "tokens=*" %%i in ('git rev-parse HEAD')          do set _LOCAL=%%i
for /f "tokens=*" %%i in ('git rev-parse origin/main')   do set _REMOTE=%%i

if "!_LOCAL!"=="!_REMOTE!" (
    echo [OK] HEAD matches origin/main ^(!_LOCAL:~0,8!^).
    goto :at_head
)

REM HEAD != origin/main. Only fast-forward if local is an ancestor of remote.
git merge-base --is-ancestor HEAD origin/main
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Local HEAD !_LOCAL:~0,8! diverged from origin/main !_REMOTE:~0,8!.
    echo [FAIL] Refusing to boot on non-fast-forward state.
    echo [FAIL] Reconcile: git log origin/main..HEAD  then  git reset --hard origin/main.
    goto :halt
)

echo [OK] Local behind origin/main. Fast-forwarding...
git merge --ff-only origin/main --quiet
if !ERRORLEVEL! NEQ 0 (
    echo [FAIL] Fast-forward failed despite ancestor check. Refusing to boot.
    goto :halt
)
echo [OK] Fast-forwarded to !_REMOTE:~0,8!.

:at_head
echo [OK] Git guardrail passed.
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

:dirty_tree
echo [FAIL] Uncommitted changes in working tree.
echo [FAIL] Prod boot requires a clean checkout of origin/main.
echo [FAIL] Review with 'git status'. Commit, stash, or restore before retry.
goto :halt

:halt
echo.
echo ============================================
echo   BOOT HALTED - Git guardrail tripped.
echo ============================================
pause >nul
exit /b 1

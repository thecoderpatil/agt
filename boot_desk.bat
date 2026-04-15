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

for /f "
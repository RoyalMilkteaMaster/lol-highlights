@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   LoL Highlights System - Supervised Workers
echo ============================================================
echo.

set "ACTION=install"
if not "%~1"=="" set "ACTION=%~1"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\worker_tasks.ps1" -Action "%ACTION%"
if errorlevel 1 (
    echo.
    echo   [FAIL] Worker task action failed: %ACTION%
    pause
    exit /b 1
)

echo.
echo   [OK] Worker task action completed: %ACTION%
echo   Dashboard: http://127.0.0.1:8765
exit /b 0

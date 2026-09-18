@echo off
REM ============================================================
REM  check_env.bat - LoL Highlights environment health check
REM
REM  Usage: run after setup / after moving machines / when things look off
REM    cd /d <project-directory>
REM    deployment\check_env.bat [--skip-gpu]
REM
REM  All logic lives in deployment\_check_env_runner.py (keep this file ASCII).
REM ============================================================

chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "KMP_DUPLICATE_LIB_OK=TRUE"

cd /d "%~dp0\.."

REM 1) LOL_ENV_PYTHON from .env (written by setup.ps1)
set "PY_EXE="
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="LOL_ENV_PYTHON" set "PY_EXE=%%B"
    )
)
if defined PY_EXE if exist "%PY_EXE%" goto :run

REM 2) common conda locations
for %%R in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3") do (
    if exist "%%~R\envs\lol-env\python.exe" (
        set "PY_EXE=%%~R\envs\lol-env\python.exe"
        goto :run
    )
)

echo.
echo FAIL: lol-env python.exe not found.
echo   Looked at: .env LOL_ENV_PYTHON, %%USERPROFILE%%\anaconda3, %%USERPROFILE%%\miniconda3, %%ProgramData%%\anaconda3, %%ProgramData%%\miniconda3
echo   Fix: run .\setup.ps1 (or: conda env create -f deployment\environment.yml)
exit /b 1

:run
"%PY_EXE%" "%~dp0\_check_env_runner.py" %*
exit /b %errorlevel%

@echo off
REM ============================================================
REM  check_env.bat - LoL Highlights 環境健診（7 項）
REM
REM  用法：搬機後 / 環境怪怪 / Windows Update 後跑一次
REM    cd /d "C:\Users\<user>\Claude code\lol-highlights"
REM    deployment\check_env.bat
REM
REM  實際健診邏輯在 deployment\_check_env_runner.py（避開 cmd 引號地獄）
REM ============================================================

chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "KMP_DUPLICATE_LIB_OK=TRUE"

cd /d "%~dp0\.."

REM 找 lol-env Python — 先試舊機路徑，找不到再 scan C:\Users\
set "PY_EXE=%USERPROFILE%\anaconda3\envs\lol-env\python.exe"
if not exist "%PY_EXE%" (
    for /d %%U in ("C:\Users\*") do (
        if exist "%%U\anaconda3\envs\lol-env\python.exe" set "PY_EXE=%%U\anaconda3\envs\lol-env\python.exe"
    )
)

if not exist "%PY_EXE%" (
    echo.
    echo FAIL: 找不到 lol-env Python。
    echo   試過：%%USERPROFILE%%\anaconda3\envs\lol-env\python.exe
    echo   還有：C:\Users\*\anaconda3\envs\lol-env\python.exe
    echo.
    echo 補救：在新機跑 conda env create -f deployment\environment.yml
    exit /b 1
)

"%PY_EXE%" "%~dp0\_check_env_runner.py"
exit /b %errorlevel%

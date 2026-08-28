@echo off
title VLM-imageArena - First-time setup
setlocal
cd /d "%~dp0"

REM Base python used to CREATE .venv. If you have a preferred interpreter,
REM put `set "PY=<path>"` in _local_paths.bat (gitignored) -- otherwise the
REM system python on PATH is used.
if exist "%~dp0_local_paths.bat" call "%~dp0_local_paths.bat"
if not defined PY set "PY=python"

echo ========================================
echo   VLM-imageArena -- first-time setup
echo ========================================
echo.

if exist ".venv\Scripts\python.exe" (
    echo [setup] .venv already exists -- refreshing dependencies.
) else (
    echo [setup] creating virtualenv .venv with: %PY%
    "%PY%" -m venv .venv
    if errorlevel 1 goto :err
)

echo [setup] installing requirements into .venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :err
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo.
echo [setup] DONE. Next steps:
echo.
echo   1. copy src\config_local.example.py  src\config_local.py
echo      then edit it: set JUDGE_SERVER_EXE, LLAMA_ARG_MODEL and
echo      LLAMA_ARG_MMPROJ to your llama-server build + model files.
echo   2. put candidate images into  in\
echo   3. start_judge.bat            (judge server, waits for health)
echo   4. start_arena.bat            (dashboard at http://127.0.0.1:8843)
echo.
pause
exit /b 0

:err
echo.
echo [setup] FAILED -- see the messages above.
pause
exit /b 1

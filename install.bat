@echo off
title VLM-imageArena - Install llama.cpp
setlocal
cd /d "%~dp0"

REM Python resolution: .venv (created by setup.bat) -> _local_paths.bat
REM override (gitignored) -> python on PATH. The installer helper is
REM stdlib-only, so any Python 3.8+ works even without a .venv.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0_local_paths.bat" call "%~dp0_local_paths.bat"
if not defined PY set "PY=python"

REM First run without .venv: bootstrap it via setup.bat first.
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [install] .venv missing -- running setup.bat first ...
    call "%~dp0setup.bat" --no-pause
    if errorlevel 1 goto :err
    if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
)

"%PY%" src\install_llamacpp.py %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:err
echo.
echo [install] setup.bat failed -- cannot continue.
pause
exit /b 1

@echo off
title VLM-imageArena - Judge Server (llama-server)
setlocal
cd /d "%~dp0"

REM Python resolution: .venv (created by setup.bat) -> _local_paths.bat
REM override (gitignored) -> python on PATH.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0_local_paths.bat" call "%~dp0_local_paths.bat"
if not defined PY set "PY=python"

"%PY%" -m src.judge_server %*
pause

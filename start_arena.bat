@echo off
REM VLM-imageArena launcher -- starts the arena + opens the dashboard launcher
REM (tourney picker) in your browser. Just double-click. No arguments needed.
setlocal
cd /d "%~dp0"

REM Python resolution: .venv (created by setup.bat) -> _local_paths.bat
REM override (gitignored) -> python on PATH.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0_local_paths.bat" call "%~dp0_local_paths.bat"
if not defined PY set "PY=python"

title VLM-imageArena - Dashboard
echo ========================================
echo   VLM-imageArena -- starting dashboard
echo   http://127.0.0.1:8843  (tourney launcher)
echo ========================================
echo.

"%PY%" -m src.arena %*

echo.
echo [arena] Process ended. Press any key to close.
pause >nul

@echo off
title VLM-imageArena - Update
setlocal
cd /d "%~dp0"

REM Python resolution: .venv (created by setup.bat) -> _local_paths.bat
REM override (gitignored) -> python on PATH.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0_local_paths.bat" call "%~dp0_local_paths.bat"
if not defined PY set "PY=python"

echo ========================================
echo   VLM-imageArena -- update
echo ========================================
echo.

echo [update] --- repository ---
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [update] not a git checkout -- skipping repository update.
    goto :llama
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo [update] no git remote configured -- skipping repository update.
    echo [update] hint: git remote add origin ^<your-repo-url^>
    goto :llama
)
set "DIRTY=0"
for /f %%c in ('git status --porcelain --untracked-files=no ^| find /c /v ""') do set "DIRTY=%%c"
if %DIRTY% GTR 0 (
    echo [update] %DIRTY% modified tracked file^(s^) -- skipping pull for safety:
    git status --short --untracked-files=no
    goto :llama
)
git pull --ff-only
if errorlevel 1 echo [update] git pull failed -- see messages above.

:llama
echo.
echo [update] --- llama.cpp (llama-server) ---
"%PY%" src\install_llamacpp.py --update
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

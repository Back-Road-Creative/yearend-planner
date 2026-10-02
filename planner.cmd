@echo off
rem Year-End Tax & Retirement Planner launcher (Windows).
rem Release zip: python\python.exe is the bundled interpreter.
rem Developer clone: falls back to uv + .venv.
setlocal
set "PLANNER_HOME=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" -m planner %*
  exit /b %errorlevel%
)
if not exist "%~dp0.venv\Scripts\python.exe" (
  where uv >nul 2>nul || powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  uv sync --frozen --directory "%~dp0" || exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -m planner %*
exit /b %errorlevel%

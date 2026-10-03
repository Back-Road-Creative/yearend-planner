@echo off
rem Year-End Tax & Retirement Planner launcher (Windows).
rem Release zip: python\python.exe is the bundled interpreter.
rem Developer clone: falls back to uv + .venv.
rem The final "exit /b %errorlevel%" must stay OUTSIDE any ( ) block: cmd
rem expands %errorlevel% when it parses the block, so inside one it is always 0
rem and a failing command would report success (windows_proof.ps1 step 0).
setlocal
set "PLANNER_HOME=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PLANNER_PY=%~dp0python\python.exe"
if exist "%PLANNER_PY%" goto :run
set "PLANNER_PY=%~dp0.venv\Scripts\python.exe"
if exist "%PLANNER_PY%" goto :run
where uv >nul 2>nul || powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync --frozen --directory "%~dp0" || exit /b 1
:run
"%PLANNER_PY%" -m planner %*
exit /b %errorlevel%

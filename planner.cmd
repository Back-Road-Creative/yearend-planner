@echo off
rem Year-End Tax & Retirement Planner launcher (Windows).
rem Release zip: python\python.exe is the bundled interpreter.
rem Developer clone: falls back to uv + .venv.
rem Every "exit /b %errorlevel%" must stay OUTSIDE any ( ) block: cmd
rem expands %errorlevel% when it parses the block, so inside one it is always 0
rem and a failing command would report success (windows_proof.ps1 step 0).
setlocal
set "PLANNER_HOME=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
rem Warn before anything downloads or runs when this folder sits under a sync
rem client (planner/paths.py CLOUD_SYNC_PARTS): planner init refuses it too.
echo "%~dp0" | findstr /i /c:"OneDrive" /c:"Dropbox" /c:"iCloudDrive" /c:"iCloud Drive" /c:"Google Drive" /c:"My Drive" >nul
if errorlevel 1 goto :not_synced
echo WARNING: this folder is inside a cloud-sync folder (OneDrive, Dropbox, iCloud Drive or Google Drive). 1>&2
echo The planner will not keep your data here. Move the folder somewhere local, 1>&2
echo for example C:\Planner, and run planner.cmd from there. 1>&2
:not_synced
set "PLANNER_PY=%~dp0python\python.exe"
if exist "%PLANNER_PY%" goto :run
set "PLANNER_PY=%~dp0.venv\Scripts\python.exe"
if exist "%PLANNER_PY%" goto :run
where uv >nul 2>nul || powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync --frozen --directory "%~dp0" || exit /b 1
:run
set "PLANNER_LAUNCHER=cmd"
"%PLANNER_PY%" -m planner %*
set "PLANNER_RC=%errorlevel%"
rem double-clicked (no arguments) and it failed: keep the window open to read why
if "%~1"=="" if %PLANNER_RC% NEQ 0 if %PLANNER_RC% NEQ 75 pause
if %PLANNER_RC% NEQ 75 exit /b %PLANNER_RC%
rem 75: an update or rollback waits for python.exe to let go of python\.
rem data\update\swap.cmd moves the folders, this file included, so the call
rem and the exit share one line: cmd has parsed both before the file moves.
call "%~dp0data\update\swap.cmd" %* & exit /b

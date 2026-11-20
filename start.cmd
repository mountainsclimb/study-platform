@echo off
rem Resolve bank dir via scripts/bank_dir.ps1 (reads releases/CURRENT pointer; baseline if absent).
setlocal
cd /d "%~dp0"
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bank_dir.ps1"`) do set "STUDY_BANK_DIR=%%i"
if defined STUDY_BANK_DIR echo bank dir: %STUDY_BANK_DIR%
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

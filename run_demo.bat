@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
set "PYTHONPATH=%CD%\src"
start "" ".venv\Scripts\pythonw.exe" -m clockout


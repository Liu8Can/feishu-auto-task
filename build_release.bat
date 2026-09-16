@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Please run setup.bat first.
  exit /b 1
)
if not exist "assets\app-icon.ico" (
  ".venv\Scripts\python.exe" "scripts\generate_icon.py"
  if errorlevel 1 exit /b 1
)
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --onedir ^
  --name "FeishuClockoutAssistant" ^
  --paths "src" ^
  --icon "assets\app-icon.ico" ^
  --version-file "packaging\version_info.txt" ^
  --collect-submodules pywinauto ^
  --hidden-import pythoncom ^
  --hidden-import win32timezone ^
  "launcher.py"
if errorlevel 1 exit /b 1
del /q "dist\FeishuClockoutAssistant\_internal\api-ms-win-*.dll" >nul 2>nul
copy /y "THIRD_PARTY_NOTICES.md" "dist\FeishuClockoutAssistant\THIRD_PARTY_NOTICES.md" >nul
echo Release created in dist\FeishuClockoutAssistant

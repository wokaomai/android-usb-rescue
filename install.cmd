@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python -m venv .venv
) else (
  py -3 -m venv .venv
)
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check .
if errorlevel 1 exit /b 1
echo Installation complete. Run start.cmd to open the application.

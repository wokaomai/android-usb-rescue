@echo off
setlocal
cd /d "%~dp0"
set "RESCUE_READY="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import android_usb_rescue.app" >nul 2>nul
  if not errorlevel 1 set "RESCUE_READY=1"
)
if not defined RESCUE_READY (
  call install.cmd
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
start "" ".venv\Scripts\pythonw.exe" -m android_usb_rescue %*

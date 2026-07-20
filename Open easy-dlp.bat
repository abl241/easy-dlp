@echo off
REM Double-click this file to open easy-dlp on Windows.
setlocal EnableExtensions
cd /d "%~dp0"

echo Starting easy-dlp...
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python was not found.
  echo.
  echo Install Python from https://www.python.org/downloads/windows/
  echo On the first installer screen, check "Add python.exe to PATH".
  echo Also keep "tcl/tk and IDLE" enabled.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First launch — creating a private Python environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
  echo.
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
  echo.
  echo Something went wrong. See messages above.
  pause
  exit /b 1
)

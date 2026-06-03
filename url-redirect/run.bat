@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 7790 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique"`) do (
  if not "%%P"=="" (
    powershell -NoProfile -Command "Stop-Process -Id %%P -Force -ErrorAction SilentlyContinue"
  )
)

set "PYTHON_EXE="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=py -3"

if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
  echo Python was not found. Please install Python 3 first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local virtual environment...
  %PYTHON_EXE% -m venv .venv
  if errorlevel 1 (
    echo Failed to create virtual environment.
    pause
    exit /b 1
  )
)

set "APP_PY=.venv\Scripts\python.exe"

echo Installing dependencies...
"%APP_PY%" -m pip install -r requirements.txt -i https://mirrors.tencent.com/pypi/simple --trusted-host mirrors.tencent.com
if errorlevel 1 (
  echo Dependency installation failed. Trying official PyPI...
  "%APP_PY%" -m pip install -r requirements.txt -i https://pypi.org/simple
)

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 1; Start-Process 'http://127.0.0.1:7790/'"

echo Starting local Pay URL generator...
echo URL: http://127.0.0.1:7790/
echo.
echo Keep this window open while using the page.
echo Press Ctrl+C to stop.
echo.

"%APP_PY%" server.py

echo.
echo Server stopped.
pause

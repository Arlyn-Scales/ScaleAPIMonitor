@echo off
rem Scale Push Monitor - double-click to start. Optional: run.bat 9000  (custom port)
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8080
where py >nul 2>nul
if %errorlevel%==0 (
  start "" http://localhost:%PORT%/
  py -3 scale_push_monitor.py --port %PORT%
) else (
  start "" http://localhost:%PORT%/
  python scale_push_monitor.py --port %PORT%
)
pause

@echo off
title DSD Route Planner - First-Time Setup

echo ============================================================
echo   DSD Route Planner - First-Time Setup
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing / updating dependencies...
python -m pip install -q -r "%~dp0requirements.txt"
echo Done.
echo.

echo Running customer master builder...
echo (This may take 1-2 minutes for 80,000+ history rows)
echo.
python "%~dp0build_customer_master.py"

echo.
echo ============================================================
echo   Setup complete!
echo   Next: open today.xlsx and assign zones in the Vehicles sheet.
echo   Then use run.bat each morning to generate daily routes.
echo ============================================================
echo.
pause

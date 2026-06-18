@echo off
title DSD Route Planner

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    pause
    exit /b 1
)

if not exist "today.xlsx" (
    echo ERROR: today.xlsx not found.
    echo Run _setup\run_setup.bat first to generate it.
    pause
    exit /b 1
)

if not exist "_setup\customer_master.xlsx" (
    echo ERROR: _setup\customer_master.xlsx not found.
    echo Run _setup\run_setup.bat first.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   DSD Route Planner
echo ============================================================
echo.
echo   1  -  SMART mode  [recommended for daily use]
echo          Zone-aware solver. Each vehicle is biased toward
echo          its own customers but the solver can reassign
echo          stops across zones for maximum efficiency.
echo          Vehicles with no zone (Float) serve as overflow.
echo.
echo   2  -  FREE mode  [diagnostic / backup]
echo          No zone preferences. Solver assigns stops to any
echo          vehicle purely by distance. Use when many vehicles
echo          are unavailable or to benchmark SMART routes.
echo.
echo ============================================================
echo.
set /p MODE="Select mode (1 or 2): "

if "%MODE%"=="1" (
    echo.
    echo Running SMART mode...
    echo.
    python route_planner.py territory
) else if "%MODE%"=="2" (
    echo.
    echo Running FREE mode...
    echo.
    python route_planner.py optimise
) else (
    echo.
    echo Invalid selection. Please enter 1 or 2.
)

echo.
pause

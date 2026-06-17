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
echo   1  -  OPTIMISE mode
echo          Best for light days. Solver assigns stops freely
echo          across all vehicles for maximum efficiency.
echo.
echo   2  -  TERRITORY mode
echo          Best for normal and heavy days. Each vehicle serves
echo          its own zone. Overflow redistributed automatically.
echo.
echo ============================================================
echo.
set /p MODE="Select mode (1 or 2): "

if "%MODE%"=="1" (
    echo.
    echo Running OPTIMISE mode...
    echo.
    python route_planner.py optimise
) else if "%MODE%"=="2" (
    echo.
    echo Running TERRITORY mode...
    echo.
    python route_planner.py territory
) else (
    echo.
    echo Invalid selection. Please enter 1 or 2.
)

echo.
pause

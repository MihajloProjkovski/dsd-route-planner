@echo off
title DSD Route Planner - First-Time Setup
cd /d "%~dp0.."

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing project dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

echo Starting DSD Route Planner...
python -m streamlit run app.py

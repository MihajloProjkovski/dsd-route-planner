@echo off
title DSD Route Planner
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo Dependencies are not installed.
    echo Run _setup\run_setup.bat once to install them.
    pause
    exit /b 1
)

python -m streamlit run app.py

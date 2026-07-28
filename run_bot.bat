@echo off
title Bridge Bot
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [INFO] No virtual environment found. Creating one...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

:loop
cls
echo Starting Bridge Bot...

:: I removed the hidden log redirect so you can see the matrix again!
python bridge.py

echo.
echo Bot crashed or stopped! Restarting in 10 seconds...
timeout /t 10
goto loop
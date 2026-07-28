@echo off
title Update Rubika Bridge
cd /d "%~dp0"

echo [%date% %time%] Checking for updates...
git pull

if %errorlevel% equ 0 (
    echo.
    echo ✅ Update complete!
) else (
    echo.
    echo ⚠️ Update failed. Check your internet connection.
)

echo.
echo Press any key to exit...
pause >nul

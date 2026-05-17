@echo off
REM Meeting Debrief Assistant — Windows launcher
REM Double-click this file to start the app

cd /d "%~dp0"

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Python not found. Install it from https://python.org
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

python launch.py %*
if %ERRORLEVEL% neq 0 pause
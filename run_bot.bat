@echo off
REM Trading Bot Launcher for Windows
REM Created: March 4, 2026

echo ================================================================================
echo   🤖 MULTI-SCRIPT TRADING BOT v2.0
echo   Starting...
echo ================================================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://www.python.org/
    pause
    exit /b 1
)

REM Check if dependencies are installed
echo 📦 Checking dependencies...
pip show pandas >nul 2>&1
if errorlevel 1 (
    echo 📥 Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ❌ Failed to install dependencies
        pause
        exit /b 1
    )
)

echo.
echo ✅ Dependencies OK
echo.
echo 🚀 Starting Trading Bot...
echo 🛑 Press Ctrl+C to stop
echo.

REM Run the bot
python trading_bot.py

echo.
echo 🛑 Bot stopped
pause

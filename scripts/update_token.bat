@echo off
REM Access Token Updater
REM This script helps you update your Upstox access token easily

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
pushd "%PROJECT_ROOT%"

echo ================================================================================
echo   UPSTOX ACCESS TOKEN UPDATER
echo ================================================================================
echo.

set /p NEW_TOKEN="Enter your new Upstox access token: "

if "%NEW_TOKEN%"=="" (
    echo ERROR: Token cannot be empty
    popd
    pause
    exit /b 1
)

echo.
echo Updating access token in trading_bot.py...

REM Backup the original file
copy trading_bot.py trading_bot.py.backup >nul 2>&1

REM Update the token using PowerShell
powershell -Command "(Get-Content trading_bot.py) -replace 'access_token\":\s*\"[^\"]*\"', 'access_token\": \"%NEW_TOKEN%\"' | Set-Content trading_bot.py"

if errorlevel 0 (
    echo.
    echo SUCCESS! Access token updated successfully.
    echo Backup saved as: trading_bot.py.backup
    echo.
    echo You can now run the bot with: run_bot.bat
) else (
    echo.
    echo ERROR: Failed to update token
    echo Please edit trading_bot.py manually.
)

echo.
popd
pause

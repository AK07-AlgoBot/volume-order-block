@echo off
setlocal

REM Default launcher: starts API + UI + bot via run_all.ps1
REM Use --bot-only to run only trading_bot.py.
if /I "%~1"=="--bot-only" (
    shift
    call "%~dp0scripts\run_bot.bat" %*
    exit /b %errorlevel%
)

powershell -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" %*
exit /b %errorlevel%

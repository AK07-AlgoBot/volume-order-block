@echo off
REM Single launcher: dashboard API + UI + trading bot (see start.ps1). Use -BotOnly for bot only.
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %errorlevel%

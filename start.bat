@echo off
REM AK07 launcher: engine + MCP + Streamlit cockpit + minimal API. Use -Mock for local visual demo.
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %errorlevel%

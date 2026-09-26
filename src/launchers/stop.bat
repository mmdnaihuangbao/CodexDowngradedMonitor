@echo off
chcp 65001 >nul
"%~dp0CodexDowngradedMonitor.exe" --stop %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%

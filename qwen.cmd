@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-qwen.ps1" %*
exit /b %ERRORLEVEL%

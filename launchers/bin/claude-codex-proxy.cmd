@echo off
setlocal
powershell -ExecutionPolicy Bypass -NoProfile -File "%USERPROFILE%\.claude-codex-proxy\scripts\claude-codex-proxy.ps1" %*
exit /b %errorlevel%

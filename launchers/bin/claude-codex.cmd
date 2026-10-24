@echo off
setlocal
set "SCRIPT=%~dp0claude-codex.ps1"
if "%CLAUDE_CODEX_NATIVE%"=="0" goto powershell
if not exist "%~dp0claude-codex-native.exe" goto powershell
"%~dp0claude-codex-native.exe" %*
exit /b %errorlevel%
:powershell
if not exist "%SCRIPT%" (
  echo [claude-codex] PowerShell launcher missing: %SCRIPT% 1>&2
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
exit /b %errorlevel%

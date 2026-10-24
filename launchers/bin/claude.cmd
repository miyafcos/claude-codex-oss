@echo off
setlocal
if /I "%~1"=="codex" (
  shift
  call "%USERPROFILE%\bin\claude-codex.cmd" %*
  exit /b %errorlevel%
)
set "CLAUDECODE="
set "CLAUDE_CODE_ENTRYPOINT="
"%USERPROFILE%\.local\bin\claude.exe" %*
exit /b %errorlevel%

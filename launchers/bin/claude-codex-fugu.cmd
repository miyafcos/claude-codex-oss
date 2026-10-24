@echo off
setlocal
@REM === claude-codex-fugu ===
@REM Shortcut for the Sakana Fugu backend. The main launcher maps this backend
@REM to the router endpoint and uses fugu-ultra as the default model.
set "CLAUDE_CODEX_BACKEND=fugu"
call "%USERPROFILE%\bin\claude-codex.cmd" %*
exit /b %errorlevel%

@echo off
setlocal
@REM === claude-codex-gpt ===
@REM 母艦を GPT-5.5 (OpenAI Codex OAuth backend) で明示起動するショートカット。
@REM 引数なしの claude-codex と同じ既定 backend だが、意図を明示したいとき・
@REM CLAUDE_CODEX_BACKEND が環境に残っているのを上書きしたいときに使う。
set "CLAUDE_CODEX_BACKEND=gpt"
call "%USERPROFILE%\bin\claude-codex.cmd" %*
exit /b %errorlevel%

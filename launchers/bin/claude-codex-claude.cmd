@echo off
setlocal
@REM === claude-codex-claude ===
@REM 母艦を Claude (Anthropic native, Claude.ai サブスク OAuth 経由) で起動する
@REM ショートカット。CLAUDE_CODEX_BACKEND=claude を立てて claude-codex.cmd に委譲
@REM する。launcher が ANTHROPIC_BASE_URL を http://127.0.0.1:4026/claude に向け、
@REM models.json の backends.claude (aliases/capabilities/defaultModel) を適用する。
@REM API キーは不要 (ccproxy claude_api + oauth_claude が ~/.claude/.credentials.json
@REM の Claude.ai サブスク OAuth を自動使用・自動 refresh する)。
set "CLAUDE_CODEX_BACKEND=claude"
call "%USERPROFILE%\bin\claude-codex.cmd" %*
exit /b %errorlevel%

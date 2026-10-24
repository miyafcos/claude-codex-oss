@echo off
setlocal
@REM === claude-codex-ultra ===
@REM One-command ultracode launcher. ultracode is session-only and is NOT a
@REM valid settings.json effortLevel value, so the durable way to start in it
@REM is to set CLAUDE_CODEX_ULTRACODE=1 and let claude-codex.cmd inject
@REM --settings ultracode.settings.json ({"ultracode": true}). ultracode sends
@REM xhigh per-message reasoning AND has Claude plan dynamic workflows
@REM (background subagent orchestration) for substantive tasks; those subagent
@REM calls still route through ccproxy to the Codex backend.
@REM
@REM Requires Claude Code CLI >= 2.1.154 (when /effort ultracode shipped). If
@REM ultracode does not appear, run scripts\healthcheck.ps1 -Quick to check the
@REM installed claude.exe version.
@REM
@REM Note: ultracode can fan out to many parallel subagents. claude-codex
@REM starts a local 4-worker ccproxy pool by default; raise/lower it with
@REM CCPROXY_PARALLEL_WORKERS when the machine or quota calls for it.
set "CLAUDE_CODEX_ULTRACODE=1"
call "%USERPROFILE%\bin\claude-codex.cmd" %*
exit /b %errorlevel%

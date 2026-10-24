"""claude-codex-hybrid gemini_cli backend plugin.

Wraps the gemini CLI (cached OAuth, no API key) as a ccproxy provider backend.
Reached only via the dispatcher (model name gemini-*). One-shot text only —
tool-use loops are not supported, so use it for reasoning/review/council
subagents, not as the agentic main loop.
"""

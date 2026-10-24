"""claude-codex-hybrid dispatcher plugin.

Routes a single Anthropic-format endpoint (/v1/messages at the proxy root) to
the right backend plugin (codex / claude_api / gemini_cli) based on the request
model name. This is what lets ultracode subagents mix GPT and Claude (and later
Gemini) just by changing the model name — Claude Code's native subagent/Workflow
fan-out routes through one ANTHROPIC_BASE_URL and the dispatcher splits it.

Installed to ~/.config/ccproxy/plugins/dispatcher/ (filesystem plugin, no
site-packages patch), so ccproxy-api upgrades never clobber it.
"""

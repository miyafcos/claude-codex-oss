"""claude-codex-hybrid fugu backend plugin.

Wraps the Sakana Fugu API (OpenAI chat-completions compatible, Bearer auth
via FUGU_API_KEY) as a ccproxy provider backend. Reached only via the
dispatcher (model name fugu*). Internal multi-agent orchestration means token
consumption is heavier than single-model providers — prefer fugu-ultra for
reasoning / council / final-gate tasks, not as the agentic main loop.
"""

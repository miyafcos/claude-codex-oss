#!/usr/bin/env python3
"""Validate explicit ccx child session/model/effort consistency."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
from typing import Any


def validate_ccx_call(tool_input: dict[str, Any], payload: dict[str, Any]) -> tuple[int, str, str]:
    """Validate only explicitly selected ccx session definitions."""
    try:
        from subagent_models import definition, digest, load_registry, loads, select
        if os.environ.get("CLAUDE_CODEX_SUBAGENT_CONTEXT") != "claude-codex":
            raise ValueError("ccx children are restricted to claude-codex")
        path = Path(os.environ["CLAUDE_CODEX_SUBAGENT_SESSION"]).resolve()
        config = Path(os.environ["CLAUDE_CONFIG_DIR"]).resolve()
        if config.name not in {"config", "config-slim"} or config.parent.name != ".claude-codex":
            raise ValueError("Not a claude-codex config directory")
        if path.parent != config.parent / "state" / "subagent-sessions":
            raise ValueError("Unexpected session-record directory")
        record = loads(path.read_text(encoding="utf-8"))
        if record.get("schemaVersion") != 1 or record.get("sessionId") != payload.get("session_id"):
            raise ValueError("Child session ID mismatch")
        if Path(record["configDir"]).resolve() != config:
            raise ValueError("Child config directory mismatch")
        # Pin to this source checkout, or to the deployed config's own registry.
        repo = Path(__file__).resolve().parent.parent
        source_models = repo / "runtime" / "models.json"
        source_registry = repo / "runtime" / "claude-codex" / "subagent-models.json"
        if not source_models.is_file() or Path(record["modelsPath"]).resolve() != source_models.resolve():
            source_models = config / "models.json"
            source_registry = config / "subagent-models.json"
        if (Path(record["modelsPath"]).resolve() != source_models.resolve()
                or Path(record["registryPath"]).resolve() != source_registry.resolve()):
            raise ValueError("Untrusted model/registry path")
        registry = load_registry(source_models, source_registry)
        if record["modelsSha256"] != registry["modelsSha256"] or record["registrySha256"] != digest(registry):
            raise ValueError("Child model registry changed")
        agents = loads(os.environ["CLAUDE_CODEX_SUBAGENT_AGENTS"])
        if agents != record["agents"] or digest(agents) != record["agentsSha256"]:
            raise ValueError("Final CLI definitions do not match the session")
        selected = record["selected"]
        if not isinstance(selected, dict) or not selected:
            raise ValueError("No explicit child selection")
        reserved = {name for name in agents if name.lower().startswith("ccx-")}
        if reserved != set(selected):
            raise ValueError("Selected/CLI definition set mismatch")
        for name, selection in selected.items():
            if not isinstance(selection.get("defaultedEffort"), bool):
                raise ValueError("Invalid default-effort disclosure")
            expected = select(registry, selection["name"] + "=" + selection["effort"])
            expected["defaultedEffort"] = selection["defaultedEffort"]
            if selection != expected or agents.get(name) != expected["definition"] or name != expected["agentType"]:
                raise ValueError("Selected definition/effort mismatch")
        name = str(tool_input.get("subagent_type") or "")
        selection = selected[name]
        if tool_input.get("model") is not None:
            raise ValueError("ccx child model is fixed; family/model overrides are forbidden")
        if tool_input.get("fork_context") or tool_input.get("fork"):
            raise ValueError("Parent-history fork is forbidden")
        effort = tool_input.get("effort")
        if effort is not None and effort != selection["effort"]:
            raise ValueError("Child effort differs from its definition; resolve a Workflow selection")
        return 0, "", "allow_ccx_explicit"
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        return 2, "agent-model-guard: ccx session/definition/model/effort check failed; use a fresh validated selection.", "deny_ccx"


def decide(tool_input: dict[str, Any], payload: dict[str, Any] | None = None) -> tuple[int, str, str]:
    """Ordinary agent choices are user-owned; ccx sessions cannot fork history."""
    subagent_type = str(tool_input.get("subagent_type") or "")
    if subagent_type.lower().startswith("ccx-"):
        return validate_ccx_call(tool_input, payload or {})
    if (os.environ.get("CLAUDE_CODEX_SUBAGENT_CONTEXT") == "claude-codex"
            and (subagent_type.lower() == "fork" or tool_input.get("fork_context") or tool_input.get("fork"))):
        return 2, "agent-model-guard: parent-history fork is forbidden for explicit child sessions.", "deny_ccx_fork"
    return 0, "", ""


def main(argv: list[str]) -> int:
    if "--ccx-only" in argv and os.environ.get("CLAUDE_CODEX_SUBAGENT_CONTEXT") != "claude-codex":
        return 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        tool_name = str(payload.get("tool_name") or "")
        if tool_name and tool_name not in {"Agent", "Task"}:
            return 0
        tool_input = payload.get("tool_input") or {}
        code, message, reason = decide(tool_input, payload)
        if os.environ.get("CLAUDE_CODEX_SUBAGENT_CONTEXT") == "claude-codex":
            log = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "logs/ccx_guard.jsonl"
            log.parent.mkdir(parents=True, exist_ok=True)
            metadata = {"session_id": payload.get("session_id"), "tool_name": tool_name,
                        "subagent_type": tool_input.get("subagent_type"), "model": tool_input.get("model"),
                        "effort": tool_input.get("effort"), "fork": bool(tool_input.get("fork") or tool_input.get("fork_context")),
                        "exit_code": code, "reason": reason, "python": sys.executable,
                        "guard": str(Path(__file__).resolve()), "body_logged": False}
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(metadata, ensure_ascii=True) + "\n")
        if code == 2:
            print(message, file=sys.stderr)
        return code
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print("agent-model-guard: ccx session/definition/model/effort check failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

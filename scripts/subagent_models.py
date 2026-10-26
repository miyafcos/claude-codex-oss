"""Pure explicit-child selection; no credentials, network, or implicit fallback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid

EFFORTS = ("low", "medium", "high", "xhigh", "max")
NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
AGENT_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")


def _unique_object(pairs):
    result, seen = {}, set()
    for key, value in pairs:
        if key.casefold() in seen:
            raise ValueError("Duplicate JSON property (case-insensitive)")
        seen.add(key.casefold())
        result[key] = value
    return result


def loads(text: str) -> Any:
    def invalid_constant(_):
        raise ValueError("Non-finite JSON numbers are invalid")
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def definition(entry: dict, effort: str) -> dict:
    prompt = (
        "Work only on the user's explicitly requested model-specific subtask. "
        "Accept only non-confidential, self-contained task context. Never fork or request parent history. "
        "Do not choose another provider, recursively delegate, or silently change effort."
    )
    return {
        "description": f"Explicit user-requested {entry['model']} child; fixed effort={effort}. "
                       "Do not select automatically. Use Workflow for a different effort.",
        "prompt": prompt, "model": entry["model"], "effort": effort,
    }


def build_registry(models: dict) -> dict:
    if not isinstance(models, dict):
        raise ValueError("models.json must be an object")
    entries = models.get("subagentModels")
    if not isinstance(entries, list) or not entries:
        raise ValueError("subagentModels must be a nonempty list")
    backends = models.get("backends", {})
    if not isinstance(backends, dict):
        raise ValueError("Model backends must be an object")
    registered = {}
    for backend in ("gpt", "claude", "nanogpt"):
        profile = backends.get(backend, {})
        if not isinstance(profile, dict) or not isinstance(profile.get("availableModels", []), list):
            raise ValueError("Invalid model backend profile")
        for model in profile.get("availableModels", []):
            if not isinstance(model, str):
                raise ValueError("Invalid registered model ID")
            registered[model] = backend
    output, names, ids = [], set(), set()
    for item in entries:
        if not isinstance(item, dict) or set(item) - {"name", "model", "efforts", "defaultEffort"}:
            raise ValueError("Invalid subagentModels entry")
        name, model, efforts = item.get("name"), item.get("model"), item.get("efforts")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ValueError("Invalid/duplicate child short name")
        if not isinstance(model, str) or model not in registered or model in ids:
            raise ValueError("Child model must be uniquely registered in GPT/Claude/NanoGPT")
        if (not isinstance(efforts, list) or not efforts or any(not isinstance(e, str) or e not in EFFORTS for e in efforts)
                or len(set(efforts)) != len(efforts) or item.get("defaultEffort") not in efforts):
            raise ValueError("Invalid supported/default child effort")
        entry = {**item, "agentType": "ccx-" + name, "backend": registered[model]}
        entry["definition"] = definition(entry, item["defaultEffort"])
        output.append(entry)
        names.add(name)
        ids.add(model)
    all_models = sorted({model for b in backends.values() if isinstance(b, dict)
                         for model in b.get("availableModels", []) if isinstance(model, str)})
    return {"schemaVersion": 1, "modelsSha256": digest(models), "registeredModels": all_models, "models": output}


def load_registry(models_path: Path, registry_path: Path) -> dict:
    models = loads(models_path.read_text(encoding="utf-8-sig"))
    expected = build_registry(models)
    actual = loads(registry_path.read_text(encoding="utf-8-sig"))
    if actual != expected:
        raise ValueError("Generated child registry does not match models.json; regenerate before use")
    return actual


def select(registry: dict, value: str, env=None) -> dict:
    env = os.environ if env is None else env
    name, separator, effort = value.partition("=")
    if name == "glm":
        raise ValueError("GLM is ambiguous; ask once: glm-flash, glm-thinking, glm-uncensored, or glm-apex?")
    entry = next((e for e in registry["models"] if name in (e["name"], e["model"], e["agentType"])), None)
    if entry is None:
        raise ValueError("Unknown child model; use --list-subagent-models")
    effort = effort if separator else entry["defaultEffort"]
    if effort not in entry["efforts"]:
        raise ValueError(f"Unsupported effort for {entry['name']}; supported: {','.join(entry['efforts'])}")
    forced = env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "")
    if forced.lower() not in ("", "0", "false", "off", "no"):
        # Real CLI 2.1.310 treats FORCE as a boolean and uses SUBAGENT_MODEL.
        if env.get("CLAUDE_CODE_SUBAGENT_MODEL") != entry["model"]:
            raise ValueError("CLAUDE_CODE_SUBAGENT_MODEL_FORCE conflicts with child selection")
    global_effort = env.get("CLAUDE_CODE_EFFORT_LEVEL", "")
    if global_effort and global_effort != effort:
        raise ValueError("CLAUDE_CODE_EFFORT_LEVEL conflicts with child effort")
    return {"name": entry["name"], "agentType": entry["agentType"], "model": entry["model"],
            "effort": effort, "defaultedEffort": not bool(separator),
            "definition": definition(entry, effort)}


def _manual_agents(raw: str, registry: dict) -> dict:
    value = loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--agents must be a JSON object")
    for name, item in value.items():
        if not AGENT_NAME.fullmatch(name) or name.lower().startswith("ccx-"):
            raise ValueError("Invalid/reserved manual --agents name")
        if not isinstance(item, dict) or any(
            not isinstance(item.get(k), str) or not item[k].strip() for k in ("description", "prompt")
        ):
            raise ValueError("Manual --agents definitions require description and prompt strings")
        if "model" in item and (not isinstance(item["model"], str) or not item["model"].strip()):
            raise ValueError("Invalid manual agent model")
        known = set(registry["registeredModels"]) | {"sonnet", "opus", "haiku", "fable", "inherit"}
        if "model" in item and item["model"] not in known:
            raise ValueError("Unknown manual agent model")
        if "effort" in item and item["effort"] not in EFFORTS:
            raise ValueError("Invalid manual agent effort")
        entry = next((e for e in registry["models"] if item.get("model") == e["model"]), None)
        if entry and "effort" in item and item["effort"] not in entry["efforts"]:
            raise ValueError("Unsupported manual agent effort for registered model")
        for key in ("tools", "disallowedTools", "skills"):
            if key in item and (not isinstance(item[key], list) or any(not isinstance(v, str) for v in item[key])):
                raise ValueError("Invalid manual agent tool/skill list")
    return value


def child_guard_hook() -> dict:
    """Exec form avoids shell quoting and resolves the guard from the active HOME."""
    bootstrap = (
        "import os,sys,runpy;from pathlib import Path;"
        "sys.exit(0) if os.environ.get('CLAUDE_CODEX_SUBAGENT_CONTEXT')!='claude-codex' else None;"
        "p=Path(os.environ['CLAUDE_CONFIG_DIR']).parent.parent/'.claude/scripts/agent-model-guard.py';"
        "sys.path.insert(0,str(p.parent));sys.argv=['agent-model-guard.py','--ccx-only'];"
        "runpy.run_path(str(p),run_name='__main__')"
    )
    return {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": "python.exe",
            "args": ["-B", "-X", "utf8", "-c", bootstrap], "timeout": 10, "onFailure": "block"}]}


def resolve_parent(models: dict, cli_args: list[str], env: dict) -> dict:
    """Validate the launcher profile and actual dispatcher route before writing."""
    backend = env.get("CLAUDE_CODEX_BACKEND") or models.get("defaultBackend") or "gpt"
    model = None
    index = 0
    while index < len(cli_args):
        arg = cli_args[index]
        if arg == "--":
            break
        key, equals, value = arg.partition("=")
        if key.lower() in {"--backend", "--model"}:
            if not equals:
                index += 1
                if index >= len(cli_args) or cli_args[index].startswith("-"):
                    raise ValueError(key + " requires a value")
                value = cli_args[index]
            if not value:
                raise ValueError(key + " requires a value")
            if key.lower() == "--backend":
                backend = value
            else:
                model = value
        # JSON option values must not be mistaken for launcher flags.
        elif key.lower() in {"--agents", "--settings", "--session-id"} and not equals:
            index += 1
        index += 1
    backend = backend.lower()
    profiles = models.get("backends", {})
    if backend not in {"gpt", "claude", "nanogpt"} or backend not in profiles:
        raise ValueError("Tool-using child presets require the existing GPT/Claude/NanoGPT router profile")
    profile = profiles[backend]
    model = model or profile.get("defaultModel")
    if model == "default":
        model = profile.get("defaultModel")
    model = profile.get("aliases", {}).get(model, model)
    if not isinstance(model, str) or not model:
        raise ValueError("The effective parent model is missing")
    route = None
    if model.lower().startswith("anthropic/gateway/"):
        family, separator, upstream = model[len("anthropic/gateway/"):].partition("/")
        route = {"codex": "gpt", "claude": "claude", "nanogpt": "nanogpt"}.get(family.lower())
        if not route or not separator or not upstream:
            raise ValueError("Unsupported explicit parent gateway route")
        model = ("nanogpt/" + upstream.removeprefix("nanogpt/")) if route == "nanogpt" else upstream
    else:
        lowered = model.lower()
        route = ("claude" if lowered.startswith("claude") else
                 "nanogpt" if lowered.startswith("nanogpt/") else
                 "restricted" if lowered.startswith(("fugu", "gemini", "fcc-", "open_router/", "ollama/")) else "gpt")
    if route not in {"gpt", "claude", "nanogpt"} or model not in profiles[route].get("availableModels", []):
        raise ValueError("Unsupported or unregistered effective parent model/route")
    return {"backend": backend, "model": model, "route": route}


def prepare(registry: dict, selections: list[str], cli_args: list[str], *,
            models_path: Path, registry_path: Path, config_dir: Path, session_dir: Path, env=None) -> dict:
    env = dict(os.environ if env is None else env)
    if env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "").lower() not in ("", "0", "false", "off", "no"):
        raise ValueError("CLAUDE_CODE_SUBAGENT_MODEL_FORCE must be unset for launcher child presets")
    if not isinstance(selections, list) or not selections or any(not isinstance(v, str) for v in selections):
        raise ValueError("At least one explicit child selection is required")
    if not isinstance(cli_args, list) or any(not isinstance(v, str) for v in cli_args):
        raise ValueError("Invalid launcher argument list")
    filtered, manual, session_id, index = [], {}, None, 0
    positional_tail = []
    models = loads(models_path.read_text(encoding="utf-8-sig"))
    if build_registry(models) != registry:
        raise ValueError("Parent model configuration does not match the child registry")
    parent = resolve_parent(models, cli_args, env)
    while index < len(cli_args):
        arg = cli_args[index]
        if arg == "--":
            positional_tail = cli_args[index:]
            break
        key, equals, val = arg.partition("=")
        if key.lower() in ("--resume", "--continue", "--fork-session", "-r", "-c") or (
                arg.startswith(("-r", "-c")) and not arg.startswith("--")):
            raise ValueError("Explicit child definitions require a fresh session; use Workflow in an existing seat")
        if key.lower() in ("--agents", "--session-id"):
            if not equals:
                index += 1
                if index >= len(cli_args):
                    raise ValueError(key + " requires a value")
                val = cli_args[index]
            if key.lower() == "--agents":
                for name, item in _manual_agents(val, registry).items():
                    if name.casefold() in {k.casefold() for k in manual}:
                        raise ValueError("Conflicting manual agent definitions")
                    manual[name] = item
            else:
                if session_id is not None:
                    raise ValueError("Duplicate --session-id")
                session_id = str(uuid.UUID(val))
        else:
            filtered.append(arg)
        index += 1
    chosen = {}
    for value in selections:
        item = select(registry, value, env)
        if item["agentType"] in chosen:
            raise ValueError("Duplicate child selection")
        chosen[item["agentType"]] = item
    session_id = session_id or str(uuid.uuid4())
    agents = {**manual, **{name: item["definition"] for name, item in chosen.items()}}
    agents_json = json.dumps(agents, ensure_ascii=True, separators=(",", ":"))
    record = {
        "schemaVersion": 1, "sessionId": session_id, "configDir": str(config_dir.resolve()),
        "modelsPath": str(models_path.resolve()), "modelsSha256": registry["modelsSha256"],
        "registryPath": str(registry_path.resolve()), "registrySha256": digest(registry),
        "agents": agents, "agentsSha256": digest(agents), "selected": chosen,
        "parent": parent,
    }
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / (session_id + "-" + uuid.uuid4().hex + ".json")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, indent=2, ensure_ascii=True) + "\n")
    assert loads(path.read_text(encoding="utf-8")) == record
    return {
        "args": [*filtered, "--session-id", session_id, "--agents", agents_json, *positional_tail],
        "sessionPath": str(path), "agentsJson": agents_json, "selected": list(chosen.values()),
        "parent": parent,
    }

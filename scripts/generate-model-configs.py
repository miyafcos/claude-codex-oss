"""Generate model-related config files from runtime/models.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subagent_models import build_registry, child_guard_hook


REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_JSON = REPO_ROOT / "runtime" / "models.json"
SETTINGS_JSON = REPO_ROOT / "runtime" / "claude-codex" / "settings.json"
MODEL_DEFAULTS_PY = REPO_ROOT / "patches" / "ccproxy" / "plugins" / "codex" / "model_defaults.py"
PROXY_CONFIG_TOML = REPO_ROOT / "runtime" / "claude-codex-proxy" / "config.toml"
REASONING_MODELS_PY = REPO_ROOT / "patches" / "ccproxy" / "llms" / "formatters" / "anthropic_to_openai" / "reasoning_models.py"
SUBAGENT_MODELS_JSON = REPO_ROOT / "runtime" / "claude-codex" / "subagent-models.json"


def _load_models(path: Path = MODELS_JSON) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["defaultModel", "availableModels", "modelCards", "mappingRules"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"{path} missing required keys: {', '.join(missing)}")
    return data


def _settings_payload(models: dict[str, Any]) -> dict[str, Any]:
    settings = {
        "effortLevel": "xhigh",
        "language": "japanese",
        "permissions": {
            "defaultMode": "default",
        },
        "hooks": {"PreToolUse": [child_guard_hook()]},
    }
    if "modelPicker" in models:
        settings["modelPicker"] = models["modelPicker"]
    return settings


def render_settings(models: dict[str, Any]) -> str:
    return json.dumps(_settings_payload(models), indent=2, ensure_ascii=False) + "\n"


def render_model_defaults(models: dict[str, Any]) -> str:
    cards = []
    for card in models["modelCards"]:
        cards.append(
            "    ModelCard(\n"
            f"        id={card['id']!r},\n"
            f"        created={int(card['created'])},\n"
            f"        owned_by={card.get('owned_by', 'openai')!r},\n"
            "        permission=[],\n"
            f"        root={card['id']!r},\n"
            "        parent=None,\n"
            "    ),"
        )
    mappings = []
    for rule in models["mappingRules"]:
        mappings.append(
            "    ModelMappingRule("
            f"match={rule['match']!r}, target={rule['target']!r}, kind={rule['kind']!r}"
            "),"
        )
    return (
        '"""Default model metadata and mapping rules for the Codex provider.\n\n'
        "Generated from runtime/models.json by scripts/generate-model-configs.py.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from ccproxy.models.provider import ModelCard, ModelMappingRule\n\n\n"
        "DEFAULT_CODEX_MODEL_CARDS: list[ModelCard] = [\n"
        + "\n".join(cards)
        + "\n]\n\n\n"
        "DEFAULT_CODEX_MODEL_MAPPINGS: list[ModelMappingRule] = [\n"
        + "\n".join(mappings)
        + "\n]\n\n\n"
        "__all__ = [\n"
        '    "DEFAULT_CODEX_MODEL_CARDS",\n'
        '    "DEFAULT_CODEX_MODEL_MAPPINGS",\n'
        "]\n"
    )


def render_proxy_config(models: dict[str, Any]) -> str:
    # schemaVersion 2: backends.claude が定義されていれば Claude (Anthropic native)
    # backend を有効化する。auth は oauth_claude の Claude.ai サブスク OAuth
    # (API キー不要)。backends が無い旧 models.json では codex 単独 (後方互換)。
    backends = models.get("backends", {})
    has_claude = "claude" in backends
    enabled = ["codex", "oauth_codex"]
    if has_claude:
        enabled.extend(["claude_api", "oauth_claude"])
    # dispatcher: backends があれば有効化。model 名で codex/claude_api/gemini_cli に
    # 振り分ける router-only plugin (~/.config/ccproxy/plugins/dispatcher/)。
    # 母艦もサブエージェントも /router 経由になり、混在 fan-out が可能になる。
    if backends:
        enabled.append("dispatcher")
    # gemini_cli: backends.gemini があれば有効化 (gemini CLI ラップ backend)
    if "gemini" in backends:
        enabled.append("gemini_cli")
    # fugu: backends.fugu があれば有効化 (Sakana Fugu HTTP backend, OpenAI
    # chat-completions wire, Bearer via FUGU_API_KEY env var)
    if "fugu" in backends:
        enabled.append("fugu")
    # fcc: local Free Claude Code gateway. Its model catalog is dynamic, so
    # models are fetched by the adapter instead of being duplicated here.
    if "fcc" in backends:
        enabled.append("fcc")
    if "nanogpt" in backends:
        enabled.append("nanogpt")
    enabled_literal = "[" + ", ".join(f'"{name}"' for name in enabled) + "]"
    lines = [
        f"enabled_plugins = {enabled_literal}",
        "",
        "[logging]",
        'level = "WARNING"',
        "",
        "[binary]",
        "package_manager_only = false",
        "",
        "[plugins.codex]",
        'detection_home_mode = "home"',
        "",
    ]
    for rule in models["mappingRules"]:
        lines.extend(
            [
                "[[plugins.codex.model_mappings]]",
                f'match = "{rule["match"]}"',
                f'target = "{rule["target"]}"',
                f'kind = "{rule["kind"]}"',
                "",
            ]
        )
    for card in models["modelCards"]:
        owner = card.get("owned_by", "openai")
        lines.extend(
            [
                "[[plugins.codex.models_endpoint]]",
                f'id = "{card["id"]}"',
                f'root = "{card["id"]}"',
                f'owned_by = "{owner}"',
                "permission = []",
                "",
            ]
        )
    if has_claude:
        claude = backends["claude"]
        lines.extend(
            [
                "# === Claude (Anthropic native) backend ===",
                "# Enabled because models.json has backends.claude. Auth via oauth_claude",
                "# (Claude.ai subscription OAuth in ~/.claude/.credentials.json / OS keychain,",
                "# no API key, auto-refresh). base_url defaults to https://api.anthropic.com.",
                "# Model names pass through (launcher sends claude-* IDs via ANTHROPIC_*_MODEL).",
                "[plugins.claude_api]",
                "",
            ]
        )
        for model_id in claude.get("availableModels", []):
            lines.extend(
                [
                    "[[plugins.claude_api.models_endpoint]]",
                    f'id = "{model_id}"',
                    f'root = "{model_id}"',
                    'owned_by = "anthropic"',
                    "permission = []",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _validate_backend_config(models: dict[str, Any]) -> list[str]:
    """Validate per-backend / per-profile model references.

    Every aliases.* / defaultModel / subagentModel target in backends.* and
    profiles.* must exist in that scope's availableModels and must not be in
    disallowedAliasTargets. The launcher consumes these at runtime but does not
    validate them, so a typo would silently propagate; catch it here (the single
    source-of-truth tool).
    """
    errors: list[str] = []
    disallowed = set(models.get("disallowedAliasTargets", []) or [])
    backends = models.get("backends", {})
    profiles = models.get("profiles", {})
    if not isinstance(backends, dict):
        errors.append("backends must be an object")
        backends = {}
    if not isinstance(profiles, dict):
        errors.append("profiles must be an object")
        profiles = {}

    def _check_scope(label: str, scope: dict[str, Any]) -> None:
        avail = set(scope.get("availableModels", []) or [])
        if not avail:
            return
        targets: dict[str, Any] = {}
        for role, target in (scope.get("aliases", {}) or {}).items():
            targets[f"aliases.{role}"] = target
        for key in ("defaultModel", "subagentModel"):
            if scope.get(key):
                targets[key] = scope[key]
        for where, target in targets.items():
            if target in disallowed:
                errors.append(f"{label}.{where} -> {target} is in disallowedAliasTargets")
            elif target not in avail:
                errors.append(f"{label}.{where} -> {target} not in {label}.availableModels")

    for name, backend in backends.items():
        if isinstance(backend, dict):
            _check_scope(f"backends.{name}", backend)
        else:
            errors.append(f"backends.{name} must be an object")
    for name, profile in profiles.items():
        if isinstance(profile, dict):
            _check_scope(f"profiles.{name}", profile)
        else:
            errors.append(f"profiles.{name} must be an object")
    return errors


def expected_files(models: dict[str, Any]) -> dict[Path, str]:
    gpt = models.get("backends", {}).get("gpt", models)
    reasoning_models = tuple(gpt["availableModels"]) if "effort" in gpt.get("capabilities", []) else ()
    max_models = reasoning_models if "max_effort" in gpt.get("capabilities", []) else ()
    return {
        SUBAGENT_MODELS_JSON: json.dumps(build_registry(models), indent=2, ensure_ascii=False) + "\n",
        REASONING_MODELS_PY: (
            '"""Generated from runtime/models.json; do not edit by hand."""\n\n'
            f"REASONING_MODELS = {reasoning_models!r}\n"
            f"MAX_EFFORT_MODELS = {max_models!r}\n"
        ),
        SETTINGS_JSON: render_settings(models),
        SETTINGS_JSON.with_name("fcc-picker.settings.json"): json.dumps(
            {"modelPicker": {"options": [], "replaceBuiltInOptions": False}}, indent=2
        ) + "\n",
        SETTINGS_JSON.with_name("fcc-ultracode.settings.json"): json.dumps(
            {"ultracode": True, "modelPicker": {"options": [], "replaceBuiltInOptions": False}},
            indent=2,
        ) + "\n",
        MODEL_DEFAULTS_PY: render_model_defaults(models),
        PROXY_CONFIG_TOML: render_proxy_config(models),
    }


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n")


def check(models: dict[str, Any]) -> list[Path]:
    drifted: list[Path] = []
    for path, expected in expected_files(models).items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if _normalized(current) != _normalized(expected):
            drifted.append(path)
    return drifted


def write(models: dict[str, Any]) -> None:
    for path, expected in expected_files(models).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or _normalized(path.read_text(encoding="utf-8")) != _normalized(expected):
            path.write_text(expected, encoding="utf-8", newline="\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate claude-codex model config files from runtime/models.json."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when generated files drift")
    mode.add_argument("--write", action="store_true", help="rewrite generated files")
    parser.add_argument("--models-json", type=Path, default=MODELS_JSON)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    models = _load_models(args.models_json)
    backend_errors = _validate_backend_config(models)
    if backend_errors:
        print("backend/profile config errors:", file=sys.stderr)
        for err in backend_errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    if args.write:
        write(models)
        print("generated model config files")
        return 0

    drifted = check(models)
    if drifted:
        print("model config drift detected:", file=sys.stderr)
        for path in drifted:
            print(f"  {path}", file=sys.stderr)
        print("Run: uv run python scripts/generate-model-configs.py --write", file=sys.stderr)
        return 1
    print("model config files are in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())

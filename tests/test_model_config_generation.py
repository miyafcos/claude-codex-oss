"""Tests for runtime/models.json as the model config source of truth."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "generate-model-configs.py"
_MODELS_JSON = _REPO_ROOT / "runtime" / "models.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_model_configs", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_model_configs"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def test_runtime_models_json_has_expected_defaults() -> None:
    models = gen._load_models(_MODELS_JSON)
    # 2026-09-23: GPT-6 Sol is the standard/subagent lane, GPT-6 Luna the fast lane,
    # GPT-6 Astra stays the top tier + default; 5.6 tiers remain routable only.
    assert models["defaultModel"] == "gpt-6.1-sol"
    assert models["backends"]["gpt"]["defaultModel"] == "gpt-6.1-sol"
    assert models["mappingRules"][0] == {
        "match": "gpt-6.1-sol", "target": "gpt-6.1-sol", "kind": "exact"
    }
    assert models["aliases"]["fable"] == "gpt-6-astra"
    assert models["aliases"]["opus"] == "gpt-6-astra"
    assert models["aliases"]["sonnet"] == "gpt-6.1-sol"
    assert models["aliases"]["haiku"] == "gpt-6-luna"
    assert models["aliases"]["subagent"] == "gpt-6.1-sol"
    assert "gpt-5.6-sol" in models["availableModels"]
    assert "gpt-5.3-codex-spark" in models["disallowedAliasTargets"]
    assert "gpt-5.3-codex-spark" not in models["aliases"].values()
    assert "gpt-5.3-codex-spark" not in models["availableModels"]
    assert "xhigh_effort" in models["capabilities"]
    assert "max_effort" in models["capabilities"]

    gpt = models["backends"]["gpt"]
    assert gpt["aliases"] == {
        "fable": "gpt-6-astra",
        "opus": "gpt-6-astra",
        "sonnet": "gpt-6.1-sol",
        "haiku": "gpt-6-luna",
        "subagent": "gpt-6.1-sol",
    }
    expected_picker_names = {
        "gpt": {
            "fable": "GPT-6 Astra",
            "opus": "GPT-6 Astra",
            "sonnet": "GPT-6.1 Sol",
            "haiku": "GPT-6 Luna",
        },
        "claude": {
            "fable": "Claude Fable 5.1",
            "opus": "Claude Opus 5.5",
            "sonnet": "Claude Sonnet 5.5",
            "haiku": "Claude Haiku 5.5",
        },
        "gemini": {
            "fable": "Gemini 3.1 Pro Preview",
            "opus": "Gemini 3.1 Pro Preview",
            "sonnet": "Gemini 3.5 Flash",
            "haiku": "Gemini 3.1 Flash-Lite",
        },
        "fugu": {
            "fable": "Fugu Ultra",
            "opus": "Fugu Ultra",
            "sonnet": "Fugu Ultra",
            "haiku": "Fugu",
        },
        "fcc": {
            "fable": "Open Models Default",
            "opus": "Open Models Default",
            "sonnet": "Open Models Default",
            "haiku": "Open Models Default",
        },
    }
    for backend_name, expected_names in expected_picker_names.items():
        display = models["backends"][backend_name]["display"]
        assert {
            family: display[f"{family}Name"]
            for family in ("fable", "opus", "sonnet", "haiku")
        } == expected_names

    claude = models["backends"]["claude"]
    assert claude["defaultModel"] == "claude-opus-5-5"
    assert claude["subagentModel"] == "claude-opus-5-5"
    assert claude["availableModels"] == [
        "claude-opus-5-5",
        "claude-fable-5-1",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    assert claude["aliases"] == {
        "fable": "claude-fable-5-1",
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
        "subagent": "claude-opus-5-5",
    }

    gemini = models["backends"]["gemini"]
    assert gemini["defaultModel"] == "gemini-3.5-flash"
    assert gemini["subagentModel"] == "gemini-3.5-flash"
    assert gemini["availableModels"] == [
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
    ]
    assert gemini["aliases"] == {
        "fable": "gemini-3.1-pro-preview",
        "opus": "gemini-3.1-pro-preview",
        "sonnet": "gemini-3.5-flash",
        "haiku": "gemini-3.1-flash-lite",
        "subagent": "gemini-3.5-flash",
    }

    assert all(
        backend["baseUrlPath"] == "/router"
        for name, backend in models["backends"].items()
        if name != "fcc"
    )
    fcc = models["backends"]["fcc"]
    assert fcc["baseUrlPath"] == "/router/fcc"
    assert fcc["defaultModel"] == "fcc-default"
    assert fcc["availableModels"] == ["fcc-default"]


def test_search_does_not_have_a_cross_provider_model_override() -> None:
    models = gen._load_models(_MODELS_JSON)
    assert "serverTools" not in models
    assert gen._validate_backend_config(models) == []


def test_validation_reports_invalid_backend_types_without_crash() -> None:
    for value in (None, [], "invalid"):
        models = gen._load_models(_MODELS_JSON)
        models["backends"] = value
        errors = gen._validate_backend_config(models)
        assert "backends must be an object" in errors


def test_generated_settings_does_not_block_dynamic_gateway_models() -> None:
    models = gen._load_models(_MODELS_JSON)
    settings = gen._settings_payload(models)

    assert "availableModels" not in settings
    assert "model" not in settings


def test_model_picker_contains_six_models_in_order() -> None:
    models = gen._load_models(_MODELS_JSON)
    settings = gen._settings_payload(models)
    picker = settings["modelPicker"]

    assert picker == models["modelPicker"]
    assert picker["replaceBuiltInOptions"] is True
    assert [option["model"] for option in picker["options"]] == [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-luna",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    assert all(option["label"] and option["description"] for option in picker["options"])
    assert all(
        option["behavesAs"] == "claude-opus-5-5"
        for option in picker["options"][:3]
    )
    routable = set(models["availableModels"])
    for backend in models["backends"].values():
        routable.update(backend["availableModels"])
    assert all(option["model"] in routable for option in picker["options"])
    assert {"gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"} <= routable
    assert "availableModels" not in settings
    assert "model" not in settings


def test_fcc_picker_overlay_restores_backend_discovery() -> None:
    models = gen._load_models(_MODELS_JSON)
    expected = gen.expected_files(models)
    path = gen.SETTINGS_JSON.with_name("fcc-picker.settings.json")
    assert expected[path] == (
        '{\n  "modelPicker": {\n    "options": [],\n'
        '    "replaceBuiltInOptions": false\n  }\n}\n'
    )


def test_settings_preserves_legacy_catalog_without_model_picker() -> None:
    models = gen._load_models(_MODELS_JSON)
    models.pop("modelPicker")

    assert "modelPicker" not in gen._settings_payload(models)


def test_backend_context_windows_are_positive_integers() -> None:
    models = gen._load_models(_MODELS_JSON)
    backends_with_context = {
        name: backend["contextTokens"]
        for name, backend in models["backends"].items()
        if "contextTokens" in backend
    }

    assert backends_with_context == {"gpt": 1000000, "fcc": 52000}
    assert all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in backends_with_context.values()
    )


def test_generated_files_are_in_sync() -> None:
    models = gen._load_models(_MODELS_JSON)
    assert gen.check(models) == []


def test_check_cli_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "in sync" in result.stdout


def test_proxy_config_is_generated_from_models() -> None:
    models = gen._load_models(_MODELS_JSON)
    rendered = gen.render_proxy_config(models)
    for model in models["availableModels"]:
        assert f'id = "{model}"' in rendered
    assert 'match = "claude-"' in rendered
    assert 'target = "gpt-6-astra"' in rendered
    assert 'target = "gpt-5.6-sol"' in rendered
    assert 'match = "gpt-"\n' not in rendered
    assert all(rule["kind"] == "exact" for rule in models["mappingRules"] if rule["match"].startswith(("gpt-", "o1-", "o3-")))
    assert 'id = "claude-fable-5-1"' in rendered
    assert 'id = "claude-opus-5-5"' in rendered
    assert 'id = "claude-sonnet-5"' in rendered
    assert 'id = "claude-haiku-4-5"' in rendered
    assert '"fcc"' in rendered.splitlines()[0]

"""Explicit-child registry, launcher preparation and candidate guard contracts."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import subagent_models as sm

MODELS = ROOT / "runtime/models.json"
REGISTRY = ROOT / "runtime/claude-codex/subagent-models.json"
DATA = sm.load_registry(MODELS, REGISTRY)
spec = importlib.util.spec_from_file_location("candidate_agent_guard", ROOT / "scripts/agent-model-guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def prepare(tmp_path, selections=None, args=None, env=None):
    config = tmp_path / "home \u65e5\u672c\u8a9e" / ".claude-codex/config"
    return sm.prepare(DATA, selections or ["deepseek=high", "glm-thinking=max"], args or [],
                      models_path=MODELS, registry_path=REGISTRY, config_dir=config,
                      session_dir=config.parent / "state/subagent-sessions", env=env or {})


@pytest.mark.parametrize("entry,effort", [(e, v) for e in DATA["models"] for v in e["efforts"]])
def test_every_registered_supported_effort(entry, effort):
    result = sm.select(DATA, entry["name"] + "=" + effort, {})
    assert result["model"] == entry["model"] and result["effort"] == effort
    assert result["definition"] == sm.definition(entry, effort)
    assert sm.select(DATA, entry["model"] + "=" + effort, {}) == result


@pytest.mark.parametrize("value", ["glm", "unknown=high", "deepseek=none", "deepseek=medium",
                                  "glm-thinking=low", "glm-uncensored=max", "deepseek=", "=high", "DeepSeek=high"])
def test_bad_or_ambiguous_selection_rejected(value):
    with pytest.raises(ValueError):
        sm.select(DATA, value, {})


def test_omitted_effort_is_disclosed():
    result = sm.select(DATA, "glm-thinking", {})
    assert result["effort"] == "high" and result["defaultedEffort"] is True


@pytest.mark.parametrize("env", [
    {"CLAUDE_CODE_EFFORT_LEVEL": "max"},
    {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "gpt-6-astra"},
    {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "anything", "CLAUDE_CODE_SUBAGENT_MODEL": "gpt-6-astra"},
])
def test_conflicting_environment_rejected_without_mutating(env):
    before = env.copy()
    with pytest.raises(ValueError):
        sm.select(DATA, "deepseek=high", env)
    assert env == before


def test_matching_workflow_environment_is_accepted():
    assert sm.select(DATA, "deepseek=high", {
        "CLAUDE_CODE_EFFORT_LEVEL": "high", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        "CLAUDE_CODE_SUBAGENT_MODEL": "nanogpt/deepseek/deepseek-v4.1-flash-1017",
    })["effort"] == "high"


def test_public_fable_is_an_ordinary_explicit_model_choice():
    result = sm.select(DATA, "claude-fable=high", {})
    assert result["model"] == "claude-fable-5-1" and result["effort"] == "high"
    assert "requiresFableOk" not in result
    assert "final adjudication" not in result["definition"]["prompt"]


def test_no_fugu_fcc_gemini_child_presets():
    assert {e["backend"] for e in DATA["models"]} == {"gpt", "claude", "nanogpt"}


@pytest.mark.parametrize("change", ["duplicate_name", "unknown_model", "unsupported_backend", "bad_default",
                                  "bad_effort", "private_policy_field", "duplicate_model"])
def test_invalid_registry_fails_closed(change):
    models = sm.loads(MODELS.read_text(encoding="utf-8"))
    first = models["subagentModels"][0]
    if change == "duplicate_name":
        models["subagentModels"][1]["name"] = first["name"]
    elif change == "duplicate_model":
        models["subagentModels"][1]["model"] = first["model"]
    elif change == "unknown_model":
        first["model"] = "nanogpt/unknown"
    elif change == "unsupported_backend":
        first["model"] = "fugu-ultra"
    elif change == "bad_default":
        first["defaultEffort"] = "none"
    elif change == "bad_effort":
        first["efforts"] = ["high", "high"]
    else:
        next(e for e in models["subagentModels"] if e["name"] == "claude-fable")["requiresFableOk"] = True
    with pytest.raises(ValueError):
        sm.build_registry(models)


def test_generated_registry_drift_is_rejected(tmp_path):
    path = tmp_path / "registry.json"
    content = copy.deepcopy(DATA)
    content["models"][0]["definition"]["model"] = "unknown"
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        sm.load_registry(MODELS, path)


def test_manual_definitions_and_session_id_preserved(tmp_path):
    manual = {"My_Agent": {"description": "Synthetic custom agent", "prompt": "Synthetic only",
                            "model": "sonnet", "tools": ["Read"], "effort": "low"}}
    sid = str(uuid.uuid4())
    result = prepare(tmp_path, args=["--agents", json.dumps(manual), "--session-id=" + sid, "-p", "fixture"])
    definitions = json.loads(result["agentsJson"])
    assert definitions["My_Agent"] == manual["My_Agent"]
    assert set(definitions) == {"My_Agent", "ccx-deepseek", "ccx-glm-thinking"}
    assert result["args"][result["args"].index("--session-id") + 1] == sid
    record = sm.loads(Path(result["sessionPath"]).read_text(encoding="utf-8"))
    assert record["agents"] == definitions and record["agentsSha256"] == sm.digest(definitions)
    assert "\\u65e5" in Path(result["sessionPath"]).read_text(encoding="utf-8")


def test_options_terminator_keeps_prompt_and_injection_order(tmp_path):
    result = prepare(tmp_path, args=["-p", "--", "--resume", "Synthetic prompt"])
    assert result["args"][-3:] == ["--", "--resume", "Synthetic prompt"]
    assert result["args"].index("--agents") < result["args"].index("--")


@pytest.mark.parametrize("args", [
    ["--agents", "{"], ["--agents", "[]"], ["--agents", '{"ccx-deepseek":{}}'],
    ["--agents", '{"CCX-DeepSeek":{}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x"},"A":{"prompt":"y","description":"y"}}'],
    ["--agents", '{"a":{"prompt":"x"}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x","effort":"none"}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x","model":"unknown"}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x","model":"nanogpt/z-ai/glm-5.3-thinking","effort":"low"}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x","unknown":NaN}}'],
    ["--agents", '{"a":{"prompt":"x","description":"x","tools":1}}'],
    ["--agents"], ["--session-id"], ["--session-id", "bad"],
    ["--resume", "fixture"], ["--continue"], ["--fork-session"], ["-rfixture"],
])
def test_invalid_launcher_arguments_fail_before_session_write(tmp_path, args):
    with pytest.raises((ValueError, TypeError)):
        prepare(tmp_path, args=args)
    assert not list(tmp_path.rglob("*.json"))


def test_duplicate_selection_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Duplicate child"):
        prepare(tmp_path, selections=["deepseek=high", "deepseek=max"])


@pytest.mark.parametrize("backend", ["fugu", "fcc", "gemini"])
def test_restricted_parent_profiles_do_not_gain_tool_using_children(tmp_path, backend):
    with pytest.raises(ValueError, match="router profile"):
        prepare(tmp_path, args=["--backend", backend])
    with pytest.raises(ValueError, match="router profile"):
        prepare(tmp_path, env={"CLAUDE_CODEX_BACKEND": backend})


@pytest.fixture
def selected_session(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODEX_SUBAGENT_") or key in (
            "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
            monkeypatch.delenv(key, raising=False)
    result = prepare(tmp_path)
    record = sm.loads(Path(result["sessionPath"]).read_text(encoding="utf-8"))
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_CONTEXT", "claude-codex")
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_SESSION", result["sessionPath"])
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_AGENTS", result["agentsJson"])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", record["configDir"])
    return result, record, {"session_id": record["sessionId"]}


def test_candidate_guard_allows_valid_session_only(selected_session):
    _, _, payload = selected_session
    for name in ("ccx-deepseek", "ccx-glm-thinking"):
        assert guard.decide({"subagent_type": name, "prompt": "Synthetic isolated task"}, payload)[0] == 0


@pytest.mark.parametrize("override", [
    {"model": "sonnet"}, {"model": "nanogpt/deepseek/deepseek-v4.1-flash-1017"},
    {"model": ""}, {"effort": "max"}, {"fork_context": True}, {"fork": True},
])
def test_guard_rejects_call_override_or_fork(selected_session, override):
    _, _, payload = selected_session
    assert guard.decide({"subagent_type": "ccx-deepseek", "prompt": "[FABLE-OK] synthetic", **override}, payload)[0] == 2


def test_guard_rejects_fork_bypass_in_selected_session(selected_session):
    _, _, payload = selected_session
    assert guard.decide({"subagent_type": "fork", "model": "sonnet", "prompt": "[FABLE-OK] synthetic"}, payload)[0] == 2


def test_guard_rejects_environment_drift(selected_session, monkeypatch):
    _, _, payload = selected_session
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    assert guard.decide({"subagent_type": "ccx-deepseek"}, payload)[0] == 2


def test_public_guard_fable_uses_same_session_contract(selected_session, tmp_path, monkeypatch):
    result = prepare(tmp_path, selections=["claude-fable=high"])
    record = sm.loads(Path(result["sessionPath"]).read_text(encoding="utf-8"))
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_SESSION", result["sessionPath"])
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_AGENTS", result["agentsJson"])
    payload = {"session_id": record["sessionId"]}
    call = {"subagent_type": "ccx-claude-fable", "prompt": "Synthetic ordinary task"}
    assert guard.decide(call, payload)[0] == 0
    assert guard.decide({**call, "fork_context": True}, payload)[0] == 2
    assert guard.decide({**call, "effort": "max"}, payload)[0] == 2


@pytest.mark.parametrize("field", ["sessionId", "modelsSha256", "registrySha256", "agentsSha256",
                                  "modelsPath", "registryPath", "configDir"])
def test_guard_rejects_mismatching_record(selected_session, field):
    result, record, payload = selected_session
    record[field] = "mismatch"
    Path(result["sessionPath"]).write_text(json.dumps(record), encoding="utf-8")
    assert guard.decide({"subagent_type": "ccx-deepseek"}, payload)[0] == 2


def test_guard_rejects_final_definition_mismatch(selected_session, monkeypatch):
    result, record, payload = selected_session
    altered = record["agents"]
    altered["ccx-deepseek"]["effort"] = "max"
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_AGENTS", json.dumps(altered))
    assert guard.decide({"subagent_type": "ccx-deepseek"}, payload)[0] == 2


@pytest.mark.parametrize("profile", ["config", "config-slim"])
def test_guard_deployment_layout_uses_active_config_registry(selected_session, monkeypatch, profile):
    result, record, payload = selected_session
    base = Path(record["configDir"])
    base.mkdir(parents=True)
    (base / "models.json").write_bytes(MODELS.read_bytes())
    (base / "subagent-models.json").write_bytes(REGISTRY.read_bytes())
    active = base.with_name(profile)
    active.mkdir(parents=True, exist_ok=True)
    (active / "models.json").write_bytes(MODELS.read_bytes())
    (active / "subagent-models.json").write_bytes(REGISTRY.read_bytes())
    record["modelsPath"] = str(active / "models.json")
    record["registryPath"] = str(active / "subagent-models.json")
    record["configDir"] = str(active)
    Path(result["sessionPath"]).write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", record["configDir"])
    monkeypatch.setattr(guard, "__file__", str(base.parents[1] / ".claude/scripts/agent-model-guard.py"))
    assert guard.decide({"subagent_type": "ccx-deepseek"}, payload)[0] == 0


def test_guard_rejects_unknown_model_even_with_matching_record_hash(selected_session, monkeypatch):
    result, record, payload = selected_session
    record["agents"]["ccx-deepseek"]["model"] = "nanogpt/unknown"
    record["selected"]["ccx-deepseek"]["model"] = "nanogpt/unknown"
    record["selected"]["ccx-deepseek"]["definition"]["model"] = "nanogpt/unknown"
    record["agentsSha256"] = sm.digest(record["agents"])
    Path(result["sessionPath"]).write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODEX_SUBAGENT_AGENTS", json.dumps(record["agents"]))
    assert guard.decide({"subagent_type": "ccx-deepseek"}, payload)[0] == 2


def test_public_guard_does_not_impose_personal_agent_roles(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODEX_SUBAGENT_CONTEXT", raising=False)
    assert guard.decide({"subagent_type": "ccx-deepseek"})[0] == 2
    for model in (None, "inherit", "fable", "gpt-6-astra", "opus", "sonnet", "haiku"):
        call = {"subagent_type": "general-purpose", "prompt": "Synthetic ordinary task"}
        if model is not None:
            call["model"] = model
        assert guard.decide(call)[0] == 0
    assert guard.decide({"subagent_type": "fork", "model": "opus"})[0] == 0


def test_public_models_and_generated_registry_have_no_personal_gate():
    models = sm.loads(MODELS.read_text(encoding="utf-8"))
    assert all("requiresFableOk" not in entry for entry in models["subagentModels"])
    assert all("requiresFableOk" not in entry for entry in DATA["models"])
    for entry in DATA["models"]:
        for effort in entry["efforts"]:
            selected = sm.select(DATA, entry["name"] + "=" + effort, {})
            assert "requiresFableOk" not in selected
            assert "FABLE-OK" not in selected["definition"]["prompt"]
    assert sm.build_registry(models) == DATA


def test_public_resolver_fable_selection_and_legacy_gate_flag():
    import subprocess
    script = str(ROOT / "scripts/resolve-subagent.py")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE_", "ANTHROPIC_"))}
    result = subprocess.run([sys.executable, "-B", script, "claude-fable=high"], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    selected = json.loads(result.stdout)
    assert selected["model"] == "claude-fable-5-1" and selected["workflowEnforced"] is False
    assert "requiresFableOk" not in selected
    refused = subprocess.run([sys.executable, "-B", script, "claude-fable=high", "--fable-ok"],
                             env=env, capture_output=True, text=True, timeout=20)
    assert refused.returncode == 2

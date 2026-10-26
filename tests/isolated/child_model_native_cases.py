"""Focused native executable acceptance; no installed launcher or proxy access."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("NATIVE_LAUNCHER_TEST_EXE", str(ROOT / "launchers/native/target/release/claude-codex-native.exe")))


def fixture(tmp_path):
    assert BUILD.is_file(), "Build the focused candidate offline; do not use an installed exe"
    folder = tmp_path / "bin \u65e5\u672c\u8a9e"
    folder.mkdir()
    exe = folder / "claude-codex-native.exe"
    exe.write_bytes(BUILD.read_bytes())
    (folder / "claude-codex.ps1").write_bytes((ROOT / "launchers/bin/claude-codex.ps1").read_bytes())
    home = tmp_path / "home \u65e5\u672c\u8a9e"
    config = home / ".claude-codex/config"
    config.mkdir(parents=True)
    for source, target in [
        ("runtime/models.json", "models.json"),
        ("runtime/claude-codex/subagent-models.json", "subagent-models.json"),
        ("scripts/resolve-subagent.py", "resolve-subagent.py"),
        ("scripts/subagent_models.py", "subagent_models.py"),
    ]:
        (config / target).write_bytes((ROOT / source).read_bytes())
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(
        ("CLAUDE_", "ANTHROPIC_", "OPENAI_", "CCPROXY_", "MYCMUX_", "NANOGPT_"))}
    env.update({
        "HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CODEX_HOME": str(home),
        "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "local"),
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "CLAUDE_CODEX_PROXY_CMD": str(tmp_path / "must-not-start.cmd"),
        "CLAUDE_CODEX_CLAUDE_EXE": str(tmp_path / "must-not-run.exe"),
        "CLAUDE_CODEX_PYTHON_EXE": sys.executable,
        "FUGU_API_KEY": "synthetic-not-a-real-key",
    })
    return exe, home, env


@pytest.mark.parametrize("args", [["--subagent", "deepseek=high"], ["--subagent=glm-thinking=max"],
                                  ["--list-subagent-models"]])
def test_native_plan_delegates_before_auth_and_mutation(tmp_path, args):
    exe, home, env = fixture(tmp_path)
    before = sorted(str(p) for p in home.rglob("*"))
    env["CLAUDE_CODEX_NATIVE_SELFTEST"] = "plan"
    result = subprocess.run([str(exe), *args], env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["path"] == "fallback" and plan["reason"] == "explicit-subagent-options"
    assert plan["args"] == args and plan["env_diff"] == {}
    assert before == sorted(str(p) for p in home.rglob("*"))


def test_native_actual_fallback_lists_without_proxy_or_auth(tmp_path):
    exe, home, env = fixture(tmp_path)
    result = subprocess.run([str(exe), "--list-subagent-models"], env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert b"glm-thinking" in result.stdout and b"deepseek" in result.stdout
    assert not (home / ".claude-codex/state").exists()
    assert not (home / ".claude-codex/data").exists()


def test_native_no_child_options_retains_existing_hash_gate(tmp_path):
    exe, home, env = fixture(tmp_path)
    with (exe.parent / "claude-codex.ps1").open("ab") as script:
        script.write(b"\r\n# synthetic unknown hash rejection fixture\r\n")
    env["CLAUDE_CODEX_NATIVE_SELFTEST"] = "plan"
    result = subprocess.run([str(exe), "-p", "Synthetic fixture"], env=env, capture_output=True, timeout=20)
    assert result.returncode == 0
    plan = json.loads(result.stdout)
    assert plan["path"] == "fallback" and plan["reason"] == "ps-launcher-unknown"


def test_staged_resolver_uses_adjacent_registry_without_flags(tmp_path):
    _, home, env = fixture(tmp_path)
    script = home / ".claude-codex/config/resolve-subagent.py"
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(script), "deepseek=high"],
                            env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    selection = json.loads(result.stdout)
    assert selection["model"] == "nanogpt/deepseek/deepseek-v4.1-flash-1017"
    assert selection["effort"] == "high" and selection["workflowEnforced"] is False

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_token_persistence_follows_all_install_preflight_checks():
    source = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    persist = source.index('[Environment]::SetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN"')
    assert persist > source.index("$conflicts = @(Get-InstallConflicts")
    assert persist > source.index("if ($upstreamChanged.Count -gt 0)")
    assert persist > source.index("$preparedSettings = @{}")
    assert persist < source.index("New-Item -ItemType Directory -Force $BackupRoot")
    assert source.count('[Environment]::SetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN"') == 1


def test_required_literal_install_sources_exist():
    source = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    paths = re.findall(r'Source = Join-Path \$RepoRoot "([^"\r\n]+)"', source)
    assert paths
    missing = [relative for relative in paths
               if not relative.endswith("claude-codex-native.exe")
               and not (ROOT / relative.replace("\\", "/")).is_file()]
    assert missing == []


def test_public_settings_require_normal_permission_prompts() -> None:
    settings = json.loads((ROOT / "runtime/claude-codex/settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"] == {"defaultMode": "default"}
    assert "skipAutoPermissionPrompt" not in settings
    assert "skipDangerousModePermissionPrompt" not in settings
    assert "enabledPlugins" not in settings


def test_install_preview_returns_before_any_prerequisite_or_write() -> None:
    source = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
    preview = source.index("if (-not $Apply)")
    early_return = source.index("    return", preview)
    assert preview < early_return < source.index("function New-ProxyToken")
    assert early_return < source.index("Checking prerequisites")
    assert "[switch]$Apply" in source[:preview]


def test_install_preview_does_not_create_target_home() -> None:
    shell = shutil.which("powershell.exe")
    if shell is None:
        pytest.skip("Windows PowerShell is required for the installer preview")
    base = Path(tempfile.mkdtemp(prefix="claude-codex-preview-"))
    target = base / "未使用のホーム"
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & $env:PUBLIC_INSTALL_TEST_SCRIPT -HomeDir $env:PUBLIC_INSTALL_TEST_HOME"],
        env={**os.environ, "PUBLIC_INSTALL_TEST_SCRIPT": str(ROOT / "scripts/install.ps1"),
             "PUBLIC_INSTALL_TEST_HOME": str(target)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    preview = json.loads(result.stdout.decode("utf-8-sig"))
    assert preview["mode"] == "read-only preview"
    assert Path(preview["home"]) == target
    assert not target.exists()
    assert list(base.iterdir()) == []

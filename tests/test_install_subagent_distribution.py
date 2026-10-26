"""Apply the real installer map/functions without executing installer top level."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
param([string]$Repository, [string]$Fixture)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $Repository 'scripts\install.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }
$names = @('Merge-OwnedSettings', 'Merge-SubagentGuardHook', 'Test-SubagentGuardHook', 'Get-InstallConflicts', 'Copy-LauncherFile', 'Test-SettingEqual', 'Get-InstallText', 'Test-PristineUpstream', 'Get-OwnedExisting', 'Get-SettingsInstallValue')
foreach ($function in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in $names}, $true)) { Invoke-Expression $function.Extent.Text }
$mapAst = @($ast.FindAll({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left -is [Management.Automation.Language.VariableExpressionAst] -and $n.Left.VariablePath.UserPath -eq 'copyMap'}, $true))[0]
$RepoRoot = $Repository
$HomeDir = Join-Path $Fixture 'installed home'
$TargetClaudeCodex = Join-Path $HomeDir '.claude-codex'
$TargetProxy = Join-Path $HomeDir '.claude-codex-proxy'
$TargetBin = Join-Path $HomeDir 'bin'
$TargetSitePackages = Join-Path $HomeDir 'site\ccproxy'
$AdoptInstalled = @()
Invoke-Expression $mapAst.Extent.Text
$allMap = $copyMap
$entries = @($allMap | Where-Object {
    $_.Target.StartsWith($TargetClaudeCodex + '\') -and
    (Split-Path $_.Target -Leaf) -in @('models.json', 'settings.json', 'subagent-models.json', 'resolve-subagent.py', 'subagent_models.py') -or
    $_.Target.StartsWith((Join-Path $HomeDir '.claude\scripts') + '\')
})
if ($entries.Count -ne 12) { throw "Expected 8 child targets plus models/settings full/slim, got $($entries.Count)" }
if (@($allMap.Target | Group-Object | Where-Object Count -gt 1).Count) { throw 'Duplicate copy targets' }
foreach ($entry in $allMap) {
    if (-not $entry.ContainsKey('Optional') -and -not (Test-Path -LiteralPath $entry.Source)) { throw "Missing distribution source: $($entry.Source)" }
}
$manifest = Join-Path $HomeDir 'manifest.json'
$cases = [Collections.Generic.List[string]]::new()
function Save-Json($Path, $Value) {
    $null = New-Item -ItemType Directory -Force (Split-Path $Path)
    [IO.File]::WriteAllText($Path, (($Value | ConvertTo-Json -Depth 100) + "`n"), [Text.UTF8Encoding]::new($false))
}
function Snapshot {
    return (@(Get-ChildItem -LiteralPath $HomeDir -File -Recurse | Sort-Object FullName | ForEach-Object {
        "$($_.FullName)|$((Get-FileHash -LiteralPath $_.FullName).Hash)|$($_.LastWriteTimeUtc.Ticks)"
    }) -join "`n")
}
function Apply-Distribution {
    $conflicts = @(Get-InstallConflicts $entries $manifest)
    if ($conflicts.Count) { throw "Unexpected conflict: $conflicts" }
    $prior = @{}
    if (Test-Path $manifest) { foreach ($r in (ConvertFrom-Json ([IO.File]::ReadAllText($manifest))).files) { $prior[$r.target] = $r } }
    $prepared = @{}
    foreach ($e in $entries) {
        if ((Split-Path $e.Source -Leaf) -eq 'settings.json') { $prepared[$e.Target] = Get-SettingsInstallValue $e $prior[$e.Target] }
    }
    $records = @()
    foreach ($e in $entries) {
        $null = New-Item -ItemType Directory -Force (Split-Path $e.Target)
        if ($prepared.ContainsKey($e.Target)) { Save-Json $e.Target $prepared[$e.Target] }
        else { Copy-LauncherFile $e.Source $e.Target }
        $r = @{target=$e.Target;sha256=(Get-FileHash -LiteralPath $e.Target).Hash}
        if ($prepared.ContainsKey($e.Target)) { $r.ownedSettings = ConvertFrom-Json ([IO.File]::ReadAllText($e.Source, [Text.Encoding]::UTF8)) }
        $records += $r
    }
    Save-Json $manifest @{files=$records}
}
$null = New-Item -ItemType Directory -Force $HomeDir
# A fresh user's unrelated hooks, their order, plugin and permission entries.
$user = '{"model":"user-model","permissions":{"defaultMode":"default","allow":["Read"]},"enabledPlugins":{"custom":true},"hooks":{"PostToolUse":[{"matcher":"Read","hooks":[{"type":"command","command":"after"}]}],"PreToolUse":[{"matcher":"Read","hooks":[{"type":"command","command":"first"}]},{"matcher":"Agent|Task","hooks":[{"type":"command","command":"other-agent-hook"}]},{"matcher":"Bash","hooks":[{"type":"command","command":"last"}]}]}}'
foreach ($profile in @('config', 'config-slim')) { Save-Json (Join-Path $TargetClaudeCodex "$profile\settings.json") (ConvertFrom-Json $user) }
Apply-Distribution
$cases.Add('map-eight-new-targets-and-all-sources-present')
foreach ($profile in @('config', 'config-slim')) {
    $s = ConvertFrom-Json ([IO.File]::ReadAllText((Join-Path $TargetClaudeCodex "$profile\settings.json")))
    if ($s.model -ne 'user-model' -or $s.permissions.allow[0] -ne 'Read' -or -not $s.enabledPlugins.custom -or $s.hooks.PostToolUse[0].hooks[0].command -ne 'after') { throw 'User settings lost' }
    if ($s.hooks.PreToolUse.Count -ne 4 -or ($s.hooks.PreToolUse[0..2].hooks.command -join ',') -ne 'first,other-agent-hook,last') { throw 'Hook order changed' }
}
$cases.Add('full-slim-user-settings-and-hook-order-preserved')
$before = @($entries | ForEach-Object { (Get-FileHash -LiteralPath $_.Target).Hash }) -join ','
Apply-Distribution
$after = @($entries | ForEach-Object { (Get-FileHash -LiteralPath $_.Target).Hash }) -join ','
if ($before -cne $after) { throw 'Reapplication not byte-idempotent' }
$cases.Add('reapply-no-duplicate-hook')
# Preserve a previously owned hook even when its marker was completely removed.
$ownedSettings = @($entries | Where-Object Target -eq (Join-Path $TargetClaudeCodex 'config\settings.json'))[0]
$ownedOriginal = [IO.File]::ReadAllBytes($ownedSettings.Target)
foreach ($variant in @('removed', 'edited-without-marker')) {
    $s = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($ownedOriginal))
    if ($variant -eq 'removed') { $s.hooks.PreToolUse = @($s.hooks.PreToolUse[0..2]) }
    else { $s.hooks.PreToolUse[3].hooks[0] = [pscustomobject]@{type='command';command='user-replaced-command'} }
    Save-Json $ownedSettings.Target $s
    foreach ($adopt in @($false, $true)) {
        $AdoptInstalled = if ($adopt) { @($ownedSettings.Target) } else { @() }
        $snapshot = Snapshot
        if (@(Get-InstallConflicts $entries $manifest).Count -ne 1 -or (Snapshot) -cne $snapshot) { throw "Previously owned hook $variant was overwritten or adoption bypassed ownership" }
    }
    [IO.File]::WriteAllBytes($ownedSettings.Target, $ownedOriginal)
    $cases.Add("previously-owned-hook-$variant-refused-zero-writes")
}
$AdoptInstalled = @()
# An identical guard needs no manifest ownership; an owned unchanged guard updates.
$guard = @($entries | Where-Object { $_.Target -eq (Join-Path $HomeDir '.claude\scripts\agent-model-guard.py') })[0]
if (@(Get-InstallConflicts @($guard) (Join-Path $HomeDir 'absent.json')).Count) { throw 'Identical guard rejected' }
$cases.Add('identical-unowned-guard-accepted')
$guardOriginal = [IO.File]::ReadAllBytes($guard.Target)
[IO.File]::WriteAllText($guard.Target, 'previously owned guard')
$oldManifest = [IO.File]::ReadAllBytes($manifest)
$owned = ConvertFrom-Json ([IO.File]::ReadAllText($manifest))
@($owned.files | Where-Object target -eq $guard.Target)[0].sha256 = (Get-FileHash -LiteralPath $guard.Target).Hash
Save-Json $manifest $owned
Apply-Distribution
$cases.Add('manifest-owned-guard-update-accepted')
# Unknown / later-edited guards fail before copying even with explicit adoption.
[IO.File]::WriteAllText($guard.Target, 'unknown edited guard')
foreach ($adopt in @($false, $true)) {
    $AdoptInstalled = if ($adopt) { @($guard.Target) } else { @() }
    $snapshot = Snapshot
    if (@(Get-InstallConflicts $entries $manifest).Count -ne 1 -or (Snapshot) -cne $snapshot) { throw 'Guard conflict wrote files or was bypassed' }
}
$AdoptInstalled = @()
[IO.File]::WriteAllBytes($guard.Target, $guardOriginal)
[IO.File]::WriteAllBytes($manifest, $oldManifest)
$cases.Add('edited-guard-and-adoption-refused-zero-writes')
$settingsEntry = @($entries | Where-Object Target -eq (Join-Path $TargetClaudeCodex 'config\settings.json'))[0]
$saved = [IO.File]::ReadAllBytes($settingsEntry.Target)
foreach ($variant in @('command', 'matcher', 'duplicate', 'embedded-command')) {
    $s = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($saved))
    $hook = $s.hooks.PreToolUse[3]
    switch ($variant) {
        'command' { $hook.hooks[0].args += '--unknown' }
        'matcher' { $hook.matcher = 'Task' }
        'duplicate' { $s.hooks.PreToolUse += $hook }
        'embedded-command' { $hook.hooks += [pscustomobject]@{type='command';command='keep-sibling'}; $hook.hooks[0].timeout = 1 }
    }
    Save-Json $settingsEntry.Target $s
    $AdoptInstalled = @($settingsEntry.Target)
    $snapshot = Snapshot
    if (@(Get-InstallConflicts $entries $manifest).Count -ne 1 -or (Snapshot) -cne $snapshot) { throw "Hook conflict $variant failed closed or wrote files" }
    $cases.Add("edited-hook-$variant-refused-zero-writes")
}
$AdoptInstalled = @()
[IO.File]::WriteAllBytes($settingsEntry.Target, $saved)
# A canonical hook embedded with an unrelated sibling must keep both in place.
$s = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($saved))
$s.hooks.PreToolUse[3].hooks += [pscustomobject]@{type='command';command='keep-sibling'}
Save-Json $settingsEntry.Target $s
Apply-Distribution
$s = ConvertFrom-Json ([IO.File]::ReadAllText($settingsEntry.Target))
if ($s.hooks.PreToolUse[3].hooks.Count -ne 2 -or $s.hooks.PreToolUse[3].hooks[1].command -ne 'keep-sibling') { throw 'Sibling hook lost' }
$cases.Add('embedded-canonical-hook-sibling-preserved')
$s.permissions.defaultMode = 'manual'
Save-Json $settingsEntry.Target $s
$snapshot = Snapshot
if (@(Get-InstallConflicts $entries $manifest).Count -ne 1 -or (Snapshot) -cne $snapshot) { throw 'User permission edit overwritten' }
$cases.Add('locally-edited-owned-permission-refused-zero-writes')
Save-Json (Join-Path $Fixture 'distribution-result.json') @{home=$HomeDir;cases=@($cases);targets=@($entries.Target);installerTopLevelExecuted=$false}
Write-Output 'DISTRIBUTION_FIXTURE_OK'
'''


@pytest.fixture
def distribution(tmp_path):
    ps = shutil.which('powershell')
    if not ps:
        pytest.skip('Windows PowerShell required')
    script = tmp_path / 'distribution.ps1'
    script.write_text(FIXTURE, encoding='utf-8')
    result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                             str(script), str(ROOT), str(tmp_path)], capture_output=True, timeout=60)
    (tmp_path / 'stdout.txt').write_bytes(result.stdout)
    (tmp_path / 'stderr.txt').write_bytes(result.stderr)
    assert result.returncode == 0, (result.stdout.decode('utf-8', 'replace'), result.stderr.decode('utf-8', 'replace'))
    data = json.loads((tmp_path / 'distribution-result.json').read_text(encoding='utf-8-sig'))
    assert len(data['cases']) == 14
    return tmp_path, data


@pytest.mark.parametrize('profile', ['config', 'config-slim'])
def test_installed_full_and_slim_selection_and_guard(distribution, profile):
    tmp_path, data = distribution
    home = Path(data['home'])
    config = home / '.claude-codex' / profile
    resolver = config / 'resolve-subagent.py'
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(
        ('CLAUDE_', 'ANTHROPIC_', 'CCPROXY_', 'OPENAI_', 'NANOGPT_'))}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'roaming'),
               LOCALAPPDATA=str(home / 'local'), TEMP=str(tmp_path), TMP=str(tmp_path))

    def resolve(*args, input=None, code=0):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(resolver), *args],
                                input=input, env=env, capture_output=True, timeout=20)
        assert result.returncode == code, result.stderr
        return result

    assert b'deepseek' in resolve('--list').stdout
    chosen = json.loads(resolve('deepseek=high').stdout)
    assert chosen['effort'] == 'high' and chosen['workflowEnforced'] is False
    resolve('deepseek=unsupported', code=2)
    env['CLAUDE_CODE_EFFORT_LEVEL'] = 'low'
    resolve('deepseek=high', code=2)
    env.pop('CLAUDE_CODE_EFFORT_LEVEL')
    payload = dict(selections=['deepseek=high'], args=[], configDir=str(config),
                   sessionDir=str(home / '.claude-codex/state/subagent-sessions'))
    prepared = json.loads(resolve('--prepare', input=json.dumps(payload).encode()).stdout)
    record = json.loads(Path(prepared['sessionPath']).read_text(encoding='utf-8'))
    guard = home / '.claude/scripts/agent-model-guard.py'
    tool = dict(tool_name='Agent', session_id=record['sessionId'],
                tool_input=dict(subagent_type='ccx-deepseek', prompt='Synthetic fixture'))
    # Execute the actual installed settings hook, including its context wrapper.
    settings = json.loads((config / 'settings.json').read_text(encoding='utf-8-sig'))
    hook = next(h for row in settings['hooks']['PreToolUse'] for h in row['hooks']
                if 'agent-model-guard' in json.dumps(h))
    env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODEX_SUBAGENT_CONTEXT='claude-codex',
               CLAUDE_CODEX_SUBAGENT_SESSION=prepared['sessionPath'],
               CLAUDE_CODEX_SUBAGENT_AGENTS=prepared['agentsJson'])

    def check(code):
        result = subprocess.run([sys.executable, *hook['args']], input=json.dumps(tool).encode(),
                                env=env, capture_output=True, timeout=20)
        assert result.returncode == code, result.stderr

    check(0)
    tool['tool_input']['model'] = 'opus'
    check(2)
    tool['tool_input'].pop('model')
    tool['tool_input']['effort'] = 'low'
    check(2)
    tool['tool_input'].pop('effort')
    tool['tool_input']['fork_context'] = True
    check(2)
    tool['tool_input'].pop('fork_context')
    tool['session_id'] = 'wrong-session'
    check(2)
    env.pop('CLAUDE_CODEX_SUBAGENT_CONTEXT')
    # No selection leaves ordinary calls alone and does not create a new log.
    before = sorted(str(p) for p in config.rglob('*'))
    check(0)
    assert before == sorted(str(p) for p in config.rglob('*'))
    assert guard.is_file()

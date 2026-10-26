param(
    [string]$HomeDir = [Environment]::GetFolderPath("UserProfile"),
    [switch]$Apply,
    [switch]$Force,
    [switch]$RestartProxy,
    [string[]]$AdoptInstalled = @(),
    [string]$AdoptionFile = "",
    [string]$AdoptionReason = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$RoamingAppData = [Environment]::GetFolderPath("ApplicationData")
$TargetBin = Join-Path $HomeDir "bin"
$TargetClaudeCodex = Join-Path $HomeDir ".claude-codex"
$TargetProxy = Join-Path $HomeDir ".claude-codex-proxy"
$TargetSitePackages = Join-Path $RoamingAppData "uv\tools\ccproxy-api\Lib\site-packages\ccproxy"

if (-not $Apply) {
    [pscustomobject]@{
        mode = "read-only preview"
        home = $HomeDir
        bin = $TargetBin
        config = $TargetClaudeCodex
        proxy = $TargetProxy
        patchTarget = $TargetSitePackages
        userEnvironmentChanges = @("CLAUDE_CODEX_PROXY_TOKEN")
        notice = "No commands, authentication, installation or writes performed. Review these targets before explicitly passing -Apply."
    } | ConvertTo-Json -Depth 4
    return
}

function New-ProxyToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

# === Prerequisite checks ===
Write-Host "Checking prerequisites..."

$claudeExe = Join-Path $HomeDir ".local\bin\claude.exe"
if (-not (Test-Path $claudeExe)) {
    Write-Error "Claude Code CLI not found at $claudeExe`nInstall: https://claude.ai/download"
    exit 1
}
Write-Host "  Claude Code CLI: OK"

# Soft check: the launcher uses ANTHROPIC_DEFAULT_MODEL so a /model selection
# can persist without being overwritten on every start. Claude Code added that
# gateway default in 2.1.236. Warn rather than block so rollback remains easy.
$prevPref2 = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $claudeVerRaw = & $claudeExe --version 2>$null | Select-Object -First 1
}
catch {
    $claudeVerRaw = $null
}
finally {
    $ErrorActionPreference = $prevPref2
}
$GATEWAY_DEFAULT_MIN = [version]"2.1.236"
if ($claudeVerRaw -and ($claudeVerRaw -match '(\d+\.\d+\.\d+)')) {
    $claudeVer = [version]$Matches[1]
    if ($claudeVer -lt $GATEWAY_DEFAULT_MIN) {
        Write-Warning "Claude Code CLI $claudeVer < $GATEWAY_DEFAULT_MIN - gateway default-model persistence is unavailable. Run: claude update"
    }
    else {
        Write-Host "  Claude Code CLI version: $claudeVer (gateway defaults OK)"
    }
}
else {
    Write-Warning "Could not parse claude.exe version (gateway defaults need >= $GATEWAY_DEFAULT_MIN). Run: claude --version"
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Error "uv not found.`nInstall: pip install uv  or  https://docs.astral.sh/uv/"
    exit 1
}
Write-Host "  uv: OK"

if (-not (Test-Path $TargetSitePackages)) {
    Write-Error "ccproxy-api not found at $TargetSitePackages`nInstall: uv tool install ccproxy-api"
    exit 1
}
Write-Host "  ccproxy-api: OK"

# Native exe + 2>&1 + StrictMode/Stop traps stderr as ErrorRecord on PS 5.1.
# Also: ccproxy auth status prints a U+2713 checkmark and crashes on cp932 (Windows-JP)
# without UTF-8 mode, so PYTHONUTF8/PYTHONIOENCODING are forced for this call.
$prevPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    $null = & ccproxy auth status codex 2>$null
    $authExit = $LASTEXITCODE
}
catch {
    $authExit = -1
}
finally {
    $ErrorActionPreference = $prevPref
}
if ($authExit -ne 0) {
    Write-Warning "Codex not authenticated. After install, run: ccproxy auth login codex"
}
else {
    Write-Host "  Codex auth: OK"
}

Write-Host ""

$BackupRoot = Join-Path $TargetProxy ("backups\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N"))

$copyMap = @(
    @{
        Source = Join-Path $RepoRoot "launchers\native\target\release\claude-codex-native.exe"
        Target = Join-Path $TargetBin "claude-codex-native.exe"
        Optional = $true
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude.cmd"
        Target = Join-Path $TargetBin "claude.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude"
        Target = Join-Path $TargetBin "claude"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex.cmd"
        Target = Join-Path $TargetBin "claude-codex.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex.ps1"
        Target = Join-Path $TargetBin "claude-codex.ps1"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex"
        Target = Join-Path $TargetBin "claude-codex"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-ultra.cmd"
        Target = Join-Path $TargetBin "claude-codex-ultra.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-ultra"
        Target = Join-Path $TargetBin "claude-codex-ultra"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-fugu.cmd"
        Target = Join-Path $TargetBin "claude-codex-fugu.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-fugu"
        Target = Join-Path $TargetBin "claude-codex-fugu"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-claude.cmd"
        Target = Join-Path $TargetBin "claude-codex-claude.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-claude"
        Target = Join-Path $TargetBin "claude-codex-claude"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-gpt.cmd"
        Target = Join-Path $TargetBin "claude-codex-gpt.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-gpt"
        Target = Join-Path $TargetBin "claude-codex-gpt"
    },
    @{
        Source = Join-Path $RepoRoot "launchers\bin\claude-codex-proxy.cmd"
        Target = Join-Path $TargetBin "claude-codex-proxy.cmd"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\settings.json"
        Target = Join-Path $TargetClaudeCodex "config\settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\blue-icon.settings.json"
        Target = Join-Path $TargetClaudeCodex "config\blue-icon.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\themes\claude-codex-blue.json"
        Target = Join-Path $TargetClaudeCodex "config\themes\claude-codex-blue.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\blue-icon.settings.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\blue-icon.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\themes\claude-codex-blue.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\themes\claude-codex-blue.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\fcc-picker.settings.json"
        Target = Join-Path $TargetClaudeCodex "config\fcc-picker.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\fcc-ultracode.settings.json"
        Target = Join-Path $TargetClaudeCodex "config\fcc-ultracode.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\models.json"
        Target = Join-Path $TargetClaudeCodex "config\models.json"
    },
    @{
        Source = Join-Path $RepoRoot "tmp\empty-mcp.json"
        Target = Join-Path $TargetClaudeCodex "config\empty-mcp.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\ultracode.settings.json"
        Target = Join-Path $TargetClaudeCodex "config\ultracode.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\orchestration-policy.md"
        Target = Join-Path $TargetClaudeCodex "config\orchestration-policy.md"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\settings.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\fcc-picker.settings.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\fcc-picker.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\fcc-ultracode.settings.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\fcc-ultracode.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\models.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\models.json"
    },
    @{
        Source = Join-Path $RepoRoot "tmp\empty-mcp.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\empty-mcp.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\ultracode.settings.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\ultracode.settings.json"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\orchestration-policy.md"
        Target = Join-Path $TargetClaudeCodex "config-slim\orchestration-policy.md"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex-proxy\config.toml"
        Target = Join-Path $TargetProxy "config.toml"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex-proxy\scripts\claude-codex-proxy.ps1"
        Target = Join-Path $TargetProxy "scripts\claude-codex-proxy.ps1"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex-proxy\scripts\ccproxy-lb.py"
        Target = Join-Path $TargetProxy "scripts\ccproxy-lb.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\regenerate-codex-headers.py"
        Target = Join-Path $TargetProxy "scripts\regenerate-codex-headers.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\rotate-trace.ps1"
        Target = Join-Path $TargetProxy "scripts\rotate-trace.ps1"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\rotate-schedule.ps1"
        Target = Join-Path $TargetProxy "scripts\rotate-schedule.ps1"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\errors-watch.ps1"
        Target = Join-Path $TargetProxy "scripts\errors-watch.ps1"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\llms\formatters\anthropic_to_openai\requests.py"
        Target = Join-Path $TargetSitePackages "llms\formatters\anthropic_to_openai\requests.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\llms\formatters\anthropic_to_openai\compaction.py"
        Target = Join-Path $TargetSitePackages "llms\formatters\anthropic_to_openai\compaction.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\llms\formatters\anthropic_to_openai\streams.py"
        Target = Join-Path $TargetSitePackages "llms\formatters\anthropic_to_openai\streams.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\plugins\codex\adapter.py"
        Target = Join-Path $TargetSitePackages "plugins\codex\adapter.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\plugins\codex\model_defaults.py"
        Target = Join-Path $TargetSitePackages "plugins\codex\model_defaults.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\plugins\claude_api\routes.py"
        Target = Join-Path $TargetSitePackages "plugins\claude_api\routes.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\dispatcher\__init__.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher\__init__.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\dispatcher\config.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher\config.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\dispatcher\routes.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher\routes.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\dispatcher\plugin.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher\plugin.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\gemini_cli\__init__.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli\__init__.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\gemini_cli\config.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli\config.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\gemini_cli\adapter.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli\adapter.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\gemini_cli\plugin.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli\plugin.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fugu\__init__.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fugu\__init__.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fugu\config.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fugu\config.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fugu\adapter.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fugu\adapter.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fugu\plugin.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fugu\plugin.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fcc\__init__.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fcc\__init__.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fcc\config.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fcc\config.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fcc\adapter.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fcc\adapter.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\fcc\plugin.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\fcc\plugin.py"
    }
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\nanogpt\__init__.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\__init__.py"
    }
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\nanogpt\config.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\config.py"
    }
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\nanogpt\adapter.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\adapter.py"
    }
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\nanogpt\credentials.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\credentials.py"
    }
    @{
        Source = Join-Path $RepoRoot "runtime\ccproxy-plugins\nanogpt\plugin.py"
        Target = Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\plugin.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\subagent-models.json"
        Target = Join-Path $TargetClaudeCodex "config\subagent-models.json"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\resolve-subagent.py"
        Target = Join-Path $TargetClaudeCodex "config\resolve-subagent.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\subagent_models.py"
        Target = Join-Path $TargetClaudeCodex "config\subagent_models.py"
    },
    @{
        Source = Join-Path $RepoRoot "runtime\claude-codex\subagent-models.json"
        Target = Join-Path $TargetClaudeCodex "config-slim\subagent-models.json"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\resolve-subagent.py"
        Target = Join-Path $TargetClaudeCodex "config-slim\resolve-subagent.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\subagent_models.py"
        Target = Join-Path $TargetClaudeCodex "config-slim\subagent_models.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\agent-model-guard.py"
        Target = Join-Path $HomeDir ".claude\scripts\agent-model-guard.py"
    },
    @{
        Source = Join-Path $RepoRoot "scripts\subagent_models.py"
        Target = Join-Path $HomeDir ".claude\scripts\subagent_models.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\services\adapters\simple_converters.py"
        Target = Join-Path $TargetSitePackages "services\adapters\simple_converters.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\services\adapters\delta_utils.py"
        Target = Join-Path $TargetSitePackages "services\adapters\delta_utils.py"
    },
    @{
        Source = Join-Path $RepoRoot "patches\ccproxy\streaming\buffer.py"
        Target = Join-Path $TargetSitePackages "streaming\buffer.py"
    }
)

# A missing optional source leaves any installed binary and its prior trust
# record intact. Never hash the retained file into a new trusted baseline:
# doing so would silently adopt a local edit made since the previous install.
# A source checkout without a release build keeps the PowerShell launcher.
$retainedOptionalEntries = @($copyMap | Where-Object { $_.ContainsKey("Optional") -and $_.Optional -and -not (Test-Path -LiteralPath $_.Source) })
$copyMap = @($copyMap | Where-Object { -not ($_.ContainsKey("Optional") -and $_.Optional -and -not (Test-Path -LiteralPath $_.Source)) })

$patchManifest = ConvertFrom-Json ([IO.File]::ReadAllText((Join-Path $RepoRoot "patches\manifest.json"), [Text.Encoding]::UTF8))
$patchRoot = Join-Path $RepoRoot "patches\ccproxy"
$copyMap = @($copyMap | Where-Object { -not $_.Source.StartsWith($patchRoot, [StringComparison]::OrdinalIgnoreCase) })
foreach ($relative in @($patchManifest.upstreamFiles) + @($patchManifest.addedFiles)) {
    $copyMap += @{ Source = Join-Path $patchRoot $relative; Target = Join-Path $TargetSitePackages $relative }
}

function Get-OwnedExisting {
    param($Existing, $Generated)
    $result = [pscustomobject]@{}
    foreach ($property in $Generated.PSObject.Properties) {
        $current = if ($Existing) { $Existing.PSObject.Properties[$property.Name] } else { $null }
        if (-not $current) { continue }
        $value = if ($property.Value -is [pscustomobject] -and $current.Value -is [pscustomobject]) { Get-OwnedExisting $current.Value $property.Value } else { $current.Value }
        $result | Add-Member -NotePropertyName $property.Name -NotePropertyValue $value
    }
    return $result
}

function Test-SettingEqual {
    param($Left, $Right)
    return (($Left | ConvertTo-Json -Depth 100 -Compress) -ceq ($Right | ConvertTo-Json -Depth 100 -Compress))
}

function Test-SubagentGuardHook {
    param($Value)
    return ($null -ne $Value -and ($Value | ConvertTo-Json -Depth 100 -Compress) -match 'agent-model-guard(?:\.py)?')
}

function Merge-SubagentGuardHook {
    param($Existing, $Generated, $Previous = $null)
    # Only this dedicated hook is array-merged; all other settings keep their
    # existing ownership rules. Unrelated hooks and their order stay intact.
    $desiredRows = @($Generated)
    if ($desiredRows.Count -ne 1 -or @($desiredRows[0].hooks).Count -ne 1) {
        throw 'Unexpected generated child guard hook'
    }
    $desired = $desiredRows[0]
    $result = @()
    $found = 0
    foreach ($row in @($Existing)) {
        if ($null -eq $row) { continue }
        if (Test-SubagentGuardHook $row) {
            if (-not $row.PSObject.Properties['matcher'] -or
                $row.matcher -cne $desired.matcher -or
                -not $row.PSObject.Properties['hooks'] -or
                $row.hooks -isnot [array] -or
                @($row.PSObject.Properties).Count -ne 2) {
                throw 'Unknown or edited child guard hook'
            }
            foreach ($hook in $row.hooks) {
                if (Test-SubagentGuardHook $hook) {
                    $found++
                    if (@($hook.PSObject.Properties).Count -ne @($desired.hooks[0].PSObject.Properties).Count) {
                        throw 'Unknown or edited child guard hook'
                    }
                    foreach ($property in $desired.hooks[0].PSObject.Properties) {
                        $actual = $hook.PSObject.Properties[$property.Name]
                        if (-not $actual -or -not (Test-SettingEqual $actual.Value $property.Value)) {
                            throw 'Unknown or edited child guard hook'
                        }
                    }
                }
            }
        }
        $result += $row
    }
    if ($found -gt 1) { throw 'Duplicate child guard hooks' }
    if ($found -eq 0 -and (Test-SubagentGuardHook $Previous)) {
        throw 'Locally removed or edited owned child guard hook'
    }
    if ($found -eq 0) { $result += $desired }
    # Do not let PowerShell unwrap a one-element array into a scalar.
    return ,$result
}

function Merge-OwnedSettings {
    param([object]$Existing, [object]$Generated, [object]$Previous = $null, [string]$Path = "settings")
    if ($null -eq $Existing) { $Existing = [pscustomobject]@{} }
    if ($null -eq $Generated) { $Generated = [pscustomobject]@{} }
    $names = @($Generated.PSObject.Properties | ForEach-Object { $_.Name })
    if ($null -ne $Previous) { $names += @($Previous.PSObject.Properties | ForEach-Object { $_.Name }) }
    foreach ($name in @($names | Sort-Object -Unique)) {
        $current = $Existing.PSObject.Properties[$name]
        $desired = $Generated.PSObject.Properties[$name]
        $prior = if ($null -ne $Previous) { $Previous.PSObject.Properties[$name] } else { $null }
        if ($name -ceq 'PreToolUse' -and $Path.EndsWith('.hooks') -and $desired -and (Test-SubagentGuardHook $desired.Value)) {
            $value = Merge-SubagentGuardHook $(if ($current) { $current.Value } else { $null }) $desired.Value $(if ($prior) { $prior.Value } else { $null })
            $Existing | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
            continue
        }
        if ($desired -and $desired.Value -is [pscustomobject] -and (-not $current -or $current.Value -is [pscustomobject]) -and (-not $prior -or $prior.Value -is [pscustomobject])) {
            $value = Merge-OwnedSettings -Existing $(if ($current) { $current.Value } else { $null }) -Generated $desired.Value -Previous $(if ($prior) { $prior.Value } else { $null }) -Path "$Path.$name"
            $Existing | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
            continue
        }
        if ($desired) {
            if ($current -and -not (Test-SettingEqual $current.Value $desired.Value) -and (-not $prior -or -not (Test-SettingEqual $current.Value $prior.Value))) {
                throw "Locally edited owned setting: $Path.$name"
            }
            if (-not $current -and $prior) { throw "Locally removed owned setting: $Path.$name" }
            $Existing | Add-Member -NotePropertyName $name -NotePropertyValue $desired.Value -Force
        }
        elseif ($current) {
            if ($prior.Value -is [pscustomobject] -and $current.Value -is [pscustomobject]) {
                $value = Merge-OwnedSettings -Existing $current.Value -Generated ([pscustomobject]@{}) -Previous $prior.Value -Path "$Path.$name"
                if (@($value.PSObject.Properties).Count) { $current.Value = $value } else { $Existing.PSObject.Properties.Remove($name) }
            }
            elseif (Test-SettingEqual $current.Value $prior.Value) { $Existing.PSObject.Properties.Remove($name) }
            else { throw "Locally edited retired setting: $Path.$name" }
        }
    }
    return $Existing
}

function Get-InstallText {
    param([string]$Source, [string]$Target)
    $text = [IO.File]::ReadAllText($Source, [Text.Encoding]::UTF8)
    if ($Target -match '\.(cmd|bat|ps1)$') {
        $text = $text.TrimStart([char]0xFEFF) -replace "`r`n|`r", "`n"
        $text = $text -replace "`n", "`r`n"
    }
    if ($Source -eq (Join-Path $RepoRoot 'runtime\claude-codex-proxy\config.toml') -and $text -notmatch '(?m)^\[plugin_discovery\]') {
        $pluginsDir = (Join-Path $HomeDir '.config\ccproxy\plugins') -replace '\\', '/'
        $text += "`n[plugin_discovery]`ndirectories = [`"$pluginsDir`"]`n"
    }
    return $text
}

function Test-PristineUpstream {
    param([string]$Target)
    if (-not $Target.StartsWith($TargetSitePackages + '\', [StringComparison]::OrdinalIgnoreCase)) { return $false }
    $relative = 'ccproxy/' + ($Target.Substring($TargetSitePackages.Length + 1) -replace '\\', '/')
    $records = @(Get-ChildItem -LiteralPath (Split-Path $TargetSitePackages) -Directory -Filter 'ccproxy_api-*.dist-info' | ForEach-Object { Join-Path $_.FullName 'RECORD' } | Where-Object { Test-Path -LiteralPath $_ })
    if ($records.Count -ne 1) { return $false }
    $rows = @([IO.File]::ReadAllText($records[0], [Text.Encoding]::UTF8) | ConvertFrom-Csv -Header Path,Hash,Size | Where-Object { $_.Path -ceq $relative })
    if ($rows.Count -ne 1 -or $rows[0].Hash -notmatch '^sha256=') { return $false }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = [Convert]::ToBase64String($sha.ComputeHash([IO.File]::ReadAllBytes($Target))).TrimEnd('=') -replace '\+', '-' -replace '/', '_' }
    finally { $sha.Dispose() }
    return $rows[0].Hash -ceq ('sha256=' + $digest)
}

function Read-InstallAdoption {
    param([string]$Path)
    $data = ConvertFrom-Json ([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8))
    if (-not $data -or -not $data.PSObject.Properties['targets'] -or $data.targets -isnot [array] -or -not $data.targets.Count -or -not $data.PSObject.Properties['reason'] -or $data.reason -isnot [string] -or [string]::IsNullOrWhiteSpace($data.reason)) {
        throw 'Adoption JSON requires a nonempty targets array and a reason string.'
    }
    foreach ($target in $data.targets) {
        if ($target -isnot [string] -or [string]::IsNullOrWhiteSpace($target)) { throw 'Adoption targets must be nonempty strings.' }
    }
    return $data
}

function Get-SettingsInstallValue {
    param($Entry, $Record)
    $exists = Test-Path -LiteralPath $Entry.Target
    $adopt = $Entry.Target -in $AdoptInstalled
    $prior = if ($Record -and $Record.PSObject.Properties['ownedSettings']) { $Record.ownedSettings } else { $null }
    if (-not $exists -and $prior -and -not $adopt) { throw "Locally removed settings file: $($Entry.Target); explicitly adopt to recreate." }
    $existing = if ($exists) { ConvertFrom-Json ([IO.File]::ReadAllText($Entry.Target, [Text.Encoding]::UTF8)) } else { $null }
    $generated = ConvertFrom-Json ([IO.File]::ReadAllText($Entry.Source, [Text.Encoding]::UTF8))
    if ($adopt -and $exists -and $prior -and $prior.PSObject.Properties['hooks'] -and
        $prior.hooks.PSObject.Properties['PreToolUse'] -and (Test-SubagentGuardHook $prior.hooks.PreToolUse)) {
        # Adoption of other settings cannot erase the guard ownership history.
        $existingHooks = if ($existing -and $existing.PSObject.Properties['hooks'] -and
            $existing.hooks -is [pscustomobject] -and $existing.hooks.PSObject.Properties['PreToolUse']) { $existing.hooks.PreToolUse } else { $null }
        $null = Merge-SubagentGuardHook $existingHooks $generated.hooks.PreToolUse $prior.hooks.PreToolUse
    }
    if ($adopt) { $prior = Get-OwnedExisting $existing $generated }
    return Merge-OwnedSettings $existing $generated $prior $Entry.Target
}

function Get-InstallConflicts {
    param([object[]]$Entries, [string]$ManifestPath)
    $previous = @{}
    if (Test-Path -LiteralPath $ManifestPath) {
        $manifest = ConvertFrom-Json ([IO.File]::ReadAllText($ManifestPath, [Text.Encoding]::UTF8))
        foreach ($record in $manifest.files) { $previous[$record.target] = $record }
    }
    foreach ($entry in $Entries) {
        if (-not (Test-Path -LiteralPath $entry.Source)) { throw "Missing source: $($entry.Source)" }
        if ($entry.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex\settings.json')) {
            try { $null = Get-SettingsInstallValue $entry $previous[$entry.Target] }
            catch { Write-Warning $_.Exception.Message; $entry.Target }
            continue
        }
        if (-not (Test-Path -LiteralPath $entry.Target)) { continue }
        $adopt = $entry.Target -in $AdoptInstalled
        # A shared guard may only be identical or unchanged since our manifest.
        # Explicit adoption must not silently replace an unknown user guard.
        if ($adopt -and $entry.Target -ne (Join-Path $HomeDir '.claude\scripts\agent-model-guard.py')) { continue }
        $actual = (Get-FileHash -LiteralPath $entry.Target -Algorithm SHA256).Hash
        if ($entry.Target -match '\.exe$') {
            if ($actual -eq (Get-FileHash -LiteralPath $entry.Source -Algorithm SHA256).Hash) { continue }
            if (-not $previous.ContainsKey($entry.Target) -or $actual -ne $previous[$entry.Target].sha256) { $entry.Target }
            continue
        }
        $expected = Get-InstallText $entry.Source $entry.Target
        $installedText = [IO.File]::ReadAllText($entry.Target, [Text.Encoding]::UTF8)
        if ($installedText -ceq $expected) { continue }
        if ($entry.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex-proxy\config.toml')) {
            $canonicalInstalled = ($installedText -replace "`r`n|`r", "`n") -replace ([string][char]0xFEFF), ''
            $canonicalExpected = ($expected -replace "`r`n|`r", "`n") -replace ([string][char]0xFEFF), ''
            if ($canonicalInstalled.TrimEnd() -ceq $canonicalExpected.TrimEnd()) { continue }
        }
        if ((Test-PristineUpstream $entry.Target)) { continue }
        if (-not $previous.ContainsKey($entry.Target) -or $actual -ne $previous[$entry.Target].sha256) { $entry.Target }
    }
}

$InstallManifestPath = Join-Path $TargetProxy "install-manifest.json"
if ($AdoptionFile) {
    if ($AdoptInstalled.Count -or $AdoptionReason) { throw 'Use either -AdoptionFile or -AdoptInstalled/-AdoptionReason, not both.' }
    $adoption = Read-InstallAdoption $AdoptionFile
    $AdoptInstalled = @($adoption.targets)
    $AdoptionReason = $adoption.reason
}
foreach ($target in $AdoptInstalled) {
    $settingsTargets = @($copyMap | Where-Object { $_.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex\settings.json') } | ForEach-Object { $_.Target })
    if ([string]::IsNullOrWhiteSpace($AdoptionReason) -or $target -notin @($copyMap.Target) -or (-not (Test-Path -LiteralPath $target) -and $target -notin $settingsTargets)) {
        throw "Adoption requires an exact copy target (existing unless settings) and -AdoptionReason: $target"
    }
    Write-Warning "Explicit adoption (backup before replacing): $target; reason: $AdoptionReason"
}
$conflicts = @(Get-InstallConflicts -Entries $copyMap -ManifestPath $InstallManifestPath)
if ($conflicts.Count -gt 0) {
    foreach ($conflict in $conflicts) { Write-Warning "Unmanaged or locally edited installed file: $conflict" }
    throw "Install stopped before copying. Compare and preserve local edits first. To replace selected targets or recreate missing settings, use -AdoptionFile <JSON path> with {""targets"":[""exact full target""],""reason"":""reviewed reason""}; see README install conflict recovery. -Force does not bypass this protection."
}

# === Pre-flight: detect upstream changes for ccproxy patches ===
# If installed file differs from patch source AND installed mtime is newer,
# upstream may have shipped improvements that the old patch would clobber.
# Example: ccproxy 0.2.9 added name/call_id preservation in delta_utils.py.
$patchPattern = [regex]::Escape((Join-Path $RepoRoot "patches\ccproxy"))
$patchEntries = $copyMap | Where-Object { $_.Source -match $patchPattern }
$upstreamChanged = @()
foreach ($entry in $patchEntries) {
    if (-not (Test-Path $entry.Source)) { continue }
    if (-not (Test-Path $entry.Target)) { continue }
    $patchHash = (Get-FileHash $entry.Source -Algorithm SHA256).Hash
    $installedHash = (Get-FileHash $entry.Target -Algorithm SHA256).Hash
    if ($patchHash -eq $installedHash) { continue }
    $patchMtime = (Get-Item $entry.Source).LastWriteTime
    $installedMtime = (Get-Item $entry.Target).LastWriteTime
    if ($installedMtime -gt $patchMtime -or (Test-PristineUpstream $entry.Target)) {
        $upstreamChanged += $entry
    }
}
if ($upstreamChanged.Count -gt 0) {
    Write-Host ""
    Write-Warning "Upstream files newer than patches/ detected:"
    foreach ($entry in $upstreamChanged) {
        $rel = $entry.Source.Substring($RepoRoot.Length).TrimStart('\')
        $instSize = (Get-Item $entry.Target).Length
        $patchSize = (Get-Item $entry.Source).Length
        $instMtime = (Get-Item $entry.Target).LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        Write-Warning "  $rel"
        Write-Warning "    installed: $instSize bytes (mtime $instMtime)"
        Write-Warning "    patch:     $patchSize bytes"
    }
    Write-Warning ""
    Write-Warning "Overwriting these may lose upstream improvements."
    Write-Warning "Inspect the diffs first:"
    foreach ($entry in $upstreamChanged) {
        Write-Warning "  diff `"$($entry.Target)`" `"$($entry.Source)`""
    }
    Write-Warning ""
    if (-not $Force) {
        $resp = Read-Host "Continue overwriting anyway? [y/N]"
        if ($resp -notmatch '^[yY]') {
            Write-Host "Aborted. Update patches/ first, or re-run with -Force to override."
            exit 0
        }
    }
    else {
        Write-Host "  -Force: continuing despite upstream changes" -ForegroundColor Yellow
    }
}

# Helper: Copy a file, forcing CRLF line endings for Windows shell scripts.
#
# Background: cmd.exe is brittle when a .cmd/.bat file is LF-only — multi-line
# control flow (if/else, for, call) silently misbehaves. Symptoms range from
# "< NUL" being treated as a literal token (bench failure observed by WP-X)
# to entire if-blocks being skipped. We saw this on Windows 11 with bash-edited
# .cmd files committed via tools that strip CR. Even though .gitattributes
# declares `*.cmd text eol=crlf`, files that were checked in as LF-only are
# preserved as-is by checkout (autocrlf does not retroactively renormalize),
# so we rewrite line endings on install as a belt-and-braces guard.
#
# .ps1 is also forced to CRLF; PowerShell tolerates LF but signing and some
# legacy hosts assume CRLF. Bash scripts (no extension here) are untouched.
function Copy-LauncherFile {
    param([string]$Source, [string]$Target)

    $forceCrlf = $Target -match '\.(cmd|bat|ps1)$'
    if ($forceCrlf) {
        $bytes = [System.IO.File]::ReadAllBytes($Source)
        # Decode as UTF-8 (no BOM expected for these files) so we can
        # operate on text and preserve any non-ASCII content verbatim.
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        # Strip BOM if present (cmd.exe chokes on a BOM at byte 0).
        if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
            $text = $text.Substring(1)
        }
        # Normalize CRLF/CR/LF -> LF, then promote to CRLF.
        $text = $text -replace "`r`n", "`n"
        $text = $text -replace "`r", "`n"
        $text = $text -replace "`n", "`r`n"
        # A running helper can hold its own script open on Windows. Avoid an
        # unnecessary overwrite (and lock failure) when the installed text is
        # already identical to the normalized source.
        if ((Test-Path $Target) -and ([string]::Equals([System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($Target)), $text, [StringComparison]::Ordinal))) {
            return
        }
        # Write without BOM (UTF8Encoding($false)).
        $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($Target, $text, $utf8NoBom)
    }
    else {
        Copy-Item $Source $Target -Force
    }
}

$preparedSettings = @{}
$oldSettingsRecords = @{}
if (Test-Path -LiteralPath $InstallManifestPath) {
    $oldManifest = ConvertFrom-Json ([IO.File]::ReadAllText($InstallManifestPath, [Text.Encoding]::UTF8))
    foreach ($record in $oldManifest.files) { $oldSettingsRecords[$record.target] = $record }
}
foreach ($item in $copyMap) {
    if ($item.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex\settings.json')) {
        $preparedSettings[$item.Target] = Get-SettingsInstallValue $item $oldSettingsRecords[$item.Target]
    }
}
$proxyToken = [Environment]::GetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($proxyToken)) {
    $proxyToken = [Environment]::GetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", "Process")
    if ([string]::IsNullOrWhiteSpace($proxyToken)) {
        $proxyToken = New-ProxyToken
        Write-Host "Proxy token: generated a new per-user CLAUDE_CODEX_PROXY_TOKEN."
    }
    else {
        Write-Host "Proxy token: recording current process CLAUDE_CODEX_PROXY_TOKEN in the user environment."
    }
    [Environment]::SetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", $proxyToken, "User")
}
else {
    Write-Host "Proxy token: user environment value already present."
}
$env:CLAUDE_CODEX_PROXY_TOKEN = $proxyToken

New-Item -ItemType Directory -Force $BackupRoot | Out-Null
foreach ($item in $copyMap) {
    $targetDir = Split-Path -Parent $item.Target
    New-Item -ItemType Directory -Force $targetDir | Out-Null

    if (Test-Path $item.Target) {
        $backupTarget = Join-Path $BackupRoot ($item.Target.Substring($HomeDir.Length).TrimStart('\'))
        $backupDir = Split-Path -Parent $backupTarget
        New-Item -ItemType Directory -Force $backupDir | Out-Null
        if ($item.Target -match '\.exe$') {
            # Running Windows images can be renamed, but cannot be overwritten.
            Move-Item -LiteralPath $item.Target -Destination $backupTarget
        }
        else { Copy-Item $item.Target $backupTarget -Force }
    }

    if ($item.Source -eq (Join-Path $RepoRoot "runtime\claude-codex\settings.json")) {
        $merged = $preparedSettings[$item.Target]
        [IO.File]::WriteAllText($item.Target, (($merged | ConvertTo-Json -Depth 100) + "`n"), [Text.UTF8Encoding]::new($false))
    }
    elseif ($item.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex-proxy\config.toml')) {
        [IO.File]::WriteAllText($item.Target, (Get-InstallText $item.Source $item.Target), [Text.UTF8Encoding]::new($false))
    }
    else { Copy-LauncherFile -Source $item.Source -Target $item.Target }
}

# === Shared resource junctions: link claude-codex config to claude config ===
$ClaudeConfig = Join-Path $HomeDir ".claude"
$ClaudeCodexConfig = Join-Path $TargetClaudeCodex "config"
$wiringIssues = @()

# Claude Code resolves every ~\.claude path under CLAUDE_CONFIG_DIR. The
# launcher points CLAUDE_CONFIG_DIR at ~\.claude-codex\config, so skills,
# rules, custom slash commands, subagents and plugins must all be junctioned
# in or they are invisible to claude-codex. commands + plugins are what make
# the unified slash-command system (custom /command files and plugin commands)
# available. Targets that do not exist are skipped by the loop below.
$junctions = @(
    @{ Link = Join-Path $ClaudeCodexConfig "skills";   Target = Join-Path $ClaudeConfig "skills" },
    @{ Link = Join-Path $ClaudeCodexConfig "rules";    Target = Join-Path $ClaudeConfig "rules" },
    @{ Link = Join-Path $ClaudeCodexConfig "commands"; Target = Join-Path $ClaudeConfig "commands" },
    @{ Link = Join-Path $ClaudeCodexConfig "agents";   Target = Join-Path $ClaudeConfig "agents" },
    @{ Link = Join-Path $ClaudeCodexConfig "plugins";  Target = Join-Path $ClaudeConfig "plugins" }
)

foreach ($j in $junctions) {
    if (Test-Path $j.Target) {
        if (Test-Path $j.Link) {
            $item = Get-Item $j.Link -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $resolvedTarget = $item.Target | Select-Object -First 1
                if ($resolvedTarget -and ([string]$resolvedTarget).TrimEnd('\') -ieq $j.Target.TrimEnd('\')) {
                    Write-Host "Junction already exists: $($j.Link)"
                }
                else {
                    $wiringIssues += "$($j.Link) points to an unexpected target"
                    Write-Warning "Shared path target is unexpected and was left unchanged: $($j.Link)"
                }
                continue
            }
            # Not a junction — back up and remove
            $backupTarget = Join-Path $BackupRoot ($j.Link.Substring($HomeDir.Length).TrimStart('\'))
            Copy-Item $j.Link $backupTarget -Recurse -Force
            $wiringIssues += "$($j.Link) is not a junction"
            Write-Warning "Shared path is not a junction and was left unchanged: $($j.Link)"
            continue
        }
        New-Item -ItemType Junction -Path $j.Link -Target $j.Target | Out-Null
        Write-Host "Junction created: $($j.Link) -> $($j.Target)"
    } else {
        Write-Host "Skipped junction (target not found): $($j.Target)"
    }
}

# CLAUDE.md: hard link (symlink requires admin)
function Test-HardLinkPair {
    param([string]$Link, [string]$Target)
    if (-not (Test-Path -LiteralPath $Link) -or -not (Test-Path -LiteralPath $Target)) { return $false }
    try {
        $links = @(& fsutil.exe hardlink list $Link 2>$null | ForEach-Object { ([string]$_).Trim() })
        if ($LASTEXITCODE -ne 0) { return $false }
        $targetRelative = "\" + $Target.Substring(3).Replace('/', '\')
        return $links -contains $targetRelative
    }
    catch {
        return $false
    }
}

$claudeMdLink = Join-Path $ClaudeCodexConfig "CLAUDE.md"
$claudeMdTarget = Join-Path $ClaudeConfig "CLAUDE.md"
if ((Test-Path $claudeMdTarget) -and (-not (Test-Path $claudeMdLink))) {
    New-Item -ItemType HardLink -Path $claudeMdLink -Target $claudeMdTarget | Out-Null
    Write-Host "Hard link created: $claudeMdLink -> $claudeMdTarget"
} elseif (Test-Path $claudeMdLink) {
    if (Test-HardLinkPair -Link $claudeMdLink -Target $claudeMdTarget) {
        Write-Host "CLAUDE.md hard link already exists: $claudeMdLink"
    }
    else {
        $wiringIssues += "$claudeMdLink is not linked to $claudeMdTarget"
        Write-Warning "CLAUDE.md is not the shared hard link and was left unchanged: $claudeMdLink"
    }
}

# Memory junction (shared across Claude Code and claude-codex)
$projectSlug = "C--" + ($HomeDir.Substring(3) -replace '[\\/]', '-')
$projectDir = Join-Path $ClaudeCodexConfig "projects\$projectSlug"
$memoryLink = Join-Path $projectDir "memory"
$memoryTarget = Join-Path $ClaudeConfig "projects\$projectSlug\memory"
if ((Test-Path $memoryTarget) -and (-not (Test-Path $memoryLink))) {
    New-Item -ItemType Directory -Force $projectDir | Out-Null
    New-Item -ItemType Junction -Path $memoryLink -Target $memoryTarget | Out-Null
    Write-Host "Memory junction created: $memoryLink -> $memoryTarget"
} elseif (Test-Path $memoryLink) {
    $memoryItem = Get-Item -LiteralPath $memoryLink -Force
    if ($memoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        $memoryResolvedTarget = @($memoryItem.Target | Select-Object -First 1)[0]
        if ($memoryResolvedTarget -and ([string]$memoryResolvedTarget).TrimEnd('\') -ieq $memoryTarget.TrimEnd('\')) {
            Write-Host "Memory junction already exists: $memoryLink"
        }
        else {
            $wiringIssues += "$memoryLink points to an unexpected target"
            Write-Warning "Memory junction target is unexpected and was left unchanged: $memoryLink"
        }
    }
    else {
        $wiringIssues += "$memoryLink is not a junction"
        Write-Warning "Memory path is a real directory and was left unchanged: $memoryLink"
    }
}

# === Dispatcher filesystem plugin discovery ===
# The dispatcher plugin lives at ~/.config/ccproxy/plugins/dispatcher/ and is a
# filesystem plugin, so ccproxy only finds it when plugin_discovery.directories
# lists that path. generate-model-configs.py can't emit an absolute HOME path
# (it runs from the repo), so we inject [plugin_discovery] here at install time.
# Idempotent: skipped if the section already exists. The copied config.toml is
# regenerated from runtime/ on every install, so this re-adds it each time.
$ccproxyConfigPath = Join-Path $TargetProxy "config.toml"
if (Test-Path $ccproxyConfigPath) {
    $tomlText = Get-Content $ccproxyConfigPath -Raw
    if ($tomlText -notmatch '(?m)^\[plugin_discovery\]') {
        $pluginsDir = (Join-Path $HomeDir ".config\ccproxy\plugins") -replace '\\', '/'
        $block = "`n[plugin_discovery]`ndirectories = [`"$pluginsDir`"]`n"
        Add-Content -Path $ccproxyConfigPath -Value $block -Encoding UTF8
        Write-Host "  Injected [plugin_discovery] directories = $pluginsDir"
    }
    else {
        Write-Host "  [plugin_discovery] already present in config.toml"
    }
}

Write-Host ""
if (Test-Path -LiteralPath $InstallManifestPath) {
    Copy-Item -LiteralPath $InstallManifestPath -Destination (Join-Path $BackupRoot "install-manifest.json")
}
$installRecords = @($copyMap | ForEach-Object {
    $record = @{ source = $_.Source; target = $_.Target; sha256 = (Get-FileHash -LiteralPath $_.Target -Algorithm SHA256).Hash }
    if ($_.Source -eq (Join-Path $RepoRoot 'runtime\claude-codex\settings.json')) {
        $record.ownedSettings = ConvertFrom-Json ([IO.File]::ReadAllText($_.Source, [Text.Encoding]::UTF8))
    }
    $record
})
if ($retainedOptionalEntries.Count -gt 0 -and (Test-Path -LiteralPath $InstallManifestPath)) {
    $previousManifest = ConvertFrom-Json ([IO.File]::ReadAllText($InstallManifestPath, [Text.Encoding]::UTF8))
    foreach ($entry in $retainedOptionalEntries) {
        if (-not (Test-Path -LiteralPath $entry.Target)) { continue }
        $previousRecord = @($previousManifest.files | Where-Object { $_.target -eq $entry.Target })
        if ($previousRecord.Count -eq 1) { $installRecords += $previousRecord[0] }
    }
}
$installManifest = @{ schemaVersion = 2; adoptedTargets = $AdoptInstalled; adoptionReason = $AdoptionReason; installedAt = (Get-Date).ToString('o'); backupRoot = $BackupRoot; files = $installRecords }
[IO.File]::WriteAllText($InstallManifestPath, (($installManifest | ConvertTo-Json -Depth 100) + "`n"), [Text.UTF8Encoding]::new($false))
Write-Host "Installed files from $RepoRoot"
Write-Host "Backup directory: $BackupRoot"
if ($wiringIssues.Count -gt 0) {
    Write-Warning "Shared configuration wiring needs manual reconciliation:"
    foreach ($issue in $wiringIssues) { Write-Warning "  $issue" }
}

# === Post-apply self-diagnosis ===
# Verify the tool_reference normalizer is actually present in installed files.
# If the marker is missing, patches did not apply and the next MCP tool_result
# will trigger 400 "Failed to convert request using format chain".
$diagFiles = @(
    "services\adapters\simple_converters.py",
    "llms\formatters\anthropic_to_openai\requests.py",
    "llms\formatters\anthropic_to_openai\streams.py"
)
$diagFailed = @()
foreach ($rel in $diagFiles) {
    $path = Join-Path $TargetSitePackages $rel
    if (-not (Test-Path $path)) {
        $diagFailed += "$rel (missing)"
        continue
    }
    $content = Get-Content $path -Raw
    if ($content -notmatch 'normalize_request_for_validation') {
        $diagFailed += $rel
    }
}
Write-Host ""
if ($diagFailed.Count -gt 0) {
    Write-Host "ERROR: post-install diagnosis FAILED." -ForegroundColor Red
    Write-Host "  normalize_request_for_validation not found in:" -ForegroundColor Red
    foreach ($f in $diagFailed) {
        Write-Host "    $f" -ForegroundColor Red
    }
    Write-Host "  Patches did not apply correctly. Check the script output above." -ForegroundColor Red
    exit 1
}
else {
    Write-Host "Post-install diagnosis: normalize_request_for_validation present in all 3 files."
}

# === Multiprovider filesystem plugins present? ===
$pluginChecks = @(
    @{ Name = "dispatcher"; Path = (Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher\plugin.py") },
    @{ Name = "gemini_cli"; Path = (Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli\plugin.py") },
    @{ Name = "fugu"; Path = (Join-Path $HomeDir ".config\ccproxy\plugins\fugu\plugin.py") },
    @{ Name = "fcc"; Path = (Join-Path $HomeDir ".config\ccproxy\plugins\fcc\plugin.py") },
    @{ Name = "nanogpt"; Path = (Join-Path $HomeDir ".config\ccproxy\plugins\nanogpt\plugin.py") }
)
$pluginMissing = @($pluginChecks | Where-Object { -not (Test-Path $_.Path) })
if ($pluginMissing.Count -gt 0) {
    Write-Host ""
    Write-Host "WARNING: multiprovider plugin files missing:" -ForegroundColor Yellow
    foreach ($p in $pluginMissing) { Write-Host "    $($p.Name): $($p.Path)" -ForegroundColor Yellow }
    Write-Host "  Dispatcher/Gemini routing will not work until these are present." -ForegroundColor Yellow
}
else {
    Write-Host "Multiprovider plugins present: dispatcher, gemini_cli, fugu, fcc, nanogpt (discovered via [plugin_discovery])."
}

# === claude_api routes patch applied? (Claude backend が 422 になるのを防ぐ) ===
$claudeRoutesPath = Join-Path $TargetSitePackages "plugins\claude_api\routes.py"
if (Test-Path $claudeRoutesPath) {
    $crContent = Get-Content $claudeRoutesPath -Raw
    if ($crContent -match 'claude-codex-hybrid patch') {
        Write-Host "claude_api routes patch: applied."
    }
    else {
        Write-Host ""
        Write-Host "WARNING: claude_api routes patch NOT applied - Claude backend will 422 on Claude Code requests." -ForegroundColor Yellow
        Write-Host "  Re-run install.ps1 -Force (local edit conflicts: compare/backup, then -AdoptionFile JSON; see README), or check the upstream-change pre-flight." -ForegroundColor Yellow
    }
}

if ($RestartProxy) {
    $proxyCommand = Join-Path $TargetBin "claude-codex-proxy.cmd"
    Write-Host ""
    Write-Host "Restarting the shared proxy through the explicit maintenance path..."
    & $proxyCommand restart
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Proxy maintenance restart failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
}
elseif (Test-Path (Join-Path $TargetProxy "run\ccproxy.pid")) {
    Write-Warning "Proxy files were updated while a process is recorded as running. Run 'claude-codex-proxy restart' when active sessions are idle."
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$HomeDir = if ($env:CLAUDE_CODEX_HOME) {
    $env:CLAUDE_CODEX_HOME
}
else {
    [Environment]::GetFolderPath("UserProfile")
}
$BaseConfigDir = Join-Path $HomeDir ".claude-codex\config"
$skillProfile = if ($env:CLAUDE_CODEX_SKILL_PROFILE) { $env:CLAUDE_CODEX_SKILL_PROFILE.ToLowerInvariant() } else { "full" }
if ($skillProfile -ne "full" -and $skillProfile -ne "slim") {
    Write-Warning "Unknown CLAUDE_CODEX_SKILL_PROFILE=$skillProfile; using full."
    $skillProfile = "full"
}
$ConfigDir = if ($skillProfile -eq "slim") {
    Join-Path $HomeDir ".claude-codex\config-slim"
}
else {
    $BaseConfigDir
}
$DataDir = Join-Path $HomeDir ".claude-codex\data"
$StateDir = Join-Path $HomeDir ".claude-codex\state"
$ProxyLauncher = if ($env:CLAUDE_CODEX_PROXY_CMD) {
    $env:CLAUDE_CODEX_PROXY_CMD
}
else {
    Join-Path $HomeDir "bin\claude-codex-proxy.cmd"
}
$ClaudeExe = if ($env:CLAUDE_CODEX_CLAUDE_EXE) {
    $env:CLAUDE_CODEX_CLAUDE_EXE
}
else {
    Join-Path $HomeDir ".local\bin\claude.exe"
}
$CliArgs = @($args)

# Resolve the母艦 (main-loop) backend: --backend <name> / --backend=<name> from
# the CLI takes precedence, then CLAUDE_CODEX_BACKEND env, then models.json
# defaultBackend (resolved inside Get-ModelConfig), then "gpt". The flag is
# stripped from the args passed to claude.exe (it does not understand it).
$requestedBackend = $null
$filteredArgs = @()
for ($i = 0; $i -lt $CliArgs.Count; $i++) {
    $a = [string]$CliArgs[$i]
    if ($a -ieq "--backend") {
        if (($i + 1 -lt $CliArgs.Count) -and (-not ([string]$CliArgs[$i + 1]).StartsWith("-"))) {
            $requestedBackend = [string]$CliArgs[$i + 1]
            $i++
        }
        else {
            # 値が無い / 次が別フラグ のときは消費せず警告。--backend -p の -p を
            # 食って print mode を失う事故を防ぐ (-p は次ループで filteredArgs に残る)。
            Write-Warning "--backend requires a value (gpt/claude/gemini/fugu/fcc); ignoring."
        }
    }
    elseif ($a -ilike "--backend=*") {
        $requestedBackend = $a.Substring("--backend=".Length)
    }
    else {
        $filteredArgs += $a
    }
}
if (-not $requestedBackend -and $env:CLAUDE_CODEX_BACKEND) {
    $requestedBackend = $env:CLAUDE_CODEX_BACKEND
}
if ($requestedBackend) {
    $requestedBackend = $requestedBackend.ToLowerInvariant()
}
$CliArgs = @($filteredArgs)

function Set-EnvValue {
    param([string]$Name, [string]$Value)
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Get-PositiveIntOrNull {
    param($Value)
    $parsed = 0
    if (($null -ne $Value) -and [int]::TryParse([string]$Value, [ref]$parsed) -and ($parsed -gt 0)) {
        return $parsed
    }
    return $null
}

function Import-UserEnvIfMissing {
    param([string]$Name)
    if ([Environment]::GetEnvironmentVariable($Name, "Process")) {
        return
    }
    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($value) {
        Set-EnvValue $Name $value
    }
}

function Ensure-FccOllamaIfConfigured {
    param([bool]$WaitForReady = $true)
    $envFile = if ($env:FCC_ENV_FILE) { [string]$env:FCC_ENV_FILE } else { Join-Path $HomeDir ".fcc\.env" }
    if (-not (Test-Path -LiteralPath $envFile)) {
        return $true
    }
    $usesOllama = Select-String -LiteralPath $envFile -Pattern '^MODEL(?:_(?:FABLE|OPUS|SONNET|HAIKU))?=["'']?ollama/' -Quiet
    if (-not $usesOllama) {
        return $true
    }
    $ollamaBaseUrl = if ($env:OLLAMA_BASE_URL) { [string]$env:OLLAMA_BASE_URL } else { "" }
    if ([string]::IsNullOrWhiteSpace($ollamaBaseUrl)) {
        $baseUrlLine = Select-String -LiteralPath $envFile -Pattern '^OLLAMA_BASE_URL=' | Select-Object -First 1
        if ($null -ne $baseUrlLine) {
            $ollamaBaseUrl = ([string]$baseUrlLine.Line).Substring("OLLAMA_BASE_URL=".Length).Trim().Trim('"').Trim("'")
        }
    }
    if ([string]::IsNullOrWhiteSpace($ollamaBaseUrl)) { $ollamaBaseUrl = "http://127.0.0.1:11434" }
    $ollamaHealthUrl = $ollamaBaseUrl.TrimEnd('/') + "/api/version"
    try {
        $null = Invoke-RestMethod -Method Get -Uri $ollamaHealthUrl -TimeoutSec 2
        return $true
    }
    catch {
        # Start the configured local provider below.
    }
    try { $ollamaUri = [Uri]$ollamaBaseUrl } catch { return $false }
    if ($ollamaUri.Host -notin @("127.0.0.1", "localhost", "::1")) { return $false }
    $ollamaCommand = Get-Command "ollama.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    $localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HomeDir "AppData\Local" }
    $ollamaExe = if ($null -ne $ollamaCommand) { $ollamaCommand.Source } else { Join-Path $localAppData "Programs\Ollama\ollama.exe" }
    if (-not (Test-Path -LiteralPath $ollamaExe)) {
        return $false
    }
    try {
        Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    }
    catch {
        return $false
    }
    if (-not $WaitForReady) { return $true }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
        try {
            $null = Invoke-RestMethod -Method Get -Uri $ollamaHealthUrl -TimeoutSec 2
            return $true
        }
        catch {
            # Continue until the startup deadline.
        }
    }
    return $false
}

function Ensure-FccGateway {
    param([bool]$Required)
    $skipStart = $env:CLAUDE_CODEX_SKIP_FCC_START -eq "1"
    if ($skipStart -and -not $Required) {
        return $false
    }
    $healthUrl = if ($env:CLAUDE_CODEX_FCC_HEALTH_URL) {
        [string]$env:CLAUDE_CODEX_FCC_HEALTH_URL
    }
    else {
        "http://127.0.0.1:8082/health"
    }
    try {
        $health = Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 2
        if ($health.status -eq "healthy") {
            if (-not (Ensure-FccOllamaIfConfigured -WaitForReady $Required)) {
                if (-not $Required) { return $true }
                [Console]::Error.WriteLine("[claude-codex] FCC uses Ollama, but Ollama could not be started.")
                exit 1
            }
            return $true
        }
    }
    catch {
        # Start the local gateway below when it is installed.
    }

    if ($skipStart) {
        [Console]::Error.WriteLine("[claude-codex] FCC backend was selected, but auto-start is disabled and the FCC server is not healthy.")
        exit 1
    }

    $serverExe = if ($env:CLAUDE_CODEX_FCC_SERVER_EXE) {
        [string]$env:CLAUDE_CODEX_FCC_SERVER_EXE
    }
    else {
        Join-Path $HomeDir ".local\bin\fcc-server.exe"
    }
    if (-not (Test-Path $serverExe)) {
        if ($Required) {
            [Console]::Error.WriteLine("[claude-codex] FCC server is required for the Open Models profile but is not installed: $serverExe")
            exit 1
        }
        return $false
    }
    try {
        $previousFccHost = [Environment]::GetEnvironmentVariable("HOST", "Process")
        [Environment]::SetEnvironmentVariable("HOST", "127.0.0.1", "Process")
        try {
            Start-Process -FilePath $serverExe -WindowStyle Hidden | Out-Null
        }
        finally {
            [Environment]::SetEnvironmentVariable("HOST", $previousFccHost, "Process")
        }
    }
    catch {
        if ($Required) {
            [Console]::Error.WriteLine("[claude-codex] FCC server could not be started.")
            exit 1
        }
        return $false
    }
    if (-not $Required) {
        # Keep FCC discoverable without putting its cold-start latency on GPT,
        # Claude, Gemini, or Fugu sessions. An explicit FCC session still waits
        # for readiness below and fails closed.
        $null = Ensure-FccOllamaIfConfigured -WaitForReady $false
        return $false
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
        try {
            $health = Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 2
            if ($health.status -eq "healthy") {
                if (-not (Ensure-FccOllamaIfConfigured)) {
                    [Console]::Error.WriteLine("[claude-codex] FCC uses Ollama, but Ollama could not be started.")
                    exit 1
                }
                return $true
            }
        }
        catch {
            # Continue until the startup deadline.
        }
    }
    if ($Required) {
        [Console]::Error.WriteLine("[claude-codex] FCC server did not become healthy within 45 seconds.")
        exit 1
    }
    return $false
}

function Get-ProxyToken {
    Import-UserEnvIfMissing "CLAUDE_CODEX_PROXY_TOKEN"
    $token = [Environment]::GetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", "Process")
    if ([string]::IsNullOrWhiteSpace($token)) {
        [Console]::Error.WriteLine("[claude-codex] CLAUDE_CODEX_PROXY_TOKEN is missing.")
        [Console]::Error.WriteLine("[claude-codex] Run scripts\install.ps1 -Force once, or set a per-user token before launching.")
        exit 1
    }
    return $token
}

function Get-ModelConfig {
    param([string]$Backend)
    $fallback = @{
        baseUrlPath = "/codex"
        resolvedBackend = "gpt"
        defaultModel = "gpt-5.6-sol"
        # 実測 (2026-08-01): gpt-5.6-sol は input 247,685 で成功 → 次の要求で context_length_exceeded。
        # 上流の窓は約 250k なので、auto-compact が余裕をもって先に発火する値を申告する。
        contextTokens = 200000
        aliases = @{
            fable = "gpt-5.6-sol"
            opus = "gpt-5.6-sol"
            sonnet = "gpt-5.6-terra"
            haiku = "gpt-5.6-luna"
            subagent = "gpt-5.6-sol"
        }
        display = @{
            name = "GPT-5.6"
            fableName = "GPT-5.6 Sol"
            opusName = "GPT-5.6 Sol"
            sonnetName = "GPT-5.6 Terra"
            haikuName = "GPT-5.6 Luna"
            fableDescription = "Frontier OpenAI subscription route via Codex backend"
            opusDescription = "Frontier OpenAI subscription route via Codex backend"
            sonnetDescription = "Balanced OpenAI subscription route via Codex backend"
            haikuDescription = "Fast OpenAI subscription route via Codex backend"
        }
        capabilities = @(
            "effort",
            "xhigh_effort",
            "max_effort",
            "thinking",
            "adaptive_thinking",
            "interleaved_thinking"
        )
    }
    $path = Join-Path $ConfigDir "models.json"
    if (-not (Test-Path $path)) {
        return $fallback
    }
    try {
        $json = Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json
        $jsonProps = $json.PSObject.Properties.Name

        # backend 名解決: 引数 > json.defaultBackend > "gpt"
        $backendName = $Backend
        if (-not $backendName) {
            if (($jsonProps -contains "defaultBackend") -and $json.defaultBackend) {
                $backendName = [string]$json.defaultBackend
            }
            else {
                $backendName = "gpt"
            }
        }

        # schemaVersion 2: backends があればその backend 設定を使う
        if (($jsonProps -contains "backends") -and $json.backends) {
            $backendProps = $json.backends.PSObject.Properties.Name
            if (-not ($backendProps -contains $backendName)) {
                Write-Warning "Unknown backend '$backendName'; falling back to GPT route."
                $backendName = "gpt"
            }
            if ($backendProps -contains $backendName) {
                $b = $json.backends.$backendName
                $bProps = $b.PSObject.Properties.Name
                $aliases = @{}
                if (($bProps -contains "aliases") -and $b.aliases) {
                    foreach ($prop in $b.aliases.PSObject.Properties) {
                        $aliases[$prop.Name] = [string]$prop.Value
                    }
                }
                $display = @{}
                if (($bProps -contains "display") -and $b.display) {
                    foreach ($prop in $b.display.PSObject.Properties) {
                        $display[$prop.Name] = [string]$prop.Value
                    }
                }
                $baseUrlPath = if (($bProps -contains "baseUrlPath") -and $b.baseUrlPath) { [string]$b.baseUrlPath } else { "/codex" }
                $defModel = if (($bProps -contains "defaultModel") -and $b.defaultModel) { [string]$b.defaultModel } else { "gpt-5.6-sol" }
                $caps = if (($bProps -contains "capabilities") -and $b.capabilities) { @($b.capabilities | ForEach-Object { [string]$_ }) } else { @() }
                $contextTokens = if ($bProps -contains "contextTokens") { Get-PositiveIntOrNull $b.contextTokens } else { $null }
                return @{
                    baseUrlPath = $baseUrlPath
                    resolvedBackend = $backendName
                    defaultModel = $defModel
                    contextTokens = $contextTokens
                    aliases = $aliases
                    display = $display
                    capabilities = $caps
                }
            }
        }

        # 後方互換: flat schema (旧 models.json, GPT 単独)
        $aliases = @{}
        if (($jsonProps -contains "aliases") -and $json.aliases) {
            foreach ($prop in $json.aliases.PSObject.Properties) {
                $aliases[$prop.Name] = [string]$prop.Value
            }
        }
        $display = @{}
        if (($jsonProps -contains "display") -and $json.display) {
            foreach ($prop in $json.display.PSObject.Properties) {
                $display[$prop.Name] = [string]$prop.Value
            }
        }
        $caps = if (($jsonProps -contains "capabilities") -and $json.capabilities) { @($json.capabilities | ForEach-Object { [string]$_ }) } else { @() }
        $defModel = if (($jsonProps -contains "defaultModel") -and $json.defaultModel) { [string]$json.defaultModel } else { "gpt-5.6-sol" }
        $contextTokens = if ($jsonProps -contains "contextTokens") { Get-PositiveIntOrNull $json.contextTokens } else { $null }
        return @{
            baseUrlPath = "/codex"
            resolvedBackend = "gpt"
            defaultModel = $defModel
            contextTokens = $contextTokens
            aliases = $aliases
            display = $display
            capabilities = $caps
        }
    }
    catch {
        Write-Warning "models.json could not be read; falling back to gpt-5.6-sol defaults: $($_.Exception.Message)"
        return $fallback
    }
}

function Ensure-Junction {
    param(
        [string]$Name,
        [string]$Source,
        [switch]$Required
    )
    $link = Join-Path $ConfigDir $Name
    if (Test-Path $link) {
        return
    }
    if (-not (Test-Path $Source)) {
        if ($Required) {
            Write-Warning "$Name junction skipped; source not found: $Source"
        }
        return
    }
    try {
        New-Item -ItemType Junction -Path $link -Target $Source -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Warning "$Name junction failed. Run scripts\install.ps1 -Force or check $Source exists. $($_.Exception.Message)"
    }
}

function Ensure-ClaudeMd {
    $link = Join-Path $ConfigDir "CLAUDE.md"
    $source = Join-Path $HomeDir ".claude\CLAUDE.md"
    if (Test-Path $link) {
        if (Test-Path $source) {
            try {
                # A size difference already proves a stale copy; hash only same-size
                # pairs. A symlink reports its own size, so it is always hashed.
                $linkItem = Get-Item -LiteralPath $link
                $sourceItem = Get-Item -LiteralPath $source
                $reparse = [IO.FileAttributes]::ReparsePoint
                $sizesDiffer = (-not ($linkItem.Attributes -band $reparse)) -and (-not ($sourceItem.Attributes -band $reparse)) -and ($linkItem.Length -ne $sourceItem.Length)
                if ($sizesDiffer -or (Get-FileHash -LiteralPath $link -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash) {
                    Write-Warning "CLAUDE.md is an independent stale copy. Run scripts\install.ps1 -Force and reconcile the reported wiring issue."
                }
            }
            catch {
                Write-Warning "CLAUDE.md sharing could not be verified: $($_.Exception.Message)"
            }
        }
        return
    }
    if (-not (Test-Path $source)) {
        Write-Warning "CLAUDE.md link/copy skipped; source not found: $source"
        return
    }
    try {
        New-Item -ItemType HardLink -Path $link -Target $source -ErrorAction Stop | Out-Null
    }
    catch {
        try {
            Copy-Item -Path $source -Destination $link -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "CLAUDE.md link/copy failed. Run scripts\install.ps1 -Force. $($_.Exception.Message)"
        }
    }
}

function Test-RouterDispatch {
    param([string]$RouterBase, [string]$DefaultModel, [string]$Token)
    # dispatcher の dispatch 実経路 (POST /v1/messages) を1回だけ叩いて生死確認する。
    # GET /router/v1/models は registry 無しでも data:[] を返し 503 を出さないため不可。
    $headers = @{
        "Authorization"     = "Bearer $Token"
        "anthropic-version" = "2023-06-01"
    }
    $body = @{
        model      = $DefaultModel
        max_tokens = 1
        stream     = $false
        messages   = @(@{ role = "user"; content = "." })
    } | ConvertTo-Json -Depth 5
    try {
        $null = Invoke-RestMethod -Method Post -Uri "$RouterBase/v1/messages" -Headers $headers -Body $body -ContentType "application/json" -TimeoutSec 4
        return $true
    }
    catch {
        return $false
    }
}

# Warm-start fast path for the shared proxy.
#
# `claude-codex-proxy.cmd start` costs a second PowerShell host plus a SHA-256
# pass over the proxy's runtime files (Get-RuntimeGeneration) even when the
# proxy is already running, so every warm launch waited seconds before
# claude.exe started. After `start` reports the managed pid, the launcher
# records that pid and the manifest's runtime generation together with a
# cheap stamp (path, size, mtime) of the same inputs. A later launch skips the
# second host only while that stamp, the managed pid, the manifest, the
# maintenance lock and a live /router/v1/models probe all still agree; any
# mismatch takes the unchanged `start` path, so restart decisions stay with
# the proxy script. CLAUDE_CODEX_PROXY_FASTPATH=0 (or false/off/no) disables
# the fast path.
#
# The fast path reproduces Start-Proxy's "already running" outcome, so it is
# pinned to the proxy script it was checked against: the SHA-256 below covers
# the proxy script with CRLF folded to LF and only the host, port and lock
# literals blanked. Any other proxy edit disables the fast path (fail closed)
# until this value is updated. Before updating it, re-check that
# Test-ProxyFastPath still mirrors Start-Proxy and that Get-ProxyRuntimeProbe
# still covers every Get-RuntimeGeneration input;
# tests/test_launcher_proxy_fastpath.py checks both.
$ProxyScriptContractSha256 = "c750ff8c849b7b5da05689289e0d0d11b107364c7d1418129ea6081e9035b587"

function Get-ProxyRuntimeProbe {
    param([string]$ProxyHome, [string]$PluginRoot, [string]$ProxyLauncherPath)
    $proxyScript = Join-Path $ProxyHome "scripts\claude-codex-proxy.ps1"
    if (-not (Test-Path -LiteralPath $proxyScript)) {
        return $null
    }
    $proxyText = ([IO.File]::ReadAllText($proxyScript)).TrimStart([char]0xFEFF).Replace("`r`n", "`n")
    $hostLine = [regex]::Match($proxyText, '(?m)^\$HostName = "([0-9A-Za-z.:-]+)"$')
    $portLine = [regex]::Match($proxyText, '(?m)^\$Port = ([0-9]+)$')
    $lock = [regex]::Match($proxyText, 'Threading\.Mutex\]::new\(\$false, "([^"$`]+)"\)')
    $ready = [regex]::Match($proxyText, '\$ids -contains "([^"$`]+)"')
    if (-not ($hostLine.Success -and $portLine.Success -and $lock.Success -and $ready.Success)) {
        return $null
    }
    $contractText = $proxyText.Replace($hostLine.Value, '$HostName = "<host>"').Replace($portLine.Value, '$Port = <port>').Replace('"' + $lock.Groups[1].Value + '"', '"<lock>"')
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $contractSha = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($contractText))) -replace '-', '').ToLowerInvariant()
        if ($contractSha -ne $ProxyScriptContractSha256) {
            return $null
        }

        $generationStart = $proxyText.IndexOf("function Get-RuntimeGeneration", [StringComparison]::Ordinal)
        if ($generationStart -lt 0) {
            return $null
        }
        $generationEnd = $proxyText.IndexOf("`nfunction ", $generationStart + 1, [StringComparison]::Ordinal)
        if ($generationEnd -lt 0) {
            $generationEnd = $proxyText.Length
        }
        $generationText = $proxyText.Substring($generationStart, $generationEnd - $generationStart)
        $relatives = @([regex]::Matches($generationText, '"([A-Za-z0-9_\\]+\.py)"') | ForEach-Object { $_.Groups[1].Value })
        if ($relatives.Count -eq 0) {
            return $null
        }

        # Same roots the proxy script derives (USERPROFILE / APPDATA).
        $roamingAppData = if ($env:APPDATA) { $env:APPDATA } else { [Environment]::GetFolderPath("ApplicationData") }
        $toolSitePackages = Join-Path $roamingAppData "uv\tools\ccproxy-api\Lib\site-packages"
        $sitePackage = Join-Path $toolSitePackages "ccproxy"
        $configPath = Join-Path $ProxyHome "config.toml"
        $paths = New-Object System.Collections.Generic.List[string]
        # install-manifest.json, the dist-info RECORD and the proxy launcher are
        # extra tripwires: an install, a ccproxy upgrade or a launcher swap
        # forces one full `start`.
        foreach ($name in @("config.toml", "install-manifest.json", "scripts\claude-codex-proxy.ps1", "scripts\ccproxy-lb.py")) {
            $paths.Add((Join-Path $ProxyHome $name))
        }
        $paths.Add($ProxyLauncherPath)
        foreach ($relative in $relatives) {
            $paths.Add((Join-Path $sitePackage $relative))
        }
        if (Test-Path -LiteralPath $PluginRoot) {
            foreach ($file in ([IO.DirectoryInfo]::new($PluginRoot)).EnumerateFiles("*", [IO.SearchOption]::AllDirectories)) {
                if ($file.FullName -match '[\\/]__pycache__[\\/]' -or $file.Extension -in @(".pyc", ".pyo")) {
                    continue
                }
                $paths.Add($file.FullName)
            }
        }
        if (Test-Path -LiteralPath $toolSitePackages) {
            foreach ($distInfo in ([IO.DirectoryInfo]::new($toolSitePackages)).EnumerateDirectories("ccproxy_api-*.dist-info")) {
                $paths.Add((Join-Path $distInfo.FullName "RECORD"))
            }
        }

        # Same defaults the proxy script applies before computing its generation.
        $traceOpt = if ($env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT) { [string]$env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT } else { "0" }
        $errorLogOpt = if ($env:CCPROXY_ERROR_LOG_OPT) { [string]$env:CCPROXY_ERROR_LOG_OPT } else { "0" }
        $proxyUrl = "http://$($hostLine.Groups[1].Value):$($portLine.Groups[1].Value)"
        $parts = New-Object System.Collections.Generic.List[string]
        $parts.Add("stamp=2")
        $parts.Add("contract=$contractSha")
        $parts.Add("url=$proxyUrl")
        $parts.Add("lock=$($lock.Groups[1].Value)")
        $parts.Add("trace=$traceOpt")
        $parts.Add("error_log=$errorLogOpt")
        # config.toml is small, so its content is hashed as well as stat'ed.
        if (Test-Path -LiteralPath $configPath) {
            $parts.Add("config_sha=" + ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($configPath))) -replace '-', ''))
        }
        $sortedPaths = @($paths | Sort-Object -Unique)
        foreach ($path in $sortedPaths) {
            $info = [IO.FileInfo]::new($path)
            if ($info.Exists) {
                $parts.Add("$path|$($info.Length)|$($info.LastWriteTimeUtc.Ticks)")
            }
            else {
                $parts.Add("$path|missing")
            }
        }
        $stamp = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($parts -join "`n")))) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    return @{
        Stamp = $stamp
        ProxyUrl = $proxyUrl
        ReadyModelId = $ready.Groups[1].Value
        LockName = $lock.Groups[1].Value
        Paths = $sortedPaths
    }
}

function Read-ProxyPidFile {
    param([string]$ProxyHome)
    return [int]([IO.File]::ReadAllText((Join-Path $ProxyHome "run\ccproxy.pid")).Trim())
}

function Get-ProxyManagedState {
    param([string]$ProxyHome)
    try {
        $managedPid = Read-ProxyPidFile -ProxyHome $ProxyHome
        $process = [Diagnostics.Process]::GetProcessById($managedPid)
        try {
            if ($process.HasExited) {
                return $null
            }
        }
        finally {
            $process.Dispose()
        }
        $manifest = [IO.File]::ReadAllText((Join-Path $ProxyHome "run\ccproxy-manifest.json")) | ConvertFrom-Json
        $names = @($manifest.PSObject.Properties | ForEach-Object { $_.Name })
        # Start-Proxy reads mode and worker_count under StrictMode; a manifest
        # without them makes `start` fail, so it must not pass here.
        if (-not (($names -contains "mode") -and ($names -contains "worker_count") -and ($names -contains "runtime_generation"))) {
            return $null
        }
        $generation = [string]$manifest.runtime_generation
        if ([string]::IsNullOrWhiteSpace($generation)) {
            return $null
        }
        return @{ Pid = $managedPid; Generation = $generation }
    }
    catch {
        return $null
    }
}

function Test-ProxyModelsReady {
    param([string]$ProxyUrl, [string]$Token, [string]$ModelId)
    # Same probe as the proxy script's Test-ProxyReady, without the system
    # proxy lookup (the endpoint is local).
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create("$ProxyUrl/router/v1/models")
        $request.Method = "GET"
        $request.Proxy = $null
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $request.Headers.Add("Authorization", "Bearer $Token")
        $request.Headers.Add("anthropic-version", "2023-06-01")
        $response = $request.GetResponse()
        $reader = New-Object IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
        try {
            $body = $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
        $ids = @(($body | ConvertFrom-Json).data | ForEach-Object { [string]$_.id })
        return ($ids -contains $ModelId)
    }
    catch [Net.WebException] {
        if ($null -ne $_.Exception.Response) { $_.Exception.Response.Close() }
        return $false
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $response) { $response.Close() }
    }
}

# Returns "ok" or the reason the fast path was not taken. Reasons listed in
# $ProxyFastPathRetryReasons can be fixed by another launcher's `start`, so
# only those queue on the launcher lock (see the main flow).
function Test-ProxyFastPath {
    param($Probe, [string]$CachePath, [string]$ProxyHome, [string]$ProxyBase)
    if ($null -eq $Probe) {
        return "no-probe"
    }
    # The proxy script always probes its own host and port; another
    # CLAUDE_CODEX_PROXY_URL keeps the full path.
    if ($ProxyBase -ne $Probe.ProxyUrl) {
        return "other-url"
    }
    if (-not (Test-Path -LiteralPath $CachePath)) {
        return "no-cache"
    }
    try {
        $cache = [IO.File]::ReadAllText($CachePath) | ConvertFrom-Json
        if ([string]$cache.stamp -ne $Probe.Stamp) {
            return "stale-cache"
        }
        $cachedPid = [int]$cache.pid
        $cachedGeneration = [string]$cache.runtime_generation
    }
    catch {
        return "stale-cache"
    }
    # start/restart/stop hold this lock; queue behind maintenance through the
    # full path instead of racing a restart.
    $mutex = $null
    try {
        $mutex = New-Object Threading.Mutex($false, $Probe.LockName)
        if (-not $mutex.WaitOne(0)) {
            return "maintenance"
        }
        $mutex.ReleaseMutex()
    }
    catch [Threading.AbandonedMutexException] {
        try { $mutex.ReleaseMutex() } catch {}
        return "maintenance"
    }
    catch {
        return "maintenance"
    }
    finally {
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
    $managed = Get-ProxyManagedState -ProxyHome $ProxyHome
    if ($null -eq $managed -or $managed.Pid -ne $cachedPid -or $managed.Generation -ne $cachedGeneration) {
        return "stale-proxy"
    }
    Import-UserEnvIfMissing "CLAUDE_CODEX_PROXY_TOKEN"
    $token = [Environment]::GetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", "Process")
    if ([string]::IsNullOrWhiteSpace($token)) {
        return "no-token"
    }
    if (-not (Test-ProxyModelsReady -ProxyUrl $Probe.ProxyUrl -Token $token -ModelId $Probe.ReadyModelId)) {
        return "not-ready"
    }
    # A restart that slipped in while the probe ran replaces the pid file.
    try {
        if ((Read-ProxyPidFile -ProxyHome $ProxyHome) -ne $cachedPid) {
            return "stale-proxy"
        }
    }
    catch {
        return "stale-proxy"
    }
    return "ok"
}
$ProxyFastPathRetryReasons = @("no-cache", "stale-cache", "maintenance", "stale-proxy")

function Save-ProxyFastPathCache {
    param($Probe, [object[]]$StartOutput, [string]$CachePath, [string]$ProxyHome, [string]$PluginRoot, [string]$ProxyLauncherPath)
    if ($null -eq $Probe) {
        return
    }
    # Record only the proxy that this `start` reported as running; an
    # unmanaged proxy reports no pid and is never cached.
    $reportedPid = $null
    foreach ($line in @($StartOutput)) {
        $match = [regex]::Match([string]$line, '^(?:already running|started)\b.*?\bpid=([0-9]+)\b')
        if ($match.Success) {
            $reportedPid = [int]$match.Groups[1].Value
        }
    }
    if ($null -eq $reportedPid) {
        return
    }
    $managed = Get-ProxyManagedState -ProxyHome $ProxyHome
    if ($null -eq $managed -or $managed.Pid -ne $reportedPid) {
        return
    }
    # The inputs must not have moved while `start` ran.
    $after = Get-ProxyRuntimeProbe -ProxyHome $ProxyHome -PluginRoot $PluginRoot -ProxyLauncherPath $ProxyLauncherPath
    if ($null -eq $after -or $after.Stamp -ne $Probe.Stamp) {
        return
    }
    $record = [ordered]@{
        stamp = $Probe.Stamp
        pid = $managed.Pid
        runtime_generation = $managed.Generation
        saved_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    # Not atomic: a reader that sees a torn file fails to parse it and takes
    # the full path.
    [IO.File]::WriteAllText($CachePath, ($record | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
}

New-Item -ItemType Directory -Force -Path $ConfigDir, $DataDir, $StateDir | Out-Null
if ($skillProfile -eq "slim") {
    foreach ($name in @("settings.json", "empty-mcp.json", "models.json", "ultracode.settings.json", "fcc-picker.settings.json", "fcc-ultracode.settings.json", "orchestration-policy.md")) {
        $source = Join-Path $BaseConfigDir $name
        $target = Join-Path $ConfigDir $name
        if (Test-Path $source) {
            Copy-Item -Path $source -Destination $target -Force
        }
    }
}

$models = Get-ModelConfig -Backend $requestedBackend
$activeBackend = $models.resolvedBackend
if ($activeBackend -eq "gemini") {
    [Console]::Error.WriteLine("[claude-codex] Note: Gemini backend is one-shot (no tool-use). Best for headless -p or as an ultracode subagent, not the interactive agentic loop.")
}
if ($activeBackend -eq "fugu") {
    Import-UserEnvIfMissing "FUGU_API_KEY"
}
if ($activeBackend -eq "fcc") {
    $null = Ensure-FccGateway -Required $true
}

if (-not (Test-Path $ProxyLauncher)) {
    [Console]::Error.WriteLine("[claude-codex] Proxy launcher missing: $ProxyLauncher")
    exit 1
}

$null = Import-UserEnvIfMissing "FUGU_API_KEY"
$proxyBase = if ($env:CLAUDE_CODEX_PROXY_URL) { ([string]$env:CLAUDE_CODEX_PROXY_URL).TrimEnd("/") } else { "http://127.0.0.1:4026" }
# The proxy script resolves its home from USERPROFILE, not CLAUDE_CODEX_HOME.
$proxyUserHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath("UserProfile") }
$proxyHome = Join-Path $proxyUserHome ".claude-codex-proxy"
$proxyPluginRoot = Join-Path $proxyUserHome ".config\ccproxy\plugins"
$proxyFastPathCache = Join-Path $StateDir "proxy-fastpath.json"
$proxyProbe = $null
# A worker-count override is a maintenance request; let the proxy script judge it.
$fastPathSetting = ([string]$env:CLAUDE_CODEX_PROXY_FASTPATH).Trim().ToLowerInvariant()
if (@("0", "false", "off", "no") -notcontains $fastPathSetting -and [string]::IsNullOrWhiteSpace($env:CCPROXY_PARALLEL_WORKERS)) {
    try {
        $proxyProbe = Get-ProxyRuntimeProbe -ProxyHome $proxyHome -PluginRoot $proxyPluginRoot -ProxyLauncherPath $ProxyLauncher
    }
    catch {
        $proxyProbe = $null
    }
}
$proxyCheck = "error"
try {
    $proxyCheck = Test-ProxyFastPath -Probe $proxyProbe -CachePath $proxyFastPathCache -ProxyHome $proxyHome -ProxyBase $proxyBase
}
catch {
    $proxyCheck = "error"
}
$proxyReady = $proxyCheck -eq "ok"
if (-not $proxyReady) {
    # Launchers started together (a restored mycmux layout, parallel -p runs)
    # queue here so that one `start` refreshes the cache and the rest re-check
    # it instead of each paying for its own full `start`. Misses that another
    # launch cannot fix (other URL, unready proxy) do not queue.
    $launcherLock = $null
    $launcherLockHeld = $false
    if ($null -ne $proxyProbe -and $ProxyFastPathRetryReasons -contains $proxyCheck) {
        try {
            $launcherLock = New-Object Threading.Mutex($false, ($proxyProbe.LockName + "-launcher"))
            $waitStarted = [DateTime]::UtcNow
            $waitNoticed = $false
            while (-not $launcherLockHeld -and ([DateTime]::UtcNow - $waitStarted).TotalSeconds -lt 180) {
                try {
                    $launcherLockHeld = $launcherLock.WaitOne(250)
                }
                catch [Threading.AbandonedMutexException] {
                    $launcherLockHeld = $true
                }
                if (-not $launcherLockHeld -and -not $waitNoticed -and ([DateTime]::UtcNow - $waitStarted).TotalSeconds -ge 2) {
                    [Console]::Error.WriteLine("[claude-codex] Waiting for another claude-codex launch to finish starting the proxy...")
                    $waitNoticed = $true
                }
            }
        }
        catch {
            $launcherLockHeld = $false
        }
        if ($launcherLockHeld) {
            try {
                $proxyReady = (Test-ProxyFastPath -Probe $proxyProbe -CachePath $proxyFastPathCache -ProxyHome $proxyHome -ProxyBase $proxyBase) -eq "ok"
            }
            catch {
                $proxyReady = $false
            }
        }
    }
    try {
        if (-not $proxyReady) {
            $proxyError = Join-Path ([IO.Path]::GetTempPath()) ("ccproxy-start-{0}.err" -f [Guid]::NewGuid().ToString("N"))
            # Under the launcher's ErrorActionPreference=Stop, PowerShell 5.1
            # turns the first redirected stderr line of a native command into
            # a terminating error; run the call with Continue so stderr only
            # lands in the capture and the exit code decides.
            $proxyOutput = @(& {
                $ErrorActionPreference = "Continue"
                & $ProxyLauncher start 2> $proxyError
            })
            $proxyExit = $LASTEXITCODE
            if ($proxyExit -ne 0) {
                [Console]::Error.WriteLine("[claude-codex] Proxy startup failed. See $proxyError for details.")
                [Console]::Error.WriteLine("[claude-codex] Run for full diagnosis: powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1 -Quick")
                exit $proxyExit
            }
            # A successful start leaves an empty capture behind; do not litter %TEMP%.
            try {
                if ((Get-Item -LiteralPath $proxyError).Length -eq 0) {
                    Remove-Item -LiteralPath $proxyError
                }
            }
            catch {
                # Keep the capture when it cannot be inspected.
            }
            try {
                Save-ProxyFastPathCache -Probe $proxyProbe -StartOutput $proxyOutput -CachePath $proxyFastPathCache -ProxyHome $proxyHome -PluginRoot $proxyPluginRoot -ProxyLauncherPath $ProxyLauncher
            }
            catch {
                # Optimisation only; the next launch takes the full path again.
            }
        }
    }
    finally {
        if ($launcherLockHeld) {
            try { $launcherLock.ReleaseMutex() } catch {}
        }
        if ($null -ne $launcherLock) {
            $launcherLock.Dispose()
        }
    }
}

Set-EnvValue "CLAUDE_CONFIG_DIR" $ConfigDir
Set-EnvValue "XDG_DATA_HOME" $DataDir
Set-EnvValue "XDG_STATE_HOME" $StateDir
$proxyToken = Get-ProxyToken
Set-EnvValue "ANTHROPIC_BASE_URL" ($proxyBase + $models.baseUrlPath)
Set-EnvValue "ANTHROPIC_AUTH_TOKEN" $proxyToken
Set-EnvValue "ANTHROPIC_API_KEY" $proxyToken
Set-EnvValue "ENABLE_TOOL_SEARCH" "true"
Set-EnvValue "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" "1"
Set-EnvValue "CLAUDE_CODE_ENABLE_TODO_TOOLS" "1"

$aliases = $models.aliases
$display = $models.display
$capabilities = ($models.capabilities -join ",")
$defaultModel = if ($models.defaultModel) { [string]$models.defaultModel } else { "gpt-5.6-sol" }
$displayName = if ($display.ContainsKey("name")) { $display["name"] } else { "GPT-5.6" }

Set-EnvValue "CLAUDE_CODE_SUBAGENT_MODEL" $(if ($aliases.ContainsKey("subagent")) { $aliases["subagent"] } else { $defaultModel })
Set-EnvValue "ANTHROPIC_DEFAULT_MODEL" $defaultModel
# Profiles have different context guards (FCC is 52k; GPT is 1M). Claude
# Code 2.1.237 has no per-model context metadata, so a saved cross-profile
# /model must not silently override the launcher's selected safety boundary.
Set-EnvValue "ANTHROPIC_MODEL" $defaultModel

if ($null -eq $env:CLAUDE_CODE_MAX_CONTEXT_TOKENS) {
    $contextTokensValue = $null
    if ($null -ne $env:CLAUDE_CODEX_MAX_CONTEXT_TOKENS) {
        $contextTokensValue = Get-PositiveIntOrNull $env:CLAUDE_CODEX_MAX_CONTEXT_TOKENS
        if ($null -eq $contextTokensValue) {
            # 不正値で guard ごと無効化すると auto-compact 不発の障害が再発するため、backend 既定へ落とす。
            Write-Warning "CLAUDE_CODEX_MAX_CONTEXT_TOKENS must be a positive integer; falling back to the backend default."
        }
    }
    if ($null -eq $contextTokensValue) {
        $contextTokensValue = Get-PositiveIntOrNull $models.contextTokens
    }
    if ($null -ne $contextTokensValue) {
        Set-EnvValue "CLAUDE_CODE_MAX_CONTEXT_TOKENS" ([string]$contextTokensValue)
    }
}

if ($models.resolvedBackend -eq "gpt") {
    Set-EnvValue "CLAUDE_CODE_AUTO_COMPACT_WINDOW" "1000000"
    Set-EnvValue "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" "60"
}

foreach ($family in @("FABLE", "OPUS", "SONNET", "HAIKU")) {
    $key = $family.ToLowerInvariant()
    $nameKey = $key + "Name"
    $descriptionKey = $key + "Description"
    $target = if ($aliases.ContainsKey($key)) { $aliases[$key] } else { $defaultModel }
    $familyDisplayName = if ($display.ContainsKey($nameKey)) {
        $display[$nameKey]
    }
    else {
        $displayName
    }
    $description = if ($display.ContainsKey($descriptionKey)) {
        $display[$descriptionKey]
    }
    else {
        "OpenAI subscription route via Codex backend"
    }
    Set-EnvValue "ANTHROPIC_DEFAULT_${family}_MODEL" $target
    Set-EnvValue "ANTHROPIC_DEFAULT_${family}_MODEL_NAME" $familyDisplayName
    Set-EnvValue "ANTHROPIC_DEFAULT_${family}_MODEL_DESCRIPTION" $description
    Set-EnvValue "ANTHROPIC_DEFAULT_${family}_MODEL_SUPPORTED_CAPABILITIES" $capabilities
}
Set-EnvValue "ANTHROPIC_CUSTOM_MODEL_OPTION" ""
Set-EnvValue "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME" ""
Set-EnvValue "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION" ""

$extraArgs = @()
if ($activeBackend -eq "fcc") {
    $pickerSettings = if ($env:CLAUDE_CODEX_ULTRACODE -eq "1") { "fcc-ultracode.settings.json" } else { "fcc-picker.settings.json" }
    $extraArgs += @("--settings", (Join-Path $ConfigDir $pickerSettings))
}
$hasPrint = $false
$hasMcpOverride = $false
$hasEffortOverride = $false
foreach ($arg in $CliArgs) {
    if ($arg -ieq "--effort" -or $arg -ilike "--effort=*") {
        $hasEffortOverride = $true
    }
    if ($arg -ieq "-p" -or $arg -ieq "--print") {
        $hasPrint = $true
    }
    if ($arg -ieq "--mcp-config" -or $arg -ieq "--strict-mcp-config" -or $arg -ilike "--mcp-config=*") {
        $hasMcpOverride = $true
    }
}
$hasSettingsOverride = @($CliArgs | Where-Object { $_ -ieq "--settings" -or $_ -ilike "--settings=*" }).Count -gt 0
$iconSettings = Join-Path $ConfigDir "blue-icon.settings.json"
if ($activeBackend -eq "gpt" -and $env:CLAUDE_CODEX_ULTRACODE -ne "1" -and -not $hasSettingsOverride -and (Test-Path $iconSettings)) {
    $extraArgs += @("--settings", $iconSettings)
}
if ($activeBackend -eq "gpt" -and -not $hasEffortOverride -and -not $env:CLAUDE_CODE_EFFORT_LEVEL) {
    $extraArgs += @("--effort", "xhigh")
}
if ($hasPrint) {
    Set-EnvValue "MCP_CONNECTION_NONBLOCKING" "true"
    if (-not $hasMcpOverride) {
        $extraArgs += @("--strict-mcp-config", "--mcp-config", (Join-Path $ConfigDir "empty-mcp.json"))
    }
}
else {
    Remove-Item Env:MCP_CONNECTION_NONBLOCKING -ErrorAction SilentlyContinue
}

if ($env:CLAUDE_CODEX_ULTRACODE -eq "1") {
    if ($activeBackend -ne "fcc") {
        $extraArgs += @("--settings", (Join-Path $ConfigDir "ultracode.settings.json"))
    }
    # オーケストレーションポリシーを母艦の system prompt に追記 (ultracode 時のみ)。
    # 母艦別 orchestration-policy.<backend>.md があればそれ、無ければ共通版。
    # claude-codex 専用 (launcher 経由のみ)・素の claude code には無影響。
    $policyFile = Join-Path $ConfigDir ("orchestration-policy.{0}.md" -f $activeBackend)
    if (-not (Test-Path $policyFile)) {
        $policyFile = Join-Path $ConfigDir "orchestration-policy.md"
    }
    if (Test-Path $policyFile) {
        $extraArgs += @("--append-system-prompt-file", $policyFile)
    }

    # ultracode: 混在fanout のため全リクエストを dispatcher (/router) に通す。
    # gemini は baseUrlPath が既に /router なので二重化回避で除外。
    # SKIP env で無効化可 (機密 no-network 起動 / CI)。
    if ($activeBackend -notin @("gemini", "fcc") -and $env:CLAUDE_CODEX_SKIP_ROUTER_SMOKE -ne "1") {
        $routerBase = $proxyBase + "/router"
        if (Test-RouterDispatch -RouterBase $routerBase -DefaultModel $defaultModel -Token $proxyToken) {
            Set-EnvValue "ANTHROPIC_BASE_URL" $routerBase
        }
        else {
            $directPath = $models.baseUrlPath
            [Console]::Error.WriteLine("[claude-codex] /router dispatcher unhealthy; MIXED FAN-OUT DISABLED for this session (staying on $directPath). claude-*/gemini-* subagents will not route to other providers.")
            Set-EnvValue "ANTHROPIC_BASE_URL" ($proxyBase + $directPath)
        }
    }
}

$claudeConfig = Join-Path $HomeDir ".claude"
Ensure-Junction -Name "skills" -Source (Join-Path $claudeConfig "skills") -Required
Ensure-Junction -Name "rules" -Source (Join-Path $claudeConfig "rules") -Required
if ($skillProfile -eq "full") {
    foreach ($name in @("commands", "agents", "plugins")) {
        Ensure-Junction -Name $name -Source (Join-Path $claudeConfig $name)
    }
}
Ensure-ClaudeMd

if (-not $hasPrint -and $activeBackend -eq "gpt") {
    [Console]::Error.WriteLine("[claude-codex] GPT model aliases: opus=$($aliases['opus']), sonnet=$($aliases['sonnet']), haiku=$($aliases['haiku']), subagent=$env:CLAUDE_CODE_SUBAGENT_MODEL; use explicit claude-* IDs for Claude.")
}

if ($env:CLAUDE_CODEX_BANNER -eq "1") {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("claude-codex-hybrid (Claude Code UI via ccproxy)")
    [Console]::Error.WriteLine("  backend: $activeBackend ($displayName)")
    [Console]::Error.WriteLine("  proxy: $env:ANTHROPIC_BASE_URL")
    [Console]::Error.WriteLine("  reasoning effort: GPT default xhigh; explicit CLI/environment overrides preserved")
    [Console]::Error.WriteLine("  subagents: $env:CLAUDE_CODE_SUBAGENT_MODEL")
    [Console]::Error.WriteLine("  skill profile: $skillProfile")
    [Console]::Error.WriteLine("  trace: $HomeDir\.claude-codex-proxy\logs\claude-compat-trace.jsonl")
    [Console]::Error.WriteLine("  health: powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1 -Quick")
    [Console]::Error.WriteLine("")
}

Set-EnvValue "CLAUDECODE" ""
Set-EnvValue "CLAUDE_CODE_ENTRYPOINT" ""

if (-not (Test-Path $ClaudeExe)) {
    [Console]::Error.WriteLine("[claude-codex] Claude Code CLI not found: $ClaudeExe")
    exit 1
}

$mycmuxTabId = $env:MYCMUX_TAB_ID
$hasSessionOverride = @($CliArgs | Where-Object { $_ -match '^(--session-id|--resume|--continue|--fork-session|--print|--no-session-persistence|--help|--version)(=|$)|^-[prc]|^-[hv]$|^(agents|attach|auth|auto-mode|doctor|gateway|import|install|logs|mcp|plugin|plugins|purge|respawn|rm|setup-token|stop|kill|ultrareview|update|upgrade)$' }).Count -gt 0
if (-not $hasPrint -and $env:MYCMUX_PANE_SESSION_ID -and $mycmuxTabId -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' -and -not $hasSessionOverride) {
    $iconSessionId = [guid]::NewGuid().ToString()
    $mappingRoot = if ($env:MYCMUX_RUNTIME_DIR) { $env:MYCMUX_RUNTIME_DIR } else { Join-Path $HomeDir ".mycmux" }
    $mappingDir = Join-Path $mappingRoot "pane-sessions"
    [IO.Directory]::CreateDirectory($mappingDir) | Out-Null
    [IO.File]::WriteAllText((Join-Path $mappingDir "$mycmuxTabId.txt"), "claude-codex:$iconSessionId`n", [Text.UTF8Encoding]::new($false))
    $extraArgs += @("--session-id", $iconSessionId)
}
$allArgs = @($extraArgs + $CliArgs)
if ($hasPrint -and -not $env:CLAUDE_CODEX_KEEP_STDIN) {
    @() | & $ClaudeExe @allArgs
}
else {
    & $ClaudeExe @allArgs
}
exit $LASTEXITCODE

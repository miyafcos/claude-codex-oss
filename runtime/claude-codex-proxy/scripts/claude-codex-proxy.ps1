param(
    [ValidateSet("run", "start", "restart", "stop", "status", "logs")]
    [string]$Command = "status"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$HomeDir = if ($env:USERPROFILE) {
    $env:USERPROFILE
} else {
    [Environment]::GetFolderPath("UserProfile")
}
$RoamingAppData = if ($env:APPDATA) {
    $env:APPDATA
} else {
    [Environment]::GetFolderPath("ApplicationData")
}

$ProxyHome = Join-Path $HomeDir ".claude-codex-proxy"
$ConfigPath = Join-Path $ProxyHome "config.toml"
$LogDir = Join-Path $ProxyHome "logs"
$RunDir = Join-Path $ProxyHome "run"
$StdoutPath = Join-Path $LogDir "ccproxy.stdout.log"
$StderrPath = Join-Path $LogDir "ccproxy.stderr.log"
$LbStdoutPath = Join-Path $LogDir "ccproxy-lb.stdout.log"
$LbStderrPath = Join-Path $LogDir "ccproxy-lb.stderr.log"
$TraceRotatorStdoutPath = Join-Path $LogDir "trace-rotator.stdout.log"
$TraceRotatorStderrPath = Join-Path $LogDir "trace-rotator.stderr.log"
$CompatTracePath = Join-Path $LogDir "claude-compat-trace.jsonl"
$ErrorLogPath = Join-Path $LogDir "ccproxy-errors.jsonl"
$PidPath = Join-Path $RunDir "ccproxy.pid"
$PidManifestPath = Join-Path $RunDir "ccproxy-manifest.json"
$HostName = "127.0.0.1"
$Port = 4026

function Import-UserEnvIfMissing {
    param([string]$Name)
    if ([Environment]::GetEnvironmentVariable($Name, "Process")) {
        return
    }
    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($value) {
        [Environment]::SetEnvironmentVariable($Name, $value, "Process")
    }
}

function Get-ProxyToken {
    Import-UserEnvIfMissing "CLAUDE_CODEX_PROXY_TOKEN"
    $token = [Environment]::GetEnvironmentVariable("CLAUDE_CODEX_PROXY_TOKEN", "Process")
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "CLAUDE_CODEX_PROXY_TOKEN is missing. Run scripts\install.ps1 -Force once, or set a per-user token before launching."
    }
    return $token
}

# The router can switch to Fugu after the Claude process has already started,
# so the long-lived proxy must load this credential independently of the
# launcher's initial backend choice.
Import-UserEnvIfMissing "FUGU_API_KEY"

$ProxyToken = Get-ProxyToken
$BaseUrl = "http://$HostName`:$Port/router"
$CcproxyPython = Join-Path $RoamingAppData "uv\tools\ccproxy-api\Scripts\python.exe"
$RotateTraceScript = Join-Path $ProxyHome "scripts\rotate-trace.ps1"
$LbScript = Join-Path $ProxyHome "scripts\ccproxy-lb.py"
$MaxParallelWorkers = 8
$DefaultTraceMaxBytes = 67108864
$DefaultTraceRotations = 5

# Trace + error-log opt-in env. Raw compatibility traces may contain prompts
# and responses, so unified multi-provider sessions keep them off by default.
if (-not (Test-Path Env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT)) { $env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT = "0" }
if (-not (Test-Path Env:CCPROXY_ERROR_LOG_OPT)) { $env:CCPROXY_ERROR_LOG_OPT = "0" }

New-Item -ItemType Directory -Force $LogDir | Out-Null
New-Item -ItemType Directory -Force $RunDir | Out-Null

function Get-EnvInt {
    param(
        [string]$Name,
        [int]$Default,
        [int]$Min,
        [int]$Max
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed)) { return $Default }
    if ($parsed -lt $Min) { return $Min }
    if ($parsed -gt $Max) { return $Max }
    return $parsed
}

function Get-EnvLong {
    param(
        [string]$Name,
        [long]$Default,
        [long]$Min,
        [long]$Max
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $parsed = [long]0
    if (-not [long]::TryParse($raw, [ref]$parsed)) { return $Default }
    if ($parsed -lt $Min) { return $Min }
    if ($parsed -gt $Max) { return $Max }
    return $parsed
}

function Get-ParallelWorkerCount {
    # A shared proxy must not be torn down just because an ultracode session
    # starts while another stream is active. Parallelism is therefore an
    # explicit operator override; the stable default is one worker.
    return Get-EnvInt -Name "CCPROXY_PARALLEL_WORKERS" -Default 1 -Min 1 -Max $MaxParallelWorkers
}

function Get-BackendPorts {
    param([int]$WorkerCount)

    $ports = @()
    for ($i = 0; $i -lt $WorkerCount; $i++) {
        $ports += ($Port + 1 + $i)
    }
    return $ports
}

function Invoke-TraceRotation {
    $maxBytes = Get-EnvLong -Name "CCPROXY_TRACE_MAX_BYTES" -Default $DefaultTraceMaxBytes -Min 1048576 -Max 1099511627776
    $keep = Get-EnvInt -Name "CCPROXY_TRACE_ROTATIONS" -Default $DefaultTraceRotations -Min 1 -Max 20

    if (-not (Test-Path -LiteralPath $RotateTraceScript)) {
        Write-Output "rotate-trace: script missing, skip ($RotateTraceScript)"
        return
    }

    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $RotateTraceScript `
            -Path $CompatTracePath `
            -MaxBytes $maxBytes `
            -Keep $keep
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "rotate-trace returned exit code $LASTEXITCODE"
        }
    }
    catch {
        Write-Warning "rotate-trace failed: $_"
    }
}

function Get-TraceRotateIntervalSeconds {
    return Get-EnvInt -Name "CCPROXY_TRACE_ROTATE_INTERVAL_SECONDS" -Default 60 -Min 10 -Max 3600
}

function Get-CcproxyInvocation {
    if (-not (Test-Path $CcproxyPython)) {
        throw "ccproxy runtime is missing: $CcproxyPython"
    }

    return @{
        FilePath = $CcproxyPython
        PrefixArgs = @("-m", "ccproxy")
    }
}

function Set-CcproxyEnvironment {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:NO_COLOR = "1"
    $env:CONFIG_FILE = $ConfigPath
    $env:CCPROXY_CODEX_ADAPTIVE_REASONING_EFFORT = "xhigh"
    $env:CCPROXY_CODEX_DEFAULT_REASONING_EFFORT = "xhigh"
    $env:CCPROXY_CODEX_REASONING_SUMMARY = "auto"
    # WP-Z: enable opt-in context compaction by default. Sticky prompt_cache_key
    # + billing-header strip help OpenAI's automatic prefix cache stay warm,
    # which closes most of the cost-axis gap vs raw codex CLI. Override by
    # explicitly setting CCPROXY_CONTEXT_COMPACTION=0 before launching.
    if (-not (Test-Path Env:CCPROXY_CONTEXT_COMPACTION)) { $env:CCPROXY_CONTEXT_COMPACTION = "1" }
    # WP-AT: optional latency-improving knobs (default OFF, opt-in only).
    # Once the input prefix is cached, wall-clock latency is dominated by
    # output token generation — these knobs reduce output volume:
    #   $env:CCPROXY_BREVITY_HINT = "1"          # prepend "be concise" instruction
    #   $env:CCPROXY_MAX_OUTPUT_FLOOR = "1024"   # cap max_output_tokens
    # Set them in your shell before launching the proxy if you want them on.
    $env:CCPROXY_CLAUDE_COMPAT_TRACE = "0"
    $env:CCPROXY_CLAUDE_COMPAT_TRACE_PATH = $CompatTracePath
    $env:CCPROXY_ERROR_LOG_PATH = $ErrorLogPath
    $requiredPaths = @(
        (Join-Path $RoamingAppData "npm"),
        (Join-Path $HomeDir ".local\bin")
    )
    foreach ($requiredPath in $requiredPaths) {
        if (-not [string]::IsNullOrWhiteSpace($requiredPath) -and (Test-Path $requiredPath)) {
            $pathEntries = ($env:PATH -split ';') | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            if ($pathEntries -notcontains $requiredPath) {
                $env:PATH = "$requiredPath;$env:PATH"
            }
        }
    }
}

function Test-CodexAuth {
    Set-CcproxyEnvironment
    $invocation = Get-CcproxyInvocation
    $null = & $invocation.FilePath @($invocation.PrefixArgs + @("auth", "status", "codex"))
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Codex subscription auth is not ready; non-Codex routes can still start. Run 'codex login' before using GPT models."
    }
}

function Sync-CodexHeaderCache {
    # ccproxy v0.2.9 keys its captured Codex request headers by the exact CLI
    # version (~/.cache/ccproxy/codex_headers_<ver>.json) and refuses a cache
    # whose version differs from the running Codex CLI. Codex 0.126+ broke
    # ccproxy's built-in OAuth-path detection, so on every Codex CLI bump the
    # proxy would otherwise fall back to a stale cache and the backend would
    # reject requests with "400: requires a newer version of Codex".
    # regenerate-codex-headers.py self-heals by seeding the new version's
    # cache from the most recent one. Failure here is non-fatal — the proxy
    # still starts; the worst case is the pre-existing stale-cache behaviour.
    $script = Join-Path $PSScriptRoot "regenerate-codex-headers.py"
    if (-not (Test-Path $script)) { return }
    if (-not (Test-Path $CcproxyPython)) { return }
    try {
        $out = (& $CcproxyPython $script 2>&1 | Out-String).Trim()
        if ($out) { Write-Output ("codex-header-cache: " + ($out -replace '\s*\r?\n\s*', ' | ')) }
    }
    catch {
        Write-Output "codex-header-cache: sync skipped ($_)"
    }
}

function Get-RecordedPid {
    if (-not (Test-Path $PidPath)) {
        return $null
    }

    $rawPid = $null
    try {
        $rawPid = (Get-Content $PidPath -Raw -ErrorAction Stop).Trim()
    }
    catch {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($rawPid)) {
        return $null
    }

    try {
        return [int]$rawPid
    }
    catch {
        return $null
    }
}

function Get-ManagedProcess {
    $recordedPid = Get-RecordedPid
    if ($null -eq $recordedPid) {
        Remove-Item $PidPath -ErrorAction SilentlyContinue
        return $null
    }

    $manifest = Read-RunManifest
    $verified = Get-VerifiedProxyProcess -ProcessId $recordedPid -Manifest $manifest
    if ($null -ne $verified) { return $verified }
    $unverified = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
    if ($null -ne $unverified) {
        $unverified.Dispose()
        throw 'Recorded proxy identity could not be verified; the process was left running.'
    }
    Remove-Item $PidPath -ErrorAction SilentlyContinue
    return $null
}

function Test-LegacyProxyProcess {
    param($Process)

    try {
        $details = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)" -ErrorAction Stop
        if ([Math]::Abs(($details.CreationDate.ToUniversalTime() - $Process.StartTime.ToUniversalTime()).TotalMilliseconds) -ge 1) { return $false }
        $command = [string]$details.CommandLine
        if ($Process.ProcessName -in @('powershell', 'pwsh')) {
            if ($command -notmatch '(?i)-EncodedCommand\s+([A-Za-z0-9+/=]+)') { return $false }
            $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
            return $decoded.Contains("& '$CcproxyPython' -m ccproxy serve --config '$ConfigPath' --host '$HostName' --port ") -or
                $decoded.Contains("& '$CcproxyPython' '$LbScript' @argsList") -or
                $decoded.Contains("-File '$RotateTraceScript' -Path '$CompatTracePath'")
        }
        if ($Process.ProcessName -notin @('python', 'pythonw')) { return $false }
        $executables = @($CcproxyPython)
        $venvConfig = Join-Path (Split-Path (Split-Path $CcproxyPython -Parent) -Parent) 'pyvenv.cfg'
        if ([IO.File]::Exists($venvConfig)) {
            $venvText = [IO.File]::ReadAllText($venvConfig, [Text.Encoding]::UTF8)
            if ($venvText -match '(?m)^home\s*=\s*(.+?)\r?$') {
                $executables += Join-Path $Matches[1].Trim() 'python.exe'
            }
        }
        if ($executables -notcontains [string]$details.ExecutablePath) { return $false }
        $configArgument = '(?:"' + [regex]::Escape($ConfigPath) + '"|' + [regex]::Escape($ConfigPath) + ')(?:\s|$)'
        $lbArgument = '(?:"' + [regex]::Escape($LbScript) + '"|' + [regex]::Escape($LbScript) + ')(?:\s|$)'
        return ($command -match ('(?i)\s-m\s+ccproxy\s+serve\s+--config\s+' + $configArgument)) -or
            ($command -match ('(?i)\s' + $lbArgument))
    }
    catch { return $false }
}

function Get-VerifiedProxyProcess {
    param([int]$ProcessId, $Manifest, [ref]$IdentityState)

    if ($null -ne $IdentityState) { $IdentityState.Value = 'unknown' }
    $process = $null
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $handle = $process.Handle
        if ($null -eq $handle -or $handle -eq [IntPtr]::Zero) { throw 'Process handle is unavailable.' }
        if ($process.HasExited) {
            if ($null -ne $IdentityState) { $IdentityState.Value = 'absent' }
            throw 'Recorded process has exited.'
        }
        $identityProperty = if ($null -ne $Manifest) { $Manifest.PSObject.Properties['wrapper_start_ticks'] } else { $null }
        if ($null -ne $identityProperty) {
            $identity = $identityProperty.Value.PSObject.Properties[[string]$ProcessId]
            if ($null -eq $identity) { throw 'Recorded process identity is missing.' }
            if ([string]$identity.Value -ne $process.StartTime.ToUniversalTime().Ticks.ToString()) {
                if ($null -ne $IdentityState) { $IdentityState.Value = 'different' }
                throw 'Recorded process identity does not match.'
            }
        }
        elseif (-not (Test-LegacyProxyProcess -Process $process)) { throw 'Legacy process ownership could not be verified.' }
        if ($null -ne $IdentityState) { $IdentityState.Value = 'same' }
        return $process
    }
    catch {
        if ($null -eq $process -and $_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId*' -and $null -ne $IdentityState) {
            $IdentityState.Value = 'absent'
        }
        if ($null -ne $process) { $process.Dispose() }
        return $null
    }
}

function Test-ProxyReady {
    param(
        [int]$ListenPort = $Port
    )

    try {
        $url = "http://$HostName`:$ListenPort/router"
        $headers = @{
            Authorization     = "Bearer $ProxyToken"
            "anthropic-version" = "2023-06-01"
        }
        $response = Invoke-RestMethod -Method Get -Uri "$url/v1/models" -Headers $headers -TimeoutSec 2
        $ids = @($response.data | ForEach-Object { [string]$_.id })
        return $ids -contains "anthropic/gateway/codex/gpt-5.6-sol"
    }
    catch {
        return $false
    }
}

function Wait-ProxyReady {
    param(
        [int]$TimeoutSeconds = 20,
        [int]$ListenPort = $Port,
        [Diagnostics.Process]$HostProcess = $null
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($null -ne $HostProcess) {
            $HostProcess.Refresh()
            if ($HostProcess.HasExited) {
                throw "ccproxy host process exited before becoming ready. Check $StderrPath"
            }
        }

        if (Test-ProxyReady -ListenPort $ListenPort) {
            return $true
        }

        Start-Sleep -Milliseconds 250
    }

    return $false
}

function Wait-ExistingProxy {
    param(
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $existing = Get-ManagedProcess
        if ($null -eq $existing) {
            return $false
        }

        if (Test-ProxyReady) {
            return $true
        }

        Start-Sleep -Milliseconds 250
    }

    return $false
}

function New-ServeEncodedCommand {
    param(
        [int]$ServePort = $Port,
        [string]$ServeStdoutPath = $StdoutPath,
        [string]$ServeStderrPath = $StderrPath
    )

    $invocation = Get-CcproxyInvocation
    $pythonPath = $invocation.FilePath
    # Resolve trace + error-log opt-in flags from the caller environment so the
    # background process inherits them deterministically (the encoded script runs
    # with -NoProfile in a fresh PowerShell host).
    $traceFlag = if ($env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT -eq "1") { "1" } else { "0" }
    $errorFlag = if ($env:CCPROXY_ERROR_LOG_OPT -eq "1") { "1" } else { "0" }
    # WP-Z: resolve compaction flag the same way as the trace flag so the
    # backgrounded encoded-command script inherits the value deterministically.
    $compactionFlag = if ($env:CCPROXY_CONTEXT_COMPACTION -eq "0") { "0" } else { "1" }
    # WP-AT: propagate the opt-in latency knobs to the backgrounded process.
    # Both default to "" (unset) so the helpers in compaction.py treat them
    # as OFF — only forwarded when the caller explicitly set them.
    $brevityFlag = if ($env:CCPROXY_BREVITY_HINT) { $env:CCPROXY_BREVITY_HINT } else { "" }
    $maxOutFloor = if ($env:CCPROXY_MAX_OUTPUT_FLOOR) { $env:CCPROXY_MAX_OUTPUT_FLOOR } else { "" }
    $script = @"
`$env:PYTHONUTF8 = '1'
`$env:PYTHONIOENCODING = 'utf-8'
`$env:NO_COLOR = '1'
`$env:CONFIG_FILE = '$ConfigPath'
`$env:CCPROXY_CODEX_ADAPTIVE_REASONING_EFFORT = 'xhigh'
`$env:CCPROXY_CODEX_DEFAULT_REASONING_EFFORT = 'xhigh'
`$env:CCPROXY_CODEX_REASONING_SUMMARY = 'auto'
`$env:CCPROXY_CONTEXT_COMPACTION = '$compactionFlag'
`$env:CCPROXY_BREVITY_HINT = '$brevityFlag'
`$env:CCPROXY_MAX_OUTPUT_FLOOR = '$maxOutFloor'
`$env:CCPROXY_CLAUDE_COMPAT_TRACE = '$traceFlag'
`$env:CCPROXY_CLAUDE_COMPAT_TRACE_PATH = '$CompatTracePath'
`$env:CCPROXY_ERROR_LOG = '$errorFlag'
`$env:CCPROXY_ERROR_LOG_PATH = '$ErrorLogPath'
`$env:SECURITY__AUTH_TOKEN = `$env:CLAUDE_CODEX_PROXY_TOKEN
& '$pythonPath' -m ccproxy serve --config '$ConfigPath' --host '$HostName' --port '$ServePort' 1>> '$ServeStdoutPath' 2>> '$ServeStderrPath'
"@
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

function New-LbEncodedCommand {
    param([int[]]$BackendPorts)

    if (-not (Test-Path -LiteralPath $LbScript)) {
        throw "load balancer script is missing: $LbScript"
    }
    $backendLiteral = (($BackendPorts | ForEach-Object { "'http://$HostName`:$($_)'" }) -join ",")
    $script = @"
`$backends = @($backendLiteral)
`$argsList = @('--listen-host', '$HostName', '--listen-port', '$Port', '--timeout', '600')
foreach (`$backend in `$backends) { `$argsList += @('--backend', `$backend) }
& '$CcproxyPython' '$LbScript' @argsList 1>> '$LbStdoutPath' 2>> '$LbStderrPath'
"@
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

function New-TraceRotatorEncodedCommand {
    param(
        [long]$MaxBytes,
        [int]$Keep,
        [int]$IntervalSeconds
    )

    $script = @"
while (`$true) {
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File '$RotateTraceScript' -Path '$CompatTracePath' -MaxBytes $MaxBytes -Keep $Keep 1>> '$TraceRotatorStdoutPath' 2>> '$TraceRotatorStderrPath'
    }
    catch {
        (`"trace-rotator failed: `" + `$_) | Out-File -FilePath '$TraceRotatorStderrPath' -Encoding utf8 -Append
    }
    Start-Sleep -Seconds $IntervalSeconds
}
"@
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

function Start-TraceRotator {
    if ($env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT -ne "1") {
        return $null
    }
    if (-not (Test-Path -LiteralPath $RotateTraceScript)) {
        throw "rotate-trace script is missing: $RotateTraceScript"
    }
    $maxBytes = Get-EnvLong -Name "CCPROXY_TRACE_MAX_BYTES" -Default $DefaultTraceMaxBytes -Min 1048576 -Max 1099511627776
    $keep = Get-EnvInt -Name "CCPROXY_TRACE_ROTATIONS" -Default $DefaultTraceRotations -Min 1 -Max 20
    $intervalSeconds = Get-TraceRotateIntervalSeconds
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-ExecutionPolicy", "Bypass",
            "-NoProfile",
            "-EncodedCommand", (New-TraceRotatorEncodedCommand -MaxBytes $maxBytes -Keep $keep -IntervalSeconds $intervalSeconds)
        ) `
        -WindowStyle Hidden `
        -PassThru
    if ($null -eq $process) {
        throw "Start-Process returned no process handle for trace rotator."
    }
    return $process
}

function Read-RunManifest {
    if (-not (Test-Path -LiteralPath $PidManifestPath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $PidManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-RuntimeGeneration {
    # A warm process is reusable only when the installed config, routing
    # plugins, and patched streaming implementation still match its manifest.
    $paths = @($ConfigPath, $PSCommandPath, $LbScript)
    foreach ($dir in @(
        (Join-Path $HomeDir ".config\ccproxy\plugins\dispatcher"),
        (Join-Path $HomeDir ".config\ccproxy\plugins\fcc"),
        (Join-Path $HomeDir ".config\ccproxy\plugins\fugu"),
        (Join-Path $HomeDir ".config\ccproxy\plugins\gemini_cli")
    )) {
        if (Test-Path -LiteralPath $dir) {
            $paths += @(Get-ChildItem -LiteralPath $dir -File -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -notin @('.pyc', '.pyo') } | ForEach-Object { $_.FullName })
        }
    }
    $sitePackage = Join-Path $RoamingAppData "uv\tools\ccproxy-api\Lib\site-packages\ccproxy"
    foreach ($relative in @(
        "llms\formatters\anthropic_to_openai\requests.py",
        "llms\formatters\anthropic_to_openai\streams.py",
        "llms\formatters\anthropic_to_openai\compaction.py",
        "plugins\codex\adapter.py",
        "plugins\codex\model_defaults.py",
        "plugins\claude_api\routes.py",
        "services\adapters\simple_converters.py",
        "services\adapters\delta_utils.py",
        "streaming\buffer.py"
    )) {
        $candidate = Join-Path $sitePackage $relative
        if (Test-Path -LiteralPath $candidate) { $paths += $candidate }
    }

    $paths += Join-Path $sitePackage "llms\formatters\anthropic_to_openai\reasoning_models.py"

    $parts = @(
        "trace=$($env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT)",
        "error_log=$($env:CCPROXY_ERROR_LOG_OPT)"
    )
    foreach ($path in @($paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Sort-Object -Unique)) {
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        $parts += "$path=$hash"
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))
        return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Write-RunManifest {
    param(
        [string]$Mode,
        [int]$WorkerCount,
        [int[]]$BackendPorts,
        [int[]]$WrapperPids,
        [int]$TraceRotatorPid = 0,
        [object[]]$WrapperProcesses = @(),
        [hashtable]$WrapperStartTicks = @{},
        [string]$RuntimeGeneration = ""
    )

    $identities = @{}
    foreach ($key in $WrapperStartTicks.Keys) { $identities[[string]$key] = [string]$WrapperStartTicks[$key] }
    foreach ($wrapper in $WrapperProcesses) {
        try {
            $null = $wrapper.Handle
            $identities[[string]$wrapper.Id] = $wrapper.StartTime.ToUniversalTime().Ticks.ToString()
        }
        catch {}
    }
    if ([string]::IsNullOrWhiteSpace($RuntimeGeneration)) { $RuntimeGeneration = Get-RuntimeGeneration }
    $manifest = @{
        mode = $Mode
        port = $Port
        worker_count = $WorkerCount
        backend_ports = $BackendPorts
        wrapper_pids = $WrapperPids
        wrapper_start_ticks = $identities
        trace_rotator_pid = $TraceRotatorPid
        runtime_generation = $RuntimeGeneration
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $PidManifestPath -Encoding utf8
}

function Ensure-TraceRotator {
    param($Manifest)

    if ($env:CCPROXY_CLAUDE_COMPAT_TRACE_OPT -ne '1') { return }
    $rotatorId = [int]$Manifest.trace_rotator_pid
    if ($rotatorId -gt 0) {
        $identityState = 'unknown'
        $existingRotator = Get-VerifiedProxyProcess -ProcessId $rotatorId -Manifest $Manifest -IdentityState ([ref]$identityState)
        if ($null -ne $existingRotator) { $existingRotator.Dispose(); return }
        if ($identityState -notin @('absent', 'different')) {
            throw 'Existing trace rotator identity could not be verified; the manifest was left unchanged.'
        }
    }
    $wrapperPids = @($Manifest.wrapper_pids | Where-Object { [int]$_ -ne $rotatorId } | ForEach-Object { [int]$_ })
    $identities = @{}
    $identityProperty = $Manifest.PSObject.Properties['wrapper_start_ticks']
    foreach ($wrapperId in $wrapperPids) {
        if ($null -ne $identityProperty) {
            $identity = $identityProperty.Value.PSObject.Properties[[string]$wrapperId]
            if ($null -ne $identity) { $identities[[string]$wrapperId] = [string]$identity.Value }
        }
        else {
            $wrapper = Get-VerifiedProxyProcess -ProcessId $wrapperId -Manifest $Manifest
            if ($null -ne $wrapper) {
                try { $identities[[string]$wrapperId] = $wrapper.StartTime.ToUniversalTime().Ticks.ToString() }
                finally { $wrapper.Dispose() }
            }
        }
    }
    $rotator = Start-TraceRotator
    if ($null -eq $rotator) { throw 'Trace rotator did not start.' }
    try {
        $wrapperPids += [int]$rotator.Id
        Write-RunManifest -Mode $Manifest.mode -WorkerCount $Manifest.worker_count -BackendPorts @($Manifest.backend_ports) -WrapperPids $wrapperPids -TraceRotatorPid $rotator.Id -WrapperProcesses @($rotator) -WrapperStartTicks $identities -RuntimeGeneration $Manifest.runtime_generation
    }
    finally { $rotator.Dispose() }
}

function Start-ParallelProxy {
    param([int]$WorkerCount)

    $backendPorts = @(Get-BackendPorts -WorkerCount $WorkerCount)
    $wrapperPids = @()
    $wrapperProcesses = @()

    foreach ($listenPort in @($Port) + $backendPorts) {
        foreach ($ownerPid in @(Get-ProxyPortOwnerPids -ListenPort $listenPort)) {
            $identityState = 'unknown'
            $owner = Get-VerifiedProxyProcess -ProcessId $ownerPid -Manifest $null -IdentityState ([ref]$identityState)
            if ($null -eq $owner) {
                if ($identityState -eq 'absent') { continue }
                throw 'Proxy port owner identity could not be verified; the process was left running.'
            }
            try { Stop-Process -InputObject $owner -Force -ErrorAction Stop }
            finally { $owner.Dispose() }
            Start-Sleep -Milliseconds 250
        }
    }

    foreach ($backendPort in $backendPorts) {
        $workerStdout = Join-Path $LogDir "ccproxy.worker-$backendPort.stdout.log"
        $workerStderr = Join-Path $LogDir "ccproxy.worker-$backendPort.stderr.log"
        $process = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList @(
                "-ExecutionPolicy", "Bypass",
                "-NoProfile",
                "-EncodedCommand", (New-ServeEncodedCommand -ServePort $backendPort -ServeStdoutPath $workerStdout -ServeStderrPath $workerStderr)
            ) `
            -WindowStyle Hidden `
            -PassThru
        if ($null -eq $process) {
            throw "Start-Process returned no process handle for backend port $backendPort."
        }
        $wrapperPids += [int]$process.Id
        $wrapperProcesses += $process
    }

    foreach ($backendPort in $backendPorts) {
        if (-not (Wait-ProxyReady -ListenPort $backendPort -TimeoutSeconds 30)) {
            throw "ccproxy backend on port $backendPort did not become ready in time."
        }
    }

    $lbProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-ExecutionPolicy", "Bypass",
            "-NoProfile",
            "-EncodedCommand", (New-LbEncodedCommand -BackendPorts $backendPorts)
        ) `
        -WindowStyle Hidden `
        -PassThru
    if ($null -eq $lbProcess) {
        throw "Start-Process returned no process handle for load balancer."
    }
    $wrapperPids += [int]$lbProcess.Id
    $wrapperProcesses += $lbProcess
    $rotator = Start-TraceRotator
    $rotatorPid = 0
    if ($null -ne $rotator) {
        $rotatorPid = [int]$rotator.Id
        $wrapperPids += $rotatorPid
        $wrapperProcesses += $rotator
    }
    Set-Content -Path $PidPath -Value ([int]$lbProcess.Id) -Encoding ascii
    Write-RunManifest -Mode "parallel" -WorkerCount $WorkerCount -BackendPorts $backendPorts -WrapperPids $wrapperPids -TraceRotatorPid $rotatorPid -WrapperProcesses $wrapperProcesses

    if (-not (Wait-ProxyReady -ListenPort $Port -TimeoutSeconds 20)) {
        throw "ccproxy load balancer did not become ready in time. Check $LbStderrPath"
    }

    Write-Output "started mode=parallel workers=$WorkerCount pid=$([int]$lbProcess.Id) url=$BaseUrl"
    foreach ($wrapper in $wrapperProcesses) { $wrapper.Dispose() }
    Write-Output "backend_ports=$($backendPorts -join ',')"
    Write-Output "stdout=$LbStdoutPath"
    Write-Output "stderr=$LbStderrPath"
}

function Start-Proxy {
    $desiredWorkerCount = Get-ParallelWorkerCount
    $workerOverride = -not [string]::IsNullOrWhiteSpace($env:CCPROXY_PARALLEL_WORKERS)
    $runtimeGeneration = Get-RuntimeGeneration

    $existing = Get-ManagedProcess
    if ($null -ne $existing) {
        # Wrap each predicate in parens — `Test-ProxyReady -or (Wait-ExistingProxy)`
        # without parens is parsed as a single command invocation where `-or`
        # binds to Test-ProxyReady as a parameter, causing surprising results.
        if ((Test-ProxyReady) -or (Wait-ExistingProxy)) {
            $runtimeGeneration = Get-RuntimeGeneration
            $manifest = Read-RunManifest
            $currentWorkers = if ($null -ne $manifest -and $manifest.worker_count) { [int]$manifest.worker_count } else { 1 }
            $currentMode = if ($null -ne $manifest -and $manifest.mode) { [string]$manifest.mode } else { "single" }
            $desiredMode = if ($desiredWorkerCount -gt 1) { "parallel" } else { "single" }
            $manifestGeneration = if ($null -ne $manifest -and $manifest.runtime_generation) { [string]$manifest.runtime_generation } else { "" }
            if ($manifestGeneration -eq $runtimeGeneration -and $currentWorkers -eq $desiredWorkerCount -and $currentMode -eq $desiredMode) {
                Ensure-TraceRotator -Manifest $manifest
                $existingId = try { $existing.Id } catch { "?" }
                Write-Output "already running pid=$existingId mode=$currentMode workers=$currentWorkers url=$BaseUrl"
                return
            }
            if ($manifestGeneration -eq $runtimeGeneration -and -not $workerOverride) {
                Ensure-TraceRotator -Manifest $manifest
                $existingId = try { $existing.Id } catch { "?" }
                Write-Output "already running pid=$existingId mode=$currentMode workers=$currentWorkers url=$BaseUrl"
                return
            }
            $existingId = try { $existing.Id } catch { "?" }
            $reason = if ($manifestGeneration -ne $runtimeGeneration) { "runtime generation changed" } else { "explicit worker override" }
            throw "Proxy maintenance restart required ($reason). Existing pid=$existingId was left running; run 'claude-codex-proxy restart' when active sessions are idle."
        }

        throw "Existing proxy failed readiness checks and was left running. Run 'claude-codex-proxy restart' when active sessions are idle."
    }

    if (Test-ProxyReady) {
        $ownerPids = @(Get-ProxyPortOwnerPids -ListenPort $Port)
        if ($desiredWorkerCount -gt 1 -and $ownerPids.Count -gt 0) {
            throw "Unmanaged proxy was left running. A maintenance restart is required to change worker count."
        }
        else {
            Write-Output "running unmanaged url=$BaseUrl"
            return
        }
    }

    Invoke-TraceRotation

    if (-not (Test-Path $ConfigPath)) {
        throw "ccproxy config file is missing: $ConfigPath"
    }

    Sync-CodexHeaderCache
    Test-CodexAuth
    Set-CcproxyEnvironment

    $workerCount = $desiredWorkerCount
    if ($workerCount -gt 1) {
        Start-ParallelProxy -WorkerCount $workerCount
        return
    }

    $process = $null
    try {
        $process = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList @(
                "-ExecutionPolicy", "Bypass",
                "-NoProfile",
                "-EncodedCommand", (New-ServeEncodedCommand)
            ) `
            -WindowStyle Hidden `
            -PassThru
    }
    catch {
        throw "Failed to spawn ccproxy host process: $_"
    }

    if ($null -eq $process) {
        throw "Start-Process returned no process handle (PassThru failed)."
    }

    # Be defensive about the .Id access: if the launcher exits instantly the
    # process object can have a degenerate state under StrictMode.
    $startedPid = $null
    try { $startedPid = $process.Id } catch { $startedPid = $null }
    if ($null -eq $startedPid) {
        throw "Spawned process has no PID (likely exited immediately). Check $StderrPath"
    }

    Set-Content -Path $PidPath -Value $startedPid -Encoding ascii
    Write-RunManifest -Mode "single" -WorkerCount 1 -BackendPorts @() -WrapperPids @($startedPid) -TraceRotatorPid 0 -WrapperProcesses @($process) -RuntimeGeneration $runtimeGeneration

    if (-not (Wait-ProxyReady -TimeoutSeconds 90 -HostProcess $process)) {
        throw "ccproxy did not become ready in time. Check $StderrPath"
    }
    if ((Get-RuntimeGeneration) -ne $runtimeGeneration) {
        throw 'Proxy maintenance restart required (runtime changed during startup); the process was left running.'
    }

    $rotator = Start-TraceRotator
    $rotatorPid = 0
    $wrapperPids = @($startedPid)
    $wrapperProcesses = @($process)
    if ($null -ne $rotator) {
        $rotatorPid = [int]$rotator.Id
        $wrapperPids += $rotatorPid
        $wrapperProcesses += $rotator
    }
    Write-RunManifest -Mode "single" -WorkerCount 1 -BackendPorts @() -WrapperPids $wrapperPids -TraceRotatorPid $rotatorPid -WrapperProcesses $wrapperProcesses -RuntimeGeneration $runtimeGeneration
    foreach ($wrapper in $wrapperProcesses) { $wrapper.Dispose() }

    Write-Output "started pid=$startedPid url=$BaseUrl"
    Write-Output "stdout=$StdoutPath"
    Write-Output "stderr=$StderrPath"
}

function Get-ProxyPortOwnerPids {
    param(
        [int]$ListenPort = $Port
    )

    # The managed PID recorded in ccproxy.pid is a wrapper PowerShell host;
    # the real `python -m ccproxy serve` runs as its child and is orphaned
    # (Windows does not cascade-kill) when the wrapper dies. Resolve whatever
    # currently owns the proxy port so stop can target the orphan directly.
    try {
        $connections = @(Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop)
    }
    catch {
        return @()
    }

    $out = @()
    foreach ($conn in $connections) {
        if ($null -eq $conn) { continue }
        $ownerPid = [int]$conn.OwningProcess
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction Stop).CommandLine
            if ($cmd -and $cmd -notmatch 'ccproxy') { continue }
        }
        catch {
            # CommandLine unavailable - assume it is ours rather than leak an orphan.
        }
        $out += $ownerPid
    }
    return @($out | Sort-Object -Unique)
}

function Get-ProxyPortOwnerPid {
    param(
        [int]$ListenPort = $Port
    )

    return @(Get-ProxyPortOwnerPids -ListenPort $ListenPort | Select-Object -First 1)[0]
}

function Stop-Proxy {
    $stoppedIds = New-Object System.Collections.Generic.List[int]
    $manifest = Read-RunManifest
    $manifestPids = @()
    if ($null -ne $manifest -and $manifest.wrapper_pids) {
        $manifestPids = @($manifest.wrapper_pids | ForEach-Object { [int]$_ })
    }

    $recordedPid = Get-RecordedPid
    if ($null -ne $recordedPid) { $manifestPids += [int]$recordedPid }
    $unverifiedPids = @()
    foreach ($wrapperPid in @($manifestPids | Sort-Object -Unique)) {
        $wrapper = Get-VerifiedProxyProcess -ProcessId $wrapperPid -Manifest $manifest
        if ($null -eq $wrapper) {
            $unverifiedPids += $wrapperPid
            continue
        }
        try {
            Stop-Process -InputObject $wrapper -Force -ErrorAction Stop
            $stoppedIds.Add($wrapperPid)
        }
        catch {}
        finally { $wrapper.Dispose() }
    }
    Remove-Item $PidPath -ErrorAction SilentlyContinue

    # Kill the orphaned ccproxy python child too, so a later `start` actually
    # rebinds the port and reloads (e.g. a refreshed Codex header cache).
    $portsToStop = @($Port)
    if ($null -ne $manifest -and $manifest.backend_ports) {
        $portsToStop += @($manifest.backend_ports | ForEach-Object { [int]$_ })
    }
    else {
        for ($i = 1; $i -le $MaxParallelWorkers; $i++) {
            $portsToStop += ($Port + $i)
        }
    }
    $portsToStop = @($portsToStop | Sort-Object -Unique)
    for ($stopPass = 0; $stopPass -lt 3; $stopPass++) {
        $foundAny = $false
        foreach ($listenPort in $portsToStop) {
            $ownerPids = @(Get-ProxyPortOwnerPids -ListenPort $listenPort)
            foreach ($ownerPid in $ownerPids) {
                if ($null -eq $ownerPid -or $stoppedIds.Contains($ownerPid) -or $unverifiedPids -contains $ownerPid) { continue }
                $owner = Get-VerifiedProxyProcess -ProcessId $ownerPid -Manifest $null
                if ($null -eq $owner) { continue }
                $foundAny = $true
                try {
                    Stop-Process -InputObject $owner -Force -ErrorAction Stop
                    $stoppedIds.Add($ownerPid)
                }
                catch {}
                finally { $owner.Dispose() }
            }
        }
        if (-not $foundAny) { break }
        Start-Sleep -Milliseconds 250
    }
    Remove-Item $PidManifestPath -ErrorAction SilentlyContinue

    if ($stoppedIds.Count -gt 0) {
        Write-Output ("stopped pid=" + ($stoppedIds -join ','))
    }
    else {
        Write-Output "already stopped"
    }
}

function Format-ByteSize {
    param([long]$Bytes)

    if ($Bytes -ge 1073741824) {
        return ("{0:N1}GiB" -f ($Bytes / 1073741824))
    }
    if ($Bytes -ge 1048576) {
        return ("{0:N1}MiB" -f ($Bytes / 1048576))
    }
    if ($Bytes -ge 1024) {
        return ("{0:N1}KiB" -f ($Bytes / 1024))
    }
    return "$Bytes`B"
}

function Get-TraceStatusText {
    $liveBytes = [long]0
    if (Test-Path -LiteralPath $CompatTracePath) {
        try { $liveBytes = [long](Get-Item -LiteralPath $CompatTracePath -Force).Length } catch {}
    }

    $archiveBytes = [long]0
    $archiveCount = 0
    try {
        $archiveItems = @(Get-ChildItem -LiteralPath $LogDir -Filter 'claude-compat-trace.jsonl.*' -File -ErrorAction SilentlyContinue)
        $archiveCount = $archiveItems.Count
        foreach ($archive in $archiveItems) {
            $archiveBytes += [long]$archive.Length
        }
    } catch {}

    $maxBytes = Get-EnvLong -Name "CCPROXY_TRACE_MAX_BYTES" -Default $DefaultTraceMaxBytes -Min 1048576 -Max 1099511627776
    $keep = Get-EnvInt -Name "CCPROXY_TRACE_ROTATIONS" -Default $DefaultTraceRotations -Min 1 -Max 20
    return "trace=$(Format-ByteSize $liveBytes) archives=$archiveCount/$(Format-ByteSize $archiveBytes) rotate=$(Format-ByteSize $maxBytes)x$keep"
}

function Show-Status {
    $existing = Get-ManagedProcess
    $manifest = Read-RunManifest
    $mode = if ($null -ne $manifest -and $manifest.mode) { [string]$manifest.mode } else { "single" }
    $workers = if ($null -ne $manifest -and $manifest.worker_count) { [int]$manifest.worker_count } else { 1 }
    $traceStatus = Get-TraceStatusText
    if ($null -eq $existing) {
        if (Test-ProxyReady) {
            Write-Host "running unmanaged mode=$mode workers=$workers url=$BaseUrl $traceStatus"
            return 0
        }

        Write-Host "stopped $traceStatus"
        return 1
    }

    $existingId = try { $existing.Id } catch { "?" }
    if (Test-ProxyReady) {
        Write-Host "running pid=$existingId mode=$mode workers=$workers url=$BaseUrl $traceStatus"
        return 0
    }

    Write-Host "starting pid=$existingId url=$BaseUrl $traceStatus"
    return 1
}

function Show-Logs {
    if (-not (Test-Path $StdoutPath) -and -not (Test-Path $StderrPath)) {
        Write-Output "No logs yet."
        return
    }

    if (Test-Path $StdoutPath) {
        Write-Output "== stdout =="
        Get-Content $StdoutPath -Tail 80
    }

    if (Test-Path $StderrPath) {
        Write-Output "== stderr =="
        Get-Content $StderrPath -Tail 80
    }
}

function Invoke-WithProxyLock {
    param([scriptblock]$Action)

    $mutex = [Threading.Mutex]::new($false, "Local\claude-codex-proxy-4026")
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(180000)
        }
        catch [Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Timed out waiting for another proxy maintenance operation to finish."
        }
        & $Action
    }
    finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

switch ($Command) {
    "run" {
        Invoke-TraceRotation
        if (-not (Test-Path $ConfigPath)) {
            throw "ccproxy config file is missing: $ConfigPath"
        }

        if ((Get-ParallelWorkerCount) -gt 1) {
            Write-Output "parallel workers are only used by 'start'; foreground 'run' uses one ccproxy server."
        }
        Sync-CodexHeaderCache
        Test-CodexAuth
        Set-CcproxyEnvironment
        $env:SECURITY__AUTH_TOKEN = $ProxyToken
        $invocation = Get-CcproxyInvocation
        & $invocation.FilePath @($invocation.PrefixArgs + @(
            "serve",
            "--config", $ConfigPath,
            "--host", $HostName,
            "--port", "$Port"
        ))
        exit $LASTEXITCODE
    }
    "start" {
        Invoke-WithProxyLock { Start-Proxy }
        exit 0
    }
    "restart" {
        Invoke-WithProxyLock {
            Stop-Proxy
            Start-Proxy
        }
        exit 0
    }
    "stop" {
        Invoke-WithProxyLock { Stop-Proxy }
        exit 0
    }
    "status" {
        exit (Show-Status)
    }
    "logs" {
        Show-Logs
        exit 0
    }
}

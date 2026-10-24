<#
.SYNOPSIS
    Tail ccproxy-errors.jsonl and emit a Windows toast for each new error line.

.DESCRIPTION
    Companion to the CCPROXY_ERROR_LOG=1 opt-in. Run this in the background
    after enabling error logging in the proxy:
        powershell -ExecutionPolicy Bypass -File scripts\errors-watch.ps1 &

    The script avoids hard dependencies: if BurntToast is not installed it
    falls back to writing the message to the host stream so users that read
    a log pane (e.g. mycmux) still see it.

.PARAMETER Path
    Full path to the error log. Default:
    %USERPROFILE%\.claude-codex-proxy\logs\ccproxy-errors.jsonl

.PARAMETER PollSeconds
    Polling interval. Default 5s.
#>
param(
    [string]$Path,
    [int]$PollSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Path)) {
    $homeDir = if ($env:USERPROFILE) {
        $env:USERPROFILE
    } else {
        [Environment]::GetFolderPath('UserProfile')
    }
    $Path = Join-Path $homeDir '.claude-codex-proxy\logs\ccproxy-errors.jsonl'
}

# BurntToast is optional. Try to import; fall back to host echo on failure.
$haveToast = $false
try {
    Import-Module BurntToast -ErrorAction Stop -DisableNameChecking
    $haveToast = $true
} catch {
    Write-Warning "BurntToast module not loaded ($_). Will fall back to host output."
}

function Send-Notify {
    param([string]$Title, [string]$Body)
    if ($haveToast) {
        try {
            New-BurntToastNotification -Text $Title, $Body -AppLogo $null
            return
        } catch {
            # fall through to host echo
        }
    }
    Write-Host "[ccproxy-error] $Title :: $Body"
}

# Wait for the file to exist
while (-not (Test-Path -LiteralPath $Path)) {
    Write-Host "Waiting for $Path..."
    Start-Sleep -Seconds $PollSeconds
}

Write-Host "Watching $Path"

$lastSize = (Get-Item -LiteralPath $Path).Length
while ($true) {
    Start-Sleep -Seconds $PollSeconds
    if (-not (Test-Path -LiteralPath $Path)) {
        # Rotated away. Reset.
        $lastSize = 0
        continue
    }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -lt $lastSize) {
        # Truncated or rotated, reset
        $lastSize = 0
    }
    if ($size -le $lastSize) {
        continue
    }

    $stream = $null
    $reader = $null
    try {
        $stream = [System.IO.FileStream]::new($Path, 'Open', 'Read', 'ReadWrite')
        $stream.Seek($lastSize, 'Begin') | Out-Null
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try {
                $rec = $line | ConvertFrom-Json
                $title = "ccproxy $($rec.status) $($rec.error_type)"
                $detail = "$($rec.phase) | model=$($rec.request.model)"
                if ($rec.error) {
                    $detail += " | $(($rec.error -split "`n")[0])"
                }
                Send-Notify -Title $title -Body $detail
            } catch {
                Send-Notify -Title "ccproxy error" -Body $line
            }
        }
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
    $lastSize = $size
}

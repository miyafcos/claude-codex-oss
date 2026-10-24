<#
.SYNOPSIS
    Rotate the ccproxy claude-compat-trace.jsonl file when it exceeds a size threshold.

.DESCRIPTION
    The ccproxy adapter opens the trace file with `open(path, "a")` on every
    write, so rotating the file with Move-Item is safe even while the proxy
    is running: subsequent writes simply create a fresh file at the original
    path. No proxy restart required.

    Rotation scheme (Keep=3):
        claude-compat-trace.jsonl.3  -> deleted
        claude-compat-trace.jsonl.2  -> claude-compat-trace.jsonl.3
        claude-compat-trace.jsonl.1  -> claude-compat-trace.jsonl.2
        claude-compat-trace.jsonl    -> claude-compat-trace.jsonl.1
        (new empty file is created automatically by the next ccproxy write)

.PARAMETER Path
    Full path to the trace file. Defaults to
    %USERPROFILE%\.claude-codex-proxy\logs\claude-compat-trace.jsonl

.PARAMETER MaxBytes
    Rotate when the file is larger than this. Default 64 MiB.

.PARAMETER Keep
    Number of historical files to retain (excluding the live file). Default 5.

.PARAMETER Force
    Rotate even if the file is below MaxBytes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\rotate-trace.ps1
#>
param(
    [string]$Path,
    [long]$MaxBytes = 67108864,
    [int]$Keep = 5,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Path)) {
    $homeDir = if ($env:USERPROFILE) {
        $env:USERPROFILE
    } else {
        [Environment]::GetFolderPath('UserProfile')
    }
    $Path = Join-Path $homeDir '.claude-codex-proxy\logs\claude-compat-trace.jsonl'
}

if ($Keep -lt 1) {
    throw "Keep must be >= 1 (got $Keep)"
}
if ($MaxBytes -lt 1024) {
    throw "MaxBytes must be >= 1024 (got $MaxBytes)"
}

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Output "rotate-trace: source missing, nothing to do ($Path)"
    exit 0
}

$item = Get-Item -LiteralPath $Path -Force
$size = $item.Length

if (-not $Force -and $size -le $MaxBytes) {
    Write-Output "rotate-trace: under threshold ($size <= $MaxBytes bytes), skip"
    exit 0
}

Write-Output "rotate-trace: rotating $Path ($size bytes, threshold=$MaxBytes, keep=$Keep)"

# Step 1: prune archives beyond the current Keep setting. Older versions or
# env overrides may have created .6, .7, ... files; leave cleanup to the next
# explicit rotation instead of deleting them during status checks.
$archiveDir = Split-Path -Parent $Path
$archiveName = Split-Path -Leaf $Path
if (Test-Path -LiteralPath $archiveDir) {
    $extraArchives = @(
        Get-ChildItem -LiteralPath $archiveDir -Filter "$archiveName.*" -File -ErrorAction SilentlyContinue |
            Where-Object {
                $match = [regex]::Match($_.Name, '^' + [regex]::Escape($archiveName) + '\.(\d+)$')
                $match.Success -and ([int]$match.Groups[1].Value -gt $Keep)
            }
    )
    foreach ($archive in $extraArchives) {
        try {
            Remove-Item -LiteralPath $archive.FullName -Force
            Write-Output "  pruned extra archive $($archive.FullName)"
        } catch {
            Write-Warning "  failed to prune $($archive.FullName): $_"
        }
    }
}

# Step 2: drop the oldest retained archive if present
$oldest = "$Path.$Keep"
if (Test-Path -LiteralPath $oldest) {
    try {
        Remove-Item -LiteralPath $oldest -Force
        Write-Output "  removed $oldest"
    } catch {
        Write-Warning "  failed to remove $oldest`: $_"
    }
}

# Step 3: shift archives N-1 -> N, N-2 -> N-1, ..., 1 -> 2
for ($i = $Keep - 1; $i -ge 1; $i--) {
    $src = "$Path.$i"
    $dst = "$Path.$($i + 1)"
    if (Test-Path -LiteralPath $src) {
        try {
            Move-Item -LiteralPath $src -Destination $dst -Force
            Write-Output "  moved $src -> $dst"
        } catch {
            Write-Warning "  failed to move $src -> $dst`: $_"
        }
    }
}

# Step 4: live file -> .1
$firstArchive = "$Path.1"
try {
    Move-Item -LiteralPath $Path -Destination $firstArchive -Force
    Write-Output "  moved $Path -> $firstArchive"
} catch {
    # If Move-Item fails (rare on Windows when another process holds the
    # file open), fall back to truncating the live file.
    Write-Warning "  Move-Item failed ($_). Falling back to truncate."
    try {
        Set-Content -LiteralPath $Path -Value '' -Encoding utf8 -NoNewline
        Write-Output "  truncated $Path"
    } catch {
        throw "  unable to rotate or truncate $Path`: $_"
    }
    exit 0
}

# Step 5: ccproxy reopens with `open(path, "a")` per write so the live file
# will be re-created automatically. We pre-create an empty one so other
# tools (tail -f, file watchers) do not see a brief missing-file state.
try {
    New-Item -ItemType File -Path $Path -Force | Out-Null
    Write-Output "  created empty $Path"
} catch {
    Write-Warning "  could not pre-create $Path (ccproxy will create on next write): $_"
}

Write-Output "rotate-trace: done"
exit 0

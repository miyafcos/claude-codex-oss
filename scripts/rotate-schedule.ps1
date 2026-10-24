<#
.SYNOPSIS
    Register / unregister / inspect the rotate-trace scheduled task.

.DESCRIPTION
    Wraps schtasks.exe so the recurring trace-log rotation can be set up
    with a single command.

    The task name is fixed to ``claude-codex-hybrid-rotate-trace`` and runs
    ``scripts/rotate-trace.ps1`` (which only rotates if the live trace file
    is over its size threshold, default 64 MiB). Default frequencies:

      * hourly  : every 1 hour starting from the current minute
      * daily   : 02:00 local time
      * monthly : day 1 at 02:00 local time

    Both register and unregister are idempotent: re-running register
    replaces the existing definition; unregister silently succeeds when
    the task does not exist.

    This script intentionally mirrors healthcheck-schedule.ps1 instead of
    sharing a helper module — schtasks invocations differ in only a few
    arguments, and the explicit duplication keeps each ops script trivial
    to audit and edit.

.PARAMETER Action
    register | unregister | status

.PARAMETER Frequency
    hourly (default), daily, or monthly. Only used by register.

.PARAMETER Time
    Optional HH:mm string (24-hour). Default: 02:00 for daily/monthly.
    Ignored for hourly (schtasks computes the start automatically).

.PARAMETER TaskName
    Override the schtasks task name. Defaults to claude-codex-hybrid-rotate-trace.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\rotate-schedule.ps1 -Action register -Frequency hourly

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\rotate-schedule.ps1 -Action unregister
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('register', 'unregister', 'status')]
    [string]$Action,

    [ValidateSet('hourly', 'daily', 'monthly')]
    [string]$Frequency = 'hourly',

    [string]$Time = '02:00',

    [string]$TaskName = 'claude-codex-hybrid-rotate-trace'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$RotateScript = Join-Path $RepoRoot 'scripts\rotate-trace.ps1'

function Test-TaskExists {
    param([string]$Name)
    $proc = Start-Process -FilePath schtasks.exe -ArgumentList @('/query', '/tn', $Name) -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $env:TEMP 'rt-sched-out.txt') `
        -RedirectStandardError  (Join-Path $env:TEMP 'rt-sched-err.txt')
    return ($proc.ExitCode -eq 0)
}

switch ($Action) {
    'status' {
        Write-Output "task name    : $TaskName"
        Write-Output "rotate script: $RotateScript"
        if (Test-TaskExists -Name $TaskName) {
            Write-Output "state        : REGISTERED"
            & schtasks.exe /query /tn $TaskName /v /fo LIST 2>$null | Select-String -Pattern '^(TaskName|Status|Next Run Time|Last Run Time|Schedule Type|Start Time|Run As User|Task To Run):' | ForEach-Object { Write-Output $_.ToString() }
        } else {
            Write-Output "state        : NOT REGISTERED"
            Write-Output "hint         : powershell -ExecutionPolicy Bypass -File scripts\rotate-schedule.ps1 -Action register -Frequency hourly"
        }
        exit 0
    }

    'unregister' {
        if (-not (Test-TaskExists -Name $TaskName)) {
            Write-Output "rotate-schedule: task '$TaskName' not registered, nothing to do"
            exit 0
        }
        & schtasks.exe /delete /tn $TaskName /f
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Write-Error "schtasks /delete returned exit code $rc"
            exit $rc
        }
        Write-Output "rotate-schedule: removed task '$TaskName'"
        exit 0
    }

    'register' {
        if (-not (Test-Path -LiteralPath $RotateScript)) {
            Write-Error "rotate-trace.ps1 not found at $RotateScript"
            exit 2
        }

        # Build the command line schtasks will invoke. -File quoting keeps
        # paths with spaces (e.g. C:\Users\example with future profile name)
        # working without further escaping.
        $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$RotateScript`""

        # Translate frequency -> schtasks args. Hourly uses /sc HOURLY /mo 1
        # which schedules the task to run every 1 hour from the registration
        # time; /st is optional and we omit it so the first run happens at
        # the next aligned hour.
        $schedArgs = switch ($Frequency) {
            'hourly'  { @('/sc', 'HOURLY',  '/mo', '1') }
            'daily'   { @('/sc', 'DAILY',   '/st', $Time) }
            'monthly' { @('/sc', 'MONTHLY', '/d', '1', '/st', $Time) }
        }

        $args = @('/create', '/tn', $TaskName, '/tr', $cmd, '/f') + $schedArgs

        & schtasks.exe @args
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Write-Error "schtasks /create returned exit code $rc"
            exit $rc
        }
        Write-Output "rotate-schedule: registered '$TaskName' [$Frequency] -> $cmd"
        exit 0
    }
}

#Requires -Version 7.0
<#
.SYNOPSIS
    Show whether the background companion is alive, and what it is doing.

.DESCRIPTION
    Answers the only two questions that matter once the companion runs hidden:

      * is the scheduled task registered, and when did it last run?
      * is the process actually there, and does it have a visible window?

    It also tails today's log, so "it is running" can be checked without
    attaching to anything.

.PARAMETER TaskName
    Name of the scheduled task to inspect.

.PARAMETER LogLines
    How many trailing log lines to print. Defaults to 12.

.EXAMPLE
    .\status-companion.ps1
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'CodexMicroAllowanceCompanion',
    [int] $LogLines = 12
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

Write-Host ''
Write-Host '  Codex Micro companion status' -ForegroundColor Cyan

# ------------------------------------------------------------------ the task
Write-Section "Scheduled task: $TaskName"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Warn2 'not registered. Run .\install-autostart.ps1 -StartNow to set it up.'
}
else {
    $info = $task | Get-ScheduledTaskInfo
    $trigger = $task.Triggers | Select-Object -First 1
    $delay = if ($trigger -and $trigger.Delay) { $trigger.Delay } else { 'none' }
    Write-Ok "state=$($task.State)  logon delay=$delay"
    Write-Host "         last run : $($info.LastRunTime)  result=$($info.LastTaskResult)" -ForegroundColor DarkGray
    Write-Host "         next run : $($info.NextRunTime)" -ForegroundColor DarkGray
    Write-Host "         hidden   : window style in action = $([bool] ($task.Actions[0].Arguments -match 'WindowStyle Hidden'))" -ForegroundColor DarkGray
    if ($info.LastTaskResult -ne 0 -and $task.State -ne 'Running') {
        Write-Warn2 "the task is not running and its last result was $($info.LastTaskResult); see the log below."
    }
}

# ------------------------------------------------------------- the processes
Write-Section 'Processes'
$cim = Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='python.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue
$mine = $cim | Where-Object {
    $_.CommandLine -and ($_.CommandLine -like '*start-companion.ps1*' -or $_.CommandLine -like '*windows_companion.py*')
}

if (-not $mine) {
    Write-Warn2 'nothing is running. Start it with .\start-companion.cmd or .\install-autostart.ps1 -StartNow.'
}
else {
    $visible = 0
    foreach ($process in $mine) {
        $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        $handle = if ($live) { $live.MainWindowHandle } else { 0 }
        $kind = if ($process.CommandLine -like '*windows_companion.py*') { 'companion' }
                elseif ($process.CommandLine -like '*start-companion.ps1*') { 'launcher' }
                else { 'helper' }
        $window = if ($handle -eq 0) { 'no visible window' } else { "VISIBLE WINDOW (handle $handle)" }
        if ($handle -ne 0) { $visible++ }
        Write-Host ("         {0,-10} pid {1,-7} {2}" -f $kind, $process.ProcessId, $window) -ForegroundColor DarkGray
    }
    if ($visible -eq 0) {
        Write-Ok "running in the background; nothing takes the foreground ($($mine.Count) process(es))"
    }
    else {
        Write-Warn2 "$visible process(es) have a visible window; that should not happen with the hidden task."
    }
}

# ------------------------------------------------------------------ the log
Write-Section "Log: $((Get-Date).ToString('yyyy-MM-dd'))"
$log = Join-Path $script:LogsDir ('companion-{0:yyyy-MM-dd}.log' -f (Get-Date))
if (Test-Path -LiteralPath $log) {
    Get-Content -LiteralPath $log -Tail $LogLines | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    $size = (Get-Item -LiteralPath $log).Length
    Write-Host "         ($log, $([Math]::Round($size / 1KB, 1)) KB)" -ForegroundColor DarkGray
}
else {
    Write-Warn2 "no log for today yet ($log)"
}

Write-Host ''

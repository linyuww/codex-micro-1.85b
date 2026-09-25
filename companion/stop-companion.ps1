#Requires -Version 7.0
<#
.SYNOPSIS
    Stop the background companion without unregistering it.

.DESCRIPTION
    Stops the scheduled task and any process it left behind (the launcher, the
    Python companion, and the short-lived PowerShell bridge it spawns for WinRT).
    The task stays registered, so it starts again at the next logon -- use
    uninstall-autostart.ps1 if that is not what you want.

.PARAMETER TaskName
    Name of the scheduled task to stop.

.EXAMPLE
    .\stop-companion.ps1
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'CodexMicroAllowanceCompanion'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

$stopped = 0

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Ok "scheduled task '$TaskName' stopped"
    $stopped++
}

# Stopping the task does not always reap the whole tree, so sweep for anything
# still holding this folder's scripts.
$cim = Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='python.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue
$mine = $cim | Where-Object {
    $_.CommandLine -and ($_.CommandLine -like '*start-companion.ps1*' -or $_.CommandLine -like '*windows_companion.py*')
}
foreach ($process in $mine) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "  stopped pid $($process.ProcessId) ($($process.Name))" -ForegroundColor DarkGray
    $stopped++
}

if ($stopped -eq 0) {
    Write-Warn2 'nothing was running.'
}
else {
    Write-Host ''
    Write-Host '  The board keeps the last allowance it was told, and renders it as STALE' -ForegroundColor DarkGray
    Write-Host '  after a reboot. Start it again with .\start-companion.cmd.' -ForegroundColor DarkGray
}
Write-Host ''

#Requires -Version 7.0
<#
.SYNOPSIS
    Remove the companion's logon task.

.DESCRIPTION
    Stops the task if it is running and unregisters it. Nothing else is touched:
    logs and configuration stay on disk.

.PARAMETER TaskName
    Name of the scheduled task. Must match what install-autostart.ps1 used.

.EXAMPLE
    .\uninstall-autostart.ps1
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'CodexMicroAllowanceCompanion'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  No scheduled task named '$TaskName' -- nothing to remove." -ForegroundColor DarkGray
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "  Removed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host '  Logs are still under companion\logs.' -ForegroundColor DarkGray

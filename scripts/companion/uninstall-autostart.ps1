#Requires -Version 7.0
<#
.SYNOPSIS
    Remove the companion's logon task.

.DESCRIPTION
    Stops the task if it is running and unregisters it. Nothing else is touched:
    logs and configuration stay on disk.

    A task created from an elevated shell cannot be removed from a normal one --
    its file is owned by BUILTIN\Administrators and your account is left with
    read-only access. This script detects that and prints the exact elevated
    command instead of a bare Access-denied.

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
. (Join-Path $PSScriptRoot '_common.ps1')

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  No scheduled task named '$TaskName' -- nothing to remove." -ForegroundColor DarkGray
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
}
catch {
    $taskFile = Join-Path $env:SystemRoot "System32\Tasks\$TaskName"
    $owner = if (Test-Path -LiteralPath $taskFile) { (Get-Acl -LiteralPath $taskFile).Owner } else { 'unknown' }
    Write-Bad "could not remove '$TaskName': $($_.Exception.Message)"
    Write-Host "         task file owner: $owner" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '         The task was created from an elevated shell, so removing it needs one too.' -ForegroundColor DarkGray
    Write-Host '         Open Windows Terminal as Administrator and run:' -ForegroundColor DarkGray
    Write-Host "           Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor White
    Write-Host ''
    Write-Host '         The companion itself is already stopped; only the logon entry is left.' -ForegroundColor DarkGray
    exit 1
}

Write-Host "  Removed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host '  Logs are still under logs\companion.' -ForegroundColor DarkGray

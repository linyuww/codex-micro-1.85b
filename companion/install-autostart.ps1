#Requires -Version 7.0
<#
.SYNOPSIS
    Register the companion as a scheduled task that starts at logon.

.DESCRIPTION
    Creates (or replaces) a per-user scheduled task that runs
    start-companion.ps1 hidden in the background. It needs no Administrator
    rights: the task runs as you, in your session.

    The task is what turns "remember to run the script" into "the dashboard is
    always current". Without a process holding the GATT link, Windows is allowed
    to power the radio down and the board drops the connection every ~33 s
    (README 6.14).

.PARAMETER TaskName
    Name of the scheduled task. Defaults to "CodexMicroAllowanceCompanion".

.PARAMETER StartNow
    Also start the task immediately, so you do not have to log out and back in.

.EXAMPLE
    .\install-autostart.ps1
    .\install-autostart.ps1 -StartNow
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'CodexMicroAllowanceCompanion',
    [switch] $StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

$pwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
if (-not (Test-Path -LiteralPath $pwsh)) {
    $found = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Bad 'PowerShell 7 is required for the scheduled task but was not found.'
        exit 2
    }
    $pwsh = $found.Source
}

$launcher = Join-Path $PSScriptRoot 'start-companion.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Bad "start-companion.ps1 not found next to this script ($launcher)"
    exit 2
}

$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$settings.MultipleInstances = 'IgnoreNew'

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'Keeps the Codex Micro board allowance in sync over BLE (see companion\README.md).' | Out-Null

Write-Ok "scheduled task '$TaskName' registered for $env:USERDOMAIN\$env:USERNAME at logon"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
    Write-Ok "started; last result $($info.LastTaskResult)"
}

Write-Host ''
Write-Host '  Logs : ' -NoNewline; Write-Host (Join-Path $PSScriptRoot 'logs') -ForegroundColor DarkGray
Write-Host '  Stop : ' -NoNewline; Write-Host 'Stop-ScheduledTask -TaskName ' -NoNewline -ForegroundColor DarkGray; Write-Host $TaskName -ForegroundColor DarkGray
Write-Host '  Undo : ' -NoNewline; Write-Host '.\uninstall-autostart.ps1' -ForegroundColor DarkGray
Write-Host ''

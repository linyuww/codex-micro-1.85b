#Requires -Version 7.0
<#
.SYNOPSIS
    Register the companion as a hidden scheduled task that starts at logon.

.DESCRIPTION
    Why a logon task and not a Windows service: the companion talks to the board
    through WinRT Bluetooth and reads the allowance from your own Codex CLI
    login. Both live in your interactive session. A service runs in session 0,
    where the Bluetooth device is not reachable, so it would start and then sync
    nothing at all.

    The window is hidden (`-WindowStyle Hidden` plus a Hidden task), so it never
    takes the foreground, steals focus, or shows up on the taskbar. Its output
    still lands in companion\logs\.

    The task also starts 30 s after logon. The companion itself already retries
    until the radio is ready, so this is belt-and-braces; it just avoids writing
    a few "board not visible" lines at every boot.

    Replacing an existing task needs the same privilege that created it. If it
    was registered from an elevated shell, a normal one cannot delete it and
    this script says so instead of failing silently.

.PARAMETER TaskName
    Name of the scheduled task. Defaults to "CodexMicroAllowanceCompanion".

.PARAMETER DelaySeconds
    Logon delay before the companion starts. Defaults to 30.

.PARAMETER StartNow
    Also start the task immediately, so you do not have to log out and back in.

.PARAMETER Restart
    Stop a running instance first, then start it again.

.EXAMPLE
    .\install-autostart.ps1
    .\install-autostart.ps1 -StartNow
    .\install-autostart.ps1 -StartNow -Restart
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'CodexMicroAllowanceCompanion',
    [int] $DelaySeconds = 30,
    [switch] $StartNow,
    [switch] $Restart
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

# -WindowStyle Hidden is what keeps it off the foreground; -NoLogo keeps the
# banner out of the log; -NonInteractive guarantees it can never block on input.
$argument = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-WindowStyle Hidden -File `"$launcher`""

$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
if ($DelaySeconds -gt 0) { $trigger.Delay = "PT${DelaySeconds}S" }

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden
$settings.MultipleInstances = 'IgnoreNew'

# -Principal is not optional here: registering without it fails with Access
# denied on this machine (measured). Interactive = run in this user's session,
# which is the only place the Bluetooth device is reachable.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

# ---------------------------------------------------------------- replace it
# Overwriting with -Force is refused (Access denied) when the existing task was
# created from an elevated shell -- and so is unregistering it, because the task
# file's owner is then BUILTIN\Administrators and your account is left with
# read-only access. Detect that and explain it rather than dying.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$canRegister = $true
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  replaced the existing task '$TaskName'" -ForegroundColor DarkGray
    }
    catch {
        $canRegister = $false
        $taskFile = Join-Path $env:SystemRoot "System32\Tasks\$TaskName"
        $owner = if (Test-Path -LiteralPath $taskFile) { (Get-Acl -LiteralPath $taskFile).Owner } else { 'unknown' }
        Write-Warn2 "the existing task '$TaskName' cannot be replaced from this shell ($($_.Exception.Message))"
        Write-Host "         task file owner: $owner" -ForegroundColor DarkGray
        Write-Host '         It was created from an elevated shell, so this account has read-only access to it.' -ForegroundColor DarkGray
        Write-Host ''
        Write-Host '         It still works. Its action already points at this same start-companion.ps1,' -ForegroundColor DarkGray
        Write-Host '         so the board re-discovery and the retry-until-the-radio-is-ready behaviour' -ForegroundColor DarkGray
        Write-Host '         apply to it as well. Only the Hidden flag, the logon delay and restart-on-' -ForegroundColor DarkGray
        Write-Host '         failure are missing.' -ForegroundColor DarkGray
        Write-Host ''
        Write-Host '         To swap in the full definition, run these two lines once from an' -ForegroundColor DarkGray
        Write-Host '         Administrator terminal:' -ForegroundColor DarkGray
        Write-Host "           Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor White
        Write-Host "           & '$PSCommandPath' -StartNow" -ForegroundColor White
        Write-Host ''
    }
}

if ($canRegister) {
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal `
            -Description 'Keeps the Codex Micro board allowance in sync over BLE. Hidden background task; see companion\README.md.' `
            -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Bad "registering '$TaskName' failed: $($_.Exception.Message)"
        exit 3
    }

    $registered = Get-ScheduledTask -TaskName $TaskName
    Write-Ok "task '$TaskName' registered: runs as $env:USERDOMAIN\$env:USERNAME, hidden, ${DelaySeconds}s after logon"
    Write-Host "         hidden=$($registered.Settings.Hidden)  trigger delay=$($registered.Triggers[0].Delay)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- start it
if ($StartNow -or $Restart) {
    # Stop-ScheduledTask does not reap the child tree, so sweep for leftovers
    # before starting: two --watch loops would fight over the same GATT link.
    & (Join-Path $PSScriptRoot 'stop-companion.ps1') -TaskName $TaskName
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 6
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "started; task state $state"
}

Write-Host ''
Write-Host '  Check  : ' -NoNewline; Write-Host '.\status-companion.ps1' -ForegroundColor DarkGray
Write-Host '  Stop   : ' -NoNewline; Write-Host '.\stop-companion.ps1' -ForegroundColor DarkGray
Write-Host '  Undo   : ' -NoNewline; Write-Host '.\uninstall-autostart.ps1' -ForegroundColor DarkGray
Write-Host '  Logs   : ' -NoNewline; Write-Host (Join-Path $PSScriptRoot 'logs') -ForegroundColor DarkGray
Write-Host ''

#Requires -Version 7.0
<#
.SYNOPSIS
    Repair the allowance link, in the order the README prescribes.

.DESCRIPTION
    Each step is opt-in, so running this with no switches only reports state.
    Steps, cheapest first:

        -ApplyPower      stop Windows powering the radio/board down (needs admin)
        -RestorePower    undo -ApplyPower
        -ToggleRadio     power-cycle the Bluetooth radio; fixes a wedged host stack
        -RepairPairing   unpair/re-pair the board to clear a stale bond

    Not included on purpose: erasing the board's NVS bond region. It is
    destructive and needs a COM port, so it stays a documented manual step --
    see debug/porting-log.md 6.13 and scripts/companion/README.md.

.PARAMETER ApplyPower
    Write the power-policy registry values. Requires an elevated shell.

.PARAMETER RestorePower
    Undo -ApplyPower.

.PARAMETER ToggleRadio
    Turn the Bluetooth radio off, wait, and turn it back on. Every Bluetooth
    device on this machine disconnects for a few seconds.

.PARAMETER RepairPairing
    Ask Windows to unpair and pair the board again.

.EXAMPLE
    .\repair-link.ps1
    .\repair-link.ps1 -ApplyPower          # from an Administrator terminal
    .\repair-link.ps1 -ToggleRadio
#>
[CmdletBinding()]
param(
    [switch] $ApplyPower,
    [switch] $RestorePower,
    [switch] $ToggleRadio,
    [switch] $RepairPairing
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

$config = Get-CompanionConfig
$python = Resolve-Python -Configured $config.PythonPath
if (-not $python) {
    Write-Bad 'No Python interpreter found; set PythonPath in scripts\companion\config.psd1.'
    exit 2
}

$powerScript = Join-Path $script:ToolsDir 'bt_power_repair.py'
$radioScript = Join-Path $script:ToolsDir 'bt_radio_toggle.py'
$address = Get-BoardAddress -Configured $config.DeviceAddress -Python $python

Write-Host ''
Write-Host '  Codex Micro link repair' -ForegroundColor Cyan
Write-Host "    elevated : $(Test-Elevated)"
Write-Host "    board    : $(if ($address) { $address } else { 'not found' })"

$didSomething = $false

# ---------------------------------------------------------------- power policy
Write-Section 'Power policy (debug/porting-log.md 6.14)'
if (Test-Path -LiteralPath $powerScript) {
    & $python $powerScript 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

    if ($ApplyPower -or $RestorePower) {
        if (-not (Test-Elevated)) {
            Write-Bad 'this step needs an elevated shell. Open Windows Terminal as Administrator and re-run:'
            Write-Host "    & '$PSCommandPath' $(if ($ApplyPower) { '-ApplyPower' } else { '-RestorePower' })"
        }
        else {
            $flag = if ($ApplyPower) { '--apply' } else { '--restore' }
            & $python $powerScript $flag 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            if ($LASTEXITCODE -eq 0) { Write-Ok "power policy updated ($flag)" } else { Write-Bad "bt_power_repair.py $flag failed" }
            $didSomething = $true
        }
    }
    else {
        Write-Host '    (pass -ApplyPower from an Administrator shell to fix this)' -ForegroundColor DarkGray
    }
}
else {
    Write-Warn2 "skipped: $powerScript not found"
}

# ---------------------------------------------------------------- radio toggle
Write-Section 'Bluetooth radio (debug/porting-log.md 6.16: wedged host stack)'
if ($ToggleRadio) {
    if (-not (Test-Path -LiteralPath $radioScript)) {
        Write-Bad "skipped: $radioScript not found"
    }
    else {
        Write-Warn2 'every Bluetooth device on this machine will disconnect for a few seconds.'
        & $python $radioScript 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $didSomething = $true
    }
}
else {
    Write-Host '    (pass -ToggleRadio to power-cycle it; no admin needed)' -ForegroundColor DarkGray
    if (Test-Path -LiteralPath $radioScript) {
        & $python $radioScript --status 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
}

# ---------------------------------------------------------------- pairing
Write-Section 'Host-side bond (debug/porting-log.md 6.16)'
if ($RepairPairing) {
    if (-not $address) {
        Write-Bad 'cannot repair pairing: the board is not enumerated. Pair it from Settings instead.'
    }
    else {
        & $python $script:CompanionScript --device-address $address --repair-pairing -v 2>&1 |
            ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $didSomething = $true
    }
}
else {
    Write-Host '    (pass -RepairPairing to unpair and re-pair the board)' -ForegroundColor DarkGray
    Write-Host '    Note: PairAsync() returns Failed from a plain console process, so this can only' -ForegroundColor DarkGray
    Write-Host '    repair a record that already exists. For a first-time pair use Settings.' -ForegroundColor DarkGray
}

Write-Section 'Next'
if ($didSomething) {
    Write-Host '    Run .\diagnose.ps1 to confirm, then .\start-companion.cmd.' -ForegroundColor DarkGray
}
else {
    Write-Host '    Nothing was changed. Pick a step above, or start with .\diagnose.ps1.' -ForegroundColor DarkGray
}
Write-Host ''

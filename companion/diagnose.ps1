#Requires -Version 7.0
<#
.SYNOPSIS
    Read-only health check of the whole allowance path.

.DESCRIPTION
    Walks the same layers the README's troubleshooting order uses, cheapest
    first, and prints a verdict per layer:

        1. can this machine read the Codex allowance at all?   (no Bluetooth)
        2. does Windows see the board?                         (BLE enumeration)
        3. does the board expose the quota service?            (GATT discovery)
        4. is the radio allowed to stay awake?                 (power policy)

    Nothing here writes to the board or changes system settings.

.EXAMPLE
    .\diagnose.ps1
#>
[CmdletBinding()]
param(
    [int] $HoldSeconds = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

$config = Get-CompanionConfig
$python = Resolve-Python -Configured $config.PythonPath
if (-not $python) {
    Write-Bad 'No Python interpreter found; set PythonPath in companion\config.psd1.'
    exit 2
}

Write-Host ''
Write-Host '  Codex Micro companion diagnostics (read-only)' -ForegroundColor Cyan
Write-Host "    python : $python"
Write-Host "    repo   : $script:RepoRoot"

$problems = [System.Collections.Generic.List[string]]::new()

# ---------------------------------------------------------------- layer 1
Write-Section '1. Can this machine read the Codex allowance?'
$json = & $python $script:CompanionScript --json-only -v 2>&1
$json | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
$snapshot = $json | Where-Object { $_ -match '^\{.*"five_hour_remaining_percent".*"weekly_remaining_percent".*\}$' } | Select-Object -Last 1
if ($snapshot) {
    Write-Ok "allowance read: $snapshot"
}
else {
    Write-Bad 'the Codex CLI did not return both 5-hour and weekly allowances'
    $problems.Add('allowance read failed -- check that the codex CLI is logged in (set CodexPath in config.psd1 if it is not on PATH)')
}

# ---------------------------------------------------------------- layer 2
Write-Section '2. Does Windows see the board over BLE?'
$address = Get-BoardAddress -Configured $config.DeviceAddress -Python $python
if ($address) {
    Write-Ok "board found: $address"
}
else {
    Write-Bad 'no BLE device named "Codex Micro" is known to Windows'
    $problems.Add('board not enumerated -- pair it: Settings -> Bluetooth & devices -> Add device -> "Codex Micro"')
}

# ---------------------------------------------------------------- layer 3
Write-Section '3. Does the board expose the quota service?'
if ($address) {
    $probe = & $python $script:CompanionScript --device-address $address `
        --probe-only --hold-seconds $HoldSeconds -v 2>&1
    $probe | Where-Object { $_ -match '^\s*device=' } | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    $summary = $probe | Where-Object { $_ -match '^\s*device=' } | Select-Object -Last 1
    if ($summary -match 'service_discovery=Success' -and $summary -match 'quota_characteristic=yes') {
        Write-Ok 'quota service and characteristic discovered; the link held for the whole probe'
    }
    else {
        Write-Bad "GATT layer is not healthy: $summary"
        $problems.Add('GATT discovery failed -- see README 6.11 (Windows GATT cache) and 6.16 (stale bond)')
    }
}
else {
    Write-Warn2 'skipped: no board address'
}

# ---------------------------------------------------------------- layer 4
Write-Section '4. May Windows power the Bluetooth radio and the board down?'
$powerScript = Join-Path $script:ToolsDir 'bt_power_repair.py'
if (Test-Path -LiteralPath $powerScript) {
    $power = & $python $powerScript 2>&1
    $power | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    if ($power -match 'MAY BE POWERED DOWN') {
        Write-Warn2 'something can still be powered down while idle (see the list above)'
        $problems.Add('power policy: run repair-link.ps1 as Administrator to keep the listed nodes powered')
    }
    else {
        Write-Ok 'every matched node is kept powered'
    }
}
else {
    Write-Warn2 "skipped: $powerScript not found"
}

$radioScript = Join-Path $script:ToolsDir 'bt_radio_toggle.py'
if (Test-Path -LiteralPath $radioScript) {
    $radio = & $python $radioScript --status 2>&1
    $radio | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

$hidScript = Join-Path $script:ToolsDir 'hid_caps.py'
if (Test-Path -LiteralPath $hidScript) {
    Write-Section '5. Does Windows expose the board HID interface to the desktop app?'
    $hid = & $python $hidScript 2>&1
    $hid | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    if ($hid -match 'no HID interface') {
        Write-Warn2 'the desktop app has no HID node to open (README 6.16: stale host-side record)'
    }
}

# ---------------------------------------------------------------- verdict
Write-Section 'Verdict'
if ($problems.Count -eq 0) {
    Write-Ok 'the allowance path is healthy end to end.'
    Write-Host '    Start .\start-companion.cmd to keep it that way.' -ForegroundColor DarkGray
}
else {
    for ($i = 0; $i -lt $problems.Count; $i++) {
        Write-Host "  $($i + 1). $($problems[$i])" -ForegroundColor Yellow
    }
}
Write-Host ''
exit $(if ($problems.Count -eq 0) { 0 } else { 1 })

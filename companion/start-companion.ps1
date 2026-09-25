#Requires -Version 7.0
<#
.SYNOPSIS
    Keep the Codex Micro board's allowance in sync, continuously.

.DESCRIPTION
    Runs windows_companion.py --watch in the foreground and restarts it if it
    ever exits, so the board never sits idle long enough for Windows to power
    the Bluetooth radio down.

    Why "continuously" matters: with no GATT client attached, Windows is allowed
    to drop the radio's power, the board sees no traffic for its 32 s
    supervision timeout and tears the link down, and the cycle repeats. A
    companion that holds the link is the no-admin fix (README 6.14).

.PARAMETER Once
    Read and write the allowance exactly once, then exit. Useful as a test.

.PARAMETER Probe
    Discover the quota service and hold the link; never writes. Read-only.

.PARAMETER Interval
    Override the refresh interval from config.psd1.

.PARAMETER NoRestart
    Exit when the companion exits instead of restarting it.

.EXAMPLE
    .\start-companion.ps1
    .\start-companion.ps1 -Once
    .\start-companion.ps1 -Probe
#>
[CmdletBinding()]
param(
    [switch] $Once,
    [switch] $Probe,
    [int] $Interval = 0,
    [switch] $NoRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

if (-not (Test-Path -LiteralPath $script:CompanionScript)) {
    Write-Bad "windows_companion.py not found at $script:CompanionScript"
    Write-Host '  Run this script from inside the repository checkout.'
    exit 2
}

$config = Get-CompanionConfig
$python = Resolve-Python -Configured $config.PythonPath
if (-not $python) {
    Write-Bad 'No Python interpreter found.'
    Write-Host '  Install Python 3.10+ or set PythonPath in companion\config.psd1.'
    exit 2
}

$address = Get-BoardAddress -Configured $config.DeviceAddress -Python $python
if (-not $address) {
    Write-Bad 'The board is not visible to Windows over BLE.'
    Write-Host ''
    Write-Host '  Do this first:'
    Write-Host '    1. Power the board on and wait for its dashboard.'
    Write-Host '    2. Windows Settings -> Bluetooth & devices -> Add device'
    Write-Host '       -> pick "Codex Micro".'
    Write-Host '    3. Re-run this script.'
    Write-Host ''
    Write-Host '  If it is already paired, run .\diagnose.ps1 to see which layer is stuck.'
    exit 3
}

if ($Interval -le 0) {
    $Interval = if ($config.IntervalSeconds) { [int] $config.IntervalSeconds } else { 60 }
}
if ($Interval -lt 10) {
    Write-Warn2 "interval $Interval is below the 10 s floor; using 10."
    $Interval = 10
}

$arguments = @($script:CompanionScript, '--device-address', $address, '-v')
if ($Probe) {
    $arguments += @('--probe-only', '--hold-seconds', '120')
}
elseif ($Once) {
    $arguments += '--once'
}
else {
    $arguments += @('--watch', '--interval', "$Interval")
}
if ($config.WriteAttempts) { $arguments += @('--write-attempts', "$($config.WriteAttempts)") }
if ($config.WriteTimeoutMs) { $arguments += @('--write-timeout-ms', "$($config.WriteTimeoutMs)") }
if ($config.CodexPath) { $arguments += @('--codex-path', $config.CodexPath) }

$logDirectory = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logFile = Join-Path $logDirectory ('companion-{0:yyyy-MM-dd}.log' -f (Get-Date))

Write-Host ''
Write-Host '  Codex Micro allowance companion' -ForegroundColor Cyan
Write-Host "    python  : $python"
Write-Host "    board   : $address"
Write-Host "    mode    : $(if ($Probe) { 'probe only' } elseif ($Once) { 'single write' } else { "watch, every ${Interval}s" })"
Write-Host "    log     : $logFile"
Write-Host ''
Write-Host '  Press Ctrl+C to stop.' -ForegroundColor DarkGray
Write-Host ''

$attempt = 0
while ($true) {
    $attempt++
    $started = Get-Date
    "$($started.ToString('yyyy-MM-dd HH:mm:ss'))  run #$attempt  $python $($arguments -join ' ')" |
        Out-File -LiteralPath $logFile -Append -Encoding utf8

    & $python @arguments 2>&1 | Tee-Object -FilePath $logFile -Append
    $code = $LASTEXITCODE
    $ran = ((Get-Date) - $started).TotalSeconds

    "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  run #$attempt exited code=$code after $([int] $ran)s" |
        Out-File -LiteralPath $logFile -Append -Encoding utf8

    if ($Once -or $Probe -or $NoRestart) { exit $code }

    # A healthy --watch run is long; a short one means the link or the Codex CLI
    # is unhappy, so back off instead of hammering it.
    $backoff = if ($ran -lt 30) { [Math]::Min(15 * $attempt, 60) } else { 5 }
    Write-Warn2 "companion exited (code $code) after $([int] $ran)s; restarting in ${backoff}s"
    Start-Sleep -Seconds $backoff
}

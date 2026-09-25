#!/usr/bin/env python3
"""Dump every BLE device Windows knows about, with the raw DeviceInformation id."""
import base64
import json
import os
import subprocess
import sys

PS = r"""
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await-Op {
    param($Operation, [Type] $ResultType, [int] $TimeoutMs = 20000)
    $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait($TimeoutMs)) { throw 'timeout' }
    return $task.Result
}
[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
$selector = [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelector()
$infos = Await-Op ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection]) 20000
foreach ($info in $infos) {
    $name = [string] $info.Name
    if ([string]::IsNullOrEmpty($name)) { continue }
    $paired = $false
    try { $paired = [bool] $info.Pairing.IsPaired } catch { }
    $canPair = $false
    try { $canPair = [bool] $info.Pairing.CanPair } catch { }
    $addr = ''
    try {
        $dev = Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromIdAsync([string]$info.Id)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 8000
        if ($dev -ne $null) { $addr = $dev.BluetoothAddress.ToString('X12') }
    } catch { }
    $conn = 'n/a'
    try {
        if ($dev -ne $null) { $conn = [string] $dev.ConnectionStatus }
    } catch { }
    [Console]::Out.WriteLine('@@CX@@' + (@{ name = $name; id = [string] $info.Id; paired = $paired; canPair = $canPair; addr = $addr; conn = $conn } | ConvertTo-Json -Compress))
}
"""


def main() -> int:
    assert PS.isascii()
    command = base64.b64encode(PS.encode("utf-16-le")).decode("ascii")
    powershell = os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows",
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", command],
        capture_output=True, timeout=180, check=False,
    )
    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("@@CX@@"):
            print(json.dumps(json.loads(line[6:]), ensure_ascii=False))
    err = (completed.stderr or b"").decode("utf-8", "replace").strip()
    if err:
        print("STDERR:", err[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())

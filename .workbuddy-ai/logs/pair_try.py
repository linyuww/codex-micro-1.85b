#!/usr/bin/env python3
"""Try several WinRT pairing strategies against one BLE address.

The companion's --repair-pairing uses PairAsync(ConfirmOnly) and it returns
Failed from a console process. This probe walks the other documented
combinations so we can tell "Windows refuses" apart from "we called it wrong".
"""

from __future__ import annotations

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
    param($Operation, [Type] $ResultType, [int] $TimeoutMs = 30000)
    $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait($TimeoutMs)) { throw 'timeout' }
    return $task.Result
}

function Emit {
    param($Row)
    [Console]::Out.WriteLine('@@CX@@' + ($Row | ConvertTo-Json -Compress -Depth 6))
}

[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingKinds, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingProtectionLevel, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingResult, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

$address = [uint64] __ADDRESS__
$device = Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($address)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 20000
if ($null -eq $device) { Emit @{ step = 'resolve'; status = 'not_found' }; exit 0 }
$pairing = $device.DeviceInformation.Pairing
Emit @{ step = 'device'; paired = [bool] $pairing.IsPaired; can_pair = [bool] $pairing.CanPair; conn = [string] $device.ConnectionStatus }

$kinds = [Windows.Devices.Enumeration.DevicePairingKinds]
$levels = [Windows.Devices.Enumeration.DevicePairingProtectionLevel]

$attempts = @(
    @{ name = 'ConfirmOnly/Default';  kind = $kinds::ConfirmOnly; level = $levels::Default },
    @{ name = 'ConfirmOnly/Encrypt';  kind = $kinds::ConfirmOnly; level = $levels::Encryption },
    @{ name = 'None/Default';         kind = $kinds::None;        level = $levels::Default },
    @{ name = 'ProvidePin/Default';   kind = $kinds::ProvidePin;  level = $levels::Default }
)

foreach ($a in $attempts) {
    $pairing = $device.DeviceInformation.Pairing
    if ([bool] $pairing.IsPaired) {
        Emit @{ step = 'skip'; name = $a.name; why = 'already paired' }
        continue
    }
    try {
        $r = Await-Op ($pairing.PairAsync($a.kind, $a.level)) ([Windows.Devices.Enumeration.DevicePairingResult]) 45000
        Emit @{ step = 'pair'; name = $a.name; status = [string] $r.Status; protection = [string] $r.ProtectionLevelUsed }
    } catch {
        Emit @{ step = 'pair'; name = $a.name; status = 'exception'; message = $_.Exception.Message }
    }
    Start-Sleep -Seconds 3
    if ([bool] $device.DeviceInformation.Pairing.IsPaired) { break }
}

Emit @{ step = 'final'; paired = [bool] $device.DeviceInformation.Pairing.IsPaired; conn = [string] $device.ConnectionStatus }
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: pair_try.py aa:bb:cc:dd:ee:ff")
        return 2
    octets = sys.argv[1].split(":")
    octets.reverse()
    value = int("".join(octets), 16)

    source = PS.replace("__ADDRESS__", str(value))
    assert source.isascii()
    command = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    powershell = os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows",
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", command],
        capture_output=True, timeout=400, check=False,
    )
    out = (completed.stdout or b"").decode("utf-8", "replace")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("@@CX@@"):
            print(json.dumps(json.loads(line[6:]), ensure_ascii=False))
    err = (completed.stderr or b"").decode("utf-8", "replace").strip()
    if err and "@@CX@@" not in out:
        print("STDERR:", err[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

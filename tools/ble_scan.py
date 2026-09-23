#!/usr/bin/env python3
"""One-off WinRT BLE advertisement scan: find the board's advertised address.

The companion deliberately never scans -- it addresses one bound board by
address. But a board whose bond generation was just bumped is unknown to
Windows, so there is nothing to address yet, and `FromBluetoothAddressAsync`
returns null for an address Windows has never seen. This helper is the one
place a scan is legitimate: it only reports what is on the air, it pairs
nothing, and it writes nothing.

    python tools/ble_scan.py [seconds]
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile

# Kept short enough for -EncodedCommand (a .ps1 is not worth the ceremony for
# a throwaway diagnostic). ASCII only, as everywhere else in this project.
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

# Enumerate rather than watch. A BluetoothLEAdvertisementWatcher needs a WinRT
# delegate, and PowerShell's scriptblock-to-delegate coercion for WinRT events
# silently never fires, so the watcher reports nothing even while the board is
# clearly advertising. FindAllAsync returns every BLE device Windows already
# knows -- paired or merely seen -- which is exactly the set we can act on.
$selector = [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelector()
$infos = Await-Op ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection]) 20000

foreach ($info in $infos) {
    $name = [string] $info.Name
    if ([string]::IsNullOrEmpty($name)) { continue }
    $id = [string] $info.Id
    $address = ''
    if ($id -match '([0-9a-fA-F]{12})$') { $address = $Matches[1].ToUpper() }
    $paired = $false
    try { $paired = [bool] $info.Pairing.IsPaired } catch { }
    [Console]::Out.WriteLine('@@CX@@' + (@{ address = $address; name = $name; paired = $paired; id = $id } | ConvertTo-Json -Compress))
}
"""


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    source = PS.replace("__SECONDS__", str(seconds))
    assert source.isascii(), "generated PowerShell must stay ASCII"

    command = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    if len(command) > 32000:
        sys.stderr.write("scan script grew past the CreateProcess limit\n")
        return 2

    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    powershell = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", command],
        capture_output=True,
        timeout=seconds + 60,
        check=False,
    )

    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    found = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("@@CX@@"):
            continue
        try:
            found.append(json.loads(line[len("@@CX@@"):]))
        except ValueError:
            continue

    if not found:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        sys.stderr.write(f"no named advertisements seen (rc={completed.returncode})\n")
        if stderr:
            sys.stderr.write(re.sub(r"<[^>]+>", "", stderr)[:500] + "\n")
        return 1

    for item in sorted(found, key=lambda e: e.get("name") or ""):
        address = item.get("address") or ""
        pretty = ":".join(address[i:i + 2] for i in range(0, 12, 2)) if len(address) == 12 else address
        print(f"{item.get('name')}\t{pretty}\trssi={item.get('rssi')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

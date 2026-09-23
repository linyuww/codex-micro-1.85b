#!/usr/bin/env python3
"""Read-only dump of the Windows-side state for the Codex Micro BLE link.

Answers the questions a serial log cannot:
  * which PnP nodes exist for the board's BLE address, and in what state
  * whether Windows believes the board is paired
  * whether a BLE bond (LTK) record exists in the Bluetooth registry

Changes nothing.  Some registry paths are only readable from an elevated
shell; the script reports that as a gap instead of failing.

    python outputs/ble_host_diag.py [address]
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys

DEFAULT_ADDRESS = "28:84:85:B2:1C:79"

PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Emit {
    param($Row)
    [Console]::Out.WriteLine('@@CX@@' + ($Row | ConvertTo-Json -Compress -Depth 5))
}

$addr = '__ADDRESS__'                    # aa:bb:cc:dd:ee:ff
$compact = $addr.Replace(':', '').ToUpper()

# --- 1. PnP nodes that mention the address --------------------------------
foreach ($d in (Get-PnpDevice | Where-Object { $_.InstanceId -like ('*' + $compact + '*') })) {
    Emit @{
        src = 'pnp-addr'
        status = [string] $d.Status
        cls = [string] $d.Class
        name = [string] $d.FriendlyName
        id = [string] $d.InstanceId
    }
}

# --- 2. Every BLE enumerator / HID-over-GATT node -------------------------
foreach ($d in (Get-PnpDevice | Where-Object {
        $_.InstanceId -like 'BTHLE*' -or $_.InstanceId -like 'BTHLEDEVICE*' })) {
    Emit @{
        src = 'pnp-ble'
        status = [string] $d.Status
        cls = [string] $d.Class
        name = [string] $d.FriendlyName
        id = [string] $d.InstanceId
    }
}

# --- 3. Anything named like the board -------------------------------------
foreach ($d in (Get-PnpDevice | Where-Object { $_.FriendlyName -like '*Codex*' })) {
    Emit @{
        src = 'pnp-name'
        status = [string] $d.Status
        cls = [string] $d.Class
        name = [string] $d.FriendlyName
        id = [string] $d.InstanceId
    }
}

# --- 4. WinRT view: paired? connected? ------------------------------------
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await-Op {
    param($Operation, [Type] $ResultType, [int] $TimeoutMs = 15000)
    $task = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait($TimeoutMs)) { throw 'timeout' }
    return $task.Result
}
[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]

$octets = $addr.Split(':')
[array]::Reverse($octets)
$value = [uint64] ('0x' + ($octets -join ''))
try {
    $dev = Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($value)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 15000
    if ($null -eq $dev) {
        Emit @{ src = 'winrt'; note = 'no BluetoothLEDevice for this address' }
    } else {
        Emit @{
            src = 'winrt'
            name = [string] $dev.Name
            device_id = [string] $dev.DeviceId
            connection_status = [string] $dev.ConnectionStatus
            paired = [bool] $dev.DeviceInformation.Pairing.IsPaired
            can_pair = [bool] $dev.DeviceInformation.Pairing.CanPair
        }
    }
} catch {
    Emit @{ src = 'winrt'; note = ('query failed: ' + $_.Exception.Message) }
}

# --- 5. Bluetooth registry: bond records ----------------------------------
$keysRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys'
try {
    $adapters = Get-ChildItem $keysRoot -ErrorAction Stop
    Emit @{ src = 'reg'; note = ('Keys subkeys: ' + (($adapters | ForEach-Object { $_.PSChildName }) -join ', ')) }
    foreach ($a in $adapters) {
        $devs = Get-ChildItem $a.PSPath -ErrorAction SilentlyContinue
        Emit @{ src = 'reg-bond'; adapter = $a.PSChildName; count = @($devs).Count }
        foreach ($d in $devs) {
            if ($d.PSChildName.ToUpper() -eq $compact) {
                $vals = (Get-Item $d.PSPath).GetValueNames()
                Emit @{ src = 'reg-bond-hit'; adapter = $a.PSChildName; device = $d.PSChildName; values = ($vals -join ',') }
            }
        }
    }
} catch {
    Emit @{ src = 'reg'; note = ('Keys unreadable (needs SYSTEM/admin): ' + $_.Exception.Message) }
}

$devsRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices'
try {
    $list = Get-ChildItem $devsRoot -ErrorAction Stop
    Emit @{ src = 'reg-devices'; count = @($list).Count; names = (($list | ForEach-Object { $_.PSChildName }) -join ',') }
} catch {
    Emit @{ src = 'reg-devices'; note = ('unreadable: ' + $_.Exception.Message) }
}

Emit @{ src = 'done' }
"""


def _run(source: str, timeout: int = 120) -> str:
    assert source.isascii(), "generated PowerShell must stay ASCII"
    command = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    powershell = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", command],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
    if not stdout.strip() and stderr:
        sys.stderr.write(re.sub(r"<[^>]+>", "", stderr)[:1500] + "\n")
    return stdout


def main() -> int:
    address = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDRESS
    rows = []
    for line in _run(PS.replace("__ADDRESS__", address)).splitlines():
        line = line.strip()
        if line.startswith("@@CX@@"):
            try:
                rows.append(json.loads(line[len("@@CX@@"):]))
            except ValueError:
                pass

    if not rows:
        print("no rows returned from PowerShell")
        return 1

    for row in rows:
        src = row.get("src")
        if src == "pnp-addr":
            print(f"[addr ] {row.get('status'):<10} {row.get('cls'):<12} {row.get('name')}  {row.get('id')}")
        elif src == "pnp-ble":
            print(f"[ble  ] {row.get('status'):<10} {row.get('cls'):<12} {row.get('name')}  {row.get('id')}")
        elif src == "pnp-name":
            print(f"[name ] {row.get('status'):<10} {row.get('cls'):<12} {row.get('name')}  {row.get('id')}")
        elif src == "winrt":
            if row.get("note"):
                print(f"[winrt] {row['note']}")
            else:
                print(f"[winrt] name={row.get('name')} paired={row.get('paired')} "
                      f"can_pair={row.get('can_pair')} conn={row.get('connection_status')}")
                print(f"[winrt] device_id={row.get('device_id')}")
        elif src == "reg":
            print(f"[reg  ] {row.get('note')}")
        elif src == "reg-devices":
            print(f"[reg  ] Devices entries={row.get('count')} {row.get('note') or ''}")
        elif src == "reg-bond":
            print(f"[bond ] adapter {row.get('adapter')}: {row.get('count')} device key(s)")
        elif src == "reg-bond-hit":
            print(f"[BOND!] adapter {row.get('adapter')} device {row.get('device')} values={row.get('values')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Turn the Bluetooth radio off and back on.

This is the standard remedy for a wedged host stack: after a long run of
connect/disconnect churn, Windows can stop establishing BLE links altogether --
`BluetoothLEDevice.FromBluetoothAddressAsync` still resolves the device and
`Pairing.UnpairAsync` still succeeds, but every `PairAsync` returns `Failed`
with `connection_status = Disconnected`, and a plain GATT session never becomes
Active. Restarting the radio clears that state.

The radio is toggled through the WinRT Radio API, which is the same switch the
Settings app exposes, so it needs no elevation. It does drop every active
Bluetooth link for a few seconds (headsets, mice), which is why this is a
separate, explicit tool and never something the companion does on its own.

    python tools/bt_radio_toggle.py            # off, wait, on
    python tools/bt_radio_toggle.py --status   # report only, change nothing
"""

from __future__ import annotations

import base64
import json
import os
import re
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

[void][Windows.Devices.Radios.Radio, Windows.Devices.Radios, ContentType = WindowsRuntime]
[void][Windows.Devices.Radios.RadioState, Windows.Devices.Radios, ContentType = WindowsRuntime]
[void][Windows.Devices.Radios.RadioAccessStatus, Windows.Devices.Radios, ContentType = WindowsRuntime]

$radioType = [System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]
$radios = Await-Op ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) $radioType 20000

$bt = $null
foreach ($r in $radios) {
    [Console]::Out.WriteLine('@@CX@@' + (@{
        event = 'radio'; name = [string] $r.Name
        kind = [string] $r.Kind; state = [string] $r.State } | ConvertTo-Json -Compress))
    if ([string] $r.Kind -eq 'Bluetooth') { $bt = $r }
}

if ($null -eq $bt) {
    [Console]::Out.WriteLine('@@CX@@' + (@{ event = 'error'; code = 'no_radio' } | ConvertTo-Json -Compress))
    exit 10
}

if ($env:CX_TOGGLE -ne '1') { exit 0 }

$off = Await-Op ($bt.SetStateAsync([Windows.Devices.Radios.RadioState]::Off)) ([Windows.Devices.Radios.RadioAccessStatus]) 20000
[Console]::Out.WriteLine('@@CX@@' + (@{ event = 'set_off'; status = [string] $off } | ConvertTo-Json -Compress))
Start-Sleep -Seconds 4

$on = Await-Op ($bt.SetStateAsync([Windows.Devices.Radios.RadioState]::On)) ([Windows.Devices.Radios.RadioAccessStatus]) 20000
[Console]::Out.WriteLine('@@CX@@' + (@{ event = 'set_on'; status = [string] $on } | ConvertTo-Json -Compress))
Start-Sleep -Seconds 3

[Console]::Out.WriteLine('@@CX@@' + (@{
    event = 'done'; state = [string] $bt.State } | ConvertTo-Json -Compress))
"""


def run_ps(source: str, timeout: int) -> tuple[int, list[dict]]:
    assert source.isascii(), "generated PowerShell must stay ASCII"
    command = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    if len(command) > 32000:
        raise SystemExit("script grew past the CreateProcess limit")
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
        env={**os.environ, "CX_TOGGLE": os.environ.get("CX_TOGGLE", "0")},
    )
    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("@@CX@@"):
            continue
        try:
            events.append(json.loads(line[len("@@CX@@"):]))
        except ValueError:
            continue
    if completed.returncode not in (0,) and not events:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        sys.stderr.write(re.sub(r"<[^>]+>", "", stderr)[:400] + "\n")
    return completed.returncode, events


def main() -> int:
    status_only = "--status" in sys.argv
    if not status_only:
        os.environ["CX_TOGGLE"] = "1"

    code, events = run_ps(PS, timeout=90)

    for event in events:
        if event.get("event") == "radio":
            print(f"radio: {event.get('name')} kind={event.get('kind')} "
                  f"state={event.get('state')}")
        elif event.get("event") in ("set_off", "set_on"):
            print(f"{event['event']}: {event.get('status')}")
        elif event.get("event") == "done":
            print(f"radio is now {event.get('state')}")
        elif event.get("event") == "error":
            print(f"error: {event.get('code')}")
            return 1

    if not events:
        print(f"no radio events (rc={code})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

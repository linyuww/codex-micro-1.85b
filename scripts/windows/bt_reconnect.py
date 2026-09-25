#!/usr/bin/env python3
"""Nudge Windows into reconnecting to the Codex Micro board.

Why this exists: after the board reboots, Windows very often does *not*
reconnect, even though it still owns the device -- the PnP nodes survive
(`BTHLEDEVICE\\{00001812-...}` and `HID\\{00001812-...}` are both still there),
so nothing changes at the HID topology level, and the desktop app only rescans
when that topology changes. Result: the board advertises for minutes, the app
stays silent, and the dashboard shows BLE ONLY / OFFLINE until Windows happens
to retry on its own schedule (measured: 10-20 minutes).

Opening a `BluetoothLEDevice` and asking for its GATT services is what forces
the reconnect: it makes Windows bring the ACL link up, which lets the HOGP
driver bind, which is the topology change the desktop app is waiting for. This
tool then drops its own session immediately so the board is free again for the
host that actually wants it.

This is a host-side action, not a firmware change, and it is safe to run
repeatedly. It never pairs or unpairs anything.

Usage:

    python scripts/windows/bt_reconnect.py --address 28:84:85:B2:1C:73
    python scripts/windows/bt_reconnect.py --address 28:84:85:B2:1C:73 --loop --interval 30

`--loop` keeps nudging until the board is connected (or the timeout expires),
which is the useful form if you want the device to come back on its own.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

EVENT_PREFIX = "@@CX@@"

PS_TEMPLATE = r"""
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

function Emit-Event {
    param([hashtable] $Payload)
    [Console]::Out.WriteLine('@@CX@@' + ($Payload | ConvertTo-Json -Compress -Depth 6))
}

[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
[void][Windows.Devices.Bluetooth.GenericAttributeProfile.GattDeviceServicesResult, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]

$addressUint = [uint64] __ADDRESS_UINT__
$holdMs = __HOLD_MS__
$loop = __LOOP__
$intervalMs = __INTERVAL_MS__
$budgetMs = __BUDGET_MS__

try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $attempt = 0
    while ($true) {
        $attempt = $attempt + 1
        $device = $null
        try {
            $device = [Windows.Devices.Bluetooth.BluetoothLEDevice] (Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($addressUint)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 20000)
        } catch { }

        if ($null -eq $device) {
            Emit-Event @{ event = 'error'; code = 'device_not_found'; message = 'Windows has no BluetoothLEDevice for this address; pair it in Settings first' }
            exit 12
        }

        $before = [string] $device.ConnectionStatus
        Emit-Event @{
            event = 'probe'
            attempt = $attempt
            name = [string] $device.Name
            paired = [bool] $device.DeviceInformation.Pairing.IsPaired
            connection_status = $before
        }

        if ($before -eq 'Connected') {
            Emit-Event @{ event = 'done'; connected = $true; attempts = $attempt }
            $device.Dispose()
            exit 0
        }

        # The nudge: asking for services is what makes Windows bring the link up.
        # Uncached, so it also throws away any stale GATT database.
        $status = 'unknown'
        $count = -1
        try {
            $services = Await-Op ($device.GetGattServicesAsync([Windows.Devices.Bluetooth.BluetoothCacheMode]::Uncached)) ([Windows.Devices.Bluetooth.GenericAttributeProfile.GattDeviceServicesResult]) 30000
            $status = [string] $services.Status
            $count = $services.Services.Count
        } catch {
            $status = 'Exception: ' + $_.Exception.Message
        }
        Start-Sleep -Milliseconds $holdMs
        $after = [string] $device.ConnectionStatus
        Emit-Event @{
            event = 'nudge'
            attempt = $attempt
            services_status = $status
            services_count = $count
            connection_status = $after
        }
        $device.Dispose()

        if ($after -eq 'Connected') {
            Emit-Event @{ event = 'done'; connected = $true; attempts = $attempt }
            exit 0
        }
        if (-not $loop) {
            Emit-Event @{ event = 'done'; connected = $false; attempts = $attempt }
            exit 1
        }
        if ($sw.ElapsedMilliseconds -ge $budgetMs) {
            Emit-Event @{ event = 'done'; connected = $false; attempts = $attempt; reason = 'timeout' }
            exit 1
        }
        Start-Sleep -Milliseconds $intervalMs
    }
} catch {
    Emit-Event @{ event = 'error'; code = 'exception'; message = $_.Exception.Message }
    exit 11
}
"""


class ReconnectError(RuntimeError):
    pass


def address_to_uint64(address: str) -> int:
    digits = re.sub(r"[^0-9a-fA-F]", "", address)
    if len(digits) != 12:
        raise ReconnectError(f"{address!r} is not a 12-digit Bluetooth address")
    return int(digits, 16)


def find_powershell() -> str:
    """Windows PowerShell 5.1 only -- the WinRT AsTask bridge needs it."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    found = shutil.which("powershell.exe")
    if found:
        return found
    raise ReconnectError("cannot find powershell.exe; the WinRT bridge needs it")


def build_script(address: str, hold_ms: int, loop: bool, interval_ms: int,
                 budget_ms: int) -> str:
    source = PS_TEMPLATE
    source = source.replace("__ADDRESS_UINT__", str(address_to_uint64(address)))
    source = source.replace("__HOLD_MS__", str(hold_ms))
    source = source.replace("__LOOP__", "$true" if loop else "$false")
    source = source.replace("__INTERVAL_MS__", str(interval_ms))
    source = source.replace("__BUDGET_MS__", str(budget_ms))
    if not source.isascii():
        raise ReconnectError("the PowerShell source must stay pure ASCII")
    return source


def run_ps(source: str, timeout: float) -> tuple[list[dict], str]:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".ps1", delete=False, encoding="ascii", newline="\r\n"
    )
    try:
        handle.write(source)
        handle.close()
        completed = subprocess.run(
            [
                find_powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                handle.name,
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    stderr = (completed.stderr or b"").decode("utf-8", "replace")
    events: list[dict] = []
    for line in stdout.splitlines():
        if not line.startswith(EVENT_PREFIX):
            continue
        try:
            events.append(json.loads(line[len(EVENT_PREFIX) :]))
        except json.JSONDecodeError:
            continue
    return events, stderr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nudge Windows into reconnecting to the Codex Micro board."
    )
    parser.add_argument("--address", required=True, help="board BLE address")
    parser.add_argument(
        "--hold-ms",
        type=int,
        default=2500,
        help="how long to keep the nudge session before dropping it (default 2500)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="keep nudging until the board is connected or the budget runs out",
    )
    parser.add_argument(
        "--interval", type=int, default=30, help="seconds between nudges (default 30)"
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=300,
        help="seconds to keep trying in --loop mode (default 300)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    options = parser.parse_args()

    source = build_script(
        options.address,
        max(500, options.hold_ms),
        options.loop,
        max(1000, options.interval * 1000),
        max(1000, options.budget * 1000),
    )

    timeout = (options.budget if options.loop else 60) + 90
    try:
        events, stderr = run_ps(source, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("bt_reconnect: the PowerShell bridge did not finish in time",
              file=sys.stderr)
        return 1

    if options.verbose:
        for event in events:
            print(f"[reconnect] {json.dumps(event, ensure_ascii=False)}",
                  file=sys.stderr)

    if not events:
        print("bt_reconnect: the PowerShell bridge produced no events",
              file=sys.stderr)
        if stderr.strip():
            print(stderr.strip()[:2000], file=sys.stderr)
        return 1

    for probe in (e for e in events if e.get("event") == "probe"):
        print(
            "attempt {attempt}: name={name} paired={paired} link={status}".format(
                attempt=probe.get("attempt"),
                name=probe.get("name"),
                paired=probe.get("paired"),
                status=probe.get("connection_status"),
            )
        )
    for nudge in (e for e in events if e.get("event") == "nudge"):
        print(
            "  nudge: services={services_status} count={count} link={status}".format(
                services_status=nudge.get("services_status"),
                count=nudge.get("services_count"),
                status=nudge.get("connection_status"),
            )
        )

    error = next((e for e in events if e.get("event") == "error"), None)
    if error is not None:
        print(f"bt_reconnect failed: {error.get('message')}", file=sys.stderr)
        return 1

    done = next((e for e in events if e.get("event") == "done"), None)
    connected = bool(done and done.get("connected"))
    if connected:
        print("board is connected; the desktop app should pick it up now")
        return 0

    print(
        "board is still not connected after {attempts} nudge(s); if it keeps "
        "failing, reset the host stack with scripts/windows/bt_radio_toggle.py".format(
            attempts=(done or {}).get("attempts")
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

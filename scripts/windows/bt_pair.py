#!/usr/bin/env python3
"""Discover and pair the Codex Micro board from the command line.

Why this exists: `DeviceInformation.Pairing.PairAsync()` is the obvious API and
it is the one `windows_companion.py --repair-pairing` uses, but it needs a
`BluetoothLEDevice`, and `BluetoothLEDevice.FromBluetoothAddressAsync()` returns
null for an address Windows has no cached record of. So for a board Windows has
forgotten -- which is exactly the state after the board's bonds are cleared --
there is nothing to call PairAsync on.

The two facts that make this tool possible, both measured on Windows 11 24H2:

  * `DeviceInformation.FindAllAsync(BluetoothLEDevice.GetDeviceSelectorFromPairingState(false))`
    performs a real advertisement scan and returns devices Windows has never
    seen (~30 s per call; it is a scan, not a cache read). Plain
    `GetDeviceSelector()` does not scan and returns only cached devices.
  * `DeviceInformationCustomPairing.PairAsync()` with a `PairingRequested`
    handler can complete a Just Works pairing from a console process. The plain
    `PairAsync()` overload needs a caller that can show the system confirmation
    UI, so it is only tried first as the cheap path.

PowerShell 5.1 is required, not PowerShell 7: the WinRT `AsTask` bridge lives in
`System.Runtime.WindowsRuntime`, which .NET Core cannot reflect over
("Operation is not supported on this platform", 0x80131539). And WinRT events
cannot be subscribed with `Register-ObjectEvent` there either -- the handler has
to be attached with the generated `add_*` method.

Usage:

    python scripts/windows/bt_pair.py --list
    python scripts/windows/bt_pair.py --address 28:84:85:B2:1C:73 --unpair
    python scripts/windows/bt_pair.py --name "Codex Micro"

Nothing is paired or unpaired unless asked; --list only scans.
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

function Get-RemoteAddress {
    param([string] $Id)
    $m = [regex]::Matches($Id, '((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})')
    if ($m.Count -gt 0) { return $m[$m.Count - 1].Value.ToUpper() }
    return ''
}

[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingKinds, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingProtectionLevel, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DevicePairingResult, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Enumeration.DeviceUnpairingResult, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

$wantAddress = '__ADDRESS__'
$wantName = '__NAME__'
$scanAttempts = __SCAN_ATTEMPTS__
$shouldPair = __PAIR__
$shouldUnpair = __UNPAIR__
$addressUint = [uint64] __ADDRESS_UINT__

try {
    # --- 1. active scan ----------------------------------------------------
    $selector = [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelectorFromPairingState($false)
    $target = $null
    $attempt = 0
    while (($attempt -lt $scanAttempts) -and ($null -eq $target)) {
        $attempt = $attempt + 1
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $found = Await-Op ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection]) 120000
        $rows = @()
        foreach ($d in $found) {
            $addr = Get-RemoteAddress ([string] $d.Id)
            $name = ''
            try { $name = [string] $d.Name } catch { }
            $rows += ('{0}|{1}' -f $name, $addr)
            if ($null -eq $target) {
                if ($wantAddress -and $addr -eq $wantAddress) { $target = $d }
                elseif ($wantName -and $name -eq $wantName) { $target = $d }
            }
        }
        Emit-Event @{ event = 'scan'; attempt = $attempt; count = $found.Count; elapsed_ms = $sw.ElapsedMilliseconds; devices = $rows }
        if ($null -eq $target) { Start-Sleep -Seconds 2 }
    }

    if ($null -eq $target) {
        Emit-Event @{ event = 'error'; code = 'not_found'; message = 'the board did not appear in the scan' }
        exit 12
    }

    $info = $target
    $pairing = $info.Pairing
    Emit-Event @{
        event = 'found'
        name = [string] $info.Name
        id = [string] $info.Id
        paired = [bool] $pairing.IsPaired
        can_pair = [bool] $pairing.CanPair
    }

    # --- 2. unpair (optional) ---------------------------------------------
    if ($shouldUnpair -and $pairing.IsPaired) {
        $u = Await-Op ($pairing.UnpairAsync()) ([Windows.Devices.Enumeration.DeviceUnpairingResult]) 30000
        Emit-Event @{ event = 'unpair'; status = [string] $u.Status }
        Start-Sleep -Seconds 2
        $again = $null
        try {
            $again = [Windows.Devices.Bluetooth.BluetoothLEDevice] (Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($addressUint)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 20000)
        } catch { }
        if ($null -eq $again) {
            Emit-Event @{ event = 'done'; paired = $false; note = 'unpaired; Windows no longer enumerates the board' }
            exit 0
        }
        $pairing = $again.DeviceInformation.Pairing
    }

    if (-not $shouldPair) {
        Emit-Event @{ event = 'done'; paired = [bool] $pairing.IsPaired }
        exit 0
    }

    # --- 3. pairing --------------------------------------------------------
    # Attempt 1 is the plain overload: cheapest, and it is what works when the
    # caller happens to be allowed to raise the system confirmation UI.
    $simple = $null
    try {
        $simple = Await-Op ($pairing.PairAsync([Windows.Devices.Enumeration.DevicePairingKinds]::ConfirmOnly)) ([Windows.Devices.Enumeration.DevicePairingResult]) 90000
        Emit-Event @{ event = 'pair_simple'; status = [string] $simple.Status; protection = [string] $simple.ProtectionLevelUsed }
    } catch {
        Emit-Event @{ event = 'pair_simple'; status = 'Exception'; message = $_.Exception.Message }
    }

    if ($null -eq $simple -or [string] $simple.Status -ne 'Paired') {
        # Attempt 2 answers the pairing request in-process, which is what makes
        # this work from a console. WinRT events cannot be subscribed with
        # Register-ObjectEvent here, so the generated add_* method is used.
        $script:pairKind = ''
        $script:pairAccepted = $false
        $script:pairError = ''
        $handler = {
            param($sender, $e)
            $script:pairKind = [string] $e.PairingKind
            try {
                $script:pairAccepted = $true
                $e.Accept()
            } catch {
                $script:pairError = $_.Exception.Message
            }
        }

        $custom = $pairing.Custom
        $custom.add_PairingRequested($handler)
        $kinds = [Windows.Devices.Enumeration.DevicePairingKinds]::ConfirmOnly -bor
                 [Windows.Devices.Enumeration.DevicePairingKinds]::ProvidePin -bor
                 [Windows.Devices.Enumeration.DevicePairingKinds]::ConfirmPinMatch
        $custom_result = $null
        try {
            $custom_result = Await-Op ($custom.PairAsync($kinds, [Windows.Devices.Enumeration.DevicePairingProtectionLevel]::Default)) ([Windows.Devices.Enumeration.DevicePairingResult]) 90000
            Emit-Event @{
                event = 'pair_custom'
                status = [string] $custom_result.Status
                protection = [string] $custom_result.ProtectionLevelUsed
                requested_kind = $script:pairKind
                accepted = [bool] $script:pairAccepted
                handler_error = $script:pairError
            }
        } catch {
            Emit-Event @{ event = 'pair_custom'; status = 'Exception'; message = $_.Exception.Message }
        } finally {
            try { $custom.remove_PairingRequested($handler) } catch { }
        }
    }

    # --- 4. re-read the result --------------------------------------------
    Start-Sleep -Seconds 3
    $device = $null
    try {
        $device = [Windows.Devices.Bluetooth.BluetoothLEDevice] (Await-Op ([Windows.Devices.Bluetooth.BluetoothLEDevice]::FromBluetoothAddressAsync($addressUint)) ([Windows.Devices.Bluetooth.BluetoothLEDevice]) 20000)
    } catch { }
    if ($null -ne $device) {
        Emit-Event @{
            event = 'done'
            name = [string] $device.Name
            paired = [bool] $device.DeviceInformation.Pairing.IsPaired
            connection_status = [string] $device.ConnectionStatus
        }
        $device.Dispose()
    } else {
        Emit-Event @{ event = 'done'; paired = $null }
    }
    exit 0
} catch {
    Emit-Event @{ event = 'error'; code = 'exception'; message = $_.Exception.Message }
    exit 11
}
"""


class PairError(RuntimeError):
    pass


def address_to_uint64(address: str) -> int:
    """'28:84:85:B2:1C:73' -> 0x288485B21C73 (the WinRT BluetoothAddress form)."""
    digits = re.sub(r"[^0-9a-fA-F]", "", address)
    if len(digits) != 12:
        raise PairError(f"{address!r} is not a 12-digit Bluetooth address")
    return int(digits, 16)


def normalise_address(address: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", address).upper()
    if len(digits) != 12:
        raise PairError(f"{address!r} is not a 12-digit Bluetooth address")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def find_powershell() -> str:
    """Windows PowerShell 5.1 only -- see the module docstring."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    found = shutil.which("powershell.exe")
    if found:
        return found
    raise PairError("cannot find powershell.exe; the WinRT bridge needs it")


def build_script(
    address: str | None,
    name: str | None,
    scan_attempts: int,
    pair: bool,
    unpair: bool,
) -> str:
    address_upper = normalise_address(address) if address else ""
    address_uint = str(address_to_uint64(address)) if address else "0"
    source = PS_TEMPLATE
    source = source.replace("__ADDRESS__", address_upper)
    source = source.replace("__ADDRESS_UINT__", address_uint)
    source = source.replace("__NAME__", (name or "").replace("'", ""))
    source = source.replace("__SCAN_ATTEMPTS__", str(scan_attempts))
    source = source.replace("__PAIR__", "$true" if pair else "$false")
    source = source.replace("__UNPAIR__", "$true" if unpair else "$false")
    if not source.isascii():
        raise PairError("the PowerShell source must stay pure ASCII")
    return source


def run_ps(source: str, timeout: float) -> tuple[list[dict], str, int]:
    """Stage the script as ASCII .ps1, run it, and collect @@CX@@ events."""
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
    return events, stderr, completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover the Codex Micro board with a real BLE scan and pair it "
            "from the command line."
        )
    )
    parser.add_argument("--address", default=None, help="board BLE address")
    parser.add_argument("--name", default="Codex Micro", help="board name")
    parser.add_argument(
        "--scan-attempts",
        type=int,
        default=3,
        help="scan passes to run; each takes about 30 s (default 3)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="only scan and list what was discovered; pair nothing",
    )
    parser.add_argument(
        "--unpair",
        action="store_true",
        help="remove the host-side half of the bond before pairing",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    options = parser.parse_args()

    if not options.address and not options.name:
        parser.error("give --address or --name")

    pair = not options.list
    source = build_script(
        options.address,
        options.name,
        max(1, options.scan_attempts),
        pair=pair,
        unpair=options.unpair,
    )

    # Each scan pass can take 120 s; pairing adds up to 2 x 90 s plus slack.
    timeout = options.scan_attempts * 130 + 30 + (220 if pair else 0)
    try:
        events, stderr, _ = run_ps(source, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("bt_pair: the PowerShell bridge did not finish in time", file=sys.stderr)
        return 1

    if options.verbose:
        for event in events:
            print(f"[pair] {json.dumps(event, ensure_ascii=False)}", file=sys.stderr)

    if not events:
        print("bt_pair: the PowerShell bridge produced no events", file=sys.stderr)
        if stderr.strip():
            print(stderr.strip()[:2000], file=sys.stderr)
        return 1

    for scan in (e for e in events if e.get("event") == "scan"):
        print(
            "scan {attempt}: {count} device(s) in {elapsed_ms}ms".format(
                attempt=scan.get("attempt"),
                count=scan.get("count"),
                elapsed_ms=scan.get("elapsed_ms"),
            )
        )
        for row in scan.get("devices") or []:
            print(f"  {row}")

    error = next((e for e in events if e.get("event") == "error"), None)
    if error is not None:
        print(f"bt_pair failed: {error.get('message')}", file=sys.stderr)
        return 1

    found = next((e for e in events if e.get("event") == "found"), None)
    if found is None:
        print("bt_pair: the board was not discovered", file=sys.stderr)
        return 1

    print(
        "device={name} was_paired={paired} can_pair={can_pair}".format(
            name=found.get("name"),
            paired=found.get("paired"),
            can_pair=found.get("can_pair"),
        )
    )

    if options.list:
        return 0

    for name in ("pair_simple", "pair_custom"):
        attempt = next((e for e in events if e.get("event") == name), None)
        if attempt is None:
            continue
        print(
            "{name}: status={status} protection={protection} kind={kind} "
            "accepted={accepted} error={error}".format(
                name=name,
                status=attempt.get("status"),
                protection=attempt.get("protection"),
                kind=attempt.get("requested_kind"),
                accepted=attempt.get("accepted"),
                error=attempt.get("handler_error") or attempt.get("message") or "",
            )
        )

    done = next((e for e in events if e.get("event") == "done"), None)
    if done is not None:
        print(
            "paired_now={paired} link={link} note={note}".format(
                paired=done.get("paired"),
                link=done.get("connection_status"),
                note=done.get("note") or "",
            )
        )

    if done is None or done.get("paired") is not True:
        print(
            "bt_pair: Windows did not report a fresh pairing. Open "
            "Settings > Bluetooth and add the board there instead.",
            file=sys.stderr,
        )
        return 1

    print(
        "pairing complete; if the Codex UI still ignores the device, reopen the "
        "ChatGPT desktop app so it re-binds the HID interface"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

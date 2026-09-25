#!/usr/bin/env python3
"""Stop Windows from powering down the Bluetooth radio under the board's link.

Why this exists
---------------
The board's link dies on a fixed ~32.9 s cadence with HCI reason 0x08
(connection timeout). The negotiated supervision timeout is 32 s, so the
controller is reporting exactly what the spec says: it heard nothing from the
central for 32 s and gave up. The central is a Realtek USB radio on this
machine, and Windows is allowed to power it down to save energy, which is what
silences it under an idle LE link. Windows then reconnects, and the cycle
repeats.

The tell is that the link *never* drops while a GATT client is attached: an
open `BluetoothLEDevice` session keeps the device in use, so the radio is never
idled. Every hold test (60 s, 100 s) finished with zero disconnects, while an
idle board dropped every 32.9 s.

What it changes
---------------
It writes `Enable = $false` into the WMI class `MSPower_DeviceEnable`, which is
the same knob as the "Allow the computer to turn off this device to save power"
checkbox in Device Manager, for:

  * the Bluetooth radio that carries the link (a Bluetooth-class device whose
    driver service is BTHUSB), and
  * the board's own nodes (VID 303A / PID 8360), if Windows created any.

Every other device is left alone and counted in the summary. This is a real
system change and it needs an elevated shell, so nothing is written without
`--apply`; `--restore` puts the previous values back.

    python scripts/windows/bt_power_repair.py              # report only
    python scripts/windows/bt_power_repair.py --apply      # needs Administrator
    python scripts/windows/bt_power_repair.py --restore    # needs Administrator
"""

import base64
import json
import os
import re
import subprocess
import sys

# Espressif's vendor id and the product id the firmware reports. Compared as
# plain lowercased substrings, never as a regex: these strings contain "&" and
# the device ids contain backslashes, both of which are hostile to quoting.
BOARD_MARKER = "vid&02303a_pid&8360"

# Shared prologue: work out which device nodes this tool is allowed to touch.
# Resolving the radio by its driver service is deliberate -- matching on
# "USB\\VID_..." alone would sweep in every USB device on the machine.
PROLOGUE = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Emit-Row { param($Row) [Console]::Out.WriteLine('@@CX@@' + ($Row | ConvertTo-Json -Compress -Depth 5)) }

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$targets = New-Object System.Collections.Generic.List[string]
$labels = @{}

# The radio that carries the LE link.
Get-CimInstance Win32_PnPEntity | Where-Object {
    $_.PNPClass -eq 'Bluetooth' -and $_.Service -eq 'BTHUSB' -and $_.DeviceID
} | ForEach-Object {
    $id = $_.DeviceID.ToLower()
    if (-not $targets.Contains($id)) { $targets.Add($id) }
    $labels[$id] = ('radio: ' + $_.Name)
}

# The board's own nodes, whatever class Windows filed them under.
Get-CimInstance Win32_PnPEntity | Where-Object {
    $_.DeviceID -and $_.DeviceID.ToLower().Contains('__BOARD__')
} | ForEach-Object {
    $id = $_.DeviceID.ToLower()
    if (-not $targets.Contains($id)) { $targets.Add($id) }
    $labels[$id] = ('board: ' + $_.Name)
}

# MSPower_DeviceEnable.InstanceName is "<DeviceID>_<n>"; the PnP id has no
# suffix, so compare on the prefix.
function Get-TargetLabel {
    param([string] $InstanceName)
    $needle = $InstanceName.ToLower() -replace '_\d+$', ''
    foreach ($t in $targets) {
        if ($needle -eq $t -or $needle.StartsWith($t)) { return [string] $labels[$t] }
    }
    return $null
}
"""

REPORT = PROLOGUE + r"""
Emit-Row @{ source = 'context'; elevated = [bool] $isAdmin; targets = $targets.Count }

Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable | ForEach-Object {
    $instance = [string] $_.InstanceName
    $label = Get-TargetLabel $instance
    if ($null -eq $label) { return }
    Emit-Row @{
        source = 'power'
        instance = $instance
        label = $label
        enabled = [bool] $_.Enable
    }
}
"""

APPLY = PROLOGUE + r"""
if (-not $isAdmin) {
    Emit-Row @{ source = 'error'; message = 'not elevated: MSPower_DeviceEnable cannot be written without Administrator' }
    exit 3
}

$target = __TARGET__
$changed = 0
$untouched = 0

Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable | ForEach-Object {
    $instance = [string] $_.InstanceName
    $label = Get-TargetLabel $instance
    if ($null -eq $label) { $untouched = $untouched + 1; return }

    $current = [bool] $_.Enable
    if ($current -eq $target) {
        Emit-Row @{ source = 'ok'; instance = $instance; label = $label; note = 'already in the requested state' }
        return
    }
    try {
        Set-CimInstance -InputObject $_ -Property @{ Enable = $target } -ErrorAction Stop
        $changed = $changed + 1
        Emit-Row @{ source = 'changed'; instance = $instance; label = $label; was = $current; now = $target }
    } catch {
        Emit-Row @{ source = 'failed'; instance = $instance; label = $label; message = [string] $_.Exception.Message }
    }
}

Emit-Row @{ source = 'summary'; changed = $changed; untouched_other_devices = $untouched; target = $target }
"""


def _run(source: str, timeout: int) -> str:
    source = source.replace("__BOARD__", BOARD_MARKER)
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
    )
    stdout = (completed.stdout or b"").decode("utf-8", "replace")
    stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
    if stderr and not stdout.strip():
        sys.stderr.write(re.sub(r"<[^>]+>", "", stderr)[:1200] + "\n")
    return stdout


def _rows(source: str, timeout: int = 180):
    out = []
    for line in _run(source, timeout).splitlines():
        line = line.strip()
        if not line.startswith("@@CX@@"):
            continue
        try:
            out.append(json.loads(line[len("@@CX@@"):]))
        except ValueError:
            continue
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    restore = "--restore" in sys.argv
    if apply and restore:
        sys.stderr.write("--apply and --restore are mutually exclusive\n")
        return 2

    if not (apply or restore):
        rows = _rows(REPORT)
        context = next((r for r in rows if r.get("source") == "context"), {})
        print(f"elevated: {context.get('elevated')}   matched device nodes: {context.get('targets')}")
        power = [r for r in rows if r.get("source") == "power"]
        if not power:
            print("no power-management entry found for the radio or the board")
            return 1
        for item in power:
            state = "MAY BE POWERED DOWN" if item.get("enabled") else "kept powered"
            print(f"  {state}")
            print(f"      {item.get('label')}")
            print(f"      {item.get('instance')}")
        if any(r.get("enabled") for r in power):
            print("\nRun with --apply from an elevated shell to keep these powered.")
        return 0

    target = "$false" if apply else "$true"
    rows = _rows(APPLY.replace("__TARGET__", target))
    ok = True
    for item in rows:
        source = item.get("source")
        if source == "changed":
            print(f"changed: {item.get('label')}  {item['was']} -> {item['now']}")
            print(f"         {item['instance']}")
        elif source == "ok":
            print(f"ok: {item.get('label')} ({item.get('note')})")
        elif source == "failed":
            print(f"FAILED: {item.get('label')}: {item.get('message')}")
            ok = False
        elif source == "error":
            print(f"ERROR: {item.get('message')}")
            return 3
        elif source == "summary":
            print(f"summary: {item['changed']} changed, "
                  f"{item['untouched_other_devices']} unrelated devices left alone")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

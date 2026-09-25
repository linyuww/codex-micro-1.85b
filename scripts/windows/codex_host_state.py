#!/usr/bin/env python3
"""Inspect the host side of the Codex Micro link on Windows.

Diagnostic only: it reads process and device state and changes nothing.

It answers the questions a serial log cannot:

  * Is the desktop app that speaks the HID channel actually running, and what
    did it launch with? `codex-ornament-bridge.exe` is the process that owns
    the device link, so its command line says which device it expects.
  * Which Bluetooth radio is in the machine, and is Windows allowed to power
    it down to save energy? A radio that is switched off under an idle LE link
    is a classic cause of a link that dies on the supervision timeout and then
    reconnects forever.

    python scripts/windows/codex_host_state.py [process-filter]
"""

import base64
import json
import os
import re
import subprocess
import sys

PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Emit-Row {
    param($Row)
    [Console]::Out.WriteLine('@@CX@@' + ($Row | ConvertTo-Json -Compress -Depth 4))
}

# --- Processes that make up the Codex / ChatGPT desktop app ----------------
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '__FILTER__' } |
    ForEach-Object {
        Emit-Row @{
            source = 'process'
            name = $_.Name
            pid = $_.ProcessId
            path = $_.ExecutablePath
            cmd = $_.CommandLine
        }
    }

# --- Bluetooth radios and their power-management policy -------------------
Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable |
    Where-Object { $_.InstanceName -match 'BTH|BLUETOOTH|USB\\VID' } |
    ForEach-Object {
        Emit-Row @{
            source = 'radio-power'
            instance = $_.InstanceName
            enabled = $_.Enable
        }
    }

Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.PNPClass -eq 'Bluetooth' } |
    ForEach-Object {
        Emit-Row @{
            source = 'bt-device'
            name = $_.Name
            status = $_.Status
            deviceid = $_.DeviceID
            service = $_.Service
        }
    }

# --- Every device node that belongs to the board -------------------------
#
# Windows builds a chain for a bonded HOGP device: BTHLE\DEV_<addr> (the LE
# enumerator), BTHLEDEVICE\{<service-uuid>}_..._<addr> for each GATT service it
# enumerated, and -- only once the HID service is usable -- a HIDClass node
# that applications can actually open. A board that has the first two but no
# HIDClass node is paired and discoverable yet cannot drive anything, which is
# exactly the "connected but the Codex UI does not react" symptom. Printing the
# whole chain makes the missing link obvious instead of inferred.
Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.DeviceID -and $_.DeviceID.ToLower().Contains('__BOARD__') } |
    ForEach-Object {
        Emit-Row @{
            source = 'board-node'
            name = $_.Name
            pnpclass = $_.PNPClass
            status = $_.Status
            service = $_.Service
            deviceid = $_.DeviceID
        }
    }

# --- HIDClass devices, to see whether the board has a usable HID node ----
Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.PNPClass -eq 'HIDClass' } |
    ForEach-Object {
        Emit-Row @{
            source = 'hid-class'
            name = $_.Name
            status = $_.Status
            deviceid = $_.DeviceID
        }
    }
"""

# The board's own nodes are identified by its BLE address, not by VID/PID: the
# address is what Windows puts in every node of the chain.
BOARD_MARKER = "288485b21c79"


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
    if not stdout.strip() and stderr:
        sys.stderr.write(re.sub(r"<[^>]+>", "", stderr)[:1200] + "\n")
    return stdout


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "ornament|chatgpt|codex"
    rows = []
    for line in _run(PS.replace("__FILTER__", pattern), 120).splitlines():
        line = line.strip()
        if not line.startswith("@@CX@@"):
            continue
        try:
            rows.append(json.loads(line[len("@@CX@@"):]))
        except ValueError:
            continue

    if not rows:
        sys.stderr.write("no rows returned\n")
        return 1

    processes = [r for r in rows if r.get("source") == "process"]
    print(f"=== matching processes ({len(processes)}) ===")
    for item in processes:
        print(f"  {item.get('name')} (pid {item.get('pid')})")
        if item.get("path"):
            print(f"      path: {item['path']}")
        if item.get("cmd"):
            print(f"      cmd : {item['cmd'][:300]}")

    radios = [r for r in rows if r.get("source") == "bt-device"]
    if radios:
        print(f"\n=== Bluetooth PnP devices ({len(radios)}) ===")
        for item in radios:
            print(f"  {item.get('name')}  status={item.get('status')}  service={item.get('service')}")
            print(f"      {item.get('deviceid')}")

    power = [r for r in rows if r.get("source") == "radio-power"]
    if power:
        print(f"\n=== radio power-management policy ({len(power)}) ===")
        for item in power:
            state = "ENABLED (radio may be powered down)" if item.get("enabled") else "disabled (radio stays on)"
            print(f"  {state}")
            print(f"      {item.get('instance')}")

    nodes = [r for r in rows if r.get("source") == "board-node"]
    print(f"\n=== device nodes for the board ({len(nodes)}) ===")
    if not nodes:
        print("  none -- Windows has no record of this board at all")
    for item in nodes:
        print(f"  [{item.get('pnpclass')}] {item.get('name')}  status={item.get('status')}")
        print(f"      service={item.get('service')}")
        print(f"      {item.get('deviceid')}")
    has_hid_class = any((r.get("pnpclass") or "") == "HIDClass" for r in nodes)
    print(f"  -> HIDClass node present: {has_hid_class}")
    if nodes and not has_hid_class:
        print("     No HIDClass node means Windows never finished bringing up the")
        print("     HID interface, so no application can open it. A bonded board")
        print("     with BTHLEDEVICE nodes but no HIDClass node is exactly the")
        print("     'connected but the Codex UI does not react' state.")

    hid = [r for r in rows if r.get("source") == "hid-class"]
    print(f"\n=== HIDClass devices on this machine ({len(hid)}) ===")
    for item in hid:
        print(f"  {item.get('name')}  status={item.get('status')}")
        print(f"      {item.get('deviceid')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Detect -- and repair -- the one BLE failure the firmware cannot fix.

Why this exists
---------------

"Bluetooth is connected but Codex shows limited functionality" has (at least)
four different causes, and only one of them lives in the firmware. The most
confusing one is *partial GATT enumeration*: Windows brings the ACL link up,
encrypts it, reads a handful of attributes -- and then stops. It never reads the
HID service, so `BthLEEnum` never creates the HID-over-GATT child node, so
`node-hid` in Codex Desktop has nothing to open, so `CodexMicroService` stays
completely silent (not "connected but broken" -- it never sees a device at all).

Measured on 2026-09-25, board advertising 28:84:85:B2:1C:75:

    board serial   : reads handle 68 (quota), 63/64/65 (battery) -- nothing else
    PnP tree       : only BTHLE\\DEV_288485B21C75, zero BTHLEDEVICE\\{...} children
    hid_caps.py    : "no HID interface with VID_303A&PID_8360 is present"
    desktop log    : zero CodexMicro lines

Nothing in the firmware can change this: the board is advertising, bonding and
serving the full attribute table the whole time. What fixes it is forcing the
host to throw its enumeration state away -- toggling the Bluetooth radio does
exactly that, and the very next connection reads handles 42..68 (the whole
database, HID included) and the desktop app completes its
`v.oai.rgbcfg -> v.oai.thstatus -> device.status` handshake.

So: **if this tool says "partial enumeration", stop looking at the firmware.**
Re-flashing cannot help, which is exactly why a reflash "made no difference".

Usage
-----

    python scripts/windows/ble_enum_guard.py              # report only, changes nothing
    python scripts/windows/ble_enum_guard.py --fix        # toggle the radio and re-check

`--fix` drops every active Bluetooth link for a few seconds (headset, mouse),
which is why it is opt-in. Exit code is 0 when the host is healthy (or was
repaired), 1 otherwise -- usable as a pre-flight check after flashing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

BOARD_NAME = "Codex Micro"
HOGP_UUID = "{00001812-0000-1000-8000-00805F9B34FB}"

PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

function Emit {
    param($Row)
    [Console]::Out.WriteLine('@@CX@@' + ($Row | ConvertTo-Json -Compress -Depth 5))
}

$want = '__NAME__'
$devices = @(Get-PnpDevice)

# One BTHLE\DEV_<addr> node per BLE address Windows has ever met.
foreach ($d in ($devices | Where-Object {
        $_.InstanceId -like 'BTHLE\DEV_*' -and $_.FriendlyName -eq $want })) {
    Emit @{ src = 'board'; status = [string] $d.Status; id = [string] $d.InstanceId }
}

# BthLEEnum creates one child per GATT service it finished enumerating.
foreach ($d in ($devices | Where-Object { $_.InstanceId -like 'BTHLEDEVICE\*' })) {
    Emit @{ src = 'child'; status = [string] $d.Status; cls = [string] $d.Class;
            name = [string] $d.FriendlyName; id = [string] $d.InstanceId }
}

# The interface node-hid opens. Its absence is what silences Codex Desktop.
foreach ($d in ($devices | Where-Object { $_.InstanceId -like 'HID\*' })) {
    Emit @{ src = 'hid'; status = [string] $d.Status; name = [string] $d.FriendlyName;
            id = [string] $d.InstanceId }
}

Emit @{ src = 'done' }
"""


def _run(source: str, timeout: int = 180) -> str:
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


_ADDR_IN_ID = re.compile(r"_([0-9A-F]{12})(?:\\|$)")


def _address_of(instance_id: str) -> str | None:
    """Pull the trailing board address out of a BTHLEDEVICE/HID instance id.

    Both forms embed it as `..._<12 hex>` (upper case) just before the last
    backslash, e.g.
    `BTHLEDEVICE\\{00001812-...}_DEV_VID&02303A_PID&8360_REV&0101_288485B21C75\\...`
    """
    match = _ADDR_IN_ID.search(instance_id.upper())
    return match.group(1) if match else None


def _uuid_of(instance_id: str) -> str | None:
    match = re.match(r"[^\\]*\\?\{([0-9A-Fa-f-]{36})\}", instance_id)
    return "{" + match.group(1).upper() + "}" if match else None


def snapshot() -> dict:
    """Return {address: {'board': id, 'children': [...], 'hid': [...]}}."""
    rows = []
    for line in _run(PS.replace("__NAME__", BOARD_NAME)).splitlines():
        line = line.strip()
        if line.startswith("@@CX@@"):
            try:
                rows.append(json.loads(line[len("@@CX@@"):]))
            except ValueError:
                pass

    found: dict[str, dict] = {}
    for row in rows:
        src = row.get("src")
        instance_id = row.get("id") or ""
        if src == "board":
            compact = instance_id.split("\\")[1] if "\\" in instance_id else ""
            key = compact.replace("DEV_", "") or "UNKNOWN"
            found.setdefault(key, {"board": instance_id, "children": [], "hid": []})
        elif src in ("child", "hid"):
            address = _address_of(instance_id)
            if not address:
                continue
            key = "children" if src == "child" else "hid"
            entry = found.setdefault(address, {"board": None, "children": [], "hid": []})
            entry[key].append({
                "uuid": _uuid_of(instance_id),
                "name": row.get("name") or "",
                "status": row.get("status") or "",
            })
    return found


def _verdict(entry: dict) -> str:
    if not entry.get("board"):
        return "unknown"
    hogp = any(c["uuid"] == HOGP_UUID for c in entry["children"])
    interface = any(h["uuid"] == HOGP_UUID for h in entry["hid"])
    if hogp and interface:
        return "healthy"
    if not entry["children"]:
        return "never-enumerated"
    return "partial"


def report(found: dict) -> int:
    # Only addresses Windows has a BTHLE\DEV node for can be the board; the
    # children/HID of unrelated BLE devices carry their own address and would
    # otherwise show up as noise.
    found = {a: e for a, e in found.items() if e.get("board")}
    if not found:
        print("no BLE enumerator node named %r -- Windows has never met this board"
              % BOARD_NAME)
        print("  next step: pair it (scripts/windows/bt_pair.py --list, then Settings ->"
              " Bluetooth -> Add device)")
        return 1

    worst = 0
    for address, entry in sorted(found.items()):
        verdict = _verdict(entry)
        children = entry["children"]
        print(f"[{address}] {verdict}")
        print(f"  BTHLE\\DEV node        : {'present' if entry['board'] else 'MISSING'}")
        print(f"  BTHLEDEVICE children  : {len(children)}"
              + ("  <- no GATT enumeration ever finished" if not children else ""))
        for child in children:
            print(f"    {child['uuid']}  {child['status']:<10} {child['name']}")
        print(f"  HID interface nodes   : {len(entry['hid'])}"
              + ("  <- Codex Desktop has nothing to open" if not entry['hid'] else ""))
        if verdict == "healthy":
            print("  verdict               : OK, the desktop app should be talking to it")
        elif verdict == "partial":
            print("  verdict               : PARTIAL ENUMERATION (host side, not firmware)")
        else:
            print("  verdict               : NEVER ENUMERATED (host side, not firmware)")
        if verdict != "healthy":
            worst = 1
    return worst


def fix() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    toggle = os.path.join(here, "bt_radio_toggle.py")
    print(f"--- toggling the Bluetooth radio ({toggle}) ---")
    result = subprocess.run([sys.executable, toggle], check=False)
    if result.returncode != 0:
        print("radio toggle failed; nothing else was changed")
        return 1

    print("--- waiting for Windows to re-enumerate (up to 90 s) ---")
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(5)
        found = snapshot()
        for address, entry in found.items():
            if _verdict(entry) == "healthy":
                print(f"repaired: {address} now has the HOGP driver and a HID interface")
                return 0
        print("  still enumerating ...")
    print("radio toggle did not restore enumeration")
    print("next: re-pair once (Settings -> Bluetooth -> Add device), see debug/ble-troubleshooting.md 7.1")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix", action="store_true",
                        help="toggle the Bluetooth radio and re-check when unhealthy")
    args = parser.parse_args()

    status = report(snapshot())
    if status == 0 or not args.fix:
        return status
    return fix()


if __name__ == "__main__":
    raise SystemExit(main())

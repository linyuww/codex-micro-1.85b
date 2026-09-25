#!/usr/bin/env python3
"""Send a Codex Micro RPC over the BLE HID interface, byte-for-byte like ChatGPT Desktop.

The desktop app (see `wl_device_comm` in app.asar) frames every host->device
message as a 64-byte HID output report:

    [0] = 6        report id (consumed by the HID stack, never sent over BLE)
    [1] = 2        channel (CHANNEL_RPC)
    [2] = n        payload length, 0..61
    [3..] = n bytes of UTF-8 JSON

and opens the device the way node-hid does (FILE_FLAG_OVERLAPPED).  This tool
reproduces that exactly, so a failure here is the app's failure and a success
here means the host->device path is sound.

Usage:
    python scripts/windows/hid_rpc.py --method sys.version
    python scripts/windows/hid_rpc.py --method device.status
    python scripts/windows/hid_rpc.py --method v.oai.rgbcfg --params '{"keys":{"effect":0}}'
    python scripts/windows/hid_rpc.py --listen 10          # just read reports for 10 s
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hid_caps import enumerate_hid_paths  # noqa: E402

REPORT_ID = 6
CHANNEL_RPC = 2
MAX_CHUNK = 61
REPORT_LEN = 64

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wt.DWORD),
        ("OffsetHigh", wt.DWORD),
        ("hEvent", wt.HANDLE),
    ]


def find_path() -> str | None:
    for path in enumerate_hid_paths():
        upper = path.upper()
        if "303A" in upper and "8360" in upper:
            return path
    return None


def frame(payload: bytes) -> list[bytes]:
    """Split a JSON payload into 64-byte reports exactly like sendDataHID()."""
    reports = []
    offset = 0
    while offset < len(payload):
        chunk = min(MAX_CHUNK, len(payload) - offset)
        data = bytearray(REPORT_LEN)
        data[0] = REPORT_ID
        data[1] = CHANNEL_RPC
        data[2] = chunk
        data[3 : 3 + chunk] = payload[offset : offset + chunk]
        reports.append(bytes(data))
        offset += chunk
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method")
    parser.add_argument("--params", default="{}")
    parser.add_argument("--id", type=int, default=1)
    parser.add_argument("--listen", type=float, default=3.0,
                        help="seconds to read input reports after the write")
    parser.add_argument("--plain", action="store_true",
                        help="open without FILE_FLAG_OVERLAPPED")
    args = parser.parse_args()

    path = find_path()
    if path is None:
        print("Codex Micro HID interface is not present (is the board connected?)")
        return 1
    print(f"path: {path}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, wt.DWORD, ctypes.c_void_p,
    ]
    kernel32.CreateEventW.restype = wt.HANDLE
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, wt.BOOL, wt.BOOL, ctypes.c_wchar_p]
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
    kernel32.GetOverlappedResult.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
        ctypes.POINTER(wt.DWORD), wt.BOOL]
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(OVERLAPPED)]

    flags = 0 if args.plain else FILE_FLAG_OVERLAPPED
    handle = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, flags, None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        err = ctypes.get_last_error()
        print(f"open failed: {err} (0x{err:08X}) {ctypes.FormatError(err).strip()}")
        return 1
    print(f"opened (overlapped={not args.plain})")

    try:
        if args.method:
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as exc:
                print(f"--params is not valid JSON: {exc}")
                return 1
            request = json.dumps(
                {"id": args.id, "method": args.method, "params": params},
                separators=(",", ":"),
            )
            payload = (request + "\n").encode("utf-8")
            reports = frame(payload)
            print(f"request: {request}")
            print(f"framing: {len(payload)}B payload -> {len(reports)} report(s)")

            for index, report in enumerate(reports, 1):
                written = wt.DWORD(0)
                ov = OVERLAPPED()
                ov.hEvent = kernel32.CreateEventW(None, True, False, None)
                ok = kernel32.WriteFile(
                    ctypes.c_void_p(handle), report, REPORT_LEN,
                    ctypes.byref(written), ctypes.byref(ov))
                if not ok:
                    err = ctypes.get_last_error()
                    if err == ERROR_IO_PENDING:
                        kernel32.WaitForSingleObject(ov.hEvent, 3000)
                        ok = kernel32.GetOverlappedResult(
                            ctypes.c_void_p(handle), ctypes.byref(ov),
                            ctypes.byref(written), False)
                    else:
                        print(f"  report {index}: WriteFile failed "
                              f"{err} (0x{err:08X}) {ctypes.FormatError(err).strip()}")
                        continue
                if ok:
                    print(f"  report {index}: wrote {written.value} bytes")
                else:
                    err = ctypes.get_last_error()
                    print(f"  report {index}: GetOverlappedResult failed "
                          f"{err} (0x{err:08X}) {ctypes.FormatError(err).strip()}")
                kernel32.CloseHandle(ov.hEvent)

        if args.listen > 0:
            print(f"--- listening {args.listen}s for device reports ---")
            deadline = time.time() + args.listen
            while time.time() < deadline:
                buf = ctypes.create_string_buffer(REPORT_LEN)
                read = wt.DWORD(0)
                ov = OVERLAPPED()
                ov.hEvent = kernel32.CreateEventW(None, True, False, None)
                ok = kernel32.ReadFile(
                    ctypes.c_void_p(handle), buf, REPORT_LEN,
                    ctypes.byref(read), ctypes.byref(ov))
                err = ctypes.get_last_error()
                if not ok and err != ERROR_IO_PENDING:
                    print(f"  ReadFile failed {err} (0x{err:08X}) "
                          f"{ctypes.FormatError(err).strip()}")
                    kernel32.CloseHandle(ov.hEvent)
                    break
                wait = kernel32.WaitForSingleObject(ov.hEvent, 1000)
                if wait == WAIT_TIMEOUT:
                    kernel32.CancelIoEx(ctypes.c_void_p(handle), ctypes.byref(ov))
                    kernel32.CloseHandle(ov.hEvent)
                    continue
                if wait != WAIT_OBJECT_0:
                    kernel32.CloseHandle(ov.hEvent)
                    continue
                if not kernel32.GetOverlappedResult(
                        ctypes.c_void_p(handle), ctypes.byref(ov),
                        ctypes.byref(read), False):
                    kernel32.CloseHandle(ov.hEvent)
                    continue
                data = buf.raw[: read.value]
                channel = data[1] if len(data) > 1 else -1
                length = data[2] if len(data) > 2 else 0
                text = data[3 : 3 + length].decode("utf-8", "replace")
                print(f"  <- channel={channel} len={length} {text!r}")
                kernel32.CloseHandle(ov.hEvent)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    return 0


if __name__ == "__main__":
    sys.exit(main())

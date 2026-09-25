#!/usr/bin/env python3
"""Dump the HID capabilities Windows derives for the Codex Micro.

The desktop app opens the board through node-hid (HIDAPI) and writes a
64-byte output report ([report id 6][63-byte body]).  `WriteFile` to a HID
device only accepts exactly `OutputReportByteLength` bytes, so any mismatch
between the firmware's report map and what Windows caches shows up here as
ERROR_INVALID_PARAMETER (0x57).

Usage:
    python scripts/windows/hid_caps.py                 # all Codex Micro HID interfaces
    python scripts/windows/hid_caps.py --write-test    # also try a zero-filled write
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys

VID = 0x303A
PID = 0x8360

GUID_DEVINTERFACE_HID = "{4D1E55B2-F16F-11CF-88CB-001111000030}"

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wt.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wt.ULONG),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


def guid_from_string(text: str) -> GUID:
    import uuid

    u = uuid.UUID(text)
    g = GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def enumerate_hid_paths() -> list[str]:
    setupapi = ctypes.WinDLL("setupapi")
    guid = guid_from_string(GUID_DEVINTERFACE_HID)

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        wt.DWORD,
    ]
    handle = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        raise ctypes.WinError()

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        wt.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ]

    paths: list[str] = []
    index = 0
    while True:
        data = SP_DEVICE_INTERFACE_DATA()
        data.cbSize = ctypes.sizeof(data)
        if not setupapi.SetupDiEnumDeviceInterfaces(
            handle, None, ctypes.byref(guid), index, ctypes.byref(data)
        ):
            break
        index += 1

        required = wt.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(
            handle, ctypes.byref(data), None, 0, ctypes.byref(required), None
        )
        buffer = ctypes.create_string_buffer(required.value)
        # cbSize is a DWORD at the head of the variable-length detail struct.
        ctypes.cast(buffer, ctypes.POINTER(wt.DWORD))[0] = (
            8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        )
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(
            handle, ctypes.byref(data), buffer, required.value, None, None
        ):
            continue
        path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
        paths.append(path)

    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList(handle)
    return paths


def describe(path: str, write_test: bool) -> None:
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or not handle:
        err = ctypes.get_last_error()
        print(f"  open failed: {err} ({ctypes.FormatError(err).strip()})")
        return

    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        if not hid.HidD_GetAttributes(ctypes.c_void_p(handle), ctypes.byref(attrs)):
            print("  HidD_GetAttributes failed")
            return
        print(
            f"  VID={attrs.VendorID:04X} PID={attrs.ProductID:04X} "
            f"version={attrs.VersionNumber:04X}"
        )

        for name, fn in (
            ("manufacturer", hid.HidD_GetManufacturerString),
            ("product", hid.HidD_GetProductString),
            ("serial", hid.HidD_GetSerialNumberString),
        ):
            buf = ctypes.create_unicode_buffer(256)
            if fn(ctypes.c_void_p(handle), buf, ctypes.sizeof(buf)):
                print(f"  {name}: {buf.value!r}")

        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(
            ctypes.c_void_p(handle), ctypes.byref(preparsed)
        ):
            err = ctypes.get_last_error()
            print(f"  HidD_GetPreparsedData failed: {err}")
            return
        try:
            caps = HIDP_CAPS()
            status = hid.HidP_GetCaps(preparsed, ctypes.byref(caps))
            if status != 0x110000:
                print(f"  HidP_GetCaps status=0x{status:X}")
                return
            print(f"  UsagePage=0x{caps.UsagePage:04X} Usage=0x{caps.Usage:04X}")
            print(
                f"  input={caps.InputReportByteLength}B "
                f"output={caps.OutputReportByteLength}B "
                f"feature={caps.FeatureReportByteLength}B"
            )
            print(
                f"  output value caps={caps.NumberOutputValueCaps} "
                f"output button caps={caps.NumberOutputButtonCaps}"
            )
        finally:
            hid.HidD_FreePreparsedData(preparsed)

        if write_test and caps.OutputReportByteLength:
            length = caps.OutputReportByteLength
            payload = ctypes.create_string_buffer(length)
            payload[0] = 0x06  # report id
            written = wt.DWORD(0)
            kernel32.WriteFile.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                wt.DWORD,
                ctypes.POINTER(wt.DWORD),
                ctypes.c_void_p,
            ]
            ok = kernel32.WriteFile(
                ctypes.c_void_p(handle),
                payload,
                length,
                ctypes.byref(written),
                None,
            )
            if ok:
                print(f"  write test: OK ({written.value} bytes)")
            else:
                err = ctypes.get_last_error()
                print(f"  write test: FAILED {err} (0x{err:08X}) {ctypes.FormatError(err).strip()}")
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-test", action="store_true")
    args = parser.parse_args()

    matches = []
    for path in enumerate_hid_paths():
        # Bluetooth LE HID interface paths are lower-case and carry the PnP
        # source prefix, e.g. `hid#{00001812-...}_dev_vid&02303a_pid&8360_...`.
        upper = path.upper()
        if f"303A" in upper and f"8360" in upper:
            matches.append(path)

    if not matches:
        print(f"no HID interface with VID_{VID:04X}&PID_{PID:04X} is present")
        return 1

    print(f"=== {len(matches)} HID interface(s) for VID_{VID:04X}&PID_{PID:04X} ===")
    for path in matches:
        print(path)
        describe(path, args.write_test)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Reset the board and capture its serial log from the very first line.

A plain capture started after `esptool --after hard_reset` always misses the
boot banner, because the board is already running by the time the port is
reopened. This tool drives the reset itself over the same handle it then reads
from, so the log starts at the first byte.

The reset sequence is the USB-Serial/JTAG one the ESP32-S3 on this board
exposes (the board enumerates as VID 303A PID 1001, USB mode USB-Serial/JTAG).

Usage:
    python scripts/windows/serial_reset_capture.py [COM5] [40]
"""

from __future__ import annotations

import sys
import time

import serial


def reset_usb_serial_jtag(ser: serial.Serial) -> None:
    """Pulse EN without ever pulling IO0 low, so the chip boots the app.

    Driving DTR high asserts IO0, and if EN is released while IO0 is still low
    the ROM comes up in `waiting for download` instead of running the firmware.
    Only RTS (EN) is toggled here; DTR stays low for the whole sequence.
    """
    ser.setDTR(False)  # IO0 high: normal boot
    ser.setRTS(True)  # EN low: hold in reset
    time.sleep(0.15)
    ser.setRTS(False)  # EN high: release
    time.sleep(0.05)


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0

    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as exc:  # noqa: BLE001 - surface the port error verbatim
        print(f"OPEN FAILED: {exc}")
        return 1

    try:
        reset_usb_serial_jtag(ser)
        deadline = time.time() + seconds
        while time.time() < deadline:
            data = ser.read(4096)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

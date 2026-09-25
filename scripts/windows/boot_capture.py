import subprocess, sys, time, serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

# Trigger a clean reset first, then read whatever the ROM/app emits.
subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32s3",
                "-p", port, "--after", "hard_reset", "flash_id"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

deadline = time.time() + seconds
ser = None
while time.time() < deadline:
    if ser is None:
        try:
            ser = serial.Serial(port, 115200, timeout=0.2)
        except Exception:
            time.sleep(0.1)
            continue
    try:
        data = ser.read(4096)
    except Exception:
        try:
            ser.close()
        except Exception:
            pass
        ser = None
        time.sleep(0.1)
        continue
    if data:
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()
if ser is not None:
    ser.close()

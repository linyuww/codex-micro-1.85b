import sys, time, serial
port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
try:
    ser = serial.Serial(port, 115200, timeout=0.2)
except Exception as exc:
    print("OPEN FAILED:", exc)
    sys.exit(1)
deadline = time.time() + seconds
try:
    while time.time() < deadline:
        data = ser.read(4096)
        if data:
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
finally:
    ser.close()

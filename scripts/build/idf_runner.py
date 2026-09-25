#!/usr/bin/env python3
"""Run ESP-IDF's idf.py from a Git Bash shell.

idf.py refuses to start when MSYSTEM is present in the environment, and Git
Bash re-injects that variable into every child process. This launcher rebuilds
the child environment without the MSys markers and then hands over to idf.py.
It changes nothing in the ESP-IDF installation itself.
"""

import os
import subprocess
import sys

MSYS_MARKERS = ("MSYSTEM", "MINGW_PREFIX", "MINGW_CHOST", "MINGW_PACKAGE_PREFIX")


def main() -> int:
    idf_path = os.environ.get("IDF_PATH")
    if not idf_path:
        sys.stderr.write("IDF_PATH is not set; source idf_env.sh first\n")
        return 2
    env = {key: value for key, value in os.environ.items() if key not in MSYS_MARKERS}
    env["MSYS_NO_PATHCONV"] = "1"
    command = [sys.executable, os.path.join(idf_path, "tools", "idf.py")] + sys.argv[1:]
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run idf.py from Git Bash without the two silent-failure traps.

Git Bash (MSYS) breaks `idf.py` in two ways that both look like success:

1. idf.py only calls main() when MSYSTEM is absent from the environment
   (see tools/idf.py, `if __name__ == '__main__'`). Git Bash always injects
   MSYSTEM=MINGW64, so the script prints the "MSys/Mingw is no longer
   supported" warning, skips main() entirely, and exits 0. Nothing is
   built, and the exit code says it worked. `env -u MSYSTEM` does not
   help, because Git Bash re-injects the variable into every child.

2. A ';'-separated PATH passed through Git Bash gets mangled by MSYS path
   conversion: `D:/Espressif/tools/cmake/3.30.2/bin` becomes
   `D;C:\\...\\PortableGit\\...\\Espressif\\tools\\cmake\\3.30.2\\bin`.
   cmake and ninja are then never found, and idf.py reports
   `"cmake" must be available on the PATH`.

Both are avoided the same way: build the environment inside Python, where
Git Bash never sees the values, and forward to idf.py with that exact
mapping. MSYSTEM is popped; PATH is written directly into os.environ.

Settings are read from idf_env.bat rather than hard-coded, so this script
follows the toolchain if the installation is ever updated.

    python tools/idf_build.py build
    python tools/idf_build.py -p COM5 flash
    python tools/idf_build.py size
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDF_ENV_BAT = ROOT / "idf_env.bat"

# Windows needs these to spawn anything at all; a toolchain-only PATH
# leaves subprocess creation unable to find the loader.
BASE_PATH = r"C:\Windows\System32;C:\Windows"


def expand(value: str, env: dict) -> str:
    """Expand %VAR% references the way cmd.exe would."""
    def sub(match: re.Match) -> str:
        return env.get(match.group(1), match.group(0))
    return re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", sub, value)


def load_idf_env(bat: Path):
    """Parse idf_env.bat into (variables, ordered PATH fragments).

    The batch file appends to PATH once per tool, so the fragments are
    collected in order and joined later rather than merged one at a time.
    """
    variables: dict = {}
    path_fragments: list = []
    text = bat.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r'set\s+"([^="]+)=([^"]*)"', text):
        key, value = match.group(1).strip(), match.group(2).strip()
        if key.upper() == "PATH":
            path_fragments.append(expand(value, variables))
        else:
            variables[key] = expand(value, variables)
    return variables, path_fragments


def main() -> int:
    if not IDF_ENV_BAT.exists():
        print(f"missing {IDF_ENV_BAT}", file=sys.stderr)
        return 1

    variables, path_fragments = load_idf_env(IDF_ENV_BAT)

    idf_path = variables.get("IDF_PATH")
    python_exe = variables.get("IDF_PYTHON") or sys.executable
    if not idf_path:
        print("IDF_PATH not found in idf_env.bat", file=sys.stderr)
        return 1

    env = dict(os.environ)
    # The whole point of this script: idf.py skips main() if this is set.
    env.pop("MSYSTEM", None)

    for key, value in variables.items():
        if key.upper() != "PATH":
            env[key] = value

    # Backslash form, and assembled here so Git Bash cannot convert it.
    tool_path = ";".join(f.replace("/", "\\") for f in path_fragments)
    env["PATH"] = f"{tool_path};{BASE_PATH}"

    idf_py = Path(idf_path) / "tools" / "idf.py"
    command = [python_exe, str(idf_py)] + sys.argv[1:]
    print(f"$ {python_exe} {idf_py} {' '.join(sys.argv[1:])}", flush=True)
    return subprocess.call(command, env=env, cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
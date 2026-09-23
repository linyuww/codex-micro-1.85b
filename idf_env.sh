#!/usr/bin/env bash
# Source the ESP-IDF v5.4.1 install already present on this machine.
# No tool or package is installed by this script.
#
# PATH entries use Windows backslash form on purpose: MSys rewrites POSIX-style
# entries when it builds the environment of a native Windows process, which
# would corrupt "D:/Espressif/..." into a bogus relative path.

export IDF_TOOLS_PATH='D:\Espressif'
export IDF_PATH='D:\Espressif\frameworks\esp-idf-v5.4.1'
export IDF_PYTHON_ENV_PATH='D:\Espressif\python_env\idf5.4_py3.12_env'
export IDF_PYTHON='D:\Espressif\python_env\idf5.4_py3.12_env\Scripts\python.exe'

IDF_TOOL_PATH_ENTRIES=(
  '/d/Espressif/tools/xtensa-esp-elf/esp-14.2.0_20241119/xtensa-esp-elf/bin'
  '/d/Espressif/tools/riscv32-esp-elf/esp-14.2.0_20241119/riscv32-esp-elf/bin'
  '/d/Espressif/tools/esp-clang/esp-18.1.2_20240912/esp-clang/bin'
  '/d/Espressif/tools/esp32ulp-elf/2.38_20240113/esp32ulp-elf/bin'
  '/d/Espressif/tools/ninja/1.12.1'
  '/d/Espressif/tools/cmake/3.30.2/bin'
  '/d/Espressif/tools/ccache/4.10.2/ccache-4.10.2-windows-x86_64'
  '/d/Espressif/tools/esp-rom-elfs/20241011'
  '/d/Espressif/tools/xtensa-esp-elf-gdb/14.2_20240403/xtensa-esp-elf-gdb/bin'
)
IDF_TOOL_PATH=''
for entry in "${IDF_TOOL_PATH_ENTRIES[@]}"; do
  IDF_TOOL_PATH="${IDF_TOOL_PATH}${entry}:"
done
export PATH="${IDF_TOOL_PATH}${PATH}"

export IDF_CCACHE_ENABLE=0
# Normally exported by "idf_tools.py export"; set explicitly because MSys
# cannot run that helper. gdbinit.cmake hard-fails without ESP_ROM_ELF_DIR.
export ESP_ROM_ELF_DIR='D:/Espressif/tools/esp-rom-elfs/20241011/'
export OPENOCD_SCRIPTS='D:/Espressif/tools/openocd-esp32/v0.12.0-esp32-20241016/openocd-esp32/share/openocd/scripts'
unset PYTHONPATH
unset PYTHONHOME
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Keep MSys from rewriting ESP-IDF's own path arguments.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# pwd -W yields a Windows-style path, which is what native python needs.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W)"

idf() {
  "$IDF_PYTHON" "$PROJECT_DIR/idf_runner.py" "$@"
}

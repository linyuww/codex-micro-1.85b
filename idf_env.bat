@echo off
rem Use the ESP-IDF installation already present on this machine.
rem Nothing is installed or downloaded by this script.
set "IDF_TOOLS_PATH=D:\Espressif"
set "IDF_PATH=D:\Espressif\frameworks\esp-idf-v5.4.1"
set "IDF_PYTHON_ENV_PATH=D:\Espressif\python_env\idf5.4_py3.12_env"
set "IDF_PYTHON=%IDF_PYTHON_ENV_PATH%\Scripts\python.exe"

set "PATH=%IDF_TOOLS_PATH%\tools\xtensa-esp-elf\esp-14.2.0_20241119\xtensa-esp-elf\bin;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\riscv32-esp-elf\esp-14.2.0_20241119\riscv32-esp-elf\bin;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\esp-clang\esp-18.1.2_20240912\esp-clang\bin;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\esp32ulp-elf\2.38_20240113\esp32ulp-elf\bin;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\ninja\1.12.1;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\cmake\3.30.2\bin;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\ccache\4.10.2\ccache-4.10.2-windows-x86_64;%PATH%"
set "PATH=%IDF_TOOLS_PATH%\tools\esp-rom-elfs\20241011;%PATH%"
set "IDF_CCACHE_ENABLE=0"
set "PYTHONPATH="
set "PYTHONHOME="

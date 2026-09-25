@echo off
rem This file lives in scripts\build\, but idf.py must run from the project
rem root (the directory holding CMakeLists.txt), so step up two levels.
call "%~dp0idf_env.bat"
cd /d "%~dp0..\.."
"%IDF_PYTHON%" "%IDF_PATH%\tools\idf.py" %*

@echo off
call "%~dp0idf_env.bat"
cd /d "%~dp0"
"%IDF_PYTHON%" "%IDF_PATH%\tools\idf.py" %*

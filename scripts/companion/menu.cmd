@echo off
rem Double-click entry point for the companion menu.
rem ASCII only: cmd.exe reads this file in the OEM code page.

setlocal
set "SCRIPT=%~dp0menu.ps1"

set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%PWSH%" set "PWSH="
if not defined PWSH for %%I in (pwsh.exe) do set "PWSH=%%~$PATH:I"

if not defined PWSH (
  echo.
  echo   PowerShell 7 is required but was not found.
  echo   Expected at: %ProgramFiles%\PowerShell\7\pwsh.exe
  echo.
  pause
  exit /b 1
)

"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "CODE=%ERRORLEVEL%"
endlocal
exit /b %CODE%

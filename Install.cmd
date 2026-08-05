@echo off
rem ===================================================================
rem  Grindstone - Windows installer.  Double-click this file.
rem
rem  It opens a small window, asks which shortcuts you want, then
rem  installs everything the app needs (including Python and Node.js
rem  if they are missing) and builds it.
rem
rem  Nothing here needs to be installed first: Windows PowerShell and
rem  WinForms ship with Windows.
rem
rem  Scripted use:  Install.cmd -NoUi
rem ===================================================================
setlocal

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"

set "SCRIPT=%~dp0tools\installer\windows\Install.ps1"
if not exist "%SCRIPT%" (
  echo.
  echo   Could not find the installer at:
  echo     %SCRIPT%
  echo.
  echo   Install.cmd has to stay in the repository root next to the
  echo   tools\ folder. If you downloaded a single file, clone or
  echo   download the whole repository instead.
  echo.
  pause
  exit /b 1
)

rem -NoUi runs in this console; anything else opens the window. Hidden
rem -WindowStyle keeps the PowerShell console out of the way - the flash
rem you may see is that console being created and immediately hidden.
echo %* | find /i "-NoUi" >nul
if not errorlevel 1 (
  "%PS%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
  exit /b %errorlevel%
)

start "" "%PS%" -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%SCRIPT%" %*
exit /b 0

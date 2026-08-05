# Unattended setup from a fresh clone (Windows PowerShell).
#
#   git clone https://github.com/KadePrice123/Grindstone.git
#   cd Grindstone
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# This is the no-window, no-shortcuts path, kept for scripts and CI. To install
# normally, double-click Install.cmd instead: same work, plus the desktop and
# Start Menu shortcuts.
#
# Missing prerequisites (Python 3.12+, Node 20+) are installed for you.
# Everything else lives INSIDE the clone: .venv, code/app/node_modules,
# code/app/out.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Throws on any failure ($ErrorActionPreference above), so reaching the next
# line means the install and the gate both succeeded.
& "$here\tools\installer\windows\Install.ps1" -NoUi -NoShortcuts @args

Write-Host ''
Write-Host 'Run the app with:' -ForegroundColor Green
Write-Host '  cd code\app; npm run start'

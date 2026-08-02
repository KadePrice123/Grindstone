# One-command setup from a fresh clone (Windows PowerShell).
#
#   git clone https://github.com/KadePrice123/Grindstone.git
#   cd Grindstone
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Prerequisites it checks but does not install: Python 3.12+ and Node 20+.
# Everything it creates lives INSIDE the clone (.venv, code/app/node_modules).

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Need($cmd, $hint) {
  try { Get-Command $cmd -ErrorAction Stop | Out-Null }
  catch { Write-Host "MISSING: $cmd - $hint" -ForegroundColor Red; exit 1 }
}
Need python 'install Python 3.12+ from python.org or the Microsoft Store'
Need npm    'install Node.js 20+ (LTS) from nodejs.org'

$py = (python --version) 2>&1
Write-Host "Python: $py"
Write-Host "Node:   $(node --version)  npm: $(npm --version)"

# 1. Python venv inside the clone
if (-not (Test-Path "$here\.venv")) {
  Write-Host "Creating .venv ..."
  python -m venv "$here\.venv"
}
& "$here\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "$here\requirements.txt"
if (-not $?) { Write-Host 'pip install failed' -ForegroundColor Red; exit 1 }
Write-Host "Python deps installed."

# 2. Frontend deps
Push-Location "$here\code\app"
npm install --no-fund --no-audit
$npmOk = $?
Pop-Location
if (-not $npmOk) { Write-Host 'npm install failed' -ForegroundColor Red; exit 1 }

# 3. The offline verification gate - proves the install is complete.
Push-Location "$here\code"
& "$here\.venv\Scripts\python.exe" selftest.py
$gateOk = $?
Pop-Location
if (-not $gateOk) {
  Write-Host 'The verification gate FAILED - see the FAIL lines above.' -ForegroundColor Red
  exit 1
}

Write-Host ''
Write-Host 'Setup complete. Run the app with:' -ForegroundColor Green
Write-Host '  cd code\app; npm run start'

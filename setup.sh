#!/usr/bin/env bash
# One-command setup from a fresh clone (Linux/macOS).
#
#   git clone https://github.com/KadePrice123/Grindstone.git
#   cd Grindstone && ./setup.sh
#
# Prerequisites it checks but does not install: Python 3.12+ and Node 20+.
# Everything it creates lives INSIDE the clone (.venv, code/app/node_modules).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

need() { command -v "$1" >/dev/null || { echo "MISSING: $1 — $2"; exit 1; }; }
need python3 'install Python 3.12+'
need npm 'install Node.js 20+ (LTS)'

echo "Python: $(python3 --version)   Node: $(node --version)"

[ -d "$here/.venv" ] || python3 -m venv "$here/.venv"
"$here/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$here/requirements.txt"
echo "Python deps installed."

(cd "$here/code/app" && npm install --no-fund --no-audit)
# Electron's postinstall can silently skip the binary download; without this
# the app later dies with an opaque spawn ENOENT (fresh-clone test).
if [ ! -e "$here/code/app/node_modules/electron/dist/electron" ] \
   && [ ! -e "$here/code/app/node_modules/electron/dist/electron.exe" ]; then
  echo "Electron binary missing - fetching it now ..."
  (cd "$here/code/app" && node node_modules/electron/install.js)
fi

(cd "$here/code" && "$here/.venv/bin/python" selftest.py)

echo
echo "Setup complete. Run the app with:  cd code/app && npm run start"

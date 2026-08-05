#!/usr/bin/env bash
# Unattended setup from a fresh clone (Linux/macOS).
#
#   git clone https://github.com/KadePrice123/Grindstone.git
#   cd Grindstone && ./setup.sh
#
# This is the no-dialog, no-shortcuts path, kept for scripts and CI. To install
# normally, run ./install.sh (Linux) or double-click Install.command (macOS):
# same work, plus the desktop icon and menu entry.
#
# Missing prerequisites (Python 3.12+, Node 20+) are installed for you.
# Everything else lives INSIDE the clone: .venv, code/app/node_modules,
# code/app/out.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

exec bash "$here/tools/installer/posix/install.sh" --no-shortcuts "$@"

#!/usr/bin/env bash
# ===================================================================
#  Grindstone - Linux installer.
#
#    ./install.sh            ask, then install
#    ./install.sh --no-ui    unattended
#
#  Most Linux file managers will not run a .sh on double-click without
#  being told to, so this is the one command to run. macOS users:
#  double-click Install.command instead.
# ===================================================================
cd "$(dirname "$0")" || exit 1
exec bash ./tools/installer/posix/install.sh "$@"

#!/usr/bin/env bash
# ===================================================================
#  Grindstone - macOS installer.  Double-click this file in Finder.
#
#  Finder opens .command files in Terminal, which is why this is the
#  macOS entry point rather than install.sh: double-clicking a .sh
#  opens it in a text editor instead of running it.
#
#  It asks which shortcuts you want, then installs everything the app
#  needs - including Python and Node.js if they are missing.
# ===================================================================
cd "$(dirname "$0")" || exit 1
if [ ! -x ./tools/installer/posix/install.sh ]; then
  # A ZIP download or a copy across filesystems can lose the exec bit.
  chmod +x ./tools/installer/posix/install.sh 2>/dev/null
fi
GS_FROM_FINDER=1 exec bash ./tools/installer/posix/install.sh "$@"

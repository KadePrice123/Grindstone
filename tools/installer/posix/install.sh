#!/usr/bin/env bash
#
# Grindstone installer for macOS and Linux.
#
# Reached by double-clicking Install.command (macOS) or running ./install.sh
# (Linux) in the repo root. Detects Python and Node, installs whichever are
# missing, builds the clone, runs the verification gate and creates whichever
# shortcuts you tick.
#
#   ./install.sh              ask, then install
#   ./install.sh --no-ui      no dialog: desktop + menu entry + gate
#   ./install.sh --help
#
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# --------------------------------------------------------------------- logging
LOG="${TMPDIR:-/tmp}/grindstone-setup.log"
: > "$LOG" 2>/dev/null || LOG=/dev/null
gs_log()  { printf '    %s\n'   "$*" | tee -a "$LOG"; }
gs_step() { printf '\n==> %s\n' "$*" | tee -a "$LOG"; }
gs_warn() { printf 'warn: %s\n' "$*" | tee -a "$LOG"; }
gs_die()  {
  printf '\nERROR: %s\n' "$*" | tee -a "$LOG" >&2
  printf '\nFull log: %s\n' "$LOG" >&2
  # Finder closes the Terminal window the moment the script ends; without this
  # the error is on screen for a few milliseconds.
  if [ "${GS_FROM_FINDER:-0}" = 1 ] && [ -r /dev/tty ]; then
    printf '\nPress Return to close.\n' >&2
    read -r _ < /dev/tty || true
  fi
  exit 1
}

# -------------------------------------------------------------------- platform
case "$(uname -s)" in
  Darwin) GS_OS=mac ;;
  Linux)  GS_OS=linux ;;
  *) gs_die "Unsupported system: $(uname -s). Windows users: run Install.cmd instead." ;;
esac

. "$SELF_DIR/ui.sh"
. "$SELF_DIR/shortcuts.sh"

# ------------------------------------------------------------------ repo root
ROOT="$SELF_DIR"
while [ "$ROOT" != "/" ] && [ ! -f "$ROOT/requirements.txt" ]; do
  ROOT="$(dirname "$ROOT")"
done
[ -f "$ROOT/requirements.txt" ] && [ -f "$ROOT/code/selftest.py" ] ||
  gs_die "Could not find the Grindstone clone above $SELF_DIR. Keep the installer inside the repository."

# ------------------------------------------------------------------ arguments
WANT_UI=1; ARG_PICKS=''; FORCE_LAUNCH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-ui)        WANT_UI=0; ARG_PICKS='desktop apps gate' ;;
    --no-shortcuts) WANT_UI=0; ARG_PICKS='gate' ;;
    --no-gate)      WANT_UI=0; ARG_PICKS='desktop apps' ;;
    --launch)       FORCE_LAUNCH=1 ;;
    --help|-h)      sed -n '2,14p' "$0"; exit 0 ;;
    *) gs_die "Unknown option: $1" ;;
  esac
  shift
done

# ------------------------------------------------------------------- helpers
# $1 >= $2, dotted numeric. Not `sort -V`: older macOS sort has no -V.
gs_ver_ge() {
  awk -v v1="$1" -v v2="$2" 'BEGIN{
    n1=split(v1,a,"."); n2=split(v2,b,".");
    for(i=1;i<=3;i++){ x=(i<=n1?a[i]+0:0); y=(i<=n2?b[i]+0:0);
      if(x>y) exit 0; if(x<y) exit 1 }
    exit 0 }'
}

gs_download() {
  gs_log "downloading $1"
  if   command -v curl >/dev/null 2>&1; then curl -fL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else return 1; fi
}

gs_run() {   # log a command and stream it
  gs_log "\$ $*"
  "$@" 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

gs_have_sudo() { command -v sudo >/dev/null 2>&1; }

MIN_PY=3.12.0
MIN_NODE=20.0.0

gs_find_python() {
  local c v
  PYTHON=''; PYTHON_VER=''
  for c in python3.13 python3.12 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    v="$("$c" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)" || continue
    [ -n "$v" ] || continue
    if gs_ver_ge "$v" "$MIN_PY"; then
      PYTHON="$(command -v "$c")"; PYTHON_VER="$v"; return 0
    fi
  done
  return 1
}

gs_find_node() {
  NODE=''; NODE_VER=''; NPM=''
  command -v node >/dev/null 2>&1 || return 1
  local v; v="$(node --version 2>/dev/null | sed 's/^v//')"
  [ -n "$v" ] || return 1
  gs_ver_ge "$v" "$MIN_NODE" || return 1
  command -v npm >/dev/null 2>&1 || return 1
  NODE="$(command -v node)"; NODE_VER="$v"; NPM="$(command -v npm)"
  return 0
}

# --------------------------------------------------------- installing Python
gs_install_python() {
  gs_step 'Installing Python 3.12'
  if [ "$GS_OS" = mac ]; then
    if command -v brew >/dev/null 2>&1; then
      gs_run brew install python@3.12 || gs_warn 'brew could not install Python'
      # Keg-only formulae are not linked into PATH.
      local bp; bp="$(brew --prefix 2>/dev/null)/opt/python@3.12/bin"
      [ -d "$bp" ] && PATH="$bp:$PATH" && export PATH
    fi
    if ! gs_find_python; then
      local pkg="${TMPDIR:-/tmp}/grindstone-python.pkg"
      gs_download 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg' "$pkg" ||
        gs_die 'Could not download Python. Install Python 3.12+ from python.org, then run this again.'
      gs_log 'installing - macOS will ask for your password'
      gs_have_sudo || gs_die 'sudo is required to install Python system-wide.'
      gs_run sudo installer -pkg "$pkg" -target / || gs_die 'The Python installer failed.'
      rm -f "$pkg"
    fi
  else
    gs_linux_pkg_install python3 python3-venv python3-pip ||
      gs_die 'Could not install Python. Install Python 3.12+ with your package manager, then run this again.'
  fi
  hash -r 2>/dev/null || true
  gs_find_python || gs_die 'Python still is not on PATH. Open a new terminal and run the installer again.'
  gs_log "Python $PYTHON_VER ready"
}

# Best-effort package install across the common distros.
gs_linux_pkg_install() {
  local mgr=''
  for m in apt-get dnf yum pacman zypper apk; do
    command -v "$m" >/dev/null 2>&1 && { mgr="$m"; break; }
  done
  [ -n "$mgr" ] || return 1
  gs_have_sudo || { gs_warn "no sudo available - please run: $mgr install $*"; return 1; }
  case "$mgr" in
    apt-get) gs_run sudo apt-get update -qq; gs_run sudo apt-get install -y "$@" ;;
    dnf|yum) gs_run sudo "$mgr" install -y "$@" ;;
    pacman)  gs_run sudo pacman -Sy --noconfirm "$@" ;;
    zypper)  gs_run sudo zypper --non-interactive install "$@" ;;
    apk)     gs_run sudo apk add "$@" ;;
  esac
}

# ----------------------------------------------------------- installing Node
GS_NODE_VERSION=v22.20.0

gs_install_node() {
  gs_step 'Installing Node.js LTS'
  if [ "$GS_OS" = mac ]; then
    if command -v brew >/dev/null 2>&1; then
      gs_run brew install node || gs_warn 'brew could not install Node'
    fi
    if ! gs_find_node; then
      local pkg="${TMPDIR:-/tmp}/grindstone-node.pkg"
      gs_download "https://nodejs.org/dist/$GS_NODE_VERSION/node-$GS_NODE_VERSION.pkg" "$pkg" ||
        gs_die 'Could not download Node.js. Install Node 20+ from nodejs.org, then run this again.'
      gs_log 'installing - macOS will ask for your password'
      gs_have_sudo || gs_die 'sudo is required to install Node system-wide.'
      gs_run sudo installer -pkg "$pkg" -target / || gs_die 'The Node installer failed.'
      rm -f "$pkg"
    fi
  else
    # A private copy under ~/.local needs no root and pins a version new
    # enough for Electron, which distro packages frequently are not.
    local arch
    case "$(uname -m)" in
      x86_64|amd64)  arch=x64 ;;
      aarch64|arm64) arch=arm64 ;;
      armv7l)        arch=armv7l ;;
      *) gs_die "No Node build for $(uname -m). Install Node 20+ yourself, then run this again." ;;
    esac
    local dir="$HOME/.local/share/grindstone"
    local name="node-$GS_NODE_VERSION-linux-$arch"
    local tgz="${TMPDIR:-/tmp}/$name.tar.gz"
    mkdir -p "$dir"
    gs_download "https://nodejs.org/dist/$GS_NODE_VERSION/$name.tar.gz" "$tgz" ||
      gs_die 'Could not download Node.js. Install Node 20+ with your package manager, then run this again.'
    gs_run tar -xzf "$tgz" -C "$dir" || gs_die 'Could not unpack the Node.js archive.'
    rm -f "$tgz"
    PATH="$dir/$name/bin:$PATH"; export PATH
    gs_log "Node installed privately at $dir/$name"
    gs_log 'It is on PATH for this install; the app itself does not need it afterwards.'
  fi
  hash -r 2>/dev/null || true
  gs_find_node || gs_die 'Node still is not on PATH. Open a new terminal and run the installer again.'
  gs_log "Node $NODE_VER ready"
}

# ------------------------------------------------------------------- the work
gs_python_env() {
  local venv_py="$ROOT/.venv/bin/python"
  if [ ! -x "$venv_py" ]; then
    gs_step 'Creating the virtual environment'
    if ! gs_run "$PYTHON" -m venv "$ROOT/.venv"; then
      # Debian and Ubuntu split venv into its own package and this is the
      # single most common failure on those systems.
      gs_warn 'venv creation failed - trying to install the python3-venv package'
      gs_linux_pkg_install python3-venv >/dev/null 2>&1
      gs_run "$PYTHON" -m venv "$ROOT/.venv" ||
        gs_die "Could not create a virtualenv. On Debian/Ubuntu: sudo apt-get install python3-venv"
    fi
  else
    gs_log 'virtual environment already present'
  fi
  [ -x "$venv_py" ] || gs_die "venv creation did not produce $venv_py"

  gs_step 'Installing Python dependencies'
  gs_run "$venv_py" -m pip install --disable-pip-version-check -q -r "$ROOT/requirements.txt" ||
    gs_die 'pip install failed.'
  gs_log 'Python dependencies installed'
}

gs_frontend() {
  gs_step 'Installing frontend dependencies'
  ( cd "$ROOT/code/app" && gs_run "$NPM" install --no-fund --no-audit ) ||
    gs_die 'npm install failed.'

  # Electron's postinstall can silently skip the binary download; without this
  # the app later dies with an opaque spawn ENOENT (fresh-clone test).
  if [ ! -e "$(gs_electron_bin)" ]; then
    gs_log 'Electron binary missing - fetching it now'
    ( cd "$ROOT/code/app" && gs_run "$NODE" node_modules/electron/install.js ) || true
  fi
  [ -e "$(gs_electron_bin)" ] || gs_die 'The Electron binary could not be downloaded.'
  gs_log 'frontend dependencies installed'
}

gs_build() {
  # `npm run start` is electron-vite PREVIEW: it serves out/, it does not
  # create it. Without this a genuinely fresh clone has nothing to launch.
  gs_step 'Building the app'
  ( cd "$ROOT/code/app" && gs_run "$NPM" run build ) || gs_die 'The build failed.'
  [ -f "$ROOT/code/app/out/main/index.js" ] || gs_die 'The build produced no out/main/index.js'
  gs_log 'build complete'
}

gs_gate() {
  gs_step 'Running the verification gate'
  if ! command -v git >/dev/null 2>&1; then
    gs_warn 'git not found - the gate needs it to scan tracked files. Skipping.'
    return 0
  fi
  ( cd "$ROOT/code" && gs_run "$ROOT/.venv/bin/python" selftest.py ) ||
    gs_die 'The verification gate FAILED - see the FAIL line above.'
  gs_log 'gate passed'
}

# ============================================================== run the thing
printf '\n  Grindstone setup\n  %s\n' "$ROOT"

gs_step 'Checking what is already installed'
if gs_find_python; then gs_log "Python $PYTHON_VER  ($PYTHON)"; else gs_log 'Python 3.12+  not found - will be installed'; fi
if gs_find_node;   then gs_log "Node.js $NODE_VER  ($NODE)";   else gs_log 'Node.js 20+   not found - will be installed'; fi
command -v git >/dev/null 2>&1 || gs_warn 'git not found - the app runs fine, but the verification gate needs it'

if [ "$WANT_UI" = 1 ]; then
  PICKS="$(gs_ask_options)" || gs_die 'Cancelled.'
else
  PICKS="$ARG_PICKS"
fi
[ "$FORCE_LAUNCH" = 1 ] && PICKS="$PICKS launch"
gs_log "options: ${PICKS:-none}"

[ -n "$PYTHON" ] || gs_install_python
[ -n "$NODE" ]   || gs_install_node

gs_python_env
gs_frontend
gs_build
gs_picked gate "$PICKS" && gs_gate
gs_install_shortcuts "$PICKS"

printf '\n  Setup complete.\n' | tee -a "$LOG"
if gs_picked launch "$PICKS"; then
  gs_step 'Starting Grindstone'
  gs_launch_app
else
  printf '  Start it from your applications menu, or run:\n    %s .\n' "$(gs_electron_bin)"
fi
printf '\n  Log: %s\n\n' "$LOG"

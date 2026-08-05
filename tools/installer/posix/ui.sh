#!/usr/bin/env bash
# The option picker, in whatever dialog toolkit this machine actually has.
#
# Deliberately no Python/Tk, no Node, no pip install: the installer exists to
# put those on the machine, so it cannot depend on them. osascript is part of
# macOS; zenity/kdialog ship with GNOME/KDE; and if neither is there the
# terminal prompt always works.
#
# gs_ask_options prints the chosen keys, space separated, on stdout. Every
# prompt goes to stderr so the caller can capture the answer cleanly.

# key|label|default(on/off)
GS_OPTIONS_MAC='desktop|Desktop shortcut|on
apps|Add to the Applications folder|on
dock|Keep it in the Dock|on
gate|Run the verification gate|on
launch|Open Grindstone when finished|on'

GS_OPTIONS_LINUX='desktop|Desktop shortcut|on
apps|Add to the applications menu|on
dock|Pin to the favourites bar|off
gate|Run the verification gate|on
launch|Open Grindstone when finished|on'

gs_options_spec() {
  if [ "$GS_OS" = mac ]; then printf '%s\n' "$GS_OPTIONS_MAC"
  else printf '%s\n' "$GS_OPTIONS_LINUX"; fi
}

gs_field() { printf '%s' "$1" | cut -d'|' -f"$2"; }

# ------------------------------------------------------------------- macOS
_gs_ask_osascript() {
  local labels='' defaults='' line label
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    label="$(gs_field "$line" 2)"
    labels="$labels, \"$label\""
    if [ "$(gs_field "$line" 3)" = on ]; then defaults="$defaults, \"$label\""; fi
  done <<EOF
$(gs_options_spec)
EOF
  labels="${labels#, }"
  defaults="${defaults#, }"

  local chosen
  chosen="$(osascript <<EOF 2>/dev/null
set opts to {$labels}
set pre to {$defaults}
set picked to choose from list opts with title "Grindstone Setup" with prompt "Everything Grindstone needs will be installed.

Tick what you would like as well:" default items pre with multiple selections allowed and empty selection allowed
if picked is false then
  return "##CANCEL##"
end if
set AppleScript's text item delimiters to "\n"
return picked as text
EOF
)"
  [ "$chosen" = '##CANCEL##' ] && return 1
  # Map labels back to keys.
  local out='' l
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if [ "$(gs_field "$line" 2)" = "$l" ]; then out="$out $(gs_field "$line" 1)"; fi
    done <<EOF
$(gs_options_spec)
EOF
  done <<EOF
$chosen
EOF
  printf '%s' "${out# }"
}

# -------------------------------------------------------------------- zenity
_gs_ask_zenity() {
  local args line chosen
  args=''
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if [ "$(gs_field "$line" 3)" = on ]; then args="$args TRUE"; else args="$args FALSE"; fi
    args="$args \"$(gs_field "$line" 2)\" \"$(gs_field "$line" 1)\""
  done <<EOF
$(gs_options_spec)
EOF
  # eval so the quoted triples above expand as separate argv entries.
  chosen="$(eval zenity --list --checklist \
      --title=\"Grindstone Setup\" \
      --text=\"Everything Grindstone needs will be installed.\\nTick what you would like as well:\" \
      --column=\"\" --column=\"Option\" --column=\"key\" \
      --hide-column=3 --print-column=3 --separator=\" \" \
      --width=460 --height=320 $args 2>/dev/null)" || return 1
  printf '%s' "$chosen"
}

# ------------------------------------------------------------------- kdialog
_gs_ask_kdialog() {
  local args line chosen
  args=''
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    args="$args \"$(gs_field "$line" 1)\" \"$(gs_field "$line" 2)\" \"$(gs_field "$line" 3)\""
  done <<EOF
$(gs_options_spec)
EOF
  chosen="$(eval kdialog --title \"Grindstone Setup\" \
      --checklist \"Tick what you would like:\" $args 2>/dev/null)" || return 1
  printf '%s' "$(printf '%s' "$chosen" | tr -d '"')"
}

# ------------------------------------------------------------------ terminal

# Applies a list of 1-based indexes to the default on/off states and prints the
# keys that end up on. Split out from the prompt so it can be tested without a
# terminal attached.
#   $1 keys    "desktop apps dock"
#   $2 states  "on on off"
#   $3 answer  "1 3"
gs_apply_toggles() {
  local keys="$1" states="$2" answer="$3"
  local out='' idx=0 k s n
  for k in $keys; do
    idx=$((idx + 1))
    s="$(printf '%s' "$states" | cut -d' ' -f"$idx")"
    for n in $answer; do
      if [ "$n" = "$idx" ]; then
        if [ "$s" = on ]; then s=off; else s=on; fi
      fi
    done
    if [ "$s" = on ]; then out="$out $k"; fi
  done
  printf '%s' "${out# }"
}

_gs_ask_tty() {
  local line i=0 keys='' states='' label key def answer
  printf '\n  Grindstone setup\n\n' >&2
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    i=$((i + 1))
    key="$(gs_field "$line" 1)"; label="$(gs_field "$line" 2)"; def="$(gs_field "$line" 3)"
    keys="$keys $key"; states="$states $def"
    if [ "$def" = on ]; then printf '   %d) [x] %s\n' "$i" "$label" >&2
    else printf '   %d) [ ] %s\n' "$i" "$label" >&2; fi
  done <<EOF
$(gs_options_spec)
EOF
  printf '\n  Press Enter to accept, or type numbers to toggle (e.g. "1 3"): ' >&2
  # /dev/tty, not stdin: launched from Finder or a file manager, stdin may be
  # closed while a terminal is still attached.
  if [ -r /dev/tty ]; then read -r answer < /dev/tty || answer=''
  else read -r answer || answer=''; fi
  printf '\n' >&2

  gs_apply_toggles "${keys# }" "${states# }" "$answer"
}

# Picks the best dialog available and falls back to the terminal.
gs_ask_options() {
  if [ "$GS_OS" = mac ] && command -v osascript >/dev/null 2>&1; then
    _gs_ask_osascript && return 0
    # A non-zero here is a real cancel, not a missing tool.
    return 1
  fi
  if command -v zenity  >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    _gs_ask_zenity  && return 0
    return 1
  fi
  if command -v kdialog >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    _gs_ask_kdialog && return 0
    return 1
  fi
  _gs_ask_tty
}

# Is $1 present in the space-separated list $2?
gs_picked() {
  case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

#!/usr/bin/env bash
# Alterna entre NARANJA y APAGADO. Guarda el estado en un archivo de marca.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/rgb-naranja.state"

current="off"
[ -f "$STATE_FILE" ] && current="$(cat "$STATE_FILE" 2>/dev/null)"

if [ "$current" = "on" ]; then
    "$HERE/rgb-off.sh"
    echo "off" > "$STATE_FILE"
    notify-send -a "RGB" "LEDs apagados" 2>/dev/null || true
else
    "$HERE/apply-naranja.sh"
    echo "on" > "$STATE_FILE"
    notify-send -a "RGB" "LEDs en naranja" 2>/dev/null || true
fi

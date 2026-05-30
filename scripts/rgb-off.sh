#!/usr/bin/env bash
# Apaga TODOS los LEDs del sistema (negro). El cooler requiere modo Gen1 para
# responder, igual que apply-naranja.sh.
set -u
OPENRGB=/usr/bin/openrgb
PY=/usr/bin/python3
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD="ASUS ROG STRIX X870-A GAMING WIFI"

# Header del cooler controlable por OpenRGB (revertir a Gen1).
"$PY" "$HERE/restore_gen1.py" >/dev/null 2>&1 || true
"$OPENRGB" --noautoconnect -d "$BOARD" -z 1 -sz 22 -z 2 -sz 22 -z 3 -sz 22 >/dev/null 2>&1 || true

# Apagar todo a negro.
"$OPENRGB" --noautoconnect --mode static --color 000000 >/dev/null 2>&1 || true
"$OPENRGB" --noautoconnect -d "$BOARD" -m direct -c 000000 >/dev/null 2>&1 || true

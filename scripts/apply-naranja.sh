#!/usr/bin/env bash
# Aplica NARANJA (FF8000) a todos los componentes RGB del sistema.
#
# Mecanismo (descubierto por debugging sistemático):
#   1. El cooler Deepcool AK620 cuelga del header ADD_GEN2_1 de la ASUS X870-A.
#   2. El header arranca en modo "Gen2" corriendo un rainbow por hardware que
#      OpenRGB NO controla (su código solo soporta el direccionable Gen1).
#   3. Forzar el controlador Aura a Gen1 (EC 3E 52 53 00) hace que el header
#      acepte el control estándar de OpenRGB (device "ASUS ROG STRIX X870-A",
#      zonas Aura Addressable 1/2/3 en modo Direct).
#   4. Las zonas se dimensionan a 30 LEDs para cubrir el anillo completo.
#
# Idempotente: se puede ejecutar en cada arranque.
set -u

OPENRGB=/usr/bin/openrgb
PY=/usr/bin/python3
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) Revertir el controlador Aura a Gen1 para que el header direccionable
#    (cooler) sea controlable por OpenRGB.
"$PY" "$HERE/restore_gen1.py" >/dev/null 2>&1 || true

# 2) Asegurar tamaño de zonas direccionables (anillo del cooler ~22-30 LEDs).
"$OPENRGB" --noautoconnect -d "ASUS ROG STRIX X870-A GAMING WIFI" \
    -z 1 -sz 30 -z 2 -sz 30 -z 3 -sz 30 >/dev/null 2>&1 || true

# 3) Cargar el perfil naranja (RAM, GPU, teclado, placa onboard).
"$OPENRGB" --noautoconnect --profile naranja >/dev/null 2>&1 || true

# 4) Forzar la placa (incl. headers direccionables del cooler) a Direct naranja.
"$OPENRGB" --noautoconnect -d "ASUS ROG STRIX X870-A GAMING WIFI" \
    -m direct -c FF8000 >/dev/null 2>&1 || true

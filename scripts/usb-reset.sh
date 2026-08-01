#!/usr/bin/env bash
# usb-reset.sh — Revive controladores xHCI colgados (bug AGESA AMD 800-series).
#
# Síntomas: puertos USB muertos, mouse/teclado/mic desaparecen, EC Aura (RGB)
# inaccesible, display AK620 off. El teclado en puerto BIOS Flashback sobrevive.
# Causa: el xHCI se cuelga ("HC died; cleaning up" en dmesg) y arrastra todo
# lo conectado a sus puertos, incluido el codec Realtek ALC1220 (audio jack).
#
# Mecanismo: unbind del driver xhci_hcd -> reset del device PCI -> rebind.
# Eso fuerza un "replug virtual" del controlador y los dispositivos reaparecen.
#
# Limitación conocida: el codec Realtek ALC1220 (audio analógico por jack) NO
# reenumera con este reset — necesita cold reset del SoC (reboot). El audio
# HDMI (GPU) y USB Audio sí se recuperan.
#
# Uso:
#   usb-reset.sh              # resetear todos los xHCI con síntomas
#   usb-reset.sh 09:00.0      # resetear un BDF específico
#   usb-reset.sh --dry-run    # mostrar qué haría sin tocar nada
#
# Requiere root (pkexec/sudo). No es destructivo: no toca datos ni config.
set -euo pipefail

DRIVER=xhci_hcd
DRIVER_PATH="/sys/bus/pci/drivers/${DRIVER}"
PCI_SYS="/sys/bus/pci/devices"
DRY_RUN=0
TARGET=""

c_red()    { printf '\033[31m%s\033[0m' "$1"; }
c_green()  { printf '\033[32m%s\033[0m' "$1"; }
c_yellow() { printf '\033[33m%s\033[0m' "$1"; }
c_bold()   { printf '\033[1m%s\033[0m' "$1"; }

log()  { printf '%s\n' "$*"; }
err()  { printf '%s %s\n' "$(c_red '[ERR]')" "$*" >&2; }
warn() { printf '%s %s\n' "$(c_yellow '[WARN]')" "$*"; }
ok()   { printf '%s %s\n' "$(c_green '[OK]')" "$*"; }

needs_root() {
  if [ "$(id -u)" -ne 0 ]; then
    return 0
  fi
  return 1
}

# Detecta xHCI controllers y filtra los que tienen el driver bound.
# Si se pasa un BDF, valida que exista y sea xHCI.
list_xhci() {
  local target="$1"
  if [ -n "$target" ]; then
    local dev="${PCI_SYS}/0000:${target}"
    if [ ! -d "$dev" ]; then
      err "Device 0000:${target} no existe en /sys/bus/pci/devices/"
      return 1
    fi
    local cls
    cls=$(cat "${dev}/class" 2>/dev/null)
    # 0c0330 = USB controller, prog-if 30 = XHCI
    if [[ "$cls" != "0c0330"* ]]; then
      err "0000:${target} no es xHCI (class=${cls})"
      return 1
    fi
    echo "0000:${target}"
    return 0
  fi
  # Auto-detectar todos los xHCI con driver bound
  for bdf_path in "${DRIVER_PATH}"/*; do
    [ -d "$bdf_path" ] || continue
    local bdf
    bdf=$(basename "$bdf_path")
    [[ "$bdf" =~ ^[0-9a-f]{4}:[0-9a-f]{2}\.[0-9a-f]$ ]] || continue
    echo "$bdf"
  done
}

# Cuenta dispositivos USB visibles antes/después
count_usb_devices() {
  local n=0
  while read -r _; do n=$((n+1)); done < <(lsusb 2>/dev/null | grep -v 'Linux Foundation root hub')
  echo "$n"
}

reset_controller() {
  local bdf="$1"
  local dev="${PCI_SYS}/${bdf}"

  log "$(c_bold "Reset ${bdf}")"

  # Verificar que el driver está bound
  local drv_link
  drv_link=$(readlink -f "${dev}/driver" 2>/dev/null || true)
  if [ -z "$drv_link" ] || [ "$(basename "$drv_link")" != "$DRIVER" ]; then
    warn "${bdf}: driver ${DRIVER} no está bound (drv=${drv_link:-none})"
    return 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "  [dry-run] unbind: echo ${bdf} > ${DRIVER_PATH}/unbind"
    log "  [dry-run] reset:  echo 1 > ${dev}/reset"
    log "  [dry-run] bind:   echo ${bdf} > ${DRIVER_PATH}/bind"
    return 0
  fi

  # 1) Unbind
  if ! echo "$bdf" > "${DRIVER_PATH}/unbind" 2>/dev/null; then
    err "${bdf}: fallo unbind"
    return 1
  fi
  ok "${bdf}: unbind"

  # 2) Reset PCI device
  sleep 1
  if ! echo 1 > "${dev}/reset" 2>/dev/null; then
    err "${bdf}: fallo reset (probablemente hardware no responde)"
    # Intentar rebind para no dejarlo sin driver
    echo "$bdf" > "${DRIVER_PATH}/bind" 2>/dev/null || true
    return 1
  fi
  ok "${bdf}: reset"

  # 3) Rebind
  sleep 1
  if ! echo "$bdf" > "${DRIVER_PATH}/bind" 2>/dev/null; then
    err "${bdf}: fallo rebind — controller puede quedar sin driver"
    warn "Recovery manual: echo ${bdf} > ${DRIVER_PATH}/bind"
    return 1
  fi
  ok "${bdf}: rebind"

  # 4) Esperar a que udev enumere dispositivos
  sleep 3
  return 0
}

usage() {
  cat <<EOF
$(c_bold "usb-reset.sh") — Revive xHCI colgados (unbind/reset/rebind PCI)

$(c_bold "Uso:")
  usb-reset.sh [BDF] [--dry-run] [--help]

$(c_bold "Opciones:")
  BDF         Dirección PCI del controller (ej: 09:00.0). Si se omite,
              detecta y resetea TODOS los xHCI con driver bound.
  --dry-run   Mostrar qué haría sin ejecutar nada.
  --help      Esta ayuda.

$(c_bold "Síntomas que resuelve:")
  - Mouse/teclado USB desaparecen de lsusb
  - EC Aura (RGB) inaccesible (hidraw desaparece)
  - Micrófono USB o display AK620 off
  - dmesg: "xHCI host controller not responding, assume dead"

$(c_bold "No resuelve:")
  - Codec Realtek ALC1220 (audio jack) — necesita reboot
  - Hardware físicamente dañado

$(c_bold "Requiere:") root (ejecutar con sudo o pkexec)
EOF
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --help|-h) usage; exit 0 ;;
      *) TARGET="$1"; shift ;;
    esac
  done

  if needs_root; then
    err "Se necesita root. Usar: sudo $0 $* o pkexec $0 $*"
    exit 1
  fi

  local before
  before=$(count_usb_devices)
  log "Dispositivos USB visibles antes: $(c_bold "$before")"

  local controllers
  controllers=$(list_xhci "$TARGET") || exit 1

  if [ -z "$controllers" ]; then
    warn "No se encontraron controllers xHCI con driver bound."
    exit 0
  fi

  log "Controllers xHCI a procesar:"
  while read -r c; do log "  - $c"; done <<< "$controllers"
  log ""

  local rc=0
  while read -r bdf; do
    [ -z "$bdf" ] && continue
    reset_controller "$bdf" || rc=1
    log ""
  done <<< "$controllers"

  local after
  after=$(count_usb_devices)
  log "Dispositivos USB visibles después: $(c_bold "$after")"

  if [ "$after" -gt "$before" ]; then
    ok "Recuperados $((after - before)) dispositivo(s)."
  elif [ "$after" -eq "$before" ]; then
    warn "Sin cambios en el conteo de dispositivos."
  else
    warn "Dispositivos disminuyeron ($before -> $after). Revisar dmesg."
  fi

  # Verificar audio analógico (codec Realtek)
  log ""
  if grep -q 'Realtek' /proc/asound/card*/codec* 2>/dev/null; then
    ok "Codec Realtek detectado — audio analógico disponible."
  else
    warn "Codec Realtek NO detectado — audio jack requiere reboot."
    warn "Audio HDMI/USB debería funcionar."
  fi

  exit $rc
}

main "$@"
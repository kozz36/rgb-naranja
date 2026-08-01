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
# Una vez que el kernel marca el HC como muerto, ni usbreset(1) ni togglear
# sysfs `authorized` sirven de nada: el estado corrupto vive en el driver.
#
# Limitación conocida: el codec Realtek ALC1220 (audio analógico por jack) NO
# reenumera con este reset — necesita cold reset del SoC (reboot). El audio
# HDMI (GPU) y USB Audio sí se recuperan.
#
# Antes de tocar el driver comprueba cuatro condiciones que harían peligroso el
# reset: almacenamiento montado, perder todos los teclados, un worker del kernel
# aún bloqueado en la ruta de reset USB, y resetear un controlador vivo.
#
# Uso:
#   usb-reset.sh              # resetear solo los xHCI realmente muertos
#   usb-reset.sh 09:00.0      # resetear un BDF específico
#   usb-reset.sh --all        # resetear TODOS los xHCI (comportamiento antiguo)
#   usb-reset.sh --list       # inventario: buses, dispositivos, estado
#   usb-reset.sh --dry-run    # mostrar qué haría sin tocar nada
#
# Escala solo con pkexec. No es destructivo: no toca datos ni configuración.
#
# Nota: `set -uo pipefail` SIN `-e` a propósito. Los incrementos aritméticos
# tipo ((n++)) devuelven estado != 0 cuando n valía 0, y con -e abortarían el
# script en mitad de un conteo. Los errores se comprueban explícitamente.
set -uo pipefail

SELF=$(readlink -f "$0")
DRIVER=xhci_hcd
DRIVER_PATH="/sys/bus/pci/drivers/${DRIVER}"
PCI_SYS="/sys/bus/pci/devices"

DRY_RUN=0
LIST_ONLY=0
ALL=0
FORCE=0
PAUSE=0
TARGET=""
BLOCKERS=0

c_red()    { printf '\033[31m%s\033[0m' "$1"; }
c_green()  { printf '\033[32m%s\033[0m' "$1"; }
c_yellow() { printf '\033[33m%s\033[0m' "$1"; }
c_bold()   { printf '\033[1m%s\033[0m' "$1"; }

log()  { printf '%s\n' "$*"; }
err()  { printf '%s %s\n' "$(c_red '[ERR]')" "$*" >&2; }
warn() { printf '%s %s\n' "$(c_yellow '[WARN]')" "$*"; }
ok()   { printf '%s %s\n' "$(c_green '[OK]')" "$*"; }
info() { printf '%s %s\n' "$(c_bold '[..]')" "$*"; }

usage() {
  cat <<EOF
$(c_bold "usb-reset.sh") — Revive xHCI colgados (unbind/reset PCI/rebind)

$(c_bold "Uso:")
  usb-reset.sh [BDF] [--all] [--list] [--dry-run] [--force] [--pause] [--help]

$(c_bold "Opciones:")
  BDF         Dirección PCI del controller (ej: 09:00.0 o 0000:09:00.0).
  --all       Resetear TODOS los xHCI con driver bound. Peligroso: incluye el
              controlador del teclado. Sin esta opción y sin BDF, solo se tocan
              los controladores que el kernel ha marcado como muertos.
  --list      Inventario de controladores: buses, dispositivos, estado.
  --dry-run   Mostrar qué haría sin ejecutar nada.
  --force     Ignorar los bloqueos de preflight (se siguen imprimiendo).
  --pause     Mantener la terminal abierta al salir (lanzador .desktop).
  --help      Esta ayuda.

$(c_bold "Preflight — se niega a actuar si:")
  - hay un dispositivo de bloques montado colgando del controlador
  - se perderían todos los teclados (sin forma de intervenir si falla el bind)
  - un worker del kernel sigue bloqueado en la ruta de reset USB (el unbind
    haría deadlock sobre el mismo device lock)
  - el controlador está vivo y poblado (el reset sobra)

$(c_bold "Síntomas que resuelve:")
  - Mouse/teclado USB desaparecen de lsusb
  - EC Aura (RGB) inaccesible (hidraw desaparece)
  - Micrófono USB o display AK620 off
  - dmesg: "xHCI host controller not responding, assume dead"

$(c_bold "No resuelve:")
  - Codec Realtek ALC1220 (audio jack) — necesita reboot
  - Hardware físicamente dañado
EOF
}

# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------

# Normaliza 09:00.0 -> 0000:09:00.0
normalize_bdf() {
  case "$1" in
    *:*:*.*) printf '%s' "$1" ;;
    *)       printf '0000:%s' "$1" ;;
  esac
}

# Lista los xHCI con driver bound. Con BDF, valida que exista y sea xHCI.
list_xhci() {
  local target="$1" bdf bdf_path dev cls
  if [ -n "$target" ]; then
    dev="${PCI_SYS}/${target}"
    if [ ! -d "$dev" ]; then
      err "Device ${target} no existe en ${PCI_SYS}/"
      return 1
    fi
    cls=$(cat "${dev}/class" 2>/dev/null)
    # 0c0330 = USB controller, prog-if 30 = XHCI
    if [[ "$cls" != *"0c0330"* ]]; then
      err "${target} no es xHCI (class=${cls})"
      return 1
    fi
    printf '%s\n' "$target"
    return 0
  fi
  for bdf_path in "${DRIVER_PATH}"/*; do
    [ -d "$bdf_path" ] || continue
    bdf=$(basename "$bdf_path")
    [[ "$bdf" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$ ]] || continue
    printf '%s\n' "$bdf"
  done
}

# Root hubs (usbN) que cuelgan de este BDF.
buses_of() {
  local dev=$1 b
  for b in /sys/bus/usb/devices/usb*; do
    [ -e "$b" ] || continue
    [[ $(readlink -f "$b") == */"$dev"/* ]] && basename "$b"
  done
}

# Dispositivos USB reales (sin root hubs) colgando de este BDF.
device_count() {
  local dev=$1 d n=0
  for d in /sys/bus/usb/devices/*-*; do
    [ -e "$d/idVendor" ] || continue
    [[ $(readlink -f "$d") == */"$dev"/* ]] && n=$((n+1))
  done
  printf '%d' "$n"
}

# Total de dispositivos USB visibles, excluyendo root hubs.
# Los root hubs son siempre idVendor 1d6b (Linux Foundation). Filtrar por texto
# no vale: lsusb los imprime como "Linux Foundation 2.0 root hub", nunca como
# "Linux Foundation root hub", así que ese patrón no casaba con nada.
count_usb_devices() {
  lsusb 2>/dev/null | grep -vc 'ID 1d6b:'
}

# Estado real del controlador. Contar "HC died" no sirve: ese mensaje se queda
# en el log del arranque para siempre, así que un controlador ya recuperado por
# un rebind anterior seguiría leyéndose como muerto. El kernel emite
# "new USB bus registered" en cada bind con éxito, de modo que el estado real lo
# da cuál de los dos marcadores aparece EL ÚLTIMO en orden cronológico.
hc_state() {
  local last
  last=$(journalctl -k -b --no-pager 2>/dev/null | grep -F "$1" \
    | grep -E 'HC died|new USB bus registered' | tail -1)
  if [[ "$last" == *"HC died"* ]]; then printf 'muerto'; else printf 'vivo'; fi
}

pretty_name() {
  local n
  n=$(lspci -s "$1" 2>/dev/null | cut -d: -f3- | sed -e 's/^ *//' -e 's/ (rev [0-9a-f]*)$//')
  printf '%s' "${n:-$1}"
}

do_list() {
  local c
  printf '%-14s %-10s %-7s %-7s %s\n' BDF BUSES DISPOS ESTADO CONTROLADOR
  while read -r c; do
    [ -n "$c" ] || continue
    printf '%-14s %-10s %-7s %-7s %s\n' \
      "$c" \
      "$(buses_of "$c" | paste -sd, -)" \
      "$(device_count "$c")" \
      "$(hc_state "$c")" \
      "$(pretty_name "$c")"
  done < <(list_xhci "")
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

block() { warn "$*"; BLOCKERS=$((BLOCKERS+1)); }

preflight() {
  local bdf=$1 estado=$2 dispos=$3
  local blk name node mnt k nodo nombre p i
  local kbd_aqui=0 kbd_fuera=0
  BLOCKERS=0

  # 1. Almacenamiento montado: arrancarlo en pleno write pierde datos.
  for blk in /sys/block/*; do
    [ -e "$blk" ] || continue
    [[ $(readlink -f "$blk") == */"$bdf"/* ]] || continue
    name=$(basename "$blk")
    for node in "/dev/$name" /dev/"$name"[0-9]*; do
      [ -b "$node" ] || continue
      mnt=$(findmnt -rno TARGET "$node" 2>/dev/null)
      [ -n "$mnt" ] && block "$node está montado en $mnt — desmontar antes de resetear"
    done
  done

  # 2. Quedarse sin teclado deja sin forma de intervenir si falla el bind.
  #    udev crea alias -usb- y -usbv2- apuntando al mismo nodo event, así que
  #    hay que deduplicar por destino o todo se cuenta dos veces.
  local -A vistos=()
  for k in /dev/input/by-path/*-event-kbd; do
    [ -e "$k" ] || continue
    nodo=$(readlink -f "$k")
    [ -n "${vistos[$nodo]:-}" ] && continue
    vistos[$nodo]=1
    if [[ $(basename "$k") == *"$bdf"* ]]; then
      kbd_aqui=$((kbd_aqui+1))
    else
      kbd_fuera=$((kbd_fuera+1))
    fi
  done
  if [ "$kbd_aqui" -gt 0 ] && [ "$kbd_fuera" -eq 0 ]; then
    block "todos los teclados ($kbd_aqui) están en $bdf — te quedarías sin entrada si falla el bind"
  fi

  # 3. Un worker aún bloqueado en la ruta de reset USB haría deadlock el unbind.
  if [ "$(id -u)" -eq 0 ]; then
    for p in /proc/[0-9]*; do
      [ -r "$p/stat" ] || continue
      [ "$(awk '{print $3}' "$p/stat" 2>/dev/null)" = "D" ] || continue
      if grep -qE 'usb_reset|hub_port_init|xhci_setup_device' "$p/stack" 2>/dev/null; then
        block "pid ${p#/proc/} ($(cat "$p/comm" 2>/dev/null)) bloqueado en la ruta de reset USB — el unbind colgaría"
      fi
    done
  else
    info "comprobación de workers bloqueados aplazada (necesita root; corre tras pkexec)"
  fi

  # 4. Resetear un controlador vivo y poblado tira dispositivos que funcionan.
  if [ "$estado" = "vivo" ] && [ "$dispos" -gt 0 ]; then
    block "$bdf está vivo con $dispos dispositivos — el reset probablemente sobra"
  fi

  # Informar siempre de lo que se va a perder, aunque nada bloquee: "hay otro
  # teclado" puede ser la interfaz HID-keyboard de un ratón, no algo usable.
  local -a caen=()
  local -A vistos_in=()
  for i in /dev/input/by-path/*"$bdf"*-event*; do
    [ -e "$i" ] || continue
    nodo=$(readlink -f "$i")
    nombre=$(cat "/sys/class/input/$(basename "$nodo")/device/name" 2>/dev/null) || nombre=$(basename "$nodo")
    [ -n "${vistos_in[$nombre]:-}" ] && continue
    vistos_in[$nombre]=1
    caen+=("$nombre")
  done
  [ "${#caen[@]}" -gt 0 ] && info "dispositivos de entrada que caerán: $(IFS=','; echo "${caen[*]}")"

  [ "$BLOCKERS" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

reset_controller() {
  local bdf="$1"
  local dev="${PCI_SYS}/${bdf}"
  local drv_link estado dispos buses despues errs i

  log "$(c_bold "Reset ${bdf}") — $(pretty_name "$bdf")"

  drv_link=$(readlink -f "${dev}/driver" 2>/dev/null || true)
  if [ -z "$drv_link" ] || [ "$(basename "$drv_link")" != "$DRIVER" ]; then
    warn "${bdf}: driver ${DRIVER} no está bound (drv=${drv_link:-ninguno})"
    return 1
  fi

  estado=$(hc_state "$bdf")
  dispos=$(device_count "$bdf")
  buses=$(buses_of "$bdf" | paste -sd, -)
  info "buses: ${buses:-ninguno}   dispositivos: ${dispos}   estado: ${estado}"

  if preflight "$bdf" "$estado" "$dispos"; then
    ok "${bdf}: preflight limpio"
  elif [ "$FORCE" -eq 1 ]; then
    warn "${BLOCKERS} bloqueo(s) ignorados por --force"
  else
    err "${bdf}: ${BLOCKERS} bloqueo(s) — omitido. Usar --force si es intencionado."
    return 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "  [dry-run] unbind: echo ${bdf} > ${DRIVER_PATH}/unbind"
    log "  [dry-run] reset:  echo 1 > ${dev}/reset"
    log "  [dry-run] bind:   echo ${bdf} > ${DRIVER_PATH}/bind"
    return 0
  fi

  logger -t usb-reset "reset ${bdf} (dispositivos antes: ${dispos})" 2>/dev/null || true

  # 1) Unbind
  if ! echo "$bdf" > "${DRIVER_PATH}/unbind" 2>/dev/null; then
    err "${bdf}: fallo unbind"
    return 1
  fi
  ok "${bdf}: unbind"

  # 2) Reset del device PCI. Si falla, rebind para no dejarlo sin driver.
  sleep 1
  if [ -e "${dev}/reset" ]; then
    if ! echo 1 > "${dev}/reset" 2>/dev/null; then
      err "${bdf}: fallo reset (probablemente el hardware no responde)"
      echo "$bdf" > "${DRIVER_PATH}/bind" 2>/dev/null || true
      return 1
    fi
    ok "${bdf}: reset PCI"
  else
    warn "${bdf}: sin nodo reset en sysfs, se salta el reset PCI"
  fi

  # 3) Rebind
  sleep 1
  if ! echo "$bdf" > "${DRIVER_PATH}/bind" 2>/dev/null; then
    err "${bdf}: fallo rebind — el controlador puede quedar sin driver"
    warn "Recovery manual: echo ${bdf} > ${DRIVER_PATH}/bind"
    warn "Alternativa:     echo 1 > ${dev}/remove && echo 1 > /sys/bus/pci/rescan"
    return 1
  fi
  ok "${bdf}: rebind"

  # 4) Esperar a que udev reenumere, sondeando en vez de confiar en un sleep fijo.
  despues=0
  for i in $(seq 1 15); do
    sleep 1
    despues=$(device_count "$bdf")
    [ "$despues" -gt 0 ] && [ "$despues" -ge "$dispos" ] && break
  done

  # Solo cuentan los fallos a nivel de controlador. Los "cannot submit urb,
  # error -19" (ENODEV) de snd-usb-audio son esperados: son las URBs en vuelo
  # que quedaron colgando al desaparecer el dispositivo durante el unbind.
  errs=$(journalctl -k --no-pager --since '-1min' 2>/dev/null \
    | grep -F "$bdf" | grep -ciE 'HC died|not responding|command ring')
  errs=${errs:-0}

  logger -t usb-reset "reset ${bdf} completado (después: ${despues}, errores: ${errs})" 2>/dev/null || true

  if [ "$despues" -eq 0 ]; then
    warn "${bdf}: no reenumeró ningún dispositivo"
    warn "Fallback: echo 1 > ${dev}/remove && echo 1 > /sys/bus/pci/rescan"
    return 1
  fi
  if [ "$errs" -gt 0 ]; then
    warn "${bdf}: reenumeró ${despues} dispositivo(s) pero registró ${errs} error(es) nuevo(s)"
    warn "Revisar: journalctl -k -b | grep ${bdf}"
    return 1
  fi

  ok "${bdf}: recuperado — ${despues} dispositivo(s), sin errores nuevos"
  return 0
}

# ---------------------------------------------------------------------------

main() {
  local controllers c before after rc=0
  local -a args=()

  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run|-n) DRY_RUN=1; shift ;;
      --list|-l)    LIST_ONLY=1; shift ;;
      --all)        ALL=1; shift ;;
      --force|-f)   FORCE=1; shift ;;
      --pause|-p)   PAUSE=1; shift ;;
      --help|-h)    usage; exit 0 ;;
      -*)           err "opción desconocida: $1"; exit 1 ;;
      *)            TARGET=$(normalize_bdf "$1"); shift ;;
    esac
  done

  # Lanzado desde un .desktop la terminal se cierra al salir, así que hay que
  # sostenerla. bash no ejecuta el trap EXIT a través de exec(), de modo que la
  # reescalada por pkexec repasa --pause y la instancia root se queda el prompt.
  [ "$PAUSE" -eq 1 ] && trap 'echo; read -rp "Pulsa Enter para cerrar… " _' EXIT

  if [ ! -d "$DRIVER_PATH" ]; then
    err "El driver ${DRIVER} no tiene nada bound (¿módulo cargado?)"
    exit 1
  fi

  if [ "$LIST_ONLY" -eq 1 ]; then
    do_list
    exit 0
  fi

  # Selección de objetivos
  if [ -n "$TARGET" ]; then
    controllers=$(list_xhci "$TARGET") || exit 1
  elif [ "$ALL" -eq 1 ]; then
    controllers=$(list_xhci "")
    warn "--all: se procesarán TODOS los xHCI, incluido el del teclado"
  else
    controllers=$(while read -r c; do
      [ -n "$c" ] && [ "$(hc_state "$c")" = "muerto" ] && printf '%s\n' "$c"
    done < <(list_xhci ""))
    if [ -z "$controllers" ]; then
      log "No hay ningún controlador xHCI marcado como muerto."
      log "Usar $(c_bold '--list') para el inventario, un BDF explícito, o $(c_bold '--all')."
      exit 0
    fi
    info "auto-detectado(s) como muerto(s): $(echo "$controllers" | paste -sd' ' -)"
  fi

  # Escalada de privilegios DESPUÉS de elegir objetivos, para poder informar de
  # que no hay nada que hacer sin llegar a pedir contraseña.
  if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    info "escalando con pkexec…"
    [ "$FORCE" -eq 1 ] && args+=(--force)
    [ "$PAUSE" -eq 1 ] && args+=(--pause)
    [ "$ALL" -eq 1 ] && args+=(--all)
    [ -n "$TARGET" ] && args+=("$TARGET")
    exec pkexec "$SELF" "${args[@]}"
  fi

  before=$(count_usb_devices)
  log "Dispositivos USB visibles antes: $(c_bold "$before")"
  log ""

  while read -r c; do
    [ -z "$c" ] && continue
    reset_controller "$c" || rc=1
    log ""
  done <<< "$controllers"

  after=$(count_usb_devices)
  log "Dispositivos USB visibles después: $(c_bold "$after")"

  if [ "$after" -gt "$before" ]; then
    ok "Recuperados $((after - before)) dispositivo(s)."
  elif [ "$after" -eq "$before" ]; then
    log "Conteo de dispositivos sin cambios."
  else
    warn "Dispositivos disminuyeron ($before -> $after). Revisar journalctl -k."
  fi

  # Verificar audio analógico (codec Realtek)
  log ""
  if grep -q 'Realtek' /proc/asound/card*/codec* 2>/dev/null; then
    ok "Codec Realtek detectado — audio analógico disponible."
  else
    warn "Codec Realtek NO detectado — audio jack requiere reboot."
    warn "Audio HDMI/USB debería funcionar."
  fi

  exit "$rc"
}

main "$@"

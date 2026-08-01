#!/usr/bin/env bash
# Alterna el display LCD del Deepcool AK620 Digital PRO entre ENCENDIDO y APAGADO.
#
# Mecanismo (verificado empíricamente):
#   El display es renderizado por firmware desde valores numéricos (no hay
#   framebuffer accesible por el host), así que NO existe un "frame en negro".
#   Sin embargo, el firmware tiene un watchdog: si deja de recibir frames HID
#   durante ~pocos segundos, apaga el LCD. Por tanto:
#       off = detener deepcool-digital.service  -> pantalla en negro
#       on  = arrancar  deepcool-digital.service -> vuelve a mostrar temps
#
#   El servicio es SYSTEM, no de usuario. La regla polkit
#   /etc/polkit-1/rules.d/49-deepcool-digital.rules autoriza a este usuario a
#   gestionar SOLO esa unidad sin contraseña, para que el botón no pida clave.
#
# Fuente de verdad = estado de systemd (systemctl is-active). No usa archivo de
# marca: evita desincronización si el servicio muere/arranca por otra vía.
set -u

UNIT="deepcool-digital.service"

if systemctl is-active --quiet "$UNIT"; then
    systemctl stop "$UNIT"
    notify-send -a "LCD" "Display AK620 apagado" 2>/dev/null || true
else
    # El override de la unidad acota los reintentos
    # (StartLimitBurst=3 / StartLimitIntervalSec=120) para que una racha de
    # panics del daemon no derive en la tormenta de resets que mató al xHCI
    # 0000:09:00.0 el 2026-07-31. Efecto colateral: tras agotar el límite la
    # unidad queda en 'failed' y systemd se niega a arrancarla hasta limpiar
    # el contador, así que hay que hacerlo antes de encender.
    systemctl reset-failed "$UNIT" 2>/dev/null || true

    # Comprobar el resultado en lugar de asumirlo: 'start' puede devolver 0 y
    # el daemon morir un segundo después si el enlace HID sigue caído.
    if systemctl start "$UNIT" 2>/dev/null && systemctl is-active --quiet "$UNIT"; then
        notify-send -a "LCD" "Display AK620 encendido" 2>/dev/null || true
    else
        notify-send -a "LCD" -u critical "No se pudo encender el display AK620" \
            "El enlace HID sigue fallando. Revisar: journalctl -u $UNIT -n 20" 2>/dev/null || true
        exit 1
    fi
fi

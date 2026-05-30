# RGB Naranja — CachyOS / ASUS X870-A

Configuración para mantener **todos los componentes RGB en naranja fijo (`#FF8000`)**.

## Hardware

| Componente | Control | Detalle |
|---|---|---|
| RAM ENE DRAM x2 | OpenRGB (i2c) | bus `/dev/i2c-8` |
| GPU ASUS TUF RTX 5070 Ti | OpenRGB (i2c NVIDIA) | requiere `openrgb-git` (soporte Blackwell) |
| Teclado ASUS TUF K3 | OpenRGB (HID) | `/dev/hidraw` |
| Placa ASUS ROG STRIX X870-A (onboard) | OpenRGB (HID) | controlador Aura USB `0b05:19af` |
| Cooler Deepcool AK620 Digital PRO (anillo ARGB) | OpenRGB vía truco Gen1 | header `ADD_GEN2_1`, ver abajo |
| Cooler AK620 (pantalla temp) | `deepcool-digital-linux` | servicio aparte, opcional |

## El problema del cooler (causa raíz)

El anillo del AK620 cuelga del header **ADD_GEN2_1** de la placa. Ese header
arranca en modo **Gen2**, donde corre un efecto rainbow por hardware. OpenRGB
**no implementa** el protocolo direccionable Gen2 (enumeración con
`EC 3E`/`EC 82`/`EC 31`/`EC 3D`), así que no lo controla y el anillo se queda
en rainbow.

**Solución:** forzar el controlador Aura de vuelta a **Gen1**
(`EC 3E 52 53 00`). En Gen1 el header direccionable responde al control
estándar de OpenRGB (modo Direct sobre las zonas `Aura Addressable`). La zona
se dimensiona a **22 LEDs**, el conteo real del anillo (confirmado por el
firmware vía `GetSlotInfo`=`0x16` y verificado visualmente con un patrón de
colores — ver `scripts/count_leds.py` y `count_leds_fine.py`).

> Nota: este truco evita tener que parchear OpenRGB o hacer ingeniería inversa
> del protocolo Gen2. El parche nativo (Fase 3/4) queda pendiente como mejora.

## Uso

```bash
# Aplicar naranja a todo (idempotente)
./scripts/apply-naranja.sh
```

## Persistencia (arranque automático)

Servicio systemd de usuario en
`~/.config/systemd/user/openrgb-naranja.service` que ejecuta
`~/.local/bin/rgb-naranja.sh` (copia de `apply-naranja.sh`) al iniciar sesión.

```bash
systemctl --user status  openrgb-naranja.service
systemctl --user restart openrgb-naranja.service   # reaplicar ahora
```

## Scripts

| Script | Función |
|---|---|
| `apply-naranja.sh` | Secuencia completa: Gen1 + resize + naranja |
| `restore_gen1.py` | Revierte el controlador Aura a Gen1 |
| `gen2_discover2.py` | Diagnóstico: enumera la topología Gen2 (canal/LEDs del cooler) |
| `orgb_read_config.py` | Lee el `config_table` del controlador Aura |

## Perfil

`naranja.orp` — perfil OpenRGB con los 5 dispositivos en naranja.
Copiarlo a `~/.config/OpenRGB/` si se reinstala.

## Requisitos

- `openrgb-git` (AUR/CachyOS repo) — el estable no detecta la RTX 5070 Ti.
- Acceso a `/dev/hidraw9` e `/dev/i2c-*` vía reglas udev de OpenRGB (incluidas
  en el paquete: `/usr/lib/udev/rules.d/60-openrgb.rules`).

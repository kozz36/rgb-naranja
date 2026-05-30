#!/usr/bin/env python3
"""Test de conteo de LEDs del anillo del cooler (header direccionable, modo Gen1).
Pinta un degradado por POSICIÓN: LED0=rojo, LED en pasos de 5 marca color distinto,
para contar visualmente cuántos LEDs físicos hay y dónde termina el anillo.
  - LED 0      -> ROJO
  - LEDs 1-4   -> NARANJA
  - LED 5      -> VERDE
  - LEDs 6-9   -> NARANJA
  - LED 10     -> AZUL
  - LEDs 11-14 -> NARANJA
  - LED 15     -> MAGENTA
  - LEDs 16-19 -> NARANJA
  - LED 20     -> CYAN
  - LEDs 21+   -> BLANCO  (si ves blanco, hay más de 21 LEDs)
Cuenta los segmentos para saber el total real."""
import subprocess, sys

DEV_NAME = "ASUS ROG STRIX X870-A GAMING WIFI"
MAXLED = 32

def hexc(r, g, b): return f"{r:02X}{g:02X}{b:02X}"

NAR = (0xFF, 0x80, 0x00)
MARKS = {0:(0xFF,0,0), 5:(0,0xFF,0), 10:(0,0,0xFF),
         15:(0xFF,0,0xFF), 20:(0,0xFF,0xFF)}

def main():
    zone = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    colors = []
    for i in range(MAXLED):
        if i in MARKS: colors.append(MARKS[i])
        elif i >= 21:  colors.append((0xFF,0xFF,0xFF))  # blanco = sobrante
        else:          colors.append(NAR)
    arg = ",".join(hexc(*c) for c in colors)
    # asegurar tamaño de zona
    subprocess.run(["openrgb","--noautoconnect","-d",DEV_NAME,
                    "-z",str(zone),"-sz",str(MAXLED)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["openrgb","--noautoconnect","-d",DEV_NAME,
                    "-z",str(zone),"-m","direct","-c",arg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Zona {zone}: patrón de conteo aplicado.")
    print("Marcas: LED0=ROJO LED5=VERDE LED10=AZUL LED15=MAGENTA LED20=CYAN")
    print("LEDs 21+ = BLANCO (si ves blanco, hay >21). Cuenta hasta dónde llega el color.")

if __name__ == "__main__":
    main()

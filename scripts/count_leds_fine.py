#!/usr/bin/env python3
"""Conteo FINO de la cola del anillo. LEDs 0-20 en naranja tenue; del 21 al 31
cada uno un color distinto y vivo para identificar el ÚLTIMO LED encendido.
  21=ROJO 22=VERDE 23=AZUL 24=MAGENTA 25=CYAN 26=AMARILLO
  27=BLANCO 28=ROSA 29=MORADO 30=LIMA 31=NARANJA-vivo
El último color que VEAS encendido => total = ese índice + 1."""
import subprocess, sys

DEV_NAME = "ASUS ROG STRIX X870-A GAMING WIFI"
MAXLED = 32
TAIL = {
    21:(0xFF,0,0), 22:(0,0xFF,0), 23:(0,0,0xFF), 24:(0xFF,0,0xFF),
    25:(0,0xFF,0xFF), 26:(0xFF,0xFF,0), 27:(0xFF,0xFF,0xFF),
    28:(0xFF,0,0x80), 29:(0x80,0,0xFF), 30:(0x80,0xFF,0), 31:(0xFF,0x40,0),
}
DIM = (0x20, 0x10, 0x00)  # naranja tenue para 0-20

def hexc(c): return f"{c[0]:02X}{c[1]:02X}{c[2]:02X}"

def main():
    zone = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    colors = [TAIL.get(i, DIM) for i in range(MAXLED)]
    arg = ",".join(hexc(c) for c in colors)
    subprocess.run(["openrgb","--noautoconnect","-d",DEV_NAME,"-z",str(zone),
                    "-sz",str(MAXLED)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["openrgb","--noautoconnect","-d",DEV_NAME,"-z",str(zone),
                    "-m","direct","-c",arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Cola marcada. 21=ROJO 22=VERDE 23=AZUL 24=MAGENTA 25=CYAN 26=AMARILLO 27=BLANCO 28=ROSA 29=MORADO 30=LIMA 31=NARANJA")
    print("El ÚLTIMO color vivo encendido => total LEDs = índice + 1.")

if __name__ == "__main__":
    main()

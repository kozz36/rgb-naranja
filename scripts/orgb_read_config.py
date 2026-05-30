#!/usr/bin/env python3
"""Lee el config_table del controlador ASUS Aura USB (PID 0x19AF) en /dev/hidraw9.
Replica la query 0xEC 0xB0 (AURA_REQUEST_CONFIG_TABLE) de OpenRGB. Solo lectura."""
import os, select, sys

DEV = "/dev/hidraw9"
PKT = 65

def send(fd, payload):
    buf = bytes(payload) + bytes(PKT - len(payload))
    os.write(fd, buf)

def read_resp(fd, timeout=0.6):
    r, _, _ = select.select([fd], [], [], timeout)
    return os.read(fd, PKT) if r else None

def main():
    fd = os.open(DEV, os.O_RDWR)
    try:
        send(fd, [0xEC, 0xB0])          # AURA_REQUEST_CONFIG_TABLE
        responses = []
        table = None
        for _ in range(25):
            resp = read_resp(fd)
            if resp is None:
                continue
            responses.append(resp)
            if len(resp) >= 2 and resp[1] == 0x30:   # config-table response marker
                table = resp
                break
    finally:
        os.close(fd)

    print(f"respuestas recibidas: {len(responses)}")
    for i, r in enumerate(responses):
        print(f"  resp[{i}] (len {len(r)}): {r.hex()}")
    print()
    if table is None:
        print(">>> NO se recibió respuesta 0x30. Probando interpretar la última respuesta igual.")
        if not responses:
            sys.exit("Sin respuesta alguna del controlador.")
        table = responses[-1]

    ct = table[4:64]   # OpenRGB: config_table[0..59] = resp[4..63]
    print("config_table[0x00..0x3B]:")
    for i in range(0, len(ct), 16):
        chunk = ct[i:i+16]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        print(f"  0x{i:02X}: {hexs}")
    print()
    def g(idx):
        return ct[idx] if idx < len(ct) else None
    print(f"  [0x02] num_addressable_headers = {g(0x02)}")
    print(f"  [0x1B] num_total_mainboard_leds = {g(0x1B)}")
    print(f"  [0x1D] num_rgb_headers          = {g(0x1D)}")
    print(f"  zona 0x08..0x12 (posible mapeo/LED count por canal): "
          + " ".join(f"{g(i):02X}" if g(i) is not None else "--" for i in range(0x08, 0x13)))

if __name__ == "__main__":
    main()

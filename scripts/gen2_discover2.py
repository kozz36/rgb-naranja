#!/usr/bin/env python3
"""Discovery Gen2 v2: lee la respuesta DESPUES del ack (B2/B1/BD) y con varios
intentos. Para el controlador ASUS Aura X870-A (/dev/hidraw9)."""
import os, select, time

DEV = "/dev/hidraw9"; PKT = 65

def w(fd, b):
    os.write(fd, bytes(b) + bytes(PKT - len(b))); time.sleep(0.02)

def reads(fd, n=3, t=0.35):
    out = []
    for _ in range(n):
        r, _, _ = select.select([fd], [], [], t)
        if not r: break
        out.append(os.read(fd, PKT))
    return out

def show(label, resps):
    if not resps:
        print(f"  {label:<20} -> (sin respuesta)")
    for r in resps:
        print(f"  {label:<20} -> {r.hex()}")

def q_ack(fd, payload, ack, label):
    """query -> ack -> read (orden corregido)"""
    w(fd, payload)
    w(fd, ack)
    show(label, reads(fd))

def main():
    fd = os.open(DEV, os.O_RDWR)
    try:
        w(fd, [0xEC, 0x3F, 0xAA])
        w(fd, [0xEC, 0x3E, 0x52, 0x53, 0x01])
        w(fd, [0xEC, 0x3F, 0x55])
        time.sleep(0.15)
        w(fd, [0xEC, 0x82]); time.sleep(0.6)
        print("== drain post-rescan ==")
        show("scan", reads(fd, 12, 0.25))

        print("== GetChannelInfo 0x32 (query->B2->read) ==")
        for ch in [0x00, 0x01, 0x02, 0x10, 0x11, 0x12, 0x13]:
            q_ack(fd, [0xEC, 0x32, ch], [0xEC, 0xB2], f"32 ch=0x{ch:02X}")

        print("== GetSlotInfo 0x31 (query->B1->read) ==")
        for s in [0x00,0x01,0x02,0x03,0x04,0x10,0x11,0x12,0x13]:
            q_ack(fd, [0xEC, 0x31, s], [0xEC, 0xB1], f"31 slot=0x{s:02X}")

        print("== QueryDeviceMapping 0x3D (query->BD->read) ==")
        for did in [1, 2, 3, 4]:
            q_ack(fd, [0xEC, 0x3D, 0x00, did], [0xEC, 0xBD], f"3D dev={did}")
    finally:
        os.close(fd)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Restaura el controlador Aura a modo Gen1 (revierte el assert Gen2) para sacar
al onboard/headers de cualquier estado raro. /dev/hidraw9."""
import os, time
DEV="/dev/hidraw9"; PKT=65
def w(b):
    fd_w(bytes(b)+bytes(PKT-len(b)))
def main():
    fd=os.open(DEV,os.O_RDWR)
    try:
        def w(b): os.write(fd,bytes(b)+bytes(PKT-len(b))); time.sleep(0.02)
        w([0xEC,0x3F,0xAA])              # open session
        w([0xEC,0x3E,0x52,0x53,0x00])    # SetGenMode -> Gen1
        w([0xEC,0x3F,0x55])              # commit
        time.sleep(0.2)
        print("Controlador revertido a Gen1.")
    finally:
        os.close(fd)
if __name__=="__main__": main()

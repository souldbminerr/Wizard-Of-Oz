#!/usr/bin/env python3
import argparse
import ctypes
import struct
import sys
import time

PICOBOOT_MAGIC = 0x431FD10B
PC_EXCLUSIVE_ACCESS = 0x01
PC_REBOOT = 0x02          # RP2040 only
PC_FLASH_ERASE = 0x03
PC_READ = 0x84            # top bit set=device-to-host transfer
PC_WRITE = 0x05
PC_EXIT_XIP = 0x06

IF_CMD_STATUS = 0x42
VID_RASPBERRY_PI = 0x2E8A
PID_RP2040_USBBOOT = 0x0003

UF2_MAGIC0 = 0x0A324655
UF2_MAGIC1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FAMILY_RP2040 = 0xE48BFF56
SECTOR = 4096


class UsbBus(ctypes.Structure):
    pass


class UsbDevice(ctypes.Structure):
    pass


UsbBus._fields_ = [
    ("next", ctypes.POINTER(UsbBus)),
    ("prev", ctypes.POINTER(UsbBus)),
    ("dirname", ctypes.c_char * 512),
    ("devices", ctypes.POINTER(UsbDevice)),
    ("location", ctypes.c_uint32),
]
UsbDevice._fields_ = [
    ("next", ctypes.POINTER(UsbDevice)),
    ("prev", ctypes.POINTER(UsbDevice)),
    ("filename", ctypes.c_char * 512),
    ("bus", ctypes.POINTER(UsbBus)),
    ("descriptor", ctypes.c_ubyte * 18),
    ("config", ctypes.c_void_p),
    ("dev", ctypes.c_void_p),
    ("devnum", ctypes.c_ubyte),
]


class Picoboot:
    def __init__(self, dll_path):
        lib = ctypes.CDLL(dll_path)
        lib.usb_init.argtypes = []
        lib.usb_init.restype = None
        lib.usb_find_busses.argtypes = []
        lib.usb_find_busses.restype = ctypes.c_int
        lib.usb_find_devices.argtypes = []
        lib.usb_find_devices.restype = ctypes.c_int
        lib.usb_get_busses.argtypes = []
        lib.usb_get_busses.restype = ctypes.POINTER(UsbBus)
        lib.usb_open.argtypes = [ctypes.POINTER(UsbDevice)]
        lib.usb_open.restype = ctypes.c_void_p
        lib.usb_close.argtypes = [ctypes.c_void_p]
        lib.usb_close.restype = ctypes.c_int
        lib.usb_set_configuration.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.usb_set_configuration.restype = ctypes.c_int
        lib.usb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.usb_claim_interface.restype = ctypes.c_int
        lib.usb_bulk_write.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.usb_bulk_write.restype = ctypes.c_int
        lib.usb_bulk_read.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.usb_bulk_read.restype = ctypes.c_int
        lib.usb_control_msg.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                        ctypes.c_int, ctypes.c_int]
        lib.usb_control_msg.restype = ctypes.c_int
        lib.usb_strerror.argtypes = []
        lib.usb_strerror.restype = ctypes.c_char_p
        self.lib = lib
        self.handle = None
        self.token = 1
        self.out_ep = None
        self.in_ep = None

    def _find(self):
        self.lib.usb_init()
        self.lib.usb_find_busses()
        self.lib.usb_find_devices()
        bus = self.lib.usb_get_busses()
        while bus:
            dev = bus.contents.devices
            while dev:
                raw = bytes(dev.contents.descriptor)
                vid, pid = struct.unpack_from("<HH", raw, 8)
                if vid == VID_RASPBERRY_PI and pid == PID_RP2040_USBBOOT:
                    return dev
                dev = dev.contents.next
            bus = bus.contents.next
        return None

    def open(self):
        dev = self._find()
        if not dev:
            raise RuntimeError("no RP2040 BOOTSEL device (2e8a:0003) found")
        handle = self.lib.usb_open(dev)
        if not handle:
            raise RuntimeError("usb_open failed: "
                               + str(self.lib.usb_strerror()))
        # Read config descriptor (type 2) to locate interface 1
        buf = ctypes.create_string_buffer(512)
        n = self.lib.usb_control_msg(handle, 0x80, 6, 0x0200, 0,
                                     buf, 512, 2000)
        if n <= 0:
            raise RuntimeError("cannot read config descriptor")
        data = buf.raw[:n]
        out_ep = in_ep = None
        on_iface1 = False
        i = 0
        while i + 2 <= len(data):
            length, dtype = data[i], data[i + 1]
            if length < 2 or i + length > len(data):
                break
            if dtype == 4 and data[i + 2] == 1:  # interface 1
                on_iface1 = True
            elif dtype == 4:
                on_iface1 = False
            elif dtype == 5 and on_iface1 and data[i + 3] == 2: # bulk
                addr = data[i + 2]
                if addr & 0x80:
                    in_ep = in_ep or addr
                else:
                    out_ep = out_ep or addr
            i += length
        if not out_ep or not in_ep:
            raise RuntimeError("PICOBOOT interface endpoints not found")
        if self.lib.usb_claim_interface(handle, 1) != 0:
            if self.lib.usb_set_configuration(handle, 1) != 0:
                raise RuntimeError("cannot set configuration")
            if self.lib.usb_claim_interface(handle, 1) != 0:
                raise RuntimeError("cannot claim interface 1")
        self.handle = handle
        self.out_ep, self.in_ep = out_ep, in_ep
        print(f"claimed interface 1: OUT {out_ep:#04x} IN {in_ep:#04x}",
              flush=True)

    def _status(self, expect_token, timeout_s=30):
        buf = ctypes.create_string_buffer(16)
        deadline = time.time() + timeout_s
        while True:
            n = self.lib.usb_control_msg(self.handle, 0xC1, IF_CMD_STATUS,
                                         0, 1, buf, 16, 2000)
            if n != 16:
                raise RuntimeError("status read failed")
            token, code, cmd, prog = struct.unpack_from("<IIBB", buf.raw, 0)
            if not prog:
                if token != expect_token:
                    raise RuntimeError("status token mismatch")
                if code != 0:
                    raise RuntimeError(f"command failed, status={code}")
                return
            if time.time() > deadline:
                raise RuntimeError("command timed out")

    def _cmd(self, cmd_id, args16, cmd_size, xfer=0, tx=None):
        token = self.token
        self.token += 1
        pkt = (struct.pack("<IIBBHI", PICOBOOT_MAGIC, token, cmd_id,
                           cmd_size, 0, xfer) + bytes(args16))
        assert len(pkt) == 32
        if self.lib.usb_bulk_write(self.handle, self.out_ep, pkt, 32,
                                   3000) != 32:
            raise RuntimeError("command send failed")
        rx = b""
        if xfer:
            if cmd_id & 0x80:
                buf = ctypes.create_string_buffer(xfer)
                n = self.lib.usb_bulk_read(self.handle, self.in_ep, buf,
                                           xfer, 15000)
                if n != xfer:
                    raise RuntimeError("data receive failed")
                rx = buf.raw[:n]
            else:
                assert tx is not None and len(tx) == xfer
                if self.lib.usb_bulk_write(self.handle, self.out_ep, bytes(tx),
                                           xfer, 15000) != xfer:
                    raise RuntimeError("data send failed")
        ack = ctypes.create_string_buffer(1)
        if cmd_id & 0x80:
            if self.lib.usb_bulk_write(self.handle, self.out_ep, b"\x00", 1,
                                       3000) < 0:
                raise RuntimeError("ack send failed")
        else:
            if self.lib.usb_bulk_read(self.handle, self.in_ep, ack, 1,
                                      3000) < 0:
                raise RuntimeError("ack receive failed")
        self._status(token)
        return rx

    def exclusive(self, on):
        self._cmd(PC_EXCLUSIVE_ACCESS, bytes([1 if on else 0]) + bytes(15), 1)

    def exit_xip(self):
        self._cmd(PC_EXIT_XIP, bytes(16), 0)

    def erase(self, addr, size):
        self._cmd(PC_FLASH_ERASE, struct.pack("<II", addr, size) + bytes(8), 8)

    def write(self, addr, data):
        self._cmd(PC_WRITE,
                  struct.pack("<II", addr, len(data)) + bytes(8), 8,
                  xfer=len(data), tx=bytes(data))

    def read(self, addr, size):
        return self._cmd(PC_READ,
                         struct.pack("<II", addr, size) + bytes(8), 8,
                         xfer=size)

    def reboot(self):
        token = self.token
        self.token += 1
        pkt = (struct.pack("<IIBBHI", PICOBOOT_MAGIC, token, PC_REBOOT, 12,
                           0, 0) + struct.pack("<III", 0, 0, 100) + bytes(4))
        assert len(pkt) == 32
        # The device reboots
        try:
            self.lib.usb_bulk_write(self.handle, self.out_ep, pkt, 32, 3000)
        except OSError:
            pass

    def close(self):
        if self.handle:
            self.lib.usb_close(self.handle)
            self.handle = None


def parse_uf2(path):
    blocks = {}
    with open(path, "rb") as f:
        data = f.read()
    if len(data) % 512 or not data:
        raise ValueError("not a UF2 file")
    total = None
    for off in range(0, len(data), 512):
        b = data[off:off + 512]
        m0, m1, flags, addr, size, num, nblocks, family = struct.unpack_from(
            "<IIIIIIII", b, 0)
        end = struct.unpack_from("<I", b, 508)[0]
        if m0 != UF2_MAGIC0 or m1 != UF2_MAGIC1 or end != UF2_MAGIC_END:
            raise ValueError(f"bad UF2 magic at block {off // 512}")
        if family != UF2_FAMILY_RP2040:
            raise ValueError("UF2 family is not RP2040")
        if size != 256:
            raise ValueError("unexpected UF2 payload size")
        if not 0x10000000 <= addr < 0x10000000 + 16 * 1024 * 1024:
            raise ValueError(f"UF2 address out of flash: {addr:#x}")
        total = nblocks
        blocks[addr] = b[32:32 + 256]
    addrs = sorted(blocks)
    print(f"UF2: {len(addrs)}/{total} blocks, "
          f"{addrs[0]:#x}..{addrs[-1] + 256:#x}", flush=True)
    return addrs, blocks


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dll", required=True, help="path to 64-bit libusb0.dll")
    ap.add_argument("--uf2", required=True, help="firmware .uf2 to flash")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    addrs, blocks = parse_uf2(args.uf2)
    pb = Picoboot(args.dll)
    pb.open()
    try:
        print("exclusive access + exit XIP", flush=True)
        pb.exclusive(True)
        pb.exit_xip()
        sectors = sorted({a & ~(SECTOR - 1) for a in addrs})
        print(f"erasing {len(sectors)} sectors", flush=True)
        for i, s in enumerate(sectors):
            pb.erase(s, SECTOR)
            if (i + 1) % 64 == 0:
                print(f"  erase {i + 1}/{len(sectors)}", flush=True)
        print(f"writing {len(addrs)} pages", flush=True)
        for i, a in enumerate(addrs):
            pb.write(a, blocks[a])
            if (i + 1) % 1024 == 0:
                print(f"  write {i + 1}/{len(addrs)}", flush=True)
        if not args.no_verify:
            print("verifying", flush=True)
            for i, a in enumerate(addrs):
                if pb.read(a, 256) != blocks[a]:
                    raise RuntimeError(f"verify failed at {a:#x}")
                if (i + 1) % 2048 == 0:
                    print(f"  verify {i + 1}/{len(addrs)}", flush=True)
        print("rebooting into flash", flush=True)
        pb.reboot()
    finally:
        pb.close()
    print("done: unplug/replug if the drive does not reappear", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

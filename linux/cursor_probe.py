#!/usr/bin/env python3
"""Bounded hardware-cursor probe for the JUA254 (MCT T6). Not part of production.

Displays the packaged RGB-bars frame, uploads a 64x64 ARGB arrow to cursor
slot 0 of output 0, shows it, moves it along a fixed path, then hides it and
disables video output. Request layout follows MCT's GPL triggerdm driver
(`t6usbdongle.c`): 0x10 shape upload in 4096-byte control chunks addressed by
wIndex offset, 0x05 visibility, 0x04 position (top-left of the image).

Run only with jua254-monitor.service stopped (the probe claims the device).
`--dry-run` prints the planned control transfers without touching USB.
"""
import argparse, struct, sys, time
from pathlib import Path

SIZE = 64
SHAPE_TYPE_ALPHA = 1
CTRL_CHUNK = 4096
REQ_POS, REQ_STATE, REQ_SHAPE = 0x04, 0x05, 0x10
SLOT = 0

def arrow_pixels(size=SIZE):
    """ARGB8888 little-endian (B, G, R, A bytes): white arrow, black outline."""
    pixels = bytearray(size * size * 4)
    def put(x, y, value):
        i = (y * size + x) * 4; pixels[i:i + 4] = bytes((value, value, value, 0xFF))
    height = 40
    for y in range(height):
        width = min(y, 26) if y < 30 else max(0, 26 - (y - 30) * 3)
        for x in range(width + 1):
            edge = x in (0, width) or y == height - 1
            put(x, y, 0x00 if edge else 0xFF)
    for y in range(28, 50):  # tail
        for x in range(10 + (y - 28) // 3, 16 + (y - 28) // 3):
            if x < size: put(x, y, 0x00 if x in (10 + (y - 28) // 3, 15 + (y - 28) // 3) or y == 49 else 0xFF)
    return bytes(pixels)

def shape_payload(pixels, size=SIZE):
    if len(pixels) != size * size * 4: raise ValueError("cursor pixels must be ARGB8888 size*size")
    return struct.pack("<4H", SHAPE_TYPE_ALPHA, size, size, size * 4) + pixels

def shape_transfers(payload, slot=SLOT):
    """(bmRequestType, bRequest, wValue, wIndex, data) per vendor chunking."""
    return [(0x40, REQ_SHAPE, slot, offset, payload[offset:offset + CTRL_CHUNK])
            for offset in range(0, len(payload), CTRL_CHUNK)]

def encode_position(value):
    """Vendor encoding: negative coordinates are the 16-bit complement of -value."""
    return (~(-value)) & 0xFFFF if value < 0 else value & 0xFFFF

def position_transfer(x, y):
    return (0x40, REQ_POS, encode_position(x), encode_position(y), None)

def state_transfer(visible, slot=SLOT):
    return (0x40, REQ_STATE, slot, 1 if visible else 0, None)

def path(seconds, rate=60, width=1920, height=1080):
    """Corners, then a slow sweep across the bars, then partly off the left/top edges."""
    points = [(0, 0), (width - SIZE, 0), (width - SIZE, height - SIZE), (0, height - SIZE)]
    hold = [p for p in points for _ in range(rate)]
    sweep_frames = max(1, int(seconds * rate) - len(hold) - 2 * rate)
    sweep = [(int((width - SIZE) * n / sweep_frames), height // 2) for n in range(sweep_frames)]
    edge = [(-SIZE // 2, -SIZE // 2)] * (2 * rate)
    return hold + sweep + edge

def plan(seconds):
    yield from shape_transfers(shape_payload(arrow_pixels()))
    yield state_transfer(True)
    for x, y in path(seconds): yield position_transfer(x, y)
    yield state_transfer(False)

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not 5 <= args.seconds <= 120: ap.error("--seconds must be 5..120")
    transfers = list(plan(args.seconds))
    if args.dry_run:
        shapes = sum(1 for t in transfers if t[1] == REQ_SHAPE)
        moves = sum(1 for t in transfers if t[1] == REQ_POS)
        print(f"shape chunks={shapes} bytes={sum(len(t[4]) for t in transfers if t[1] == REQ_SHAPE)} "
              f"state_on/off=2 moves={moves} duration~{moves / 60:.1f}s")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import usb.util
    from replay_handoff import INTERFACE, load_records, validate_chunks
    from jua254_stream import DEFAULT_TEMPLATE, final_group, open_device, write_group_timed
    dev = open_device()
    if dev is None: raise SystemExit("JUA254 0711:5600 not found")
    try:
        records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
        write_group_timed(dev, final_group(records)); print("bars frame sent")
        for request_type, request, value, index, data in transfers:
            if request == REQ_SHAPE:
                if dev.ctrl_transfer(request_type, request, value, index, data, timeout=3000) != len(data):
                    raise RuntimeError("short cursor shape write")
                continue
            dev.ctrl_transfer(request_type, request, value, index, None, timeout=3000)
            if request == REQ_STATE: print("cursor", "shown" if index else "hidden")
            if request == REQ_POS: time.sleep(1 / 60)
    finally:
        for cleanup in (state_transfer(False), (0x40, 0x03, 0, 0, None)):
            try: dev.ctrl_transfer(*cleanup[:4], None, timeout=2000)
            except Exception as exc: print("cleanup error", repr(exc))
        try: usb.util.release_interface(dev, INTERFACE); usb.util.dispose_resources(dev)
        except Exception: pass
    print("output disabled")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

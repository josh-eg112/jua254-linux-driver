#!/usr/bin/env python3
"""Offline regression for the GPU capture path: helper records, frame
coalescing, type-7 tile framing, and hardware-cursor transfers."""
import pytest
import struct
from pathlib import Path

from replay_handoff import load_records, validate_chunks
from jua254_stream import (DEFAULT_TEMPLATE, FB_ROTATION_SLOTS, LiveFramer, fb_direct_header, final_group,
                           sized_payload_total)
from jua254_gpu import (CURSOR_LEAVE, CURSOR_POS, CURSOR_SHAPE, FRAME_END, FULL, REC, TILE, FrameAssembler,
                        HardwareCursor, coalesce, pad_cursor, parse_records)
from cursor_probe import REQ_POS, REQ_SHAPE, REQ_STATE

TYPE7_CAPTURE = Path(__file__).resolve().parent.parent / "shared/handoff/linux-type7-magenta-cyan-20260826"
JPEG = b"\xff\xd8" + bytes(500) + b"\xff\xd9"

def rec(kind, seq=1, x=0, y=0, w=0, h=0, payload=b"", requested=1_000_000_000, ready=1_010_000_000):
    return REC.pack(b"JGR1", kind, len(payload), seq, x, y, w, h, requested, ready) + payload

def words(header):
    return struct.unpack("<12I", header[:48])

def template():
    records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
    return final_group(records)

class Metrics(dict):
    def __missing__(self, key): return 0

def test_parse_records_handles_partial_input():
    stream = bytearray(rec(FULL, payload=JPEG) + rec(FRAME_END))
    head = bytearray(stream[:30]); assert parse_records(head) == [] and len(head) == 30
    out = parse_records(stream)
    assert [r[0] for r in out] == [FULL, FRAME_END] and out[0][8] == JPEG and not stream
    assert out[0][6] == 1.0 and abs(out[0][7] - 1.01) < 1e-9

def test_parse_records_rejects_lost_sync():
    try: parse_records(bytearray(b"XXXX" + bytes(60)))
    except RuntimeError: return
    raise AssertionError("accepted a corrupt record")

def test_assembler_groups_frames_and_passes_cursor():
    a = FrameAssembler()
    frames, cursor = a.feed(parse_records(bytearray(rec(TILE, x=32, w=32, h=1080, payload=JPEG) + rec(CURSOR_POS, x=5, y=6))))
    assert frames == [] and cursor[0][0] == CURSOR_POS
    frames, _ = a.feed(parse_records(bytearray(rec(FRAME_END, seq=7))))
    assert len(frames) == 1 and not frames[0].full and frames[0].seq == 7 and frames[0].items[0][2] == 32

def frame(full, *tiles, seq=1):
    a = FrameAssembler(); data = rec(FULL, seq=seq, w=1920, h=1080, payload=JPEG) if full else b""
    for x, payload in tiles: data += rec(TILE, seq=seq, x=x, w=32, h=1080, payload=payload)
    return a.feed(parse_records(bytearray(data + rec(FRAME_END, seq=seq))))[0][0]

def test_coalesce_keeps_newest_full_and_later_tiles():
    pending = [frame(False, (0, b"a"), seq=1), frame(True, seq=2), frame(False, (32, b"b"), (64, b"c"), seq=3), frame(False, (32, b"d"), seq=4)]
    base, tiles, newest, dropped = coalesce(pending)
    assert base.seq == 2 and dropped == 1 and newest.seq == 4
    assert sorted((t[2], t[8]) for t in tiles) == [(32, b"d"), (64, b"c")]

def test_coalesce_tiles_only():
    base, tiles, newest, dropped = coalesce([frame(False, (0, b"a")), frame(False, (0, b"b"), (32, b"c"))])
    assert base is None and dropped == 0 and sorted((t[2], t[8]) for t in tiles) == [(0, b"b"), (32, b"c")]

# Conformance check against the private research capture; skipped where it is not shipped.
@pytest.mark.skipif(not ((TYPE7_CAPTURE) / "manifest-urb.json").exists(), reason="research capture not shipped")
def test_fb_direct_header_matches_proven_live_buffer_strip():
    captured = load_records(TYPE7_CAPTURE)[37][1]
    w = words(fb_direct_header(399, 0, 0, 32, 1080, 0x30, 0x1fe030))
    c = words(captured)
    # Same as the visually proven strip after retargeting words 6/7 to buffer A.
    assert w[0] == c[0] == 7 and w[3:6] == c[3:6] and w[8:] == c[8:]
    assert (w[2], w[6], w[7]) == (399, 0x30, 0x1fe030)

def test_fb_direct_header_offsets():
    w = words(fb_direct_header(5, 256, 100, 128, 64, 0xfffef0, 0x11fdef0))
    assert w[4] == 128 | (64 << 16) and w[6] == 0xfffef0 + 100 * 1920 + 256 and w[7] == 0x11fdef0 + 50 * 1920 + 256
    for bad in ((1, 0, 32, 32), (0, 1, 32, 32), (1900, 0, 32, 32)):
        try: fb_direct_header(1, *bad, 0, 0)
        except ValueError: continue
        raise AssertionError(f"accepted {bad}")

def test_tile_group_targets_displayed_buffer_with_shared_fence():
    t = template(); framer = LiveFramer(t, fb_rotation=True, sized_payload=True)
    full = framer.group(JPEG); full_words = words(full[1][1])
    tile = framer.tile_group(JPEG, 64, 0, 32, 1080); tile_words = words(tile[1][1])
    assert full_words[0] == 3 and tile_words[0] == 7
    assert tile_words[2] == full_words[2] + 1
    assert tile_words[6] == full_words[5] + 64 and tile_words[7] == full_words[6] + 64
    assert len(tile) == 2 and len(tile[1][1]) == sized_payload_total(len(JPEG))
    assert struct.unpack("<8I", tile[0][1])[1] == sized_payload_total(len(JPEG))
    nxt = words(framer.group(JPEG)[1][1])
    assert nxt[2] == tile_words[2] + 1 and (nxt[5], nxt[6]) != (full_words[5], full_words[6])

def test_tile_group_without_rotation_keeps_type3_header_unchanged():
    t = template(); framer = LiveFramer(t, sized_payload=True)
    before = words(framer.group(JPEG)[1][1]); framer.tile_group(JPEG, 0, 0, 32, 1080)
    assert words(framer.group(JPEG)[1][1]) == before
    assert (words(framer.tile_group(JPEG, 0, 0, 32, 1080)[1][1])[6]) == FB_ROTATION_SLOTS[0][0]

def test_pad_cursor_top_left():
    pixels = bytes(range(4)) * (30 * 30)
    out = pad_cursor(30, 30, pixels)
    assert len(out) == 64 * 64 * 4 and out[:120] == pixels[:120] and out[120:256] == bytes(136)
    assert len(pad_cursor(80, 80, bytes(80 * 80 * 4))) == 64 * 64 * 4

def test_hardware_cursor_transfers():
    c = HardwareCursor(Metrics())
    shape = (CURSOR_SHAPE, 0, 3, 4, 24, 24, 0, 0, bytes(24 * 24 * 4))
    assert c.transfers() == [(0x40, REQ_STATE, 0, 0, None)]  # hidden until the pointer enters
    assert c.transfers() == []
    c.handle(shape); c.handle((CURSOR_POS, 0, 100, 200, 0, 0, 0, 0, b""))
    out = c.transfers()
    assert [t[1] for t in out] == [REQ_SHAPE] * 5 + [REQ_POS, REQ_STATE]  # 8-byte header + 16 KiB in 4 KiB chunks
    assert out[5][2:4] == (97, 196) and out[6][3] == 1
    c.handle((CURSOR_POS, 0, 1, 2, 0, 0, 0, 0, b""))
    assert c.transfers() == [(0x40, REQ_POS, (~2) & 0xFFFF, (~2) & 0xFFFF, None)]
    c.handle((CURSOR_LEAVE, 0, 0, 0, 0, 0, 0, 0, b""))
    assert [t[1] for t in c.transfers()] == [REQ_STATE]
    c.reset(); c.handle((CURSOR_POS, 0, 10, 10, 0, 0, 0, 0, b""))  # after reconnect
    assert [t[1] for t in c.transfers()] == [REQ_SHAPE] * 5 + [REQ_POS, REQ_STATE]  # 8-byte header + 16 KiB in 4 KiB chunks

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)

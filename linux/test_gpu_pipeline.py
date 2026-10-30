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
    shape = (CURSOR_SHAPE, 0, 3, 4, 24, 24, 0, 0, bytes((255, 255, 255, 255)) + bytes(24 * 24 * 4 - 4))
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

def test_hardware_cursor_replaces_hyprland_placeholders():
    from jua254_gpu import is_placeholder
    black = bytes((0, 0, 0, 255)) * (48 * 48)
    clear = bytes(48 * 48 * 4)
    real = bytes((255, 255, 255, 255)) + clear[4:]
    assert is_placeholder(48, 48, black) and is_placeholder(48, 48, clear) and not is_placeholder(48, 48, real)
    fallback = (bytes((9, 9, 9, 255)) * (64 * 64), (3, 1))
    c = HardwareCursor(Metrics(), fallback)
    c.handle((CURSOR_POS, 0, 100, 200, 0, 0, 0, 0, b""))  # entered with no image yet: the theme arrow
    assert c.shape == fallback[0] and [t[1] for t in c.transfers()][-2:] == [REQ_POS, REQ_STATE]
    c.handle((CURSOR_SHAPE, 0, 6, 2, 48, 48, 0, 0, real))
    assert c.hot == (6, 2) and c.shape[:4] == real[:4]
    c.handle((CURSOR_SHAPE, 0, 0, 0, 48, 48, 0, 0, black))  # a placeholder never replaces a real image
    assert c.hot == (6, 2) and c.shape[:4] == real[:4]
    plain = HardwareCursor(Metrics())  # without a theme arrow, placeholders are just ignored
    plain.handle((CURSOR_SHAPE, 0, 0, 0, 48, 48, 0, 0, black)); plain.handle((CURSOR_POS, 0, 1, 1, 0, 0, 0, 0, b""))
    assert plain.shape is None and plain.transfers() == [(0x40, REQ_STATE, 0, 0, None)]  # stays hidden

def test_theme_cursor_reads_inherited_xcursor_theme():
    import struct, tempfile
    from jua254_gpu import theme_cursor
    root = Path(tempfile.mkdtemp())
    (root / "default").mkdir(); (root / "default/index.theme").write_text("[Icon Theme]\nInherits=Base\n")
    (root / "Base/cursors").mkdir(parents=True)
    def image(nominal, size, hot):
        return struct.pack("<9I", 36, 0xFFFD0002, nominal, 1, size, size, hot, hot, 0) + bytes((nominal, 0, 0, 255)) * (size * size)
    small, large = image(24, 24, 3), image(48, 48, 6)
    toc_end = 16 + 2 * 12
    data = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 2) + struct.pack("<III", 0xFFFD0002, 24, toc_end) + \
           struct.pack("<III", 0xFFFD0002, 48, toc_end + len(small)) + small + large
    (root / "Base/cursors/left_ptr").write_bytes(data)
    pixels, hot = theme_cursor(48, "default", (str(root),))
    assert hot == (6, 6) and pixels[:4] == bytes((48, 0, 0, 255)) and len(pixels) == 64 * 64 * 4
    assert theme_cursor(24, "default", (str(root),))[1] == (3, 3)
    assert theme_cursor(24, "Missing", (str(root / "nowhere"),)) is None

def test_oversized_frame_is_skipped_without_reconnecting():
    """A frame too detailed for the adapter's slot is dropped and a full frame requested; the USB
    connection stays open (it used to count as a USB failure and reopen the adapter)."""
    import threading
    import jua254_gpu
    big = b"\xff\xd8" + bytes(600_000) + b"\xff\xd9"
    stop = threading.Event(); calls = {"open": 0, "full_requests": 0, "sent": []}

    class FakeCapture:
        def __init__(self, *a, **k): self.n = 0
        def fileno(self): return 0
        def read(self):
            self.n += 1
            if self.n == 1:   # the first frame is too large
                data = rec(FULL, seq=1, w=1920, h=1080, payload=big) + rec(FRAME_END, seq=1)
                return FrameAssembler().feed(parse_records(bytearray(data)))[0], []
            if self.n <= 3: return [frame(True, seq=self.n)], []
            stop.set(); return [], []
        def request_full(self): calls["full_requests"] += 1
        def close(self): pass

    class FakeFramer:
        def group(self, jpeg):
            if len(jpeg) > 469_936: raise ValueError("frame exceeds the captured envelope")
            return [jpeg]

    class Presentation:
        def begin_connection(self): pass
        def record(self, *a, **k): pass
    class Telemetry:
        def due(self, now): return True
        def maybe_write(self, *a): pass
    class Diagnostics:
        def event(self, *a, **k): pass
        def counter(self, *a, **k): pass

    def open_device():
        calls["open"] += 1; return object()
    def write_group_timed(dev, group):
        calls["sent"].append(len(group[0])); return None, 1.0, 1.0
    class Args:
        monitor = "HEADLESS-2"; quality = 80; hw_cursor = False; damage_tiles = False; tile = "32x1080"
        max_tiles = 24; full_threshold = .4; fps = 1000; seconds = None; command_ring = False
    metrics = Metrics(usb=[], end_to_end=[], frame_age_after_usb=[], frame_interval=[])
    original = jua254_gpu.GpuCapture, jua254_gpu.select.select
    jua254_gpu.GpuCapture = FakeCapture; jua254_gpu.select.select = lambda r, w, x, t: (r, [], [])
    try:
        jua254_gpu.run_gpu(Args(), None, FakeFramer(), metrics, Presentation(), Telemetry(), Diagnostics(), stop,
                           open_device, None, write_group_timed, lambda *a: None, lambda payload: "")
    finally:
        jua254_gpu.GpuCapture, jua254_gpu.select.select = original
    assert metrics["too_large"] == 1 and calls["full_requests"] >= 2   # one at open, one for the skipped frame
    assert calls["open"] == 1 and metrics["usb_failures"] == 0
    assert calls["sent"] and all(n == len(JPEG) for n in calls["sent"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)

#!/usr/bin/env python3
"""Offline regression for the hardware-cursor probe's vendor request layout."""
import struct

from cursor_probe import (CTRL_CHUNK, REQ_POS, REQ_SHAPE, REQ_STATE, SIZE, arrow_pixels, encode_position,
                          path, plan, position_transfer, shape_payload, shape_transfers, state_transfer)

def test_arrow_is_argb_with_opaque_and_transparent_pixels():
    pixels = arrow_pixels()
    assert len(pixels) == SIZE * SIZE * 4
    alphas = {pixels[i + 3] for i in range(0, len(pixels), 4)}
    assert alphas == {0x00, 0xFF}
    assert pixels[3] == 0xFF  # hotspot pixel (0, 0) is drawn

def test_shape_header_matches_vendor_struct():
    payload = shape_payload(arrow_pixels())
    assert struct.unpack("<4H", payload[:8]) == (1, SIZE, SIZE, SIZE * 4)
    assert len(payload) == 8 + SIZE * SIZE * 4

def test_shape_chunks_cover_payload_by_offset():
    payload = shape_payload(arrow_pixels()); transfers = shape_transfers(payload)
    assert all(t[:3] == (0x40, REQ_SHAPE, 0) for t in transfers)
    assert [t[3] for t in transfers] == list(range(0, len(payload), CTRL_CHUNK))
    assert all(len(t[4]) <= CTRL_CHUNK for t in transfers)
    assert b"".join(t[4] for t in transfers) == payload

def test_position_encoding_matches_vendor():
    assert encode_position(0) == 0 and encode_position(1919) == 1919
    assert encode_position(-1) == 0xFFFE and encode_position(-32) == (~32) & 0xFFFF
    assert position_transfer(-32, 5) == (0x40, REQ_POS, (~32) & 0xFFFF, 5, None)

def test_state_transfers():
    assert state_transfer(True) == (0x40, REQ_STATE, 0, 1, None)
    assert state_transfer(False) == (0x40, REQ_STATE, 0, 0, None)

def test_plan_order_and_bounds():
    transfers = list(plan(20))
    kinds = [t[1] for t in transfers]
    first_state = kinds.index(REQ_STATE)
    assert set(kinds[:first_state]) == {REQ_SHAPE}
    assert transfers[first_state] == state_transfer(True) and transfers[-1] == state_transfer(False)
    assert set(kinds[first_state + 1:-1]) == {REQ_POS}
    for x, y in path(20):
        assert -SIZE <= x <= 1920 - SIZE and -SIZE <= y <= 1080 - SIZE
    assert len(path(20)) == 20 * 60

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)

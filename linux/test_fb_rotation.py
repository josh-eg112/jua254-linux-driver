#!/usr/bin/env python3
"""Offline regression for the default-off framebuffer-rotation experiment."""
import pytest
import struct
from pathlib import Path

from replay_handoff import load_records, validate_chunks
from jua254_stream import (DEFAULT_TEMPLATE, FB_ROTATION_SLOTS, FramebufferRotation,
                           connection_groups, final_group, frame_records, template_header)

WINDOWS_CAPTURE = Path(__file__).resolve().parent.parent / "shared/handoff/windows-colors-20260825"
JPEG = b"\xff\xd8\xff" + bytes(1000) + b"\xff\xd9"

def words(header):
    return struct.unpack("<12I", header)

def load_template():
    records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
    return records, final_group(records)

def test_template_header_is_windows_seq_398_buffer_a():
    _, template = load_template()
    w = words(template_header(template))
    assert (w[0], w[2], w[5], w[6]) == (3, 398, 0x30, 0x1fe030)

# Conformance check against the private research capture; skipped where it is not shipped.
@pytest.mark.skipif(not ((WINDOWS_CAPTURE) / "manifest-urb.json").exists(), reason="research capture not shipped")
def test_slots_match_windows_capture():
    seen = []
    for path in sorted(WINDOWS_CAPTURE.glob("*.bin")):
        head = path.read_bytes()[:48]
        if len(head) == 48 and words(head)[0] == 3:
            seen.append((words(head)[5], words(head)[6]))
    assert seen, "no type-3 headers in the Windows capture"
    assert set(seen) == set(FB_ROTATION_SLOTS)
    # Windows cycles A -> B -> C in capture order.
    for previous, current in zip(seen, seen[1:]):
        assert FB_ROTATION_SLOTS.index(current) == (FB_ROTATION_SLOTS.index(previous) + 1) % 3

def test_rotation_cycles_slots_and_advances_fence_only():
    _, template = load_template(); base = template_header(template)
    rotation = FramebufferRotation(base)
    headers = [rotation.next_header() for _ in range(7)]
    targets = [(words(h)[5], words(h)[6]) for h in headers]
    assert targets[:3] == [FB_ROTATION_SLOTS[1], FB_ROTATION_SLOTS[2], FB_ROTATION_SLOTS[0]]
    assert targets[3:6] == targets[:3]
    assert [words(h)[2] for h in headers] == list(range(399, 406))
    for header in headers:
        changed = [i for i, (a, b) in enumerate(zip(words(base), words(header))) if a != b]
        assert set(changed) <= {2, 5, 6}

def test_fence_wraps_as_u32():
    base = bytearray(48); base[8:12] = (0xFFFFFFFF).to_bytes(4, "little")
    assert words(FramebufferRotation(bytes(base)).next_header())[2] == 0

def test_default_path_is_byte_identical():
    records, template = load_template()
    assert frame_records(template, JPEG) == frame_records(template, JPEG, None)
    assert connection_groups(records, template, JPEG) == connection_groups(records, template, JPEG, header=None)

def test_rotated_frame_changes_only_header_bytes():
    _, template = load_template()
    plain = frame_records(template, JPEG)
    rotated = frame_records(template, JPEG, FramebufferRotation(template_header(template)).next_header())
    assert len(plain) == len(rotated)
    for (item_a, data_a), (item_b, data_b) in zip(plain, rotated):
        assert item_a == item_b and len(data_a) == len(data_b)
    body_a = b"".join(d for _, d in plain[1::2]); body_b = b"".join(d for _, d in rotated[1::2])
    assert body_a[48:] == body_b[48:]
    assert [i for i in range(48) if body_a[i] != body_b[i]]
    assert all(8 <= i < 12 or 20 <= i < 28 for i in range(48) if body_a[i] != body_b[i])
    # 32-byte descriptors are untouched.
    assert [d for _, d in plain[0::2]] == [d for _, d in rotated[0::2]]

def test_rejects_bad_header_length():
    _, template = load_template()
    for bad in (b"", bytes(47), bytes(49)):
        for call in (lambda: frame_records(template, JPEG, bad), lambda: FramebufferRotation(bad)):
            try: call()
            except ValueError: continue
            raise AssertionError("accepted a bad header length")

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)

#!/usr/bin/env python3
"""Offline regression for the default-off sized-payload and command-ring experiments."""
import pytest
import struct
from pathlib import Path

from replay_handoff import load_records, validate_chunks
from jua254_stream import (CHUNK_BYTES, COMMAND_RING_BASE, COMMAND_RING_PRIMED_FRAMES, DEFAULT_TEMPLATE,
                           RESET_FLAG, CommandRing, FramebufferRotation, LiveFramer, Metric, final_group,
                           frame_records,
                           framed_group, sized_payload_total, template_address, template_header,
                           template_total)

WINDOWS_CAPTURE = Path(__file__).resolve().parent.parent / "shared/handoff/windows-colors-20260825"
JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 300 + b"\xff\xd9"

def load_template():
    records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
    return final_group(records)

def windows_transfers():
    """(total, address, jpeg_len) for each logical transfer in capture order."""
    files = sorted(WINDOWS_CAPTURE.glob("*.bin")); out = []; i = 0
    while i < len(files):
        total, address = struct.unpack("<8I", files[i].read_bytes())[1:3]; body = b""
        while len(body) < total:
            body += files[i + 1].read_bytes(); i += 2
        out.append((total, address, body[48:].rfind(b"\xff\xd9") + 2))
    return out

# Conformance check against the private research capture; skipped where it is not shipped.
@pytest.mark.skipif(not ((WINDOWS_CAPTURE) / "manifest-urb.json").exists(), reason="research capture not shipped")
def test_sized_total_matches_every_windows_transfer():
    transfers = windows_transfers()
    assert len(transfers) == 15
    for total, _, jpeg_len in transfers:
        assert sized_payload_total(jpeg_len) == total

def test_windows_upload_addresses_are_contiguous():
    transfers = windows_transfers()
    for (total, address, _), (_, following, _) in zip(transfers, transfers[1:]):
        assert following == address + total

def test_generated_group_reproduces_template_exactly():
    template = load_template()
    generated = framed_group(template, JPEG, None, template_total(template), template_address(template))
    assert generated == frame_records(template, JPEG)

def test_generated_group_with_rotated_header_matches_frame_records():
    template = load_template()
    header = FramebufferRotation(template_header(template)).next_header()
    generated = framed_group(template, JPEG, header, template_total(template), template_address(template))
    assert generated == frame_records(template, JPEG, header)

def test_sized_group_layout():
    template = load_template(); total = sized_payload_total(len(JPEG))
    group = framed_group(template, JPEG, None, total, 0x2345600)
    validate_chunks(group)
    descriptors = [struct.unpack("<8I", d) for _, d in group[0::2]]
    chunks = [d for _, d in group[1::2]]
    assert sum(len(c) for c in chunks) == total < template_total(template)
    assert all(len(c) <= CHUNK_BYTES for c in chunks)
    for n, words in enumerate(descriptors):
        assert words[:3] == (0, total, 0x2345600)
        assert words[3] == len(chunks[n]) and words[4] == n * CHUNK_BYTES
        assert words[5] == (1 if n < len(descriptors) - 1 else 0) and words[6:] == (0, 0)
    body = b"".join(chunks)
    assert struct.unpack("<I", body[4:8])[0] == total - 48
    assert body[48:48 + len(JPEG)] == JPEG and not any(body[48 + len(JPEG):])
    assert body[47] & RESET_FLAG == 0
    # Each record keeps the timing of the template record at the same position.
    for (item, data), (template_item, _) in zip(group, template):
        assert item["time_relative_seconds"] == template_item["time_relative_seconds"]
        assert item["length"] == len(data)

def test_reset_flag_sets_only_header_flag_byte():
    template = load_template(); total = sized_payload_total(len(JPEG))
    plain = b"".join(d for _, d in framed_group(template, JPEG, None, total, 0x2400000)[1::2])
    reset = b"".join(d for _, d in framed_group(template, JPEG, None, total, 0x2400000, True)[1::2])
    assert [i for i in range(len(plain)) if plain[i] != reset[i]] == [47]
    assert reset[47] & RESET_FLAG

def test_oversized_frames_are_rejected():
    template = load_template()
    for call in (lambda: framed_group(template, JPEG, None, template_total(template) + 2048, 0),
                 lambda: framed_group(template, bytes(5000), None, 4096, 0)):
        try: call()
        except ValueError: continue
        raise AssertionError("accepted a frame that does not fit")

def test_command_ring_places_contiguously_wraps_and_flags():
    ring = CommandRing(64); total = sized_payload_total(200000)
    placements = [ring.place(total) for _ in range(200)]
    addresses = [a for a, _ in placements]
    assert addresses[0] == COMMAND_RING_BASE
    assert all(COMMAND_RING_BASE <= a and a + total <= 64 * 1024 * 1024 for a in addresses)
    resets = [n for n, (_, r) in enumerate(placements) if r]
    assert resets[:COMMAND_RING_PRIMED_FRAMES] == list(range(COMMAND_RING_PRIMED_FRAMES))
    wraps = [n for n in range(1, 200) if addresses[n] < addresses[n - 1]]
    assert wraps and all(addresses[n] == COMMAND_RING_BASE for n in wraps)
    assert set(resets) == set(range(COMMAND_RING_PRIMED_FRAMES)) | set(wraps)
    for n in range(1, 200):
        if n not in wraps:
            assert addresses[n] == addresses[n - 1] + total

def test_command_ring_rejects_small_vram():
    try: CommandRing(35)
    except RuntimeError: return
    raise AssertionError("accepted VRAM without room for a command ring")

def test_ring_base_is_above_all_framebuffers():
    third_uv_end = 0x21fddb0 + 1920 * 1088 // 2
    assert third_uv_end <= COMMAND_RING_BASE

def test_live_framer_default_is_frame_records():
    template = load_template(); framer = LiveFramer(template)
    for _ in range(3):
        assert framer.group(JPEG) == frame_records(template, JPEG)

def test_live_framer_rotation_only_matches_previous_production_path():
    template = load_template(); framer = LiveFramer(template, fb_rotation=True)
    reference = FramebufferRotation(template_header(template))
    for _ in range(4):
        assert framer.group(JPEG) == frame_records(template, JPEG, reference.next_header())

def test_live_framer_all_experiments():
    template = load_template()
    metrics = {"command_ring_resets": 0, "usb_payload_bytes": Metric()}
    framer = LiveFramer(template, fb_rotation=True, sized_payload=True, metrics=metrics)
    framer.attach_ring(64)
    total = sized_payload_total(len(JPEG)); previous = None
    for n in range(COMMAND_RING_PRIMED_FRAMES + 3):
        group = framer.group(JPEG); validate_chunks(group)
        words = struct.unpack("<8I", group[0][1]); body = b"".join(d for _, d in group[1::2])
        assert words[1] == total and len(body) == total
        if previous is not None: assert words[2] == previous + total
        previous = words[2]
        header = struct.unpack("<12I", body[:48])
        assert header[2] == 399 + n and header[1] == total - 48
        assert bool(body[47] & RESET_FLAG) == (n < COMMAND_RING_PRIMED_FRAMES)
    assert metrics["command_ring_resets"] == COMMAND_RING_PRIMED_FRAMES
    assert metrics["usb_payload_bytes"].count == COMMAND_RING_PRIMED_FRAMES + 3

def test_live_framer_ring_without_sizing_keeps_captured_envelope():
    template = load_template(); framer = LiveFramer(template); framer.attach_ring(64)
    group = framer.group(JPEG)
    assert [len(d) for _, d in group] == [len(d) for _, d in template]
    assert struct.unpack("<8I", group[0][1])[2] == COMMAND_RING_BASE

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)

#!/usr/bin/env python3
"""Offline checks for adapter output modes (--mode) and their VRAM layout."""
import struct, sys, unittest
from pathlib import Path
import jua254_stream as js
from jua254_gpu import check_output_size
from test_adapter_open import FakeDev, install_fake_usb

TEMPLATE = js.final_group(js.load_records(js.DEFAULT_TEMPLATE))
JPEG = bytes(range(256)) * 300

def words(header): return struct.unpack("<12I", header[:48])

def entry(w, h, r, clock=100000):
    return struct.pack("<I14H", clock, r, w + 100, w, w + 10, 10, h + 20, h, h + 2, 2, 0, 1000, 0, 0x101, 0)

class Layout(unittest.TestCase):
    def test_1080p_reproduces_captured_constants(self):
        m = js.VideoMode(1920, 1080, 60)
        self.assertTrue(m.default)
        self.assertEqual(m.slots, js.FB_ROTATION_SLOTS)
        self.assertEqual(m.ring_base, js.COMMAND_RING_BASE)
        self.assertEqual(m.header(js.template_header(TEMPLATE)), js.template_header(TEMPLATE))

    def test_720p_layout(self):
        m = js.VideoMode.parse("1280x720@60")
        self.assertEqual(m.slots[0], (0x30, 0x30 + 1280 * 720))
        self.assertEqual(words(m.header(js.template_header(TEMPLATE)))[4], 1280 | (1280 << 16))
        end = m.slots[2][1] + 1280 * 720 // 2
        self.assertGreaterEqual(m.ring_base, end); self.assertEqual(m.ring_base % (1 << 20), 0)

    def test_largest_table_mode_fits_and_moves_ring(self):
        m = js.VideoMode(2048, 1152, 60)
        self.assertGreater(m.ring_base, js.COMMAND_RING_BASE)
        for (y, uv), (next_y, _) in zip(m.slots, m.slots[1:]): self.assertLessEqual(uv + 2048 * 1152 // 2, next_y)

    def test_rejects_unaligned_width(self):
        with self.assertRaises(ValueError): js.VideoMode.parse("1366x768@60")

class Framer(unittest.TestCase):
    def test_default_mode_is_byte_identical(self):
        a = js.LiveFramer(TEMPLATE, True, True); b = js.LiveFramer(TEMPLATE, True, True, mode=js.VideoMode(1920, 1080, 60))
        a.attach_ring(58); b.attach_ring(58)
        for _ in range(4): self.assertEqual(a.group(JPEG), b.group(JPEG))

    def test_720p_frames_use_mode_pitch_slots_and_ring(self):
        m = js.VideoMode(1280, 720, 60); f = js.LiveFramer(TEMPLATE, True, True, mode=m); f.attach_ring(58)
        seen = []
        for _ in range(3):
            group = f.group(JPEG)
            desc, body = group[0][1], group[1][1]
            w = words(body)
            self.assertEqual(w[4], 1280 | (1280 << 16))
            seen.append((w[5], w[6]))
            self.assertGreaterEqual(struct.unpack("<8I", desc)[2], m.ring_base)
        self.assertEqual(sorted(seen), sorted(m.slots))
        tile = words(f.tile_group(JPEG, 0, 0, 32, 720)[1][1])
        self.assertEqual(tile[5], 1280 | (1280 << 16))

    def test_non_default_mode_requires_rotation_and_sizing(self):
        with self.assertRaises(ValueError): js.LiveFramer(TEMPLATE, False, True, mode=js.VideoMode(1280, 720))

class ModeTable(unittest.TestCase):
    def setUp(self): self.saved = {k: v for k, v in sys.modules.items() if k == "usb" or k.startswith("usb.")}
    def tearDown(self):
        for k in [k for k in sys.modules if k == "usb" or k.startswith("usb.")]: del sys.modules[k]
        sys.modules.update(self.saved)

    def test_decode_1080p_entry(self):
        self.assertEqual(js.decode_mode_entry(js.MODE_1080P60), (1920, 1080, 60))

    def table_dev(self):
        table = [entry(640, 480, 60), entry(1280, 720, 60), js.MODE_1080P60]
        dev = FakeDev(); dev.writes = []
        def ctrl(rt, req, value, index, data_or_len, timeout=None):
            if rt == 0xC0 and req == 0x89:
                n = index // 32
                return table[n] if n < len(table) else bytes(32)
            if rt == 0x40: dev.writes.append((req, index, bytes(data_or_len) if data_or_len else None))
            return FakeDev.ctrl_transfer(dev, rt, req, value, index, data_or_len, timeout)
        dev.ctrl_transfer = ctrl
        return dev, table

    def test_open_sets_the_matching_table_entry(self):
        dev, table = self.table_dev(); install_fake_usb(dev)
        self.assertIs(js.open_device(js.VideoMode(1280, 720, 60)), dev)
        self.assertIn((0x12, 0, table[1]), dev.writes)

    def test_open_rejects_mode_missing_from_table(self):
        dev, _ = self.table_dev(); install_fake_usb(dev)
        with self.assertRaisesRegex(RuntimeError, "mode=2048x1152@60 mode_ok=False"):
            js.open_device(js.VideoMode(2048, 1152, 60))
        self.assertTrue(dev.disposed)

class OutputSize(unittest.TestCase):
    def test_mismatch_raises(self):
        check_output_size(None, None)
        check_output_size({"width": 1920, "height": 1080}, None)
        check_output_size({"width": 1280, "height": 720}, js.VideoMode(1280, 720))
        with self.assertRaises(RuntimeError): check_output_size({"width": 1920, "height": 1080}, js.VideoMode(1280, 720))

if __name__ == "__main__":
    unittest.main()

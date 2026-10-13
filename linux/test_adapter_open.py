#!/usr/bin/env python3
"""Offline checks for adapter open failure cleanup and logging (fake pyusb)."""
import contextlib, io, json, os, sys, types, unittest
import jua254_stream as js

class FakeDev:
    def __init__(self, hdmi=b"\x01"):
        self.hdmi = hdmi; self.claimed = False; self.disposed = False
    def set_configuration(self): pass
    def ctrl_transfer(self, rt, req, value, index, data_or_len, timeout=None):
        if rt == 0xC0 and req == 0xB0: return b"JUA254H".ljust(16, b"\0")
        if rt == 0xC0 and req == 0x87: return self.hdmi
        if rt == 0xC0 and req == 0x89: return js.MODE_1080P60
        if rt == 0x40 and req == 0x12: return 32
        return 0

def install_fake_usb(dev):
    usb = types.ModuleType("usb"); core = types.ModuleType("usb.core"); util = types.ModuleType("usb.util")
    backend = types.ModuleType("usb.backend"); libusb1 = types.ModuleType("usb.backend.libusb1")
    core.find = lambda **kw: dev
    libusb1.get_backend = lambda **kw: object()
    def claim(d, i): d.claimed = True
    def release(d, i): d.claimed = False
    def dispose(d): d.disposed = True
    util.claim_interface, util.release_interface, util.dispose_resources = claim, release, dispose
    usb.core, usb.util, usb.backend, backend.libusb1 = core, util, backend, libusb1
    sys.modules.update({"usb": usb, "usb.core": core, "usb.util": util, "usb.backend": backend, "usb.backend.libusb1": libusb1})

class AdapterOpen(unittest.TestCase):
    def setUp(self): self.saved = {k: v for k, v in sys.modules.items() if k == "usb" or k.startswith("usb.")}
    def tearDown(self):
        for k in [k for k in sys.modules if k == "usb" or k.startswith("usb.")]: del sys.modules[k]
        sys.modules.update(self.saved)

    def test_failed_check_releases_and_disposes(self):
        dev = FakeDev(hdmi=b"\x00"); install_fake_usb(dev)
        with self.assertRaisesRegex(RuntimeError, "hdmi=00"):
            js.open_device()
        self.assertFalse(dev.claimed); self.assertTrue(dev.disposed)

    def test_success_keeps_claim(self):
        dev = FakeDev(); install_fake_usb(dev)
        self.assertIs(js.open_device(), dev)
        self.assertTrue(dev.claimed); self.assertFalse(dev.disposed)

    def test_open_failure_log_is_rate_limited(self):
        metrics = {}; err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(60): js.log_open_failure(metrics, RuntimeError("busy"))
        lines = [json.loads(l) for l in err.getvalue().splitlines()]
        self.assertEqual([l["count"] for l in lines], [1, 2, 3, 4, 5, 30, 60])
        self.assertEqual(metrics["open_failures"], 60)
        self.assertEqual(metrics["last_usb_error"], "RuntimeError('busy')")

class CaptureSinkWake(unittest.TestCase):
    def setUp(self):
        js._last_wake = None; self.calls = []; self.clock = [100.0]
        self.saved = os.environ.pop("JUA254_WAKE_CAPTURE", None)
    def tearDown(self):
        os.environ.pop("JUA254_WAKE_CAPTURE", None)
        if self.saved is not None: os.environ["JUA254_WAKE_CAPTURE"] = self.saved
    def wake(self, error):
        with contextlib.redirect_stderr(io.StringIO()):
            return js.wake_capture_sink({}, RuntimeError(error), runner=lambda cmd, **kw: self.calls.append(cmd),
                                        now=lambda: self.clock[0])

    def test_off_without_env(self):
        self.assertFalse(self.wake("check failed: hdmi=00")); self.assertEqual(self.calls, [])

    def test_only_for_missing_hdmi_and_rate_limited(self):
        os.environ["JUA254_WAKE_CAPTURE"] = "/dev/video9"
        self.assertFalse(self.wake("Resource busy"))
        self.assertTrue(self.wake("check failed: hdmi=00"))
        self.assertIn("/dev/video9", self.calls[0])
        self.clock[0] += 5; self.assertFalse(self.wake("check failed: hdmi=00"))
        self.clock[0] += 6; self.assertTrue(self.wake("check failed: hdmi=00"))
        self.assertEqual(len(self.calls), 2)

class AdapterHang(unittest.TestCase):
    def setUp(self): self.sent = []; self.metrics = {}
    def runner(self, cmd, **kw): self.sent.append(cmd)
    def fail(self, error):
        with contextlib.redirect_stderr(io.StringIO()): return js.note_adapter_hang(self.metrics, error, runner=self.runner)
    def ok(self):
        with contextlib.redirect_stderr(io.StringIO()): return js.note_adapter_ok(self.metrics, runner=self.runner)

    def test_notifies_once_after_consecutive_timeouts(self):
        timeout = Exception("USBTimeoutError(110, 'Operation timed out')")
        results = [self.fail(timeout) for _ in range(12)]
        self.assertEqual(results.index(True), js.HUNG_AFTER_TIMEOUTS - 1)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.sent), 1); self.assertIn("critical", self.sent[0])
        self.assertTrue(self.ok()); self.assertEqual(len(self.sent), 2)   # recovery notice
        self.assertFalse(self.ok())

    def test_other_errors_reset_the_streak(self):
        timeout = Exception("Operation timed out")
        for _ in range(js.HUNG_AFTER_TIMEOUTS - 1): self.fail(timeout)
        self.fail(RuntimeError("check failed: hdmi=00"))
        for _ in range(js.HUNG_AFTER_TIMEOUTS - 1): self.assertFalse(self.fail(timeout))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.ok()); self.assertEqual(self.sent, [])     # no recovery notice without a hang

if __name__ == "__main__":
    unittest.main()

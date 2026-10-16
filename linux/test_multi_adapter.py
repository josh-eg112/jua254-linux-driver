#!/usr/bin/env python3
"""Several JUA254 adapters at once: locating them, and one supervised output + streamer each."""
import signal
import tempfile
from pathlib import Path

import jua254_monitor as monitor
from jua254_monitor import DEFAULT_TEMPLATE, attached_locations
from jua254_stream import find_adapter, usb_location


class FakeDevice:
    def __init__(self, bus, ports, product=0x5600): self.bus, self.port_numbers, self.idProduct = bus, ports, product


def test_usb_location_matches_sysfs_names():
    assert usb_location(FakeDevice(2, (1,))) == "2-1"
    assert usb_location(FakeDevice(3, (1, 4))) == "3-1.4"


def test_find_adapter_by_location_and_supported_model():
    devices = [FakeDevice(2, (1,), 0x1234), FakeDevice(2, (3,)), FakeDevice(2, (4,), 0x5601)]
    def find(find_all=False, custom_match=None, **_):
        matches = [d for d in devices if custom_match is None or custom_match(d)]
        return matches if find_all else (matches[0] if matches else None)
    assert find_adapter(None, find=find) is devices[1]          # an unsupported product is skipped
    assert find_adapter("2-4", find=find) is devices[2]         # the A8640H (0711:5601) is found
    assert find_adapter("2-1", find=find) is None               # an unsupported product at that port
    assert find_adapter("9-9", find=find) is None


def test_attached_locations_reads_sysfs():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for name, vid, pid in (("2-1", "0711", "5600"), ("2-1.3", "0711", "5601"), ("2-2", "0711", "5602"),
                               ("1-4", "046d", "c548"), ("usb2", "1d6b", "0003")):
            (root / name).mkdir()
            (root / name / "idVendor").write_text(vid + "\n")
            (root / name / "idProduct").write_text(pid + "\n")
        assert attached_locations(root) == ["2-1", "2-1.3"]


def run_two_adapter_scenario(attached_per_step, stop_after_sleeps):
    """The real supervisor loop with two adapters, using fakes that record every call."""
    names = {"created": [], "cleaned": [], "preserved": [], "children": []}
    owned = {}  # state file -> output name
    counter = iter(f"HEADLESS-{c}" for c in "ABCDEFGH")
    steps = iter(attached_per_step)
    last = attached_per_step[-1]

    class Child:
        def __init__(self, command): self.command = command; self.returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9

    def attached():
        nonlocal last
        try: last = next(steps)
        except StopIteration: pass
        return last
    def ensure(state_path=monitor.OWNERSHIP_FILE):
        name = next(counter); owned[str(state_path)] = name; names["created"].append((name, Path(state_path).name))
        return name, True
    def cleanup(name, state_path=monitor.OWNERSHIP_FILE): names["cleaned"].append((name, Path(state_path).name))
    def preserve(name, state_path=monitor.OWNERSHIP_FILE): names["preserved"].append((name, Path(state_path).name))
    def popen(command):
        child = Child(command); names["children"].append(child); return child
    sleeps = {"n": 0}
    def sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] >= stop_after_sleeps: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    patched = {"attached_locations": attached, "ensure_output": ensure, "assign_workspace": lambda *_: None,
               "wait_output_ready": lambda name: None, "owned_output_state": lambda name: "ready",
               "cleanup_output": cleanup, "cleanup_owned": lambda **_: None, "monitors": lambda: [],
               "preserve_output_for_restart": preserve, "apply_output_mode": lambda *_: False}
    original = {k: getattr(monitor, k) for k in patched}
    original_popen, original_sleep = monitor.subprocess.Popen, monitor.time.sleep
    try:
        for k, v in patched.items(): setattr(monitor, k, v)
        monitor.subprocess.Popen = popen; monitor.time.sleep = sleep
        class Args:
            streamer = "/streamer"; fps = 30; template = DEFAULT_TEMPLATE; seconds = None
        assert monitor.run_service(Args()) == 0
    finally:
        for k, v in original.items(): setattr(monitor, k, v)
        monitor.subprocess.Popen, monitor.time.sleep = original_popen, original_sleep
    return names


def option(command, flag):
    return command[command.index(flag) + 1] if flag in command else None


def test_two_adapters_get_their_own_output_streamer_and_state():
    names = run_two_adapter_scenario([["2-1", "2-2"]] * 5, 4)
    assert names["created"] == [("HEADLESS-A", "headless-outputs.json"), ("HEADLESS-B", "headless-outputs-2.json")]
    first, second = names["children"]
    assert option(first.command, "--monitor") == "HEADLESS-A" and option(first.command, "--usb-location") == "2-1"
    assert option(second.command, "--monitor") == "HEADLESS-B" and option(second.command, "--usb-location") == "2-2"
    # The first adapter keeps the historical single-adapter telemetry and log; the second gets its own.
    assert option(first.command, "--telemetry") is None
    assert option(second.command, "--telemetry").endswith("jua254-telemetry-2.json")
    assert sorted(names["preserved"]) == [("HEADLESS-A", "headless-outputs.json"), ("HEADLESS-B", "headless-outputs-2.json")]


def test_unplugging_one_adapter_leaves_the_other_running():
    names = run_two_adapter_scenario([["2-1", "2-2"]] * 3 + [["2-2"]] * 4, 6)
    assert names["cleaned"] == [("HEADLESS-A", "headless-outputs.json")]
    first, second = names["children"]
    assert first.returncode == 0          # the unplugged adapter's streamer was stopped
    assert second.returncode == 0         # the other stopped only at supervisor shutdown...
    assert names["preserved"] == [("HEADLESS-B", "headless-outputs-2.json")]  # ...keeping its output
    assert len(names["created"]) == 2     # no replacement output was created for the survivor

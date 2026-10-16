#!/usr/bin/env python3
"""Offline lifecycle tests for jua254_monitor ownership safeguards."""
import json
import signal
import tempfile
from pathlib import Path
from jua254_monitor import DEFAULT_TEMPLATE, apply_output_mode, cleanup_output, ensure_output, preserve_output_for_restart, recover_owned_output, remove_output, stream_command, wait_output_ready

def test_reuse_and_prune():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-4", "HEADLESS-2", "DP-1"]}))
        current = [{"name": "HEADLESS-2"}, {"name": "HEADLESS-4"}, {"name": "HEADLESS-99"}]
        calls = []
        name, created = ensure_output(state_path=state, list_monitors=lambda: current,
                                      runner=lambda *a: calls.append(a) or "")
        assert (name, created) == ("HEADLESS-2", False)
        assert calls == [("output", "remove", "HEADLESS-4")]
        assert json.loads(state.read_text())["outputs"] == ["HEADLESS-2"]

def test_create_and_cleanup():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"; visible = []
        def runner(*args):
            if args == ("output", "create", "headless"):
                visible.append({"name": "HEADLESS-8"}); return "ok\n"
            if args == ("output", "remove", "HEADLESS-8"):
                visible[:] = []; return "ok"
            raise AssertionError(args)
        name, created = ensure_output(state_path=state, list_monitors=lambda: visible, runner=runner)
        assert (name, created) == ("HEADLESS-8", True)
        cleanup_output(name, state_path=state, runner=runner)
        assert not visible and json.loads(state.read_text())["outputs"] == []

def test_unowned_and_physical_are_protected():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"; state.write_text(json.dumps({"outputs": ["HEADLESS-1"]}))
        calls = []
        cleanup_output("HEADLESS-2", state_path=state, runner=lambda *a: calls.append(a))
        assert calls == []
        try: remove_output("eDP-1", lambda *a: calls.append(a))
        except ValueError: pass
        else: raise AssertionError("physical output was accepted for removal")

def test_selector_string_is_resolved_to_monitor():
    # extcopy_selection.select_exact intentionally returns a name; lifecycle
    # code must resolve that name before reading monitor fields.
    from extcopy_selection import select_exact
    outputs = [{"name": "HEADLESS-3", "disabled": False, "activeWorkspace": {"id": 7}}]
    name = select_exact("HEADLESS-3", outputs)
    monitor = next(m for m in outputs if m["name"] == name)
    assert monitor["activeWorkspace"]["id"] == 7

def test_window_rule_places_helper_without_focus_or_workspace_dispatch():
    import jua254_monitor
    calls = []
    original_hypr = jua254_monitor.hypr
    original_clients = jua254_monitor.clients
    original_uuid4 = jua254_monitor.uuid.uuid4
    token = "1" * 32
    app_id = f"jua254-headless-helper-{token}"
    title = f"JUA254 HEADLESS Helper {token}"
    try:
        jua254_monitor.hypr = lambda *args: calls.append(args) or ""
        jua254_monitor.clients = lambda: [{"initialClass": app_id, "initialTitle": title,
                                           "workspace": {"name": "4"}, "floating": True,
                                           "size": [32, 32]}]
        jua254_monitor.uuid.uuid4 = lambda: type("UUID", (), {"hex": token})()
        client = jua254_monitor.dispatch_test_window(4, "foot --app-id {app_id} --title {title}")
    finally:
        jua254_monitor.hypr = original_hypr
        jua254_monitor.clients = original_clients
        jua254_monitor.uuid.uuid4 = original_uuid4
    assert client["workspace"]["name"] == "4"
    assert calls[0][0] == "eval" and "hl.window_rule" in calls[0][1]
    assert 'float = true' in calls[0][1] and 'size = {32, 32}' in calls[0][1]
    assert 'workspace = "4 silent"' in calls[0][1] and 'no_initial_focus = true' in calls[0][1]
    assert calls[1] == ("eval", f'hl.exec_cmd("foot --app-id {app_id} --title \'{title}\'")')
    assert "set_enabled(false)" in calls[2][1]
    assert not any("hl.dsp" in argument for call in calls for argument in call)

def test_stream_command_uses_package_template():
    class Args:
        seconds = None
        streamer = "/installed/linux/jua254_stream.py"
        fps = 30
        template = DEFAULT_TEMPLATE
    command = stream_command("HEADLESS-7", Args())
    assert command[command.index("--template") + 1] == str(DEFAULT_TEMPLATE)
    assert DEFAULT_TEMPLATE.is_absolute()
    assert "--diagnostic-bootstrap" not in command
    assert "--presentation-phase-offset-ms" not in command
    assert "--fb-rotation" in command
    assert "--sized-payload" in command
    assert "--command-ring" in command

def test_stream_command_fb_rotation_opt_out():
    class Args:
        seconds = None
        streamer = "/installed/linux/jua254_stream.py"
        fps = 30
        template = DEFAULT_TEMPLATE
        no_fb_rotation = True
    assert "--fb-rotation" not in stream_command("HEADLESS-7", Args())

def test_stream_command_payload_opt_outs():
    class Args:
        seconds = None
        streamer = "/installed/linux/jua254_stream.py"
        fps = 30
        template = DEFAULT_TEMPLATE
        no_sized_payload = True
        no_command_ring = True
    command = stream_command("HEADLESS-7", Args())
    assert "--sized-payload" not in command and "--command-ring" not in command
    assert "--fb-rotation" in command

def test_stream_command_gpu_path():
    class Args:
        seconds = None; fps = None; template = DEFAULT_TEMPLATE; streamer = "jua254_stream.py"
    # Production default: GPU capture, hardware cursor, 60 FPS, no damage tiles.
    command = stream_command("HEADLESS-7", Args())
    assert command[command.index("--capture") + 1] == "gpu-native"
    assert "--hw-cursor" in command and "--damage-tiles" not in command
    assert command[command.index("--fps") + 1] == "60"
    assert "--encoder" not in command and "--capture-direct-read" not in command
    Args.damage_tiles = True; Args.no_hw_cursor = True
    command = stream_command("HEADLESS-7", Args())
    assert "--damage-tiles" in command and "--hw-cursor" not in command
    Args.no_gpu = True
    command = stream_command("HEADLESS-7", Args())
    assert command[command.index("--capture") + 1] == "wlroots-native" and "--hw-cursor" not in command
    assert command[command.index("--fps") + 1] == "40"
    Args.fps = 30
    assert stream_command("HEADLESS-7", Args())[command.index("--fps") + 1] == "30"

def test_stream_command_mode():
    class Args:
        seconds = None; fps = None; template = DEFAULT_TEMPLATE; streamer = "jua254_stream.py"
    assert "--mode" not in stream_command("HEADLESS-7", Args())      # default stays byte-identical
    Args.mode = "1280x720@60"
    command = stream_command("HEADLESS-7", Args())
    assert command[command.index("--mode") + 1] == "1280x720@60"

def test_apply_output_mode_resizes_only_on_mismatch():
    calls = []
    outputs = [{"name": "HEADLESS-7", "width": 1920, "height": 1080, "scale": 1.25}]
    runner = lambda *a: calls.append(a) or "ok"
    assert apply_output_mode("HEADLESS-7", "1920x1080@60", lambda: outputs, runner) is False and calls == []
    assert apply_output_mode("HEADLESS-7", "1280x720@60", lambda: outputs, runner) is True
    assert calls == [("eval", 'hl.monitor({ output = "HEADLESS-7", mode = "1280x720@60", position = "auto", scale = 1.25, transform = 0 })')]
    assert apply_output_mode("HEADLESS-9", "1280x720@60", lambda: outputs, runner) is False

def test_apply_output_mode_turns_output_and_keeps_its_place():
    calls = []
    outputs = [{"name": "HEADLESS-7", "width": 1680, "height": 1050, "scale": 1, "x": 1920, "y": 0, "transform": 0}]
    runner = lambda *a: calls.append(a) or "ok"
    assert apply_output_mode("HEADLESS-7", "1680x1050@60", lambda: outputs, runner, transform=0) is False and calls == []
    assert apply_output_mode("HEADLESS-7", "1680x1050@60", lambda: outputs, runner, transform=3) is True
    assert calls == [("eval", 'hl.monitor({ output = "HEADLESS-7", mode = "1680x1050@60", position = "1920x0", scale = 1, transform = 3 })')]

def test_stream_command_explicitly_propagates_phase_experiment():
    class Args:
        seconds = 60
        streamer = "/installed/linux/jua254_stream.py"
        fps = 30
        template = DEFAULT_TEMPLATE
        presentation_phase_offset_ms = 0.0
    command = stream_command("HEADLESS-7", Args())
    index = command.index("--presentation-phase-offset-ms")
    assert command[index + 1] == "0.0"

def test_external_test_explicitly_enables_diagnostic_bars():
    class Args:
        command = "external-test"
        seconds = 30
        streamer = "/installed/linux/jua254_stream.py"
        fps = 30
        template = DEFAULT_TEMPLATE
    assert "--diagnostic-bootstrap" in stream_command("HEADLESS-7", Args())

def test_service_restart_reuses_existing_managed_output():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-9"]}))
        current = [{"name": "HEADLESS-9", "disabled": False, "width": 1920, "height": 1080,
                    "activeWorkspace": {"id": 5}}]
        calls = []
        name, created = ensure_output(state_path=state, list_monitors=lambda: current,
                                      runner=lambda *args: calls.append(args) or "")
        assert (name, created) == ("HEADLESS-9", False)
        assert calls == []
        assert wait_output_ready(name, list_monitors=lambda: current) == current[0]
        assert json.loads(state.read_text())["outputs"] == ["HEADLESS-9"]

def test_atomic_ownership_state_has_no_empty_restart_window():
    import jua254_monitor
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        jua254_monitor.save_owned({"HEADLESS-9"}, state)
        assert jua254_monitor.load_owned(state) == {"HEADLESS-9"}
        assert not state.with_name(state.name + ".tmp").exists()

def test_service_shutdown_preserves_output_for_replacement_process():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        preserve_output_for_restart("HEADLESS-9", state)
        assert json.loads(state.read_text())["outputs"] == ["HEADLESS-9"]

def ready(name):
    return {"name": name, "disabled": False, "width": 1920, "height": 1080,
            "activeWorkspace": {"id": 5}}

def test_unplug_replug_then_usb_error_reuses_replacement_output():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        visible = [ready("HEADLESS-A")]
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-A"]}))
        calls = []
        def runner(*args):
            calls.append(args)
            if args == ("output", "remove", "HEADLESS-A"):
                visible.clear(); return "ok"
            if args == ("output", "create", "headless"):
                visible.append(ready("HEADLESS-B")); return "ok"
            raise AssertionError(args)

        # Physical absence is the only event here that removes the old output.
        cleanup_output("HEADLESS-A", state_path=state, runner=runner)
        name, created = ensure_output(state_path=state, list_monitors=lambda: visible, runner=runner)
        assert (name, created) == ("HEADLESS-B", True)

        # USBError(19)/streamer exit is not compositor disappearance.
        name, output_state = recover_owned_output(name, state, lambda: visible)
        assert (name, output_state) == ("HEADLESS-B", "ready")
        name2, created2 = ensure_output(state_path=state, list_monitors=lambda: visible, runner=runner)
        assert (name2, created2) == ("HEADLESS-B", False)
        assert calls == [("output", "remove", "HEADLESS-A"), ("output", "create", "headless")]

def test_disappeared_owned_output_is_replaced_once():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-A"]}))
        visible = []
        name, output_state = recover_owned_output("HEADLESS-A", state, lambda: visible)
        assert (name, output_state) == (None, "absent")
        calls = []
        def runner(*args):
            calls.append(args)
            visible.append(ready("HEADLESS-B"))
            return "ok"
        assert ensure_output(state_path=state, list_monitors=lambda: visible, runner=runner) == ("HEADLESS-B", True)
        assert ensure_output(state_path=state, list_monitors=lambda: visible, runner=runner) == ("HEADLESS-B", False)
        assert calls == [("output", "create", "headless")]

def test_ten_streamer_exits_preserve_same_ready_output():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        visible = [ready("HEADLESS-B")]
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-B"]}))
        calls = []
        name = "HEADLESS-B"
        for _ in range(10):
            name, output_state = recover_owned_output(name, state, lambda: visible)
            assert (name, output_state) == ("HEADLESS-B", "ready")
            assert ensure_output(state_path=state, list_monitors=lambda: visible,
                                 runner=lambda *args: calls.append(args) or "") == ("HEADLESS-B", False)
        assert calls == []
        assert json.loads(state.read_text())["outputs"] == ["HEADLESS-B"]

def test_transient_compositor_failure_does_not_clear_ownership():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "owned.json"
        state.write_text(json.dumps({"version": 1, "outputs": ["HEADLESS-B"]}))
        def failed_query(): raise RuntimeError("temporary hyprctl failure")
        assert recover_owned_output("HEADLESS-B", state, failed_query) == ("HEADLESS-B", "unknown")
        assert json.loads(state.read_text())["outputs"] == ["HEADLESS-B"]

def run_supervisor_scenario(presence, child_statuses, stop_after_sleeps, seconds=None,
                            restart_after_bounded_exit=False):
    """Run the real supervisor loop with deterministic process/device fakes."""
    import jua254_monitor as monitor
    original = {name: getattr(monitor, name) for name in (
        "device_present", "attached_locations", "ensure_output", "assign_workspace", "wait_output_ready",
        "owned_output_state", "recover_owned_output", "cleanup_output",
        "cleanup_owned", "monitors", "preserve_output_for_restart")}
    original_popen = monitor.subprocess.Popen
    original_sleep = monitor.time.sleep
    events = {"creates": [], "children": [], "cleanup": [], "preserved": []}
    present_values = iter(presence)
    last_present = presence[-1]
    names = iter(("HEADLESS-A", "HEADLESS-B"))
    owned = {"name": None}

    class Child:
        def __init__(self, command, status):
            self.command = command; self.returncode = status
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9

    sleeps = {"count": 0}
    def sleep(_seconds):
        sleeps["count"] += 1
        if sleeps["count"] >= stop_after_sleeps:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    def device():
        nonlocal last_present
        try: last_present = next(present_values)
        except StopIteration: pass
        return last_present
    def ensure():
        name = next(names); owned["name"] = name; events["creates"].append(name)
        return name, True
    def popen(command):
        status = child_statuses.pop(0) if child_statuses else 1
        child = Child(command, status); events["children"].append(child)
        return child
    def recover(name):
        assert owned["name"] == name
        return name, "ready"
    def cleanup(name):
        events["cleanup"].append(name); owned["name"] = None

    try:
        monitor.device_present = device
        monitor.attached_locations = lambda: ["2-1"] if device() else []
        monitor.ensure_output = ensure
        monitor.assign_workspace = lambda *_: None
        monitor.wait_output_ready = lambda name: ready(name)
        monitor.owned_output_state = lambda name: "ready"
        monitor.recover_owned_output = recover
        monitor.cleanup_output = cleanup
        monitor.cleanup_owned = lambda: None
        monitor.monitors = lambda: [ready(owned["name"])] if owned["name"] else []
        monitor.preserve_output_for_restart = lambda name: events["preserved"].append(name)
        monitor.subprocess.Popen = popen
        monitor.time.sleep = sleep
        class Args:
            streamer = "/streamer"; fps = 30; template = DEFAULT_TEMPLATE
        Args.seconds = seconds
        Args.restart_after_bounded_exit = restart_after_bounded_exit
        assert monitor.run_service(Args()) == 0
    finally:
        for name, value in original.items(): setattr(monitor, name, value)
        monitor.subprocess.Popen = original_popen
        monitor.time.sleep = original_sleep
    return events

def test_child_status_one_and_capture_eof_restart_same_output():
    # The supervisor sees only child status; EOF/helper failures are contained
    # inside that child and follow this identical restart path.
    events = run_supervisor_scenario([True] * 8, [1, 1, 1], 3)
    assert events["creates"] == ["HEADLESS-A"]
    assert len(events["children"]) == 3
    assert all("HEADLESS-A" in child.command for child in events["children"])
    assert events["cleanup"] == []
    assert events["preserved"] == ["HEADLESS-A"]

def test_ten_child_failures_keep_supervisor_and_output():
    events = run_supervisor_scenario([True] * 24, [1] * 10, 10)
    assert events["creates"] == ["HEADLESS-A"]
    assert len(events["children"]) == 10
    assert all("HEADLESS-A" in child.command for child in events["children"])
    assert events["cleanup"] == []

def test_successful_bounded_child_exits_without_follow_on_stream():
    events = run_supervisor_scenario([True] * 4, [0, 0], 10, seconds=240)
    assert events["creates"] == ["HEADLESS-A"]
    assert len(events["children"]) == 1
    assert events["children"][0].returncode == 0
    assert events["preserved"] == ["HEADLESS-A"]

def test_bounded_follow_on_requires_explicit_restart_option():
    events = run_supervisor_scenario([True] * 6, [0, 0], 2, seconds=240,
                                     restart_after_bounded_exit=True)
    assert events["creates"] == ["HEADLESS-A"]
    assert len(events["children"]) == 2

def test_physical_unplug_waits_and_reconnect_creates_one_replacement():
    events = run_supervisor_scenario(
        [True, False, False, True, True, True], [None, 1], 4)
    assert events["creates"] == ["HEADLESS-A", "HEADLESS-B"]
    assert events["cleanup"] == ["HEADLESS-A"]
    assert events["children"][0].returncode == 0
    assert "HEADLESS-B" in events["children"][1].command

def test_signal_handler_state_survives_unplug_monitor_snapshot():
    # Regression for the exact production crash: the unplug monitor object
    # must never overwrite the saved signal-handler mapping used by finally.
    events = run_supervisor_scenario([True, False, True, True], [None, 1], 3)
    assert events["creates"] == ["HEADLESS-A", "HEADLESS-B"]
    assert events["preserved"] == ["HEADLESS-B"]

test_transient_compositor_query_failure_does_not_clear_ownership = test_transient_compositor_failure_does_not_clear_ownership

if __name__ == "__main__":
    import tempfile, jua254_monitor
    jua254_monitor.ADAPTER_SETTINGS = Path(tempfile.mkdtemp()) / "adapters.json"  # never the real settings
    for test in (test_reuse_and_prune, test_create_and_cleanup, test_unowned_and_physical_are_protected, test_selector_string_is_resolved_to_monitor, test_window_rule_places_helper_without_focus_or_workspace_dispatch, test_stream_command_uses_package_template, test_stream_command_fb_rotation_opt_out, test_stream_command_payload_opt_outs, test_stream_command_gpu_path, test_stream_command_mode, test_apply_output_mode_resizes_only_on_mismatch, test_apply_output_mode_turns_output_and_keeps_its_place, test_stream_command_explicitly_propagates_phase_experiment, test_external_test_explicitly_enables_diagnostic_bars, test_service_restart_reuses_existing_managed_output, test_atomic_ownership_state_has_no_empty_restart_window, test_service_shutdown_preserves_output_for_replacement_process, test_unplug_replug_then_usb_error_reuses_replacement_output, test_disappeared_owned_output_is_replaced_once, test_ten_streamer_exits_preserve_same_ready_output, test_transient_compositor_query_failure_does_not_clear_ownership, test_child_status_one_and_capture_eof_restart_same_output, test_ten_child_failures_keep_supervisor_and_output, test_successful_bounded_child_exits_without_follow_on_stream, test_bounded_follow_on_requires_explicit_restart_option, test_physical_unplug_waits_and_reconnect_creates_one_replacement, test_signal_handler_state_survives_unplug_monitor_snapshot): test()
    print("jua254_monitor lifecycle tests: PASS")

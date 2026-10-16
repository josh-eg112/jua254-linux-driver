#!/usr/bin/env python3
"""Own and stream a JUA254-backed independent Hyprland headless output."""
import argparse, json, os, shlex, signal, subprocess, sys, time, uuid
from pathlib import Path
from extcopy_selection import select_exact
from jua254_diagnostics import DiagnosticsState

STATE_DIR = Path(os.environ.get("JUA254_STATE_DIR", Path.home() / ".local/state/jua254"))
OWNERSHIP_FILE = STATE_DIR / "headless-outputs.json"
# Per-adapter settings, keyed by USB location ("bus-port"): {"3-1": {"mode": "1680x1050@60", "rotate": "left"}}.
ADAPTER_SETTINGS = Path(os.environ.get("JUA254_ADAPTER_SETTINGS", Path.home() / ".config/jua254/adapters.json"))
# Hyprland output transforms for a monitor turned on its side; if the picture is upside down, use the other.
ROTATIONS = {"none": 0, "right": 1, "left": 3}
HEADLESS_PREFIXES = ("HEADLESS-", "VIRTUAL-")
SUPPORTED_PRODUCTS = ("5600", "5601")  # JUA254 and A8640H (same MCT Trigger 6 hardware)
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "shared/handoff/linux-rgb-bars-20260825"

def hypr(*args):
    result = subprocess.run(["hyprctl", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "hyprctl failed")
    return result.stdout

def monitors():
    raw = hypr("monitors", "-j")
    try: return json.loads(raw)
    except json.JSONDecodeError as exc: raise RuntimeError(f"invalid hyprctl monitors JSON: {raw[:200]!r}") from exc

def clients():
    raw = hypr("clients", "-j")
    try: return json.loads(raw)
    except json.JSONDecodeError as exc: raise RuntimeError(f"invalid hyprctl clients JSON: {raw[:200]!r}") from exc

def _is_virtual(name): return any(name.startswith(p) for p in HEADLESS_PREFIXES)

def device_present():
    """Read-only VID/PID check; never opens or resets the adapter."""
    for vendor in Path("/sys/bus/usb/devices").glob("*/idVendor"):
        try:
            if vendor.read_text().strip().lower() == "0711" and (vendor.parent / "idProduct").read_text().strip().lower() in SUPPORTED_PRODUCTS:
                return True
        except OSError:
            pass
    return False

def attached_locations(root=Path("/sys/bus/usb/devices")):
    """Read-only: the USB locations ("bus-port.port", the sysfs device name) of every attached JUA254."""
    found = []
    for vendor in root.glob("*/idVendor"):
        try:
            if vendor.read_text().strip().lower() == "0711" and (vendor.parent / "idProduct").read_text().strip().lower() in SUPPORTED_PRODUCTS:
                found.append(vendor.parent.name)
        except OSError:
            pass
    return sorted(found)

def load_owned(path=OWNERSHIP_FILE):
    try:
        value = json.loads(Path(path).read_text())
        return {str(x) for x in value.get("outputs", []) if isinstance(x, str)}
    except (FileNotFoundError, OSError, ValueError, TypeError): return set()

def save_owned(names, path=OWNERSHIP_FILE):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "outputs": sorted(set(names))}, indent=2) + "\n")
    temporary.replace(path)

def remove_output(name, runner=hypr):
    if not _is_virtual(name): raise ValueError(f"refusing to remove non-virtual output {name!r}")
    return runner("output", "remove", name).strip()

def ensure_output(create=True, backend="headless", state_path=OWNERSHIP_FILE, list_monitors=monitors, runner=hypr):
    """Return (name, created), reusing one owned output and pruning owned duplicates."""
    current = {m.get("name") for m in list_monitors() if m.get("name")}
    owned = load_owned(state_path)
    present = sorted(n for n in owned if n in current and _is_virtual(n))
    chosen = present[0] if present else None
    for stale in present[1:]: remove_output(stale, runner)
    if chosen is None and create:
        # Hyprland returns plain ``ok`` here rather than the assigned name;
        # discover the new HEADLESS-N by comparing monitor listings.
        response = runner("output", "create", backend).strip()
        reported = response.splitlines()[-1] if response else ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            now = {m.get("name") for m in list_monitors() if m.get("name")}
            additions = sorted(n for n in now - current if _is_virtual(n))
            if additions:
                chosen = additions[0]
                break
            if _is_virtual(reported) and reported in now:
                chosen = reported
                break
            time.sleep(.05)
        else: raise RuntimeError(f"created output {chosen} did not appear in hyprctl monitors")
    save_owned({chosen} if chosen else set(), state_path)
    return chosen, chosen not in present

def cleanup_output(name, state_path=OWNERSHIP_FILE, runner=hypr):
    if not name: return
    owned = load_owned(state_path)
    if name in owned and _is_virtual(name):
        try: remove_output(name, runner)
        finally: save_owned(owned - {name}, state_path)

def cleanup_owned(state_path=OWNERSHIP_FILE, runner=hypr):
    """Remove all registered managed virtual outputs only."""
    for name in sorted(load_owned(state_path)):
        try: cleanup_output(name, state_path, runner)
        except (RuntimeError, ValueError): pass

def preserve_output_for_restart(name, state_path=OWNERSHIP_FILE):
    """Keep the managed name durable while systemd replaces the supervisor."""
    if name and _is_virtual(name):
        save_owned(load_owned(state_path) | {name}, state_path)

def output_workspace(output):
    work = output.get("activeWorkspace")
    return work.get("name") or work.get("id") if isinstance(work, dict) else work

def assign_workspace(name, workspace):
    """Best-effort restoration after a recreated output; unsupported APIs are harmless."""
    if workspace in (None, "", 0): return
    try:
        hypr("eval", f'hl.dispatch(hl.dsp.workspace.move({{ workspace = {json.dumps(str(workspace))}, monitor = {json.dumps(name)} }}))')
    except RuntimeError:
        pass

def owned_by_session():
    """Owned outputs per session state file: session 1's historical file, then numbered ones."""
    files = [OWNERSHIP_FILE] + sorted(STATE_DIR.glob("headless-outputs-*.json"))
    return {f.name: sorted(load_owned(f)) for f in files if load_owned(f)}

def print_status(requested=None):
    data = monitors(); per_session = owned_by_session()
    owned = sorted(name for names in per_session.values() for name in names)
    if requested is None and len(owned) == 1:
        requested = owned[0]
    selected = select_exact(requested, data) if requested else None
    print(json.dumps({"device_present": device_present(), "adapters": attached_locations(), "outputs": data,
                      "virtual_outputs": [m for m in data if _is_virtual(m.get("name", ""))],
                      "owned_outputs": owned, "owned_by_session": per_session, "requested": requested, "selected": selected,
                      "capture_protocol": "ext-image-copy-v1" if requested else None}, indent=2))

DEFAULT_MODE = "1920x1080@60"

def load_adapter_settings(path=None):
    try: data = json.loads(Path(path or ADAPTER_SETTINGS).read_text())
    except (OSError, ValueError): return {}
    return data if isinstance(data, dict) else {}

def adapter_settings(location, default_mode=DEFAULT_MODE, path=None):
    """(mode, rotate) for the adapter at `location`: its saved settings, else the defaults."""
    saved = load_adapter_settings(path).get(location) or {}
    mode = saved.get("mode") or default_mode
    rotate = saved.get("rotate") if saved.get("rotate") in ROTATIONS else "none"
    return mode, rotate

def save_adapter_settings(location, mode=None, rotate=None, reset=False, path=None):
    path = Path(path or ADAPTER_SETTINGS); data = load_adapter_settings(path)
    entry = {} if reset else dict(data.get(location) or {})
    if mode is not None: entry["mode"] = mode
    if rotate is not None: entry["rotate"] = rotate
    if entry.get("mode") == DEFAULT_MODE: entry.pop("mode")
    if entry.get("rotate") == "none": entry.pop("rotate")
    if entry: data[location] = entry
    else: data.pop(location, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n"); tmp.replace(path)
    return entry

def apply_output_mode(name, mode, list_monitors=None, runner=None, transform=0):
    """Size and turn the managed output to the adapter mode, keeping its scale and place; no-op when it
    already matches. Hyprland renders a turned output into a buffer of the mode's size (the sideways
    monitor's own orientation), so capture and the adapter mode are unchanged by the rotation."""
    list_monitors = list_monitors or monitors; runner = runner or hypr
    size, _, _ = mode.partition("@"); w, _, h = size.lower().partition("x")
    output = next((m for m in list_monitors() if m.get("name") == name), None)
    if output is None or ((output.get("width"), output.get("height")) == (int(w), int(h))
                          and output.get("transform", 0) == transform): return False
    scale = output.get("scale", 1)
    position = f'{output["x"]}x{output["y"]}' if isinstance(output.get("x"), int) and isinstance(output.get("y"), int) else "auto"
    runner("eval", f'hl.monitor({{ output = "{name}", mode = "{mode}", position = "{position}", scale = {scale}, '
                   f'transform = {transform} }})')
    return True

def wait_output_ready(name, timeout=3, list_monitors=monitors):
    """Wait until the exact output is enabled and has captureable dimensions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            output = next(m for m in list_monitors() if m.get("name") == name)
            if (not output.get("disabled", False) and output.get("width", 0) > 0 and
                    output.get("height", 0) > 0 and output_workspace(output) not in (None, "", 0)):
                return output
        except (RuntimeError, StopIteration):
            pass
        time.sleep(.05)
    raise RuntimeError(f"managed output {name} did not become capture-ready")

def owned_output_state(name, list_monitors=monitors):
    """Return ``ready``, ``not-ready``, ``absent``, or ``unknown``.

    Only a successful compositor snapshot may prove absence.  Streamer exit,
    USB errors, capture EOF/timeouts, and failed compositor queries say
    nothing about whether the independently-owned output still exists.
    """
    try:
        current = list_monitors()
    except RuntimeError:
        return "unknown"
    output = next((m for m in current if m.get("name") == name), None)
    if output is None:
        return "absent"
    if (not output.get("disabled", False) and output.get("width", 0) > 0 and
            output.get("height", 0) > 0 and output_workspace(output) not in (None, "", 0)):
        return "ready"
    return "not-ready"

def recover_owned_output(name, state_path=OWNERSHIP_FILE, list_monitors=monitors):
    """Preserve a present output after child failure; clear only proven stale ownership."""
    state = owned_output_state(name, list_monitors)
    if state == "absent":
        save_owned(load_owned(state_path) - {name}, state_path)
        return None, state
    preserve_output_for_restart(name, state_path)
    return name, state

def diagnostic(path=None):
    path = Path(path) if path else STATE_DIR / ("diagnostic-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    result = {"timestamp": time.time(), "device_present": device_present(), "owned_outputs": sorted(load_owned())}
    try: result["monitors"] = monitors()
    except Exception as exc: result["monitor_error"] = repr(exc)
    service = subprocess.run(["systemctl", "--user", "status", "jua254-monitor.service", "--no-pager"], capture_output=True, text=True)
    result["service_status"] = {"returncode": service.returncode, "stdout": service.stdout[-12000:], "stderr": service.stderr[-4000:]}
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"bundle": str(path), "device_present": result["device_present"], "owned_outputs": result["owned_outputs"]}, indent=2))
    return 0

def stream_command(name, args, mode=None):
    seconds = [] if args.seconds is None else ["--seconds", str(args.seconds)]
    diagnostic = ["--diagnostic-bootstrap"] if getattr(args, "command", None) in ("test", "external-test") else []
    state = ["--diagnostics-state", str(args.diagnostics_state)] if getattr(args, "diagnostics_state", None) else []
    phase = (["--presentation-phase-offset-ms", str(args.presentation_phase_offset_ms)]
             if getattr(args, "presentation_phase_offset_ms", None) is not None else [])
    rotation = [] if getattr(args, "no_fb_rotation", False) else ["--fb-rotation"]
    sized = [] if getattr(args, "no_sized_payload", False) else ["--sized-payload"]
    ring = [] if getattr(args, "no_command_ring", False) else ["--command-ring"]
    gpu = not getattr(args, "no_gpu", False)
    fps = args.fps if args.fps is not None else (60 if gpu else 40)
    if gpu:
        capture = ["--capture", "gpu-native"]
        capture += [] if getattr(args, "no_hw_cursor", False) else ["--hw-cursor"]
        capture += ["--damage-tiles"] if getattr(args, "damage_tiles", False) else []
        mode = mode or getattr(args, "mode", DEFAULT_MODE)
        capture += ["--mode", mode] if mode != DEFAULT_MODE else []
    else:
        capture = ["--capture", "wlroots-native", "--encoder", "hardware", "--capture-direct-read", "--capture-zero-copy"]
    cmd = [sys.executable, args.streamer, *capture, "--monitor", name,
           "--fps", str(fps), "--template", str(args.template), *diagnostic, *state, *phase, *rotation, *sized, *ring, *seconds, "--stats"]
    return cmd

def stream_output(name, args):
    child = subprocess.Popen(stream_command(name, args))
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.send_signal(signal.SIGTERM)
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
        raise

class Session:
    """One adapter's managed output and streamer. Session 1 keeps the historical single-adapter state
    files (and calls the ownership helpers exactly as before); later sessions get numbered ones."""
    def __init__(self, location, number, workspace_hint=None):
        self.location, self.number, self.workspace_hint = location, number, workspace_hint
        self.name = None; self.child = None
        self.settings = None  # (mode, rotate) the running streamer and output were set up with
        self.kw = {} if number == 1 else {"state_path": STATE_DIR / f"headless-outputs-{number}.json"}

    def stream_args(self):
        extra = ["--usb-location", self.location]
        if self.number > 1:
            extra += ["--telemetry", f"/run/user/{os.getuid()}/jua254-telemetry-{self.number}.json",
                      "--log", str(STATE_DIR / f"stream-{self.number}.json")]
        return extra

    def stop_child(self):
        if self.child is not None:
            self.child.terminate()
            try: self.child.wait(timeout=5)
            except subprocess.TimeoutExpired: self.child.kill(); self.child.wait()
            self.child = None

    def unplugged(self):
        """The adapter is gone: stop its streamer and remove its output, remembering the workspace."""
        self.stop_child()
        if self.name:
            try:
                previous_monitor = next(m for m in monitors() if m.get("name") == self.name)
                self.workspace_hint = output_workspace(previous_monitor)
            except (RuntimeError, StopIteration):
                pass
            try: cleanup_output(self.name, **self.kw)
            except RuntimeError as exc:
                print(f"JUA254 output cleanup deferred: {exc}", flush=True)
            self.name = None
        else:
            cleanup_owned(**self.kw)

    def step(self, args, diagnostics, generation):
        """One supervision step; returns seconds to sleep, or None when a bounded run completed."""
        if self.name is None:
            try:
                self.name, _ = ensure_output(**self.kw)
                assign_workspace(self.name, self.workspace_hint)
                wait_output_ready(self.name)
                print(f"JUA254 managed output={self.name} adapter={self.location}", flush=True)
            except RuntimeError as exc:
                print(f"JUA254 compositor unavailable: {exc}; retrying", flush=True)
                return 1
        if self.child is None:
            state = owned_output_state(self.name)
            if state == "absent":
                save_owned(load_owned(**self.path_kw()) - {self.name}, **self.path_kw())
                self.name = None
                return 1
            if state != "ready":
                return 1
            try:
                mode, rotate = adapter_settings(self.location, getattr(args, "mode", DEFAULT_MODE))
                if apply_output_mode(self.name, mode, transform=ROTATIONS[rotate]): wait_output_ready(self.name)
                self.settings = (mode, rotate)
                self.child = subprocess.Popen(stream_command(self.name, args, mode) + self.stream_args())
                diagnostics.event("child_start", generation=generation, child_pid=getattr(self.child, "pid", None),
                                  output=self.name, adapter=self.location)
            except (OSError, RuntimeError) as exc:
                diagnostics.counter("child_start_failures", diagnostics.state["counters"].get("child_start_failures", 0) + 1,
                                    error=repr(exc))
                print(f"JUA254 streamer start failed: {exc}; retrying", flush=True)
                return 1
        if self.child.poll() is None and adapter_settings(self.location, getattr(args, "mode", DEFAULT_MODE)) != self.settings:
            # Its resolution or rotation was changed (jua254-monitor-adapter): restart in the new one.
            print(f"JUA254 adapter={self.location} settings changed; restarting its streamer", flush=True)
            self.stop_child()
            return .1
        if self.child.poll() is not None:
            child_status = self.child.returncode
            diagnostics.event("child_exit", generation=generation, child_pid=getattr(self.child, "pid", None),
                              exit_code=child_status if child_status >= 0 else None,
                              signal=-child_status if child_status < 0 else None, adapter=self.location)
            self.child = None
            if args.seconds is not None and child_status == 0 and not getattr(args, "restart_after_bounded_exit", False):
                diagnostics.event("bounded_complete", generation=generation, output=self.name, exit_code=child_status)
                print("JUA254 bounded streamer completed; supervisor exiting", flush=True)
                return None
            print(f"JUA254 streamer exited status={child_status} adapter={self.location}; recovering child", flush=True)
            self.name, _ = recover_owned_output(self.name, **self.kw)
            # The compositor still owns the exact output (or could not be queried): retain ownership
            # and wait; never create a duplicate merely because capture is temporarily unready.
            return 1
        return .5

    def path_kw(self):
        return {"path": self.kw["state_path"]} if self.kw else {}

def run_service(args):
    """Maintain one managed output and streamer per attached adapter across unplug/replug."""
    stopping = False
    diagnostics = DiagnosticsState(getattr(args, "supervisor_diagnostics_state", None), "monitor-supervisor")
    generation = f"{os.getpid()}-{time.monotonic_ns()}"
    diagnostics.event("supervisor_start", generation=generation)
    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True
    signal_handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for s in signal_handlers: signal.signal(s, stop)
    sessions = {}; hints = {}
    try:
        while not stopping:
            attached = attached_locations()
            for location in [l for l in sessions if l not in attached]:
                session = sessions.pop(location)
                session.unplugged()
                hints[session.number] = session.workspace_hint
                print(f"JUA254 adapter {location} unplugged", flush=True)
            for location in attached:
                if location not in sessions:
                    used = {s.number for s in sessions.values()}
                    number = next(n for n in range(1, len(used) + 2) if n not in used)
                    sessions[location] = Session(location, number, hints.get(number))
            if not sessions:
                cleanup_owned()
                time.sleep(1)
                continue
            waits = [session.step(args, diagnostics, generation) for session in sorted(sessions.values(), key=lambda s: s.number)]
            if None in waits:
                break
            time.sleep(min(waits))
    finally:
        diagnostics.event("supervisor_stop", generation=generation, stopping=stopping)
        # Preserve the owned outputs and state across a systemd restart. USB unplug removes them in
        # the loop above; the explicit stop command uses the cleanup subcommand after the service stops.
        for session in sessions.values():
            session.stop_child()
            preserve_output_for_restart(session.name, **session.kw)
        if not sessions:
            preserve_output_for_restart(None)
        for s, handler in signal_handlers.items(): signal.signal(s, handler)
    return 0

def dispatch_test_window(workspace, command, timeout=3):
    """Map a uniquely identified helper directly onto the HEADLESS workspace."""
    token = uuid.uuid4().hex
    app_id = f"jua254-headless-helper-{token}"
    title = f"JUA254 HEADLESS Helper {token}"
    rule_var = f"jua254_headless_rule_{token}"
    launch = command.replace("{app_id}", shlex.quote(app_id)).replace("{title}", shlex.quote(title))
    rule = (
        f'_G.{rule_var}=hl.window_rule({{ name = {json.dumps(app_id)}, '
        f'match = {{ initial_class = {json.dumps("^" + app_id + "$")}, '
        f'initial_title = {json.dumps("^" + title + "$")} }}, float = true, '
        f'size = {{32, 32}}, workspace = {json.dumps(str(workspace) + " silent")}, '
        'no_initial_focus = true })'
    )
    hypr("eval", rule)
    try:
        hypr("eval", "hl.exec_cmd(" + json.dumps(launch) + ")")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = [c for c in clients() if c.get("initialClass") == app_id and c.get("initialTitle") == title]
            if len(matches) == 1:
                client = matches[0]
                if (client.get("workspace", {}).get("name") != str(workspace) or
                        not client.get("floating") or
                        any(abs(actual - expected) > 2 for actual, expected in zip(client.get("size", ()), (32, 32))) or
                        len(client.get("size", ())) != 2):
                    raise RuntimeError(f"HEADLESS helper placement mismatch: {client}")
                return client
            time.sleep(.05)
        raise RuntimeError(f"HEADLESS helper {app_id} did not map within {timeout}s")
    finally:
        hypr("eval", f'if _G.{rule_var} then _G.{rule_var}:set_enabled(false); _G.{rule_var}=nil end')

def run_lifecycle(args, external_test=False):
    name, _ = ensure_output()
    if not name: raise RuntimeError("could not create or reuse a JUA254 headless output")
    removed = False; old_handlers = {}
    def cleanup():
        nonlocal removed
        if not removed: removed = True; cleanup_output(name)
    def interrupt(_sig, _frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.getsignal(sig); signal.signal(sig, interrupt)
    try:
        if apply_output_mode(name, getattr(args, "mode", DEFAULT_MODE)): wait_output_ready(name)
        current_monitors = monitors()
        select_exact(name, current_monitors)  # exact-match guard; returns the name
        selected = next(m for m in current_monitors if m.get("name") == name)
        if not selected or selected.get("disabled", False): raise RuntimeError(f"owned output {name} is missing or disabled")
        workspace = output_workspace(selected)
        if external_test:
            dispatch_test_window(workspace, args.window_command)
            print(f"JUA254_EXTERNAL_TEST output={name} workspace={workspace} window={args.window_command}", flush=True)
            print("The physical JUA254 display should show this test window; the primary display should remain unchanged.", flush=True)
        else: print(f"JUA254_HEADLESS_OUTPUT={name} workspace={workspace}", flush=True)
        result = stream_output(name, args)
        if external_test:
            primary = [m for m in monitors() if m.get("name") != name and not _is_virtual(m.get("name", ""))]
            checks = {"output_enabled": bool(selected and not selected.get("disabled", False)),
                      "own_workspace": workspace not in (None, "", 0),
                      "primary_present": bool(primary), "capture_source": name,
                      "independent_source_verified": True}
            print("JUA254_EXTERNAL_TEST " + ("PASS" if all(checks[k] for k in ("output_enabled", "own_workspace", "primary_present", "independent_source_verified")) else "FAIL") + " " + json.dumps(checks), flush=True)
        return result
    finally:
        cleanup()
        for sig, handler in old_handlers.items(): signal.signal(sig, handler)

def adapter_command(args):
    attached = attached_locations()
    if args.location is None:
        if not attached: print("No JUA254 adapter is plugged in."); return 0
        for n, location in enumerate(attached, 1):
            mode, rotate = adapter_settings(location, os.environ.get("JUA254_MODE", DEFAULT_MODE))
            print(f"{location}: {mode}" + (f", rotated {rotate}" if rotate != "none" else ""))
        print("Change one with: jua254-monitor-adapter LOCATION --mode 1680x1050@60 --rotate left")
        return 0
    if args.mode is not None:
        from jua254_stream import VideoMode
        try: VideoMode.parse(args.mode)
        except ValueError as exc: print(f"--mode: {exc}", file=sys.stderr); return 2
    if args.location not in attached:
        print(f"Note: no adapter at {args.location} right now; the setting applies when one is plugged in there.")
    entry = save_adapter_settings(args.location, args.mode, args.rotate, args.reset)
    mode = entry.get("mode", DEFAULT_MODE); rotate = entry.get("rotate", "none")
    print(f"{args.location}: {mode}" + (f", rotated {rotate}" if rotate != "none" else "") +
          " (a running jua254-monitor service applies it within a second)")
    return 0

def main():
    ap = argparse.ArgumentParser(description="JUA254 independent Hyprland output controller")
    sub = ap.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status"); status.add_argument("--requested")
    diag = sub.add_parser("diagnostic"); diag.add_argument("--output")
    create = sub.add_parser("create"); create.add_argument("--backend", choices=("headless", "wayland", "x11"), default="headless")
    remove = sub.add_parser("remove"); remove.add_argument("name")
    sub.add_parser("cleanup")
    adapter = sub.add_parser("adapter", help="show or change each adapter's resolution and rotation")
    adapter.add_argument("location", nargs="?", help="USB location as listed (e.g. 3-1); omit to list the adapters")
    adapter.add_argument("--mode", help="output resolution WxH@R from the adapter's mode table, e.g. 1680x1050@60")
    adapter.add_argument("--rotate", choices=sorted(ROTATIONS), help="for a monitor turned on its side")
    adapter.add_argument("--reset", action="store_true", help="back to 1920x1080@60, not rotated")
    for command in ("test", "external-test", "run"):
        p = sub.add_parser(command); p.add_argument("--fps", type=float, default=None,
                                                    help="frame rate; default 60 on the GPU path, 40 with --no-gpu")
        p.add_argument("--presentation-phase-offset-ms", type=float, default=None,
                       help="experimental compositor-presentation phase offset; default disabled")
        p.add_argument("--no-fb-rotation", action="store_true",
                       help="decode every frame into the scanned-out VRAM buffer instead of rotating three buffers")
        p.add_argument("--no-sized-payload", action="store_true",
                       help="pad every type-3 transfer to the captured 471008-byte envelope instead of sizing it to the JPEG")
        p.add_argument("--no-command-ring", action="store_true",
                       help="reuse the captured JPEG upload address instead of advancing it through a VRAM ring")
        p.add_argument("--no-gpu", action="store_true",
                       help="use the SHM + FFmpeg path instead of GPU dmabuf capture and VA-API JPEG (jua254-gpu-capture)")
        p.add_argument("--no-hw-cursor", action="store_true",
                       help="GPU path: draw the pointer into frames instead of with the adapter's hardware cursor")
        p.add_argument("--damage-tiles", action="store_true", help="GPU path: send small damaged regions as type-7 tiles")
        p.add_argument("--mode", default=os.environ.get("JUA254_MODE", DEFAULT_MODE),
                       help="adapter output mode WxH@R from its mode table (GPU path only; default $JUA254_MODE or 1920x1080@60); "
                            "the headless output is resized to match")
        p.add_argument("--seconds", type=float, default=None if command == "run" else 30)
        p.add_argument("--restart-after-bounded-exit", action="store_true",
                       help="explicitly restart a successful --seconds run; default exits after completion")
        p.add_argument("--streamer", default=os.path.join(os.path.dirname(__file__), "jua254_stream.py"))
        p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
        p.add_argument("--window-command", default="foot --app-id {app_id} --title {title}")
        p.add_argument("--diagnostics-state", type=Path, help="opt-in streamer diagnostics JSON (off by default)")
        p.add_argument("--supervisor-diagnostics-state", type=Path,
                       help="opt-in supervisor diagnostics JSON (off by default)")
    args = ap.parse_args()
    if args.command == "status": print_status(args.requested); return 0
    if args.command == "diagnostic": return diagnostic(args.output)
    if args.command == "create":
        name, created = ensure_output(backend=args.backend); print(json.dumps({"name": name, "created": created, "owned": sorted(load_owned())}, indent=2)); return 0
    if args.command == "remove": cleanup_output(args.name); return 0
    if args.command == "cleanup": cleanup_owned(); return 0
    if args.command == "adapter": return adapter_command(args)
    if args.command == "run": return run_service(args)
    return run_lifecycle(args, external_test=args.command == "external-test")

if __name__ == "__main__": raise SystemExit(main())

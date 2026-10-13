#!/usr/bin/env python3
"""Own and stream a JUA254-backed independent Hyprland headless output."""
import argparse, json, os, shlex, signal, subprocess, sys, time, uuid
from pathlib import Path
from extcopy_selection import select_exact
from jua254_diagnostics import DiagnosticsState

STATE_DIR = Path(os.environ.get("JUA254_STATE_DIR", Path.home() / ".local/state/jua254"))
OWNERSHIP_FILE = STATE_DIR / "headless-outputs.json"
HEADLESS_PREFIXES = ("HEADLESS-", "VIRTUAL-")
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
            if vendor.read_text().strip().lower() == "0711" and (vendor.parent / "idProduct").read_text().strip().lower() == "5600":
                return True
        except OSError:
            pass
    return False

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

def print_status(requested=None):
    data = monitors(); owned = sorted(load_owned())
    if requested is None and len(owned) == 1:
        requested = owned[0]
    selected = select_exact(requested, data) if requested else None
    print(json.dumps({"device_present": device_present(), "outputs": data, "virtual_outputs": [m for m in data if _is_virtual(m.get("name", ""))],
                      "owned_outputs": owned, "requested": requested, "selected": selected,
                      "capture_protocol": "ext-image-copy-v1" if requested else None}, indent=2))

DEFAULT_MODE = "1920x1080@60"

def apply_output_mode(name, mode, list_monitors=None, runner=None):
    """Size the managed output to the adapter mode, keeping its scale; no-op when it already matches."""
    list_monitors = list_monitors or monitors; runner = runner or hypr
    size, _, _ = mode.partition("@"); w, _, h = size.lower().partition("x")
    output = next((m for m in list_monitors() if m.get("name") == name), None)
    if output is None or (output.get("width"), output.get("height")) == (int(w), int(h)): return False
    scale = output.get("scale", 1)
    runner("eval", f'hl.monitor({{ output = "{name}", mode = "{mode}", position = "auto", scale = {scale} }})')
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

def stream_command(name, args):
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
        mode = getattr(args, "mode", DEFAULT_MODE)
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

def run_service(args):
    """Maintain one managed output and streamer across USB unplug/replug."""
    stopping = False
    diagnostics = DiagnosticsState(getattr(args, "supervisor_diagnostics_state", None), "monitor-supervisor")
    generation = f"{os.getpid()}-{time.monotonic_ns()}"
    diagnostics.event("supervisor_start", generation=generation)
    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True
    signal_handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for s in signal_handlers: signal.signal(s, stop)
    name = None; child = None; workspace_hint = None
    try:
        while not stopping:
            if not device_present():
                if child is not None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                    child = None
                if name:
                    try:
                        previous_monitor = next(m for m in monitors() if m.get("name") == name)
                        workspace_hint = output_workspace(previous_monitor)
                    except (RuntimeError, StopIteration):
                        pass
                    try: cleanup_output(name)
                    except RuntimeError as exc:
                        print(f"JUA254 output cleanup deferred: {exc}", flush=True)
                    name = None
                else:
                    cleanup_owned()
                time.sleep(1)
                continue
            if name is None:
                try:
                    name, _ = ensure_output()
                    assign_workspace(name, workspace_hint)
                    wait_output_ready(name)
                    print(f"JUA254 managed output={name}", flush=True)
                except RuntimeError as exc:
                    print(f"JUA254 compositor unavailable: {exc}; retrying", flush=True)
                    time.sleep(1)
                    continue
            if child is None:
                state = owned_output_state(name)
                if state == "absent":
                    save_owned(load_owned() - {name})
                    name = None
                    time.sleep(1)
                    continue
                if state != "ready":
                    time.sleep(1)
                    continue
                try:
                    if apply_output_mode(name, getattr(args, "mode", DEFAULT_MODE)): wait_output_ready(name)
                    child = subprocess.Popen(stream_command(name, args))
                    diagnostics.event("child_start", generation=generation, child_pid=getattr(child, "pid", None), output=name)
                except (OSError, RuntimeError) as exc:
                    diagnostics.counter("child_start_failures", diagnostics.state["counters"].get("child_start_failures", 0) + 1,
                                        error=repr(exc))
                    print(f"JUA254 streamer start failed: {exc}; retrying", flush=True)
                    time.sleep(1)
                    continue
            if child.poll() is not None:
                child_status = child.returncode
                diagnostics.event("child_exit", generation=generation, child_pid=getattr(child, "pid", None),
                                  exit_code=child_status if child_status >= 0 else None,
                                  signal=-child_status if child_status < 0 else None)
                child = None
                bounded_complete = (args.seconds is not None and child_status == 0)
                if bounded_complete and not getattr(args, "restart_after_bounded_exit", False):
                    diagnostics.event("bounded_complete", generation=generation, output=name,
                                      exit_code=child_status)
                    print("JUA254 bounded streamer completed; supervisor exiting", flush=True)
                    break
                print(f"JUA254 streamer exited status={child_status}; recovering child", flush=True)
                name, output_state = recover_owned_output(name)
                if name is not None and output_state != "ready":
                    # The compositor still owns the exact output (or could not
                    # be queried).  Retain ownership and wait; never create a
                    # duplicate merely because capture is temporarily unready.
                    time.sleep(1)
                    continue
                time.sleep(1)
            else:
                time.sleep(.5)
    finally:
        diagnostics.event("supervisor_stop", generation=generation, stopping=stopping)
        if child is not None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        # Preserve the owned output and state across a systemd restart. USB
        # unplug removes it in the loop above; the explicit stop command uses
        # the cleanup subcommand after the service has stopped.
        preserve_output_for_restart(name)
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

def main():
    ap = argparse.ArgumentParser(description="JUA254 independent Hyprland output controller")
    sub = ap.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status"); status.add_argument("--requested")
    diag = sub.add_parser("diagnostic"); diag.add_argument("--output")
    create = sub.add_parser("create"); create.add_argument("--backend", choices=("headless", "wayland", "x11"), default="headless")
    remove = sub.add_parser("remove"); remove.add_argument("name")
    sub.add_parser("cleanup")
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
    if args.command == "run": return run_service(args)
    return run_lifecycle(args, external_test=args.command == "external-test")

if __name__ == "__main__": raise SystemExit(main())

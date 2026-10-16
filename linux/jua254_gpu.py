#!/usr/bin/env python3
"""GPU capture path for the JUA254 streamer.

The native helper (native/gpu_capture.c) captures the output as dmabufs and
encodes JPEG with VA-API, so no pixels cross the CPU. It reports either a full
frame (sent as a flipped type-3 update) or damage tiles (sent as type-7
FB_DIRECT updates into the displayed buffer), plus an optional separate cursor
stream that drives the adapter's hardware cursor.
"""
import json, os, select, struct, subprocess, threading, time
from pathlib import Path

from cursor_probe import SIZE as CURSOR_SIZE, position_transfer, shape_payload, shape_transfers, state_transfer

REC = struct.Struct("<4sIIIiiIIQQ")
FULL, TILE, FRAME_END, CURSOR_SHAPE, CURSOR_POS, CURSOR_LEAVE = 1, 2, 3, 4, 5, 6
NATIVE = Path(__file__).resolve().parent / "native"
BINARY = NATIVE / "jua254-gpu-capture"


class Frame:
    __slots__ = ("full", "items", "requested", "ready", "seq")
    def __init__(self, full, items, requested, ready, seq):
        self.full = full; self.items = items; self.requested = requested; self.ready = ready; self.seq = seq


def parse_records(buffer):
    """Split complete records off the front of `buffer` (consumed in place)."""
    out = []; offset = 0
    while len(buffer) - offset >= REC.size:
        magic, kind, length, seq, x, y, w, h, requested, ready = REC.unpack_from(buffer, offset)
        if magic != b"JGR1": raise RuntimeError("GPU helper stream lost sync")
        if len(buffer) - offset - REC.size < length: break
        start = offset + REC.size
        out.append((kind, seq, x, y, w, h, requested / 1e9, ready / 1e9, bytes(buffer[start:start + length])))
        offset = start + length
    del buffer[:offset]
    return out


class FrameAssembler:
    """Group FULL/TILE records into frames at each FRAME_END; pass cursor records through."""
    def __init__(self): self.items = []; self.full = False
    def feed(self, records):
        frames, cursor = [], []
        for record in records:
            kind = record[0]
            if kind == FULL: self.items.append(record); self.full = True
            elif kind == TILE: self.items.append(record)
            elif kind == FRAME_END:
                frames.append(Frame(self.full, self.items, record[6], record[7], record[1]))
                self.items = []; self.full = False
            else: cursor.append(record)
        return frames, cursor


def coalesce(pending):
    """Collapse queued frames into one update: the newest full frame (if any)
    followed by every later tile, keeping only the newest copy of each tile."""
    last_full = max((i for i, f in enumerate(pending) if f.full), default=None)
    frames = pending if last_full is None else pending[last_full:]
    dropped = len(pending) - len(frames)
    base = frames[0] if frames[0].full else None
    tiles = {}
    for frame in frames[1:] if base else frames:
        for item in frame.items: tiles[(item[2], item[3], item[4], item[5])] = item
    newest = frames[-1]
    return base, list(tiles.values()), newest, dropped


class GpuCapture:
    def __init__(self, monitor, metrics, quality=80, hw_cursor=False, damage_tiles=False, tile="32x1080",
                 max_tiles=24, full_threshold=0.4, frame_timeout_ms=500):
        if not BINARY.exists() or BINARY.stat().st_mtime < (NATIVE / "gpu_capture.c").stat().st_mtime:
            subprocess.run(["make", "-C", str(NATIVE), "jua254-gpu-capture"], check=True)
        command = [str(BINARY), monitor, "--quality", str(quality), "--frame-timeout-ms", str(frame_timeout_ms)]
        if hw_cursor: command.append("--hw-cursor")
        if damage_tiles: command += ["--damage-tiles", "--tile", tile, "--max-tiles", str(max_tiles), "--full-threshold", str(full_threshold)]
        self.command = command
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.buffer = bytearray(); self.assembler = FrameAssembler(); self.metrics = metrics
        threading.Thread(target=self._diagnostics, daemon=True).start()

    def _diagnostics(self):
        m = self.metrics
        for line in iter(self.proc.stderr.readline, b""):
            try: event = json.loads(line)
            except ValueError: m["last_native_capture_message"] = line.decode(errors="replace").strip(); continue
            kind = event.get("event")
            if kind == "frame":
                m["capture_attempts"] += 1; m["successful_captures"] += 1; m["encoded_frames"] += 1
                m["capture_ready_wait"].append(event.get("ready_wait_ms", 0.0))
                m["encode"].append(event.get("process_ms", 0.0))
                m["gpu_full_frames" if event.get("full") else "gpu_tile_frames"] += 1
                m["gpu_tiles"] += event.get("tiles", 0)
                if "quality" in event: m["jpeg_quality"] = event["quality"]; m["jpeg_reencodes"] = event.get("reencodes", 0)
            elif kind == "frame-failed": m["capture_attempts"] += 1; m["capture_failures"] += 1
            elif kind == "idle": m["capture_idle_waits"] += 1
            elif kind in ("ready", "constraints"): m[f"native_capture_{kind}"] = event
            elif kind == "fatal": m["last_capture_error"] = event

    def fileno(self): return self.proc.stdout.fileno()

    def read(self):
        chunk = os.read(self.fileno(), 1 << 20)
        if not chunk: raise RuntimeError(f"GPU helper exited ({self.proc.poll()}): {self.metrics.get('last_capture_error')}")
        self.buffer.extend(chunk)
        return self.assembler.feed(parse_records(self.buffer))

    def request_full(self):
        try: self.proc.stdin.write(b"F"); self.proc.stdin.flush()
        except OSError: pass

    def close(self):
        try: self.proc.stdin.close()
        except OSError: pass
        try: self.proc.terminate(); self.proc.wait(timeout=2)
        except Exception: self.proc.kill()


def pad_cursor(width, height, pixels, size=CURSOR_SIZE):
    """Top-left place a width x height ARGB8888 image in the size x size slot."""
    out = bytearray(size * size * 4); row = min(width, size) * 4
    for y in range(min(height, size)):
        out[y * size * 4:y * size * 4 + row] = pixels[y * width * 4:y * width * 4 + row]
    return bytes(out)


class HardwareCursor:
    """Mirror the compositor pointer onto the adapter's hardware cursor slot 0."""
    def __init__(self, metrics):
        self.metrics = metrics; self.shape = None; self.hot = (0, 0); self.pos = None; self.inside = False
        self.reset()
    def reset(self):
        self.shape_dirty = self.shape is not None; self.pos_dirty = self.pos is not None; self.shown = None
    def handle(self, record):
        kind, _, x, y, w, h, _, _, payload = record
        if kind == CURSOR_SHAPE:
            if w > CURSOR_SIZE or h > CURSOR_SIZE: self.metrics["cursor_cropped"] += 1
            self.shape = pad_cursor(w, h, payload); self.hot = (x, y); self.shape_dirty = True; self.pos_dirty = self.pos is not None
        elif kind == CURSOR_POS: self.pos = (x, y); self.inside = True; self.pos_dirty = True
        elif kind == CURSOR_LEAVE: self.inside = False
    def transfers(self):
        out = []
        if self.shape_dirty and self.shape is not None:
            out += shape_transfers(shape_payload(self.shape)); self.shape_dirty = False; self.metrics["cursor_shapes"] += 1
        visible = self.inside and self.shape is not None and self.pos is not None
        if visible and self.pos_dirty:
            out.append(position_transfer(self.pos[0] - self.hot[0], self.pos[1] - self.hot[1])); self.pos_dirty = False
            self.metrics["cursor_moves"] += 1
        if visible != self.shown: out.append(state_transfer(visible)); self.shown = visible
        return out
    def apply(self, dev):
        for request_type, request, value, index, data in self.transfers():
            written = dev.ctrl_transfer(request_type, request, value, index, data, timeout=1000)
            if data is not None and written != len(data): raise RuntimeError("short cursor control write")


def check_output_size(constraints, mode):
    """The captured output must match the adapter mode the buffers are laid out for."""
    if not constraints: return
    want = (mode.width, mode.height) if mode is not None else (1920, 1080)
    got = (constraints.get("width"), constraints.get("height"))
    if got != want:
        raise RuntimeError(f"captured output is {got[0]}x{got[1]} but adapter mode is {want[0]}x{want[1]}")

def run_gpu(args, template, framer, metrics, presentation, telemetry, diagnostics, stop, open_device, query_vram_mb,
            write_group_timed, record_successful_usb_update, payload_sha256, log_open_failure=None, note_adapter_ok=None):
    """Main loop for --capture gpu-native. Returns the open device (or None) for cleanup."""
    for name in ("gpu_full_frames", "gpu_tile_frames", "gpu_tiles", "capture_idle_waits", "cursor_shapes",
                 "cursor_moves", "cursor_cropped", "frames_coalesced", "tiles_sent", "full_sent", "tiles_skipped_need_full", "too_large"):
        metrics[name] = 0
    capture = GpuCapture(args.monitor or "eDP-1", metrics, args.quality, args.hw_cursor, args.damage_tiles,
                         args.tile, args.max_tiles, args.full_threshold)
    cursor = HardwareCursor(metrics) if args.hw_cursor else None
    dev = None; pending = []; need_full = True; period = 1 / args.fps; last_send = 0.0
    stream_started = None; previous_sent = None; next_open = 0.0
    try:
        while not stop.is_set() and (args.seconds is None or stream_started is None or time.monotonic() - stream_started < args.seconds):
            now = time.monotonic()
            timeout = max(0.0, last_send + period - now) if pending else 0.25
            if dev is None: timeout = min(timeout, max(0.0, next_open - now))
            ready, _, _ = select.select([capture], [], [], timeout)
            if ready:
                frames, cursor_records = capture.read()
                check_output_size(metrics.get("native_capture_constraints"), getattr(framer, "mode", None))
                metrics["frames_produced"] += len(frames); pending.extend(frames)
                if dev is None and pending:
                    # Nothing can be sent yet; a reconnect starts from a full frame anyway.
                    last_full = max((i for i, f in enumerate(pending) if f.full), default=None)
                    pending = pending[last_full:] if last_full is not None else []
                if cursor:
                    for record in cursor_records: cursor.handle(record)
            if dev is None and time.monotonic() >= next_open:
                try:
                    dev = open_device()
                    if dev is None: next_open = time.monotonic() + 1; continue
                    if note_adapter_ok: note_adapter_ok(metrics)
                    if args.command_ring:
                        metrics["vram_mb"] = query_vram_mb(dev); framer.attach_ring(metrics["vram_mb"])
                    presentation.begin_connection(); metrics["connections"] += 1
                    if metrics["connections"] > 1: metrics["reconnects"] += 1
                    diagnostics.counter("reconnects", metrics["reconnects"], generation=metrics["connections"])
                    diagnostics.event("adapter_open", generation=metrics["connections"])
                    need_full = True; capture.request_full()
                    if cursor: cursor.reset()
                except Exception as exc:
                    if log_open_failure: log_open_failure(metrics, exc)
                    else: metrics["last_usb_error"] = repr(exc)
                    dev = None; next_open = time.monotonic() + 1; continue
            if dev is None: continue
            try:
                if cursor: cursor.apply(dev)
                if not pending or time.monotonic() < last_send + period: continue
                if need_full and not any(f.full for f in pending):
                    metrics["tiles_skipped_need_full"] += len(pending); pending.clear(); continue
                base, tiles, newest, dropped = coalesce(pending)
                metrics["frames_consumed"] += len(pending); metrics["frames_coalesced"] += dropped + max(0, len(pending) - dropped - 1)
                pending.clear()
                try:
                    groups = ([framer.group(base.items[0][8])] if base else []) + [framer.tile_group(t[8], t[2], t[3], t[4], t[5]) for t in tiles]
                except ValueError:
                    # Too detailed even at the helper's lowest quality: skip it (not a USB failure, so
                    # no reconnect) and ask for a fresh full frame.
                    metrics["too_large"] += 1
                    need_full = True; capture.request_full(); continue
                started = time.monotonic(); first = final = None
                for group in groups:
                    _, group_first, group_final = write_group_timed(dev, group)
                    first = first or group_first; final = group_final
                done = time.monotonic(); last_send = started
                if base: need_full = False; metrics["full_sent"] += 1
                metrics["tiles_sent"] += len(tiles)
                presentation.record(first, final, capture_presentation=None)
                metrics["usb"].append((final - first) * 1000)
                metrics["end_to_end"].append((done - newest.requested) * 1000)
                metrics["frame_age_after_usb"].append((done - newest.ready) * 1000)
                record_successful_usb_update(metrics, newest.seq, done, payload_sha256(base.items[0][8] if base else tiles[-1][8]))
                if previous_sent is not None: metrics["frame_interval"].append((done - previous_sent) * 1000)
                previous_sent = done
                if metrics["sent"] == 1: diagnostics.event("first_usb", frame_id=newest.seq, update_id=1, adapter_generation=metrics["connections"])
                diagnostics.event("last_usb", frame_id=newest.seq, update_id=metrics["sent"], adapter_generation=metrics["connections"])
                if stream_started is None: stream_started = time.monotonic()
                telemetry.maybe_write(metrics, presentation, done)
            except Exception as exc:
                metrics["usb_failures"] += 1; metrics["last_usb_error"] = repr(exc)
                diagnostics.counter("usb_failures", metrics["usb_failures"], error=repr(exc))
                try:
                    import usb.util; usb.util.dispose_resources(dev)
                except Exception: pass
                dev = None; next_open = time.monotonic() + 1
    finally:
        capture.close()
        if dev is not None and cursor:
            try: dev.ctrl_transfer(*state_transfer(False)[:4], None, timeout=1000)
            except Exception: pass
    return dev

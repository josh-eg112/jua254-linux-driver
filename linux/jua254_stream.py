#!/usr/bin/env python3
"""Continuous userspace Hyprland-to-JUA254 streamer using proven USB framing."""

import argparse, collections, hashlib, json, math, os, resource, select, signal, statistics, struct, subprocess, sys, threading, time
from pathlib import Path

from build_jpeg_substitution import jpeg_metadata, normalize_component_ids
from replay_handoff import ENDPOINT, INTERFACE, MODE_1080P60, PID, VID, load_records, validate_chunks
from jua254_diagnostics import DiagnosticsState

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "shared/handoff/linux-rgb-bars-20260825"

CAPACITY = 470960
METRIC_SAMPLE_LIMIT = 16384
TELEMETRY_SCHEMA_VERSION = 2
TELEMETRY_HISTORY_LIMIT = 120
TELEMETRY_INTERVAL_SECONDS = .25

def default_telemetry_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "jua254-telemetry.json"

class LiveTelemetry:
    """Bounded, atomic snapshots of the existing production timing state."""
    def __init__(self, path, monitor, fps, interval=TELEMETRY_INTERVAL_SECONDS,
                 history_limit=TELEMETRY_HISTORY_LIMIT):
        self.path=Path(path) if path else None; self.monitor=monitor; self.fps=float(fps)
        self.interval=float(interval); self.history_limit=int(history_limit)
        self.pid=os.getpid(); self.last_write=None
    def snapshot(self, metrics, presentation, now=None):
        now=time.monotonic() if now is None else float(now)
        events=list(presentation.events)[-self.history_limit:]
        return {
            "schema":"jua254-production-telemetry", "version":TELEMETRY_SCHEMA_VERSION,
            "pid":self.pid, "monitor":self.monitor, "configured_fps":self.fps,
            "snapshot_monotonic":now,
            "counts":{
                "capture_attempts":metrics.get("capture_attempts",0),
                "successful_captures":metrics.get("successful_captures",0),
                "encoded_frames":metrics.get("encoded_frames",0),
                "frames_produced":metrics.get("frames_produced",0),
                "frames_consumed":metrics.get("frames_consumed",0),
                "updates_sent":metrics.get("sent",0),
            },
            "failures":{
                "capture":metrics.get("capture_failures",0),
                "capture_timeouts":metrics.get("capture_timeouts",0),
                "encoder":metrics.get("encoder_failures",0),
                "usb":metrics.get("usb_failures",0),
                "reconnects":metrics.get("reconnects",0),
                "adapter_open":metrics.get("open_failures",0),
            },
            "last_success":{
                "capture_frame_id":metrics.get("last_successful_capture_frame_id"),
                "capture_monotonic":metrics.get("last_successful_capture_monotonic"),
                "encoded_frame_id":metrics.get("last_successful_encode_frame_id"),
                "encode_monotonic":metrics.get("last_successful_encode_monotonic"),
                "usb_frame_id":metrics.get("last_successful_usb_frame_id"),
                "update_id":metrics.get("last_successful_update_id"),
                "usb_complete_monotonic":metrics.get("last_successful_usb_monotonic"),
                "payload_sha256":metrics.get("last_successful_payload_sha256"),
            },
            "recent_usb_start_intervals_ms":[e["start_interval_ms"] for e in events],
            "recent_usb_completion_intervals_ms":[e["completion_interval_ms"] for e in events],
            "history_limit":self.history_limit,
        }
    def maybe_write(self, metrics, presentation, now=None, force=False):
        if self.path is None: return False
        now=time.monotonic() if now is None else float(now)
        if not force and self.last_write is not None and now-self.last_write<self.interval: return False
        payload=self.snapshot(metrics,presentation,now)
        temporary=self.path.with_name(f".{self.path.name}.{self.pid}.tmp")
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with temporary.open("w") as output:
                json.dump(payload,output,separators=(",",":")); output.write("\n")
                output.flush()
            os.chmod(temporary,0o600)
            temporary.replace(self.path)
            self.last_write=now
            return True
        except OSError:
            try: temporary.unlink()
            except OSError: pass
            return False
    def close(self):
        if self.path is None: return
        try:
            if json.loads(self.path.read_text()).get("pid")==self.pid: self.path.unlink()
        except (OSError,ValueError,TypeError): pass

class Metric:
    """Thread-safe bounded timing samples with an exact streaming mean/max."""
    def __init__(self, limit=METRIC_SAMPLE_LIMIT):
        self.lock=threading.Lock(); self.values=collections.deque(maxlen=limit)
        self.count=0; self.total=0.0; self.maximum=None
    def append(self, value):
        value=float(value)
        with self.lock:
            self.values.append(value); self.count+=1; self.total+=value
            self.maximum=value if self.maximum is None else max(self.maximum,value)
    def report(self):
        with self.lock: values=list(self.values); count=self.count; total=self.total; maximum=self.maximum
        if not count: return {}
        result=summary(values); result.update({"count":count,"mean":total/count,"max":maximum,
                                               "min":min(values),"total":total,
                                               "sample_count":len(values),"sample_limit":self.values.maxlen})
        return result

def metric_set():
    names=("frame_interval","capture_ready_wait","capture_copy","capture_total",
           "frame_age_capture_finish","frame_age_before_encode","frame_age_before_usb",
           "frame_age_after_usb","encode","usb","end_to_end")
    return {name:Metric() for name in names}

class PresentationTrace:
    """Bounded steady-state USB presentation timing; never changes pacing."""
    SHORT_QUIET_MS=6.0
    IMMEDIATE_MS=.25
    def __init__(self):
        self.metrics={name:Metric() for name in
                      ("update_duration","quiet_time","start_interval",
                       "completion_interval","deadline_error")}
        self.previous_start=None; self.previous_complete=None; self.startup_transition=True
        self.connections=0; self.startup_updates=0; self.startup_transitions=0
        self.missed_deadlines=0; self.catch_up_events=0; self.immediate_after_miss=0
        self.short_quiet_events=0; self.consecutive_short=0; self.max_consecutive_short=0
        self.first_steady_start=None; self.last_steady_complete=None
        self.events=collections.deque(maxlen=METRIC_SAMPLE_LIMIT)
    def begin_connection(self):
        self.connections+=1; self.previous_start=None; self.previous_complete=None
        self.startup_transition=True
    def record(self, first_start, final_complete, target_deadline=None, capture_presentation=None):
        if self.previous_complete is None:
            self.startup_updates+=1
            self.previous_start=first_start; self.previous_complete=final_complete
            return
        if self.startup_transition:
            self.startup_transitions+=1; self.startup_transition=False
            self.previous_start=first_start; self.previous_complete=final_complete
            return
        update_ms=(final_complete-first_start)*1000
        quiet_ms=(first_start-self.previous_complete)*1000
        start_interval_ms=(first_start-self.previous_start)*1000
        completion_interval_ms=(final_complete-self.previous_complete)*1000
        self.metrics["update_duration"].append(update_ms)
        self.metrics["quiet_time"].append(quiet_ms)
        self.metrics["start_interval"].append(start_interval_ms)
        self.metrics["completion_interval"].append(completion_interval_ms)
        error_ms=None; catch_up=False
        if target_deadline is not None:
            error_ms=(first_start-target_deadline)*1000
            self.metrics["deadline_error"].append(error_ms)
            if error_ms>0:
                self.missed_deadlines+=1
            if target_deadline<=self.previous_complete:
                catch_up=True
                self.catch_up_events+=1
                if quiet_ms<=self.IMMEDIATE_MS: self.immediate_after_miss+=1
        if quiet_ms<self.SHORT_QUIET_MS:
            self.short_quiet_events+=1; self.consecutive_short+=1
            self.max_consecutive_short=max(self.max_consecutive_short,self.consecutive_short)
        else: self.consecutive_short=0
        self.events.append({"previous_final_complete":self.previous_complete,
                            "capture_presentation_time":capture_presentation,
                            "target_deadline":target_deadline,"first_descriptor_start":first_start,
                            "final_payload_complete":final_complete,"update_duration_ms":update_ms,
                            "quiet_time_ms":quiet_ms,"start_interval_ms":start_interval_ms,
                            "completion_interval_ms":completion_interval_ms,
                            "deadline_error_ms":error_ms,"catch_up":catch_up,
                            "immediate_after_overrun":catch_up and quiet_ms<=self.IMMEDIATE_MS})
        if self.first_steady_start is None: self.first_steady_start=first_start
        self.last_steady_complete=final_complete
        self.previous_start=first_start; self.previous_complete=final_complete
    def report(self):
        result={name:value.report() for name,value in self.metrics.items()}
        result.update({"connections":self.connections,"startup_updates_excluded":self.startup_updates,
                       "startup_transitions_excluded":self.startup_transitions,
                       "steady_transitions":result["quiet_time"].get("count",0),
                       "missed_deadlines":self.missed_deadlines,
                       "catch_up_events":self.catch_up_events,
                       "immediate_after_missed_deadline":self.immediate_after_miss,
                       "short_quiet_threshold_ms":self.SHORT_QUIET_MS,
                       "short_quiet_events":self.short_quiet_events,
                       "max_consecutive_short_quiet":self.max_consecutive_short,
                       "first_steady_start_monotonic":self.first_steady_start,
                       "last_steady_completion_monotonic":self.last_steady_complete})
        result["events"]=list(self.events); result["event_limit"]=self.events.maxlen
        return result

class Latest:
    def __init__(self): self.lock=threading.Lock(); self.value=None; self.number=0; self.overwritten=0; self.overwrite_times=collections.deque(maxlen=METRIC_SAMPLE_LIMIT)
    def put(self, value):
        with self.lock:
            if self.value is not None: self.overwritten += 1; self.overwrite_times.append(time.monotonic())
            self.number += 1; self.value=(self.number,value)
    def take(self):
        with self.lock: value=self.value; self.value=None; return value

class PersistentFFmpegEncoder:
    def __init__(self, backend, quality, pixel_format="rgb24", no_scale=False, async_depth=1):
        common=["ffmpeg","-hide_banner","-loglevel","error","-f","rawvideo","-pixel_format",pixel_format,"-video_size","1920x1080","-framerate","60","-i","pipe:0"]
        if backend=="hardware":
            common[1:1]=["-vaapi_device","/dev/dri/renderD128"]
            # JFIF JPEG is full-range YCbCr, but swscale defaults to limited range (black 16, white 235).
            # Convert to full range, then keep the limited tag: mjpeg_vaapi rejects full-range frames (EINVAL).
            full="out_range=full,format=nv12,setparams=range=tv,hwupload"
            video_filter=f"scale={full}" if no_scale else f"scale=1920:1080:{full}"
            output=["-vf",video_filter,"-c:v","mjpeg_vaapi","-global_quality",str(quality),"-async_depth",str(async_depth),"-jfif","1"]
        else:
            q=max(2,min(31,round((101-quality)*.29)))
            output=["-vf","scale=1920:1080","-c:v","mjpeg","-q:v",str(q),"-pix_fmt","yuvj420p"]
        self.proc=subprocess.Popen(common+output+["-threads","1","-flush_packets","1","-f","image2pipe","pipe:1"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        self.buffer=bytearray()
    def encode(self, frame):
        self.submit(frame)
        return self.receive()
    def submit(self, frame):
        if isinstance(frame, tuple): pixels,expected=frame
        else:
            header_end=frame.find(b"\n",frame.find(b"\n",frame.find(b"\n")+1)+1)+1
            pixels,expected=frame[header_end:],1920*1080*3
        if len(pixels)!=expected: raise RuntimeError("persistent encoder received a short raw frame")
        self.proc.stdin.write(pixels); self.proc.stdin.flush()
    def receive(self):
        deadline=time.monotonic()+2
        while True:
            end=self.buffer.find(b"\xff\xd9")
            if end>=0:
                jpeg=bytes(self.buffer[:end+2]); del self.buffer[:end+2]; return jpeg
            timeout=deadline-time.monotonic()
            if timeout<=0: raise RuntimeError("persistent FFmpeg encoder timeout")
            ready,_,_=select.select([self.proc.stdout],[],[],timeout)
            if not ready: continue
            chunk=os.read(self.proc.stdout.fileno(),65536)
            if not chunk: raise RuntimeError(self.proc.stderr.read().decode(errors="replace"))
            self.buffer.extend(chunk)
    def close(self):
        try: self.proc.stdin.close(); self.proc.terminate(); self.proc.wait(timeout=2)
        except Exception: self.proc.kill()

class PersistentGsrCapture:
    def __init__(self,target,fps):
        self.gsr=subprocess.Popen(["gpu-screen-recorder","-w",target,"-s","1920x1080","-k","h264","-f",str(round(fps)),"-fm","cfr","-encoder","gpu","-c","mkv","-cursor","yes","-restore-portal-session","yes","-o","/dev/stdout"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        initial=os.read(self.gsr.stdout.fileno(),12)
        if initial==b"/dev/stdout\n": initial=b""
        self.dec=subprocess.Popen(["ffmpeg","-hide_banner","-loglevel","error","-fflags","nobuffer","-flags","low_delay","-i","pipe:0","-vf","scale=1920:1080","-pix_fmt","rgb24","-f","rawvideo","pipe:1"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        def pump():
            try:
                if initial:
                    self.dec.stdin.write(initial); self.dec.stdin.flush()
                while True:
                    chunk=os.read(self.gsr.stdout.fileno(),65536)
                    if not chunk: break
                    self.dec.stdin.write(chunk); self.dec.stdin.flush()
            except Exception: pass
            try:self.dec.stdin.close()
            except Exception:pass
        self.pump=threading.Thread(target=pump,daemon=True); self.pump.start()
    def frame(self):
        size=1920*1080*3; data=bytearray(); deadline=time.monotonic()+5
        while len(data)<size:
            timeout=deadline-time.monotonic()
            if timeout<=0: raise RuntimeError("persistent capture timeout")
            ready,_,_=select.select([self.dec.stdout],[],[],timeout)
            if not ready: continue
            chunk=os.read(self.dec.stdout.fileno(),size-len(data))
            if not chunk: raise RuntimeError("persistent capture decoder EOF")
            data.extend(chunk)
        return b"P6\n1920 1080\n255\n"+bytes(data)
    def close(self):
        try: self.gsr.send_signal(signal.SIGINT); self.gsr.wait(timeout=3)
        except Exception: self.gsr.kill()
        try: self.dec.terminate(); self.dec.wait(timeout=2)
        except Exception: self.dec.kill()

class PersistentExtCopyCapture:
    """Native ext-image-copy source; raw BGRA reaches FFmpeg without conversion."""
    def __init__(self,target,metrics,zero_copy=False,direct_read=False,double_buffer=False,frame_timeout_ms=500):
        self.zero_copy=zero_copy
        self.direct_read=direct_read
        self.last_frame_started=None
        self.last_presentation_time=None
        self.timing_condition=threading.Condition(); self.frame_timings=collections.deque()
        native=Path(__file__).resolve().parent/"native"; binary=native/"jua254-extcopy-capture"
        if not binary.exists() or binary.stat().st_mtime < (native/"extcopy_capture.c").stat().st_mtime:
            subprocess.run(["make","-C",str(native)],check=True)
        command=[str(binary),target,"--frame-timeout-ms",str(frame_timeout_ms)]+(["--double-buffer"] if double_buffer else [])
        self.proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        def diagnostics():
            for line in iter(self.proc.stderr.readline,b""):
                try:
                    event=json.loads(line)
                    if event.get("event")=="frame":
                        metrics["capture_attempts"] += 1
                        metrics["successful_captures"] += 1
                        metrics["capture_ready_wait"].append(event.get("ready_wait_ms",event.get("compositor_ms",0.0)))
                        metrics["capture_copy"].append(event.get("copy_ms",0.0))
                        with self.timing_condition:
                            self.frame_timings.append((event.get("requested_ns",0)/1e9,
                                                       event.get("presentation_ns",0)/1e9
                                                       if event.get("presentation_seen") else None))
                            self.timing_condition.notify()
                    elif event.get("event")=="frame-timeout":
                        metrics["capture_attempts"] += 1
                        metrics["capture_timeouts"] += 1
                    elif event.get("event")=="frame-failed":
                        metrics["capture_attempts"] += 1
                        metrics["capture_failures"] += 1
                    elif event.get("event")=="ready": metrics["native_capture_info"]=event
                except Exception: metrics["last_native_capture_message"]=line.decode(errors="replace").strip()
        threading.Thread(target=diagnostics,daemon=True).start()
    def frame(self):
        size=1920*1080*4; data=bytearray(size) if self.direct_read else bytearray(); offset=0; deadline=time.monotonic()+1.0
        while offset<size:
            timeout=deadline-time.monotonic()
            if timeout<=0: raise RuntimeError("native ext-image-copy capture timeout")
            ready,_,_=select.select([self.proc.stdout],[],[],timeout)
            if not ready: continue
            if self.direct_read:
                count=os.readv(self.proc.stdout.fileno(),[memoryview(data)[offset:]])
                if not count: raise RuntimeError("native ext-image-copy capture EOF")
                offset+=count
            else:
                chunk=os.read(self.proc.stdout.fileno(),size-offset)
                if not chunk: raise RuntimeError("native ext-image-copy capture EOF")
                data.extend(chunk); offset+=len(chunk)
        # The experimental async path can skip this final Python copy.  Keep
        # the visually proven production behavior as the default.
        with self.timing_condition:
            if not self.frame_timings: self.timing_condition.wait(timeout=.05)
            timing=self.frame_timings.popleft() if self.frame_timings else (None,None)
            self.last_frame_started,self.last_presentation_time=timing
        return (data if self.zero_copy else bytes(data)),size
    def close(self):
        try: self.proc.terminate(); self.proc.wait(timeout=2)
        except Exception: self.proc.kill()

def encode_ppm(ppm, quality, backend, persistent=None):
    if backend=="imagemagick":
        p=subprocess.run(["magick","ppm:-","-auto-orient","-resize","1920x1080^","-gravity","center","-extent","1920x1080","-colorspace","sRGB","-sampling-factor","2x2,1x1,1x1","-quality",str(quality),"jpeg:-"],input=ppm,capture_output=True,check=True); raw=p.stdout
    else: raw=persistent.encode(ppm)
    jpeg=normalize_component_ids(raw); meta=jpeg_metadata(jpeg)
    if (meta["width"],meta["height"])!=(1920,1080) or len(jpeg)>CAPACITY: raise RuntimeError("encoded JPEG invalid or too large")
    return jpeg

def capture_loop(args, latest, stop, metrics, diagnostics):
    static=None; encoder=None; capture=None
    consecutive_failures=0
    pixel_format="bgra" if args.capture=="wlroots-native" and not (args.image or args.test_pattern) else "rgb24"
    if args.encoder in ("ffmpeg","hardware"): encoder=PersistentFFmpegEncoder(args.encoder,args.quality,pixel_format)
    if args.capture=="wlroots-native":
        capture=PersistentExtCopyCapture(args.monitor or "eDP-1",metrics,
                                         args.capture_zero_copy, args.capture_direct_read,
                                         args.capture_double_buffer)
    elif args.capture in ("pipewire","screencopy"):
        capture=PersistentGsrCapture("portal" if args.capture=="pipewire" else (args.monitor or "eDP-1"),args.fps)
    if args.image:
        p=subprocess.run(["magick",args.image,"-auto-orient","-resize","1920x1080^","-gravity","center","-extent","1920x1080","ppm:-"],capture_output=True,check=True); static=p.stdout
    frame_id=0
    while not stop.is_set():
        metrics["producer_capture_calls"]+=1
        capture_presentation=None
        try:
            t=time.monotonic()
            if static is not None: ppm=static
            elif args.test_pattern:
                color=(int(time.monotonic()*2)%3); names=("red","green","blue")
                p=subprocess.run(["magick","-size","1920x1080",f"xc:{names[color]}","ppm:-"],capture_output=True,check=True); ppm=p.stdout
            elif capture is not None:
                ppm=capture.frame()
                if isinstance(capture,PersistentExtCopyCapture) and capture.last_frame_started is not None:
                    t=capture.last_frame_started
                    capture_presentation=capture.last_presentation_time
            else:
                cmd=["grim","-t","ppm"]
                if args.monitor: cmd += ["-o",args.monitor]
                cmd += ["-"]; p=subprocess.run(cmd,capture_output=True,check=True); ppm=p.stdout
            captured=time.monotonic()
            frame_id+=1
            if frame_id == 1: diagnostics.event("first_capture", frame_id=frame_id)
            diagnostics.event("last_capture", frame_id=frame_id)
            capture_ms=(captured-t)*1000
            metrics["capture_total"].append(capture_ms)
            metrics["last_successful_capture_frame_id"]=frame_id
            metrics["last_successful_capture_monotonic"]=captured
            metrics["frame_age_capture_finish"].append(capture_ms)
            metrics["frame_age_before_encode"].append((time.monotonic()-t)*1000)
        except Exception as exc:
            if stop.is_set(): break
            metrics["capture_failures"]+=1; metrics["last_capture_error"]=repr(exc); time.sleep(.1)
            diagnostics.counter("capture_failures", metrics["capture_failures"], error=repr(exc))
            consecutive_failures+=1
            if args.capture=="wlroots-native" and consecutive_failures>=10: stop.set()
            continue
        try:
            encode_started=time.monotonic(); jpeg=encode_ppm(ppm,args.quality,args.encoder,encoder); encoded=time.monotonic()
            metrics["encode"].append((encoded-encode_started)*1000)
            metrics["encoded_frames"]+=1
            metrics["last_successful_encode_frame_id"]=frame_id
            metrics["last_successful_encode_monotonic"]=encoded
            if metrics["encoded_frames"] == 1: diagnostics.event("first_encode", frame_id=frame_id)
            diagnostics.event("last_encode", frame_id=frame_id)
            latest.put((jpeg,t,encoded,capture_presentation,frame_id))
            metrics["frames_produced"]+=1
            consecutive_failures=0
        except Exception as exc:
            if stop.is_set(): break
            metrics["encoder_failures"]+=1; metrics["last_encoder_error"]=repr(exc); time.sleep(.1)
            diagnostics.counter("encode_failures", metrics["encoder_failures"], error=repr(exc))
    if encoder: encoder.close()
    if capture: capture.close()

def capabilities():
    result={"pipewire":False,"pipewire_portal":False,"screencopy":False,"ffmpeg":True,"hardware":False,"hardware_device":"/dev/dri/renderD128"}
    try:
        data=json.loads(subprocess.run(["pw-dump"],capture_output=True,text=True,check=True).stdout)
        result["pipewire_nodes"]=[x.get("id") for x in data if x.get("type","").endswith(":Node") and x.get("info",{}).get("props",{}).get("media.class")=="Video/Source" and not str(x.get("info",{}).get("props",{}).get("node.name","")).startswith("v4l2_")]
        result["pipewire"]=bool(result["pipewire_nodes"])
    except Exception: result["pipewire_nodes"]=[]
    try:
        opts=subprocess.run(["gpu-screen-recorder","--list-capture-options"],capture_output=True,text=True,check=True).stdout.splitlines()
        result["pipewire_portal"]="portal" in opts; result["screencopy"]=any(x.startswith("eDP-1|") for x in opts)
    except Exception: pass
    probe=["ffmpeg","-y","-hide_banner","-loglevel","error","-vaapi_device",result["hardware_device"],"-f","lavfi","-i","color=red:s=1920x1080","-vf","format=nv12,hwupload","-frames:v","1","-c:v","mjpeg_vaapi","-f","image2pipe","/dev/null"]
    result["hardware"]=subprocess.run(probe).returncode==0
    return result

def final_group(records):
    starts=[]; i=0
    while i<len(records):
        starts.append(i); total=int.from_bytes(records[i][1][4:8],"little"); off=0
        while off<total: off+=int.from_bytes(records[i][1][12:16],"little"); i+=2
    return records[starts[-1]:]

def frame_records(template,jpeg,header=None):
    logical=b"".join(d for _,d in template[1::2]); header=logical[:48] if header is None else header
    if len(header)!=48: raise ValueError("video header must be 48 bytes")
    body=header+jpeg+bytes(CAPACITY-len(jpeg)); out=[]; off=0
    for n,(item,data) in enumerate(template):
        if n%2: data=body[off:off+len(data)]; off+=len(data)
        out.append((item,data))
    return out

# The three (Y, UV) VRAM destinations the Windows driver cycles through for
# type-3 (VIDEO_CMD_FLIP_PRIMARY) frames in windows-colors-20260825, seq 385-398.
FB_ROTATION_SLOTS = ((0x30,0x1fe030),(0xfffef0,0x11fdef0),(0x1fffdb0,0x21fddb0))
FB_SLOT_STRIDE = 0xfffec0           # spacing of the three captured buffer bases

MODE_TABLE_ENTRIES = 30             # 0x89 entries 0..29 are modes (0x000..0x3a0); later bytes are other data

class VideoMode:
    """An adapter output mode and the VRAM layout derived from it.

    The three buffer bases stay where Windows put them; each UV plane follows
    its Y plane after pitch * align16(height) bytes, and the command ring starts
    at the first 1 MiB boundary above the third UV plane. 1920x1080@60 gives
    exactly FB_ROTATION_SLOTS and COMMAND_RING_BASE.
    """
    def __init__(self, width, height, refresh=60):
        self.width, self.height, self.refresh = int(width), int(height), int(refresh)
        if self.width<=0 or self.height<=0 or self.width%16: raise ValueError(f"unsupported mode {self}")
        self.pitch=self.width
        plane=self.pitch*((self.height+15)//16*16)
        if plane*3//2>FB_SLOT_STRIDE: raise ValueError(f"mode {self} does not fit a framebuffer slot")
        self.slots=tuple((y,y+plane) for y,_ in FB_ROTATION_SLOTS)
        end=self.slots[-1][1]+plane//2
        self.ring_base=(end+(1<<20)-1)&~((1<<20)-1)
    @classmethod
    def parse(cls, text):
        size,_,rate=text.partition("@"); w,_,h=size.lower().partition("x")
        return cls(int(w),int(h),int(rate or 60))
    @property
    def default(self): return (self.width,self.height,self.refresh)==(1920,1080,60)
    def header(self, template_header):
        """The template type-3 header with this mode's pitch (word 4) and first buffer (words 5/6)."""
        h=bytearray(template_header)
        h[16:20]=(self.pitch|(self.pitch<<16)).to_bytes(4,"little")
        h[20:24]=self.slots[0][0].to_bytes(4,"little"); h[24:28]=self.slots[0][1].to_bytes(4,"little")
        return bytes(h)
    def __str__(self): return f"{self.width}x{self.height}@{self.refresh}"

def decode_mode_entry(blob):
    """(hactive, vactive, refresh) of one 32-byte 0x89 mode-table entry."""
    clock,refresh,htotal,hactive,_,_,vtotal,vactive=struct.unpack_from("<I7H",blob)
    return hactive,vactive,refresh

def find_mode_blob(dev, mode):
    """Read-only scan of the adapter mode table for the entry matching `mode`."""
    for n in range(MODE_TABLE_ENTRIES):
        blob=bytes(dev.ctrl_transfer(0xC0,0x89,0,n*32,32,timeout=2000))
        if decode_mode_entry(blob)==(mode.width,mode.height,mode.refresh): return blob
    return None

class FramebufferRotation:
    """Experimental default-off triple buffering of the 48-byte video header.

    Each call returns the template header with only word 2 (FenceID) advanced
    monotonically and words 5/6 (Y/UV destination) moved to the next slot, so
    the adapter decodes into a buffer that is not being scanned out. Payload,
    envelope size, descriptors, and URB structure are unchanged.
    """
    def __init__(self, template_header, slots=FB_ROTATION_SLOTS):
        if len(template_header)!=48: raise ValueError("video header must be 48 bytes")
        self.template=bytes(template_header); self.slots=tuple(slots)
        current=(int.from_bytes(self.template[20:24],"little"),int.from_bytes(self.template[24:28],"little"))
        self.index=self.slots.index(current) if current in self.slots else len(self.slots)-1
        self.fence=int.from_bytes(self.template[8:12],"little")
    def next_fence(self):
        self.fence=(self.fence+1)&0xFFFFFFFF
        return self.fence
    def displayed(self):
        """(Y, UV) VRAM offsets of the buffer the last type-3 frame flipped to."""
        return self.slots[self.index]
    def next_header(self):
        self.index=(self.index+1)%len(self.slots); self.next_fence()
        y,uv=self.slots[self.index]; header=bytearray(self.template)
        header[8:12]=self.fence.to_bytes(4,"little")
        header[20:24]=y.to_bytes(4,"little"); header[24:28]=uv.to_bytes(4,"little")
        return bytes(header)

def template_header(template):
    return b"".join(d for _,d in template[1::2])[:48]

CHUNK_BYTES = 102400
SIZED_PAD_BYTES = 1024
COMMAND_RING_BASE = 0x2300000   # first 1 MiB boundary above the third UV plane (ends 0x22fcdb0)
COMMAND_RING_PRIMED_FRAMES = 10 # vendor main2.c flags the first ten frames, as on every wrap
RESET_FLAG = 0x80               # vendor VIDEO_FLIP_HEADER.Flag on command-ring reset

def template_total(template):
    return int.from_bytes(template[0][1][4:8],"little")

def template_address(template):
    return int.from_bytes(template[0][1][8:12],"little")

def sized_payload_total(jpeg_len):
    """Windows/vendor logical length: JPEG + 48-byte header + 1024 pad, with the
    32-byte descriptor rounded to 2048 (exact for all 15 captured transfers)."""
    return -(-(jpeg_len+48+SIZED_PAD_BYTES+32)//2048)*2048-32

class CommandRing:
    """Experimental default-off JPEG upload ring, as Windows advances it.

    Each logical transfer is placed contiguously after the previous one. When
    the next transfer would pass the end of VRAM it restarts at the ring base,
    and that frame (plus the first ten after start) carries the vendor reset flag.
    """
    def __init__(self, vram_mb, base=COMMAND_RING_BASE, reserve=None):
        self.base=base; self.end=int(vram_mb)*1024*1024; self.next=base; self.placed=0
        if self.end-self.base < 2*(reserve or CAPACITY+48):
            raise RuntimeError(f"VRAM {vram_mb} MB leaves no command ring above {base:#x}")
    def place(self,total):
        wrapped=False
        if self.next+total>self.end: self.next=self.base; wrapped=True
        address=self.next; self.next+=total; self.placed+=1
        return address, wrapped or self.placed<=COMMAND_RING_PRIMED_FRAMES

def framed_group(template, jpeg, header, total, address, reset=False):
    """Build one logical type-3 transfer with generated descriptors.

    Descriptors follow the captured layout (total, address, packet size,
    offset, continue flag) split at 102400 bytes, and each record reuses the
    timing of the template record at the same position.
    """
    if total>template_total(template): raise ValueError("frame exceeds the captured envelope")
    if total<48+len(jpeg): raise ValueError("JPEG does not fit the logical transfer")
    header=bytearray(template_header(template) if header is None else header)
    if len(header)!=48: raise ValueError("video header must be 48 bytes")
    header[4:8]=(total-48).to_bytes(4,"little")
    if reset: header[47]|=RESET_FLAG
    body=bytes(header)+jpeg+bytes(total-48-len(jpeg)); out=[]; offset=0
    while offset<total:
        size=min(CHUNK_BYTES,total-offset); more=1 if offset+size<total else 0
        desc=struct.pack("<8I",0,total,address,size,offset,more,0,0)
        n=len(out)
        out.append((dict(template[n][0],length=32),desc))
        out.append((dict(template[n+1][0],length=size),body[offset:offset+size]))
        offset+=size
    return out

PITCH = 1920

def fb_direct_header(fence, x, y, width, height, y_base, uv_base, pitch=PITCH):
    """48-byte vendor VIDEO_FB_DIRECT_HEADER (type 7): decode a JPEG rectangle
    straight into the given NV12 buffer at (x, y), without a flip."""
    if x%2 or y%2 or x<0 or y<0 or x+width>pitch or width<=0 or height<=0:
        raise ValueError("FB_DIRECT rectangle must be even-aligned and inside the raster")
    return struct.pack("<10I8x", 7, 0, fence & 0xFFFFFFFF, 6, width | (height << 16), pitch | (pitch << 16),
                       y_base + y*pitch + x, uv_base + (y//2)*pitch + x, 0, 13)

class LiveFramer:
    """Build each live type-3 group. With every experiment off this is exactly
    `frame_records(template, jpeg)`, the captured envelope and descriptors.
    A non-default `mode` (VideoMode) moves pitch, buffers and ring to that mode's layout;
    it needs fb_rotation and sized_payload, since the captured envelope is 1080p-sized."""
    def __init__(self, template, fb_rotation=False, sized_payload=False, metrics=None, mode=None):
        self.template=template; self.sized=bool(sized_payload); self.ring=None; self.metrics=metrics
        self.mode=mode if mode is not None and not mode.default else None
        if self.mode and not (fb_rotation and sized_payload):
            raise ValueError(f"mode {self.mode} requires fb_rotation and sized_payload")
        self.pitch=self.mode.pitch if self.mode else PITCH
        header=self.mode.header(template_header(template)) if self.mode else template_header(template)
        slots=self.mode.slots if self.mode else FB_ROTATION_SLOTS
        self.rotation=FramebufferRotation(header,slots) if fb_rotation else None
    def attach_ring(self, vram_mb):
        self.ring=CommandRing(vram_mb,base=self.mode.ring_base) if self.mode else CommandRing(vram_mb)
    def group(self, jpeg):
        header=self.rotation.next_header() if self.rotation else None
        if not self.sized and self.ring is None: return frame_records(self.template,jpeg,header)
        total=sized_payload_total(len(jpeg)) if self.sized else template_total(self.template)
        address,reset=self.ring.place(total) if self.ring is not None else (template_address(self.template),False)
        if self.metrics is not None:
            if reset: self.metrics["command_ring_resets"]+=1
            self.metrics["usb_payload_bytes"].append(total)
        return framed_group(self.template,jpeg,header,total,address,reset)
    def tile_group(self, jpeg, x, y, width, height):
        """Type-7 update of one rectangle in the currently displayed buffer.
        FenceIDs share the type-3 counter so they stay monotonic across both."""
        buffers=self.rotation
        if buffers is None:
            if not hasattr(self,"single_buffer"):
                self.single_buffer=FramebufferRotation(template_header(self.template),slots=(FB_ROTATION_SLOTS[0],))
            buffers=self.single_buffer
        y_base,uv_base=buffers.displayed()
        header=fb_direct_header(buffers.next_fence(),x,y,width,height,y_base,uv_base,self.pitch)
        total=sized_payload_total(len(jpeg))
        address,reset=self.ring.place(total) if self.ring is not None else (template_address(self.template),False)
        if self.metrics is not None and "usb_payload_bytes" in self.metrics: self.metrics["usb_payload_bytes"].append(total)
        return framed_group(self.template,jpeg,header,total,address,reset)

def query_vram_mb(dev):
    """Vendor 0x88 (read-only): VRAM size in MB, one byte."""
    return int(bytes(dev.ctrl_transfer(0xC0,0x88,0,0,1,timeout=2000))[0])

def connection_groups(records, template, jpeg, diagnostic_bootstrap=False, header=None, live=None):
    """Choose immutable diagnostic replay or direct-live production startup."""
    live = frame_records(template, jpeg, header) if live is None else live
    return [records, live] if diagnostic_bootstrap else [live]

# Supported models: USB product ID -> model code (0xB0). Both are MCT Trigger 6 with the same
# firmware (bcdDevice 0x1010), endpoints, 58 MB VRAM and mode table (checked 2026-10-16).
MODELS = {0x5600: b"JUA254H", 0x5601: b"A8640H"}

def usb_location(dev):
    """A device's USB location as Linux names it in sysfs: "bus-port.port" (e.g. "2-1" or "2-1.3")."""
    return f"{dev.bus}-" + ".".join(str(n) for n in (dev.port_numbers or ()))

def find_adapter(location=None, backend=None, find=None):
    """The JUA254 at `location` (or the first one), or None."""
    import usb.core
    find = find or usb.core.find
    supported=lambda d: d.idProduct in MODELS
    if location is None: return find(idVendor=VID,custom_match=supported,backend=backend)
    return next((d for d in find(find_all=True,idVendor=VID,custom_match=supported,backend=backend) if usb_location(d)==location), None)

def open_device(video_mode=None, location=None):
    import usb.backend.libusb1, usb.core, usb.util
    backend=usb.backend.libusb1.get_backend(find_library=lambda _:"/usr/lib/libusb-1.0.so")
    dev=find_adapter(location,backend)
    if dev is None: return None
    try:
        dev.set_configuration(); usb.util.claim_interface(dev,INTERFACE)
        project=bytes(dev.ctrl_transfer(0xC0,0xB0,0,3,16,timeout=2000)).rstrip(b"\0")
        status=bytes(dev.ctrl_transfer(0xC0,0x87,0,0,1,timeout=2000))
        if video_mode is None or video_mode.default:
            blob=bytes(dev.ctrl_transfer(0xC0,0x89,0,0x0340,32,timeout=2000)); expected=MODE_1080P60
        else:
            blob=find_mode_blob(dev,video_mode); expected=blob
        if project!=MODELS.get(dev.idProduct) or status!=b"\x01" or blob is None or blob!=expected:
            raise RuntimeError(f"JUA254 identity/HDMI/mode check failed: project={project!r} hdmi={status.hex()} "
                               f"mode={video_mode or '1920x1080@60'} mode_ok={blob is not None and blob==expected}")
        dev.ctrl_transfer(0x40,0x03,0,0,None,timeout=2000); time.sleep(.25)
        if dev.ctrl_transfer(0x40,0x12,0,0,blob,timeout=2000)!=32: raise RuntimeError("short mode write")
        time.sleep(.25); dev.ctrl_transfer(0x40,0x03,0,1,None,timeout=2000)
        return dev
    except Exception:
        # Release the claim now; a leaked handle keeps later retries busy until garbage collection.
        try: usb.util.release_interface(dev,INTERFACE)
        except Exception: pass
        usb.util.dispose_resources(dev)
        raise

def log_open_failure(metrics, exc):
    """Count a failed adapter open and log it to stderr (journal): the first 5, then every 30th."""
    metrics["open_failures"]=metrics.get("open_failures",0)+1
    metrics["last_usb_error"]=repr(exc)
    n=metrics["open_failures"]
    if n<=5 or n%30==0:
        print(json.dumps({"event":"adapter_open_failed","count":n,"wall":time.strftime("%Y-%m-%dT%H:%M:%S%z"),"error":repr(exc)}),file=sys.stderr,flush=True)
    wake_capture_sink(metrics,exc)
    note_adapter_hang(metrics,exc)

HUNG_AFTER_TIMEOUTS = 5   # consecutive timed-out opens (~15 s) before telling the user

def notify(summary, body, urgency="normal", runner=subprocess.run):
    """Best-effort desktop notification; the journal line is the authoritative record."""
    try: runner(["notify-send","-a","JUA254","-u",urgency,summary,body],capture_output=True,timeout=5)
    except Exception: pass

def note_adapter_hang(metrics, exc, runner=subprocess.run):
    """A hung adapter firmware times out every request, and neither a USB reset nor port
    disable (no per-port power switching here) recovers it; only a physical replug does.
    After HUNG_AFTER_TIMEOUTS timed-out opens in a row, say so once."""
    if "timed out" not in str(exc).lower(): metrics["open_timeouts_in_row"]=0; return False
    metrics["open_timeouts_in_row"]=metrics.get("open_timeouts_in_row",0)+1
    if metrics["open_timeouts_in_row"]!=HUNG_AFTER_TIMEOUTS: return False
    metrics["adapter_hangs"]=metrics.get("adapter_hangs",0)+1
    print(json.dumps({"event":"adapter_hung","wall":time.strftime("%Y-%m-%dT%H:%M:%S%z"),"error":repr(exc)}),file=sys.stderr,flush=True)
    notify("JUA254 adapter is not responding","Its firmware is hung. Unplug the JUA254's USB cable and plug it back in; the display resumes on its own.","critical",runner)
    return True

def note_adapter_ok(metrics, runner=subprocess.run):
    """Successful open: clear the timeout streak and report recovery after a reported hang."""
    hung=metrics.get("open_timeouts_in_row",0)>=HUNG_AFTER_TIMEOUTS
    metrics["open_timeouts_in_row"]=0
    if hung:
        print(json.dumps({"event":"adapter_recovered","wall":time.strftime("%Y-%m-%dT%H:%M:%S%z")}),file=sys.stderr,flush=True)
        notify("JUA254 adapter recovered","The external display is streaming again.","normal",runner)
    return hung

_last_wake=None
def wake_capture_sink(metrics, exc, runner=subprocess.run, now=time.monotonic):
    """Test rigs only: an MS2109 UVC capture card asserts HDMI hot-plug only while it is
    being captured. With JUA254_WAKE_CAPTURE=/dev/videoN set, capture it for 2 s when an
    open fails with hdmi=00 (at most every 10 s) so the adapter sees a sink."""
    global _last_wake
    device=os.environ.get("JUA254_WAKE_CAPTURE")
    if not device or "hdmi=00" not in str(exc): return False
    if _last_wake is not None and now()-_last_wake<10: return False
    _last_wake=now()
    metrics["capture_sink_wakes"]=metrics.get("capture_sink_wakes",0)+1
    try:
        runner(["ffmpeg","-hide_banner","-loglevel","error","-f","v4l2","-i",device,"-t","2","-f","null","-"],
               capture_output=True,timeout=10)
    except Exception as wake_exc:
        metrics["last_capture_sink_wake_error"]=repr(wake_exc)
    print(json.dumps({"event":"capture_sink_wake","device":device,"count":metrics["capture_sink_wakes"]}),file=sys.stderr,flush=True)
    return True

def write_group_timed(dev,group):
    previous=group[0][0]["time_relative_seconds"]; first_start=None; final_complete=None
    for n,(item,data) in enumerate(group):
        ts=item["time_relative_seconds"]
        if n: time.sleep(max(0,ts-previous))
        if n==0: first_start=time.monotonic()
        if dev.write(ENDPOINT,data,timeout=5000)!=len(data): raise RuntimeError("short USB write")
        if n==len(group)-1: final_complete=time.monotonic()
        previous=ts
    return (final_complete-first_start)*1000,first_start,final_complete

def write_group(dev,group):
    return write_group_timed(dev,group)[0]

def payload_sha256(payload):
    """Hash encoded bytes without modifying the payload or transport path."""
    return hashlib.sha256(payload).hexdigest()

def presentation_phase_wait_seconds(presentation_time, now, offset_ms, refresh_hz=60.0):
    """Requested wait in seconds until the next one-refresh phase slot.

    The anchor is `presentation_time` (the captured frame's compositor
    presentation time on the monotonic clock) displaced by `offset_ms`. The
    wait is the signed elapsed time from `now` to the anchor's phase train
    reduced modulo exactly one refresh period, so any number of wrapped-around
    periods reduces to an equivalent requested wait in the half-open interval
    `[0, refresh_period)`. Floating-point guards clamp rounding so the strict
    invariant `0 <= wait < 1/refresh_hz` holds before any scheduler lateness.
    """
    if presentation_time is None or offset_ms is None:
        return None
    period=1.0/refresh_hz
    phase=float(presentation_time)+(float(offset_ms)/1000.0)
    wait=(phase-now)%period
    if wait>=period: wait-=period
    if wait<0.0: wait=0.0
    return wait

def presentation_phase_deadline(presentation_time, now, offset_ms, refresh_hz=60.0):
    """Return the next compositor-derived phase slot at or after now."""
    wait=presentation_phase_wait_seconds(presentation_time,now,offset_ms,refresh_hz)
    return None if wait is None else now+wait

def wait_for_presentation_phase(presentation_time, offset_ms, metric, lateness_metric=None,
                                now=time.monotonic, sleep=time.sleep):
    """Optionally wait without changing payloads or USB transaction structure.

    Only the mathematically bounded *requested* wait (`0 <= wait < refresh
    period`) is recorded into `metric`. Any additional actual elapsed time is
    OS/scheduler lateness from waking after the deadline and is recorded
    separately into `lateness_metric`, so future live validation can apply the
    one-refresh validity bound to the requested wait and cannot reject a
    candidate solely because the OS woke late.
    """
    if offset_ms is None or presentation_time is None:
        return None
    started=now(); wait=presentation_phase_wait_seconds(presentation_time,started,offset_ms)
    sleep(max(0.0,wait))
    if metric is not None: metric.append(wait*1000.0)
    if lateness_metric is not None:
        lateness_metric.append(max(0.0,(now()-started)*1000.0-wait*1000.0))
    return started+wait

def record_successful_usb_update(metrics, frame_id, completed, payload_hash):
    """Atomically associate payload identity with the update that completed."""
    metrics["sent"]+=1
    metrics["last_successful_usb_frame_id"]=frame_id
    metrics["last_successful_update_id"]=metrics["sent"]
    metrics["last_successful_usb_monotonic"]=completed
    metrics["last_successful_payload_sha256"]=payload_hash

def percentile(values, percent):
    """Return an interpolated percentile without external dependencies."""
    if not values: return None
    ordered=sorted(values); position=(len(ordered)-1)*percent/100
    lower=int(position); upper=min(lower+1,len(ordered)-1); fraction=position-lower
    return ordered[lower]+(ordered[upper]-ordered[lower])*fraction

def summary(values):
    if not values: return {}
    return {"count":len(values),"mean":statistics.mean(values),"p50":percentile(values,50),
            "p95":percentile(values,95),"p99":percentile(values,99),"max":max(values)}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--fps",type=float,default=15); ap.add_argument("--quality",type=int,default=80); ap.add_argument("--monitor"); ap.add_argument("--capture",choices=("auto","pipewire","screencopy","wlroots-native","gpu-native","screenshot-fallback"),default="auto"); ap.add_argument("--encoder",choices=("auto","imagemagick","ffmpeg","hardware"),default="auto"); ap.add_argument("--capture-direct-read",action="store_true",help="read native SHM frames directly into a reusable buffer"); ap.add_argument("--capture-zero-copy",action="store_true"); ap.add_argument("--capture-double-buffer",action="store_true"); ap.add_argument("--benchmark",action="store_true"); ap.add_argument("--benchmark-capture",action="store_true"); ap.add_argument("--no-disable-on-exit",action="store_true"); ap.add_argument("--stats",action="store_true"); ap.add_argument("--test-pattern",action="store_true"); ap.add_argument("--diagnostic-bootstrap",action="store_true",help="replay the immutable 48-record test-bars handoff before live capture"); ap.add_argument("--diagnostics-state",type=Path,help="opt-in live diagnostics JSON (off by default)"); ap.add_argument("--telemetry",type=Path,default=default_telemetry_path(),help="atomic live production telemetry path"); ap.add_argument("--no-telemetry",action="store_true",help="disable live telemetry"); ap.add_argument("--image"); ap.add_argument("--seconds",type=float); ap.add_argument("--template",type=Path,default=DEFAULT_TEMPLATE); ap.add_argument("--log",type=Path,default=Path(os.environ.get("XDG_STATE_HOME",Path.home()/".local/state"))/"jua254/stream-latest.json"); ap.add_argument("--config",type=Path,default=Path.home()/".config/jua254/config.json"); ap.add_argument("--presentation-phase-offset-ms",type=float,default=None,help="experimental compositor-presentation phase offset; default disabled"); ap.add_argument("--fb-rotation",action="store_true",help="rotate type-3 frames through the three Windows-captured VRAM buffers and advance FenceID (production supervisor enables this)"); ap.add_argument("--sized-payload",action="store_true",help="size each type-3 transfer to its JPEG like Windows instead of padding to the captured 471008-byte envelope (production supervisor enables this)"); ap.add_argument("--command-ring",action="store_true",help="advance the descriptor JPEG upload address through a VRAM ring like Windows, with the vendor reset flag on start/wrap (production supervisor enables this)"); ap.add_argument("--hw-cursor",action="store_true",help="gpu-native: draw the pointer with the adapter's hardware cursor instead of into frames"); ap.add_argument("--damage-tiles",action="store_true",help="gpu-native: send small damaged regions as type-7 tiles instead of full frames"); ap.add_argument("--tile",default="32x1080",help="gpu-native damage tile size WxH"); ap.add_argument("--max-tiles",type=int,default=24); ap.add_argument("--full-threshold",type=float,default=.4,help="damaged-tile area fraction above which a full frame is sent"); ap.add_argument("--usb-location",help="use the adapter at this USB location (bus-port, as in /sys/bus/usb/devices), not the first one found"); ap.add_argument("--mode",default="1920x1080@60",help="adapter output mode WxH@R from its mode table; non-default modes need --capture gpu-native, --fb-rotation and a matching --monitor size"); args=ap.parse_args()
    if not 0<args.fps<=60 or not 1<=args.quality<=100: ap.error("fps must be 0..60 and quality 1..100")
    try: video_mode=VideoMode.parse(args.mode)
    except ValueError as exc: ap.error(f"--mode: {exc}")
    if not video_mode.default and (args.capture!="gpu-native" or not args.fb_rotation):
        ap.error("non-default --mode requires --capture gpu-native and --fb-rotation")
    if args.presentation_phase_offset_ms is not None and not math.isfinite(args.presentation_phase_offset_ms): ap.error("presentation phase offset must be finite")
    caps=capabilities()
    native=(Path(__file__).resolve().parent/"native/extcopy_capture.c").exists(); caps["wlroots_native"]=native
    if args.capture=="auto": args.capture="wlroots-native" if native else ("screencopy" if caps["screencopy"] else "screenshot-fallback")
    if args.capture=="pipewire" and not (caps["pipewire"] or caps["pipewire_portal"]): raise SystemExit("PipeWire portal capture unavailable")
    if args.capture=="screencopy" and not caps["screencopy"]: raise SystemExit("persistent direct capture unavailable")
    if args.capture=="gpu-native": args.encoder="vaapi-native"; args.sized_payload=True
    elif args.encoder=="auto": args.encoder="hardware" if caps["hardware"] else "ffmpeg"
    if args.encoder=="hardware" and not caps["hardware"]: raise SystemExit("hardware MJPEG unavailable")
    selection={"capture":args.capture,"encoder":args.encoder,"capabilities":caps}
    args.config.parent.mkdir(parents=True,exist_ok=True); args.config.write_text(json.dumps(selection,indent=2)+"\n")
    if args.benchmark or args.benchmark_capture:
        print(json.dumps(selection,indent=2))
        return 0
    records=load_records(args.template); validate_chunks(records); template=final_group(records)
    stop=threading.Event(); latest=Latest(); metrics=metric_set(); presentation=PresentationTrace()
    if args.presentation_phase_offset_ms is not None: metrics["presentation_phase_wait"]=Metric(); metrics["presentation_phase_lateness"]=Metric()
    telemetry=LiveTelemetry(None if args.no_telemetry else args.telemetry,args.monitor,args.fps)
    diagnostics=DiagnosticsState(args.diagnostics_state, "streamer")
    diagnostics.event("stream_start", monitor=args.monitor)
    for name in ("capture_failures", "encode_failures", "usb_failures", "reconnects"):
        diagnostics.counter(name, 0)
    metrics.update({"capture_attempts":0,"successful_captures":0,"producer_capture_calls":0,
                    "encoded_frames":0,"frames_produced":0,"frames_consumed":0,"sent":0,"usb_failures":0,
                    "capture_failures":0,"capture_timeouts":0,"encoder_failures":0,
                    "connections":0,"reconnects":0,"started":time.time(),"fb_rotation":bool(args.fb_rotation),
                    "sized_payload":bool(args.sized_payload),"command_ring":bool(args.command_ring),"command_ring_resets":0})
    if args.sized_payload or args.command_ring: metrics["usb_payload_bytes"]=Metric()
    framer=LiveFramer(template,args.fb_rotation,args.sized_payload,metrics,video_mode)
    metrics["mode"]=str(video_mode)
    def stopping(*_): stop.set()
    signal.signal(signal.SIGINT,stopping); signal.signal(signal.SIGTERM,stopping)
    started=time.monotonic()
    cpu_started=sum(x.ru_utime+x.ru_stime for x in (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))
    if args.capture=="gpu-native":
        from jua254_gpu import run_gpu
        dev=None
        try:
            dev=run_gpu(args,template,framer,metrics,presentation,telemetry,diagnostics,stop,lambda: open_device(video_mode,args.usb_location),query_vram_mb,
                        write_group_timed,record_successful_usb_update,payload_sha256,log_open_failure,note_adapter_ok)
        finally:
            diagnostics.event("stream_stop"); telemetry.close()
            release_device(dev,args,metrics)
            write_report(args,metrics,presentation,started,cpu_started,None)
        return 0
    producer=threading.Thread(target=capture_loop,args=(args,latest,stop,metrics,diagnostics),daemon=True); producer.start(); dev=None; stream_started=None; next_frame=started; previous_sent=None
    try:
        while not stop.is_set() and (args.seconds is None or stream_started is None or time.monotonic()-stream_started<args.seconds):
            item=latest.take()
            if item is None:
                time.sleep(.01)
                continue
            _,(jpeg,capture_started,encoded,capture_presentation,frame_id)=item
            encoded_payload_hash=payload_sha256(jpeg)
            metrics["frames_consumed"]+=1
            if dev is None:
                try:
                    dev=open_device(None,args.usb_location)
                    if dev is None: time.sleep(1); continue
                    note_adapter_ok(metrics)
                    if args.command_ring:
                        metrics["vram_mb"]=query_vram_mb(dev); framer.attach_ring(metrics["vram_mb"])
                    presentation.begin_connection()
                    groups=connection_groups(records,template,jpeg,args.diagnostic_bootstrap,live=framer.group(jpeg))
                    for group in groups:
                        metrics["frame_age_before_usb"].append((time.monotonic()-capture_started)*1000)
                        wait_for_presentation_phase(capture_presentation,args.presentation_phase_offset_ms,metrics.get("presentation_phase_wait"),metrics.get("presentation_phase_lateness"))
                        usb,usb_first,usb_final=write_group_timed(dev,group)
                        presentation.record(usb_first,usb_final,capture_presentation=capture_presentation)
                    done=time.monotonic(); metrics["connections"]+=1
                    if metrics["connections"]>1: metrics["reconnects"]+=1
                    diagnostics.counter("reconnects", metrics["reconnects"], generation=metrics["connections"])
                    metrics["usb"].append(usb); metrics["end_to_end"].append((done-capture_started)*1000); metrics["frame_age_after_usb"].append((done-capture_started)*1000)
                    record_successful_usb_update(metrics,frame_id,done,encoded_payload_hash)
                    if previous_sent is not None: metrics["frame_interval"].append((done-previous_sent)*1000)
                    previous_sent=done
                    diagnostics.event("adapter_open", generation=metrics["connections"])
                    if metrics["sent"] == 1:
                        diagnostics.event("first_usb", frame_id=frame_id, update_id=metrics["sent"],
                                          adapter_generation=metrics["connections"])
                        diagnostics.event("production_handoff", frame_id=frame_id, update_id=metrics["sent"])
                    diagnostics.event("last_usb", frame_id=frame_id, update_id=metrics["sent"],
                                      adapter_generation=metrics["connections"])
                    if stream_started is None:
                        stream_started=time.monotonic(); next_frame=stream_started
                    telemetry.maybe_write(metrics,presentation,done)
                    continue
                except Exception as exc:
                    if dev is not None: metrics["usb_failures"]+=1; metrics["last_usb_error"]=repr(exc)
                    else: log_open_failure(metrics,exc)
                    time.sleep(1); continue
            try:
                metrics["frame_age_before_usb"].append((time.monotonic()-capture_started)*1000)
                wait_for_presentation_phase(capture_presentation,args.presentation_phase_offset_ms,metrics.get("presentation_phase_wait"),metrics.get("presentation_phase_lateness"))
                usb,usb_first,usb_final=write_group_timed(dev,framer.group(jpeg))
                presentation.record(usb_first,usb_final,next_frame,capture_presentation)
                done=time.monotonic(); metrics["usb"].append(usb); metrics["end_to_end"].append((done-capture_started)*1000); metrics["frame_age_after_usb"].append((done-capture_started)*1000)
                record_successful_usb_update(metrics,frame_id,done,encoded_payload_hash)
                if previous_sent is not None: metrics["frame_interval"].append((done-previous_sent)*1000)
                previous_sent=done
                diagnostics.event("last_usb", frame_id=frame_id, update_id=metrics["sent"],
                                  adapter_generation=metrics["connections"])
                telemetry.maybe_write(metrics,presentation,done)
            except Exception as exc:
                metrics["usb_failures"]+=1; metrics["last_usb_error"]=repr(exc)
                diagnostics.counter("usb_failures", metrics["usb_failures"], error=repr(exc))
                try:
                    import usb.util; usb.util.dispose_resources(dev)
                except Exception: pass
                dev=None
            next_frame+=1/args.fps; time.sleep(max(0,next_frame-time.monotonic()))
    finally:
        diagnostics.event("stream_stop")
        telemetry.close()
        stop.set(); producer.join(timeout=7)
        release_device(dev,args,metrics)
        write_report(args,metrics,presentation,started,cpu_started,latest)
    return 0

def release_device(dev,args,metrics):
    if dev is not None and not args.no_disable_on_exit:
        try: dev.ctrl_transfer(0x40,0x03,0,0,None,timeout=2000)
        except Exception as exc: metrics["disable_error"]=repr(exc)
    if dev is not None:
        try:
            import usb.util; usb.util.release_interface(dev,INTERFACE); usb.util.dispose_resources(dev)
        except Exception: pass

def write_report(args,metrics,presentation,started,cpu_started,latest):
    cpu_ended=sum(x.ru_utime+x.ru_stime for x in (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))
    reports={name:value.report() for name,value in metrics.items() if isinstance(value,Metric)}
    for name in reports: del metrics[name]
    elapsed=max(.001,time.monotonic()-started)
    metrics.update(reports)
    overwritten=latest.overwritten if latest else 0
    metrics.update({"ended":time.time(),"latest_slot_overwrites":overwritten,
                    "latest_slot_overwrite_monotonic":list(latest.overwrite_times) if latest else [],
                    "pending_frames":max(0,metrics["frames_produced"]-metrics["frames_consumed"]-overwritten),
                    "overwrite_percentage":100*overwritten/max(1,metrics["frames_produced"]),
                    "wall_clock_duration_seconds":elapsed,
                    "effective_sent_fps":metrics["sent"]/elapsed,"cpu_seconds":cpu_ended-cpu_started})
    def throughput(count,name):
        total_ms=metrics.get(name,{}).get("total",0)
        return 1000*count/total_ms if total_ms else None
    metrics["stage_throughput_fps"]={
        "capture_ready_wait":throughput(metrics["successful_captures"],"capture_ready_wait"),
        "capture_copy":throughput(metrics["successful_captures"],"capture_copy"),
        "capture_total":throughput(metrics["successful_captures"],"capture_total"),
        "encode":throughput(metrics["encoded_frames"],"encode"),
        "usb":throughput(metrics["sent"],"usb"),
        "producer":metrics["frames_produced"]/elapsed,
    }
    metrics["presentation_timing"]=presentation.report()
    metrics["cpu_percent_one_core"]=100*metrics["cpu_seconds"]/max(.001,time.monotonic()-started)
    args.log.parent.mkdir(parents=True,exist_ok=True); args.log.write_text(json.dumps(metrics,indent=2,default=str)+"\n")
    if args.stats: print(json.dumps(metrics,indent=2,default=str))

if __name__=="__main__": raise SystemExit(main())

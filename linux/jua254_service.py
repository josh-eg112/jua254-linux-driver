#!/usr/bin/env python3
"""Run the proven streamer from the user's TOML configuration.

This small supervisor deliberately leaves USB framing and presentation in
linux/jua254_stream.py.  It only translates persistent configuration into
the existing CLI and lets systemd restart the process after compositor
restarts or other transient failures.
"""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tomllib

DEFAULT_CONFIG = Path.home() / ".config/jua254/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/jua254"
STREAM = Path(__file__).with_name("jua254_stream.py")

def read_config(path):
    values = {
        "capture": "wlroots-native", "encoder": "vaapi", "cursor": True,
        "fps": 30, "quality": 80, "monitor": "eDP-1",
        "no_disable_on_exit": False, "stats": True,
        "presentation_phase_offset_ms": None,
        "log": str(DEFAULT_STATE / "stream.json"),
    }
    if path.exists():
        with path.open("rb") as f:
            values.update(tomllib.load(f))
    # The public config calls the Intel VA-API backend "vaapi"; the existing
    # proven CLI calls it "hardware".
    if values.get("encoder") == "vaapi":
        values["encoder"] = "hardware"
    if values.get("capture") == "direct-current":
        values["capture"] = "wlroots-native"
    return values

def command(values, extra):
    args = [sys.executable, str(STREAM), "--capture", str(values["capture"]),
            "--encoder", str(values["encoder"]), "--fps", str(values["fps"]),
            "--quality", str(values["quality"])]
    if values.get("monitor"): args += ["--monitor", str(values["monitor"])]
    if values.get("stats"): args.append("--stats")
    if values.get("no_disable_on_exit"): args.append("--no-disable-on-exit")
    if values.get("presentation_phase_offset_ms") is not None:
        args += ["--presentation-phase-offset-ms", str(values["presentation_phase_offset_ms"])]
    if values.get("log"): args += ["--log", os.path.expanduser(str(values["log"]))]
    return args + extra

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("extra", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    if not ns.run:
        ap.error("this helper is intended to be launched with --run")
    values = read_config(ns.config)
    os.execv(sys.executable, command(values, ns.extra))

if __name__ == "__main__":
    main()

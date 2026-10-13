#!/usr/bin/env python3
"""User-facing jua254-stream management command."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

UNIT = "jua254-stream.service"
CONFIG = Path.home() / ".config/jua254/config.toml"

def run(*args, check=False):
    return subprocess.run(list(args), check=check)

def main():
    ap = argparse.ArgumentParser(prog="jua254-stream")
    ap.add_argument("command", choices=("start", "stop", "restart", "status", "logs", "config", "benchmark"))
    ap.add_argument("--lines", type=int, default=100)
    ns = ap.parse_args()
    if ns.command == "start":
        return run("systemctl", "--user", "enable", "--now", UNIT).returncode
    if ns.command == "stop":
        return run("systemctl", "--user", "disable", "--now", UNIT).returncode
    if ns.command == "restart":
        return run("systemctl", "--user", "restart", UNIT).returncode
    if ns.command == "status":
        return run("systemctl", "--user", "status", UNIT, "--no-pager").returncode
    if ns.command == "logs":
        return run("journalctl", "--user", "-u", UNIT, "-n", str(ns.lines), "-f").returncode
    if ns.command == "config":
        if not CONFIG.exists():
            print("configuration is not installed: %s" % CONFIG, file=sys.stderr)
            return 1
        print(CONFIG.read_text(), end="")
        return 0
    # Capability probe uses the same proven backend selection without writing
    # USB or changing HDMI state.
    return run(sys.executable, str(Path(__file__).with_name("jua254_stream.py")),
               "--benchmark", "--config", str(Path.home()/".config/jua254/config.json")).returncode

if __name__ == "__main__":
    raise SystemExit(main())

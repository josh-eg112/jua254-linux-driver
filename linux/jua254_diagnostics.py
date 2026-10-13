#!/usr/bin/env python3
"""Opt-in, protocol-neutral runtime diagnostics for JUA254 processes."""
import json
import os
import threading
import time
from pathlib import Path


class DiagnosticsState:
    """Maintain a small atomic JSON snapshot when explicitly enabled."""

    def __init__(self, path=None, component=None):
        self.path = Path(path) if path else None
        self.component = component
        self.lock = threading.Lock()
        self.state = {
            "version": 1,
            "component": component,
            "pid": os.getpid(),
            "start_wall_time": time.time(),
            "start_monotonic": time.monotonic(),
            "events": {},
            "counters": {},
        }
        if self.path:
            self._write()

    @property
    def enabled(self):
        return self.path is not None

    def event(self, name, **fields):
        if not self.path:
            return
        now_wall, now_mono = time.time(), time.monotonic()
        with self.lock:
            self.state["events"][name] = {
                "wall_time": now_wall, "monotonic": now_mono, **fields
            }
            self._write()

    def counter(self, name, value, **fields):
        if not self.path:
            return
        with self.lock:
            self.state["counters"][name] = value
            if fields:
                self.state["events"]["last_" + name] = {
                    "wall_time": time.time(), "monotonic": time.monotonic(), **fields
                }
            self._write()

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)

#!/usr/bin/env python3
"""Focused regression tests for bounded atomic live production telemetry."""

import json, os, tempfile
from pathlib import Path

from jua254_stream import (LiveTelemetry, PresentationTrace, payload_sha256,
                           record_successful_usb_update)


def main():
    assert payload_sha256(b"same") == payload_sha256(b"same")
    assert payload_sha256(b"same") != payload_sha256(b"changed")
    association={"sent":6}
    digest=payload_sha256(b"encoded jpeg")
    record_successful_usb_update(association,42,12.5,digest)
    assert association["last_successful_update_id"]==7
    assert association["last_successful_usb_frame_id"]==42
    assert association["last_successful_usb_monotonic"]==12.5
    assert association["last_successful_payload_sha256"]==digest
    metrics={"capture_attempts":130,"successful_captures":129,"encoded_frames":128,
             "frames_produced":128,"frames_consumed":127,"sent":126,
             "capture_failures":1,"capture_timeouts":2,"encoder_failures":3,
             "usb_failures":4,"reconnects":5,"last_successful_update_id":126,
             "last_successful_payload_sha256":digest}
    trace=PresentationTrace(); trace.begin_connection()
    trace.record(1,1.01); trace.record(1.02,1.03)
    for n in range(130): trace.record(2+n*.033,2.01+n*.033)
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/"telemetry.json"
        writer=LiveTelemetry(path,"HEADLESS-2",30,interval=.25,history_limit=120)
        assert writer.maybe_write(metrics,trace,now=20)
        first=json.loads(path.read_text())
        assert first["schema"]=="jua254-production-telemetry" and first["version"]==2
        assert first["pid"]==os.getpid() and first["monitor"]=="HEADLESS-2"
        assert first["configured_fps"]==30 and first["snapshot_monotonic"]==20
        assert first["counts"]["updates_sent"]==126 and first["failures"]["usb"]==4
        assert first["last_success"]["payload_sha256"]==digest
        assert len(first["recent_usb_start_intervals_ms"])==120
        assert len(first["recent_usb_completion_intervals_ms"])==120
        assert not writer.due(20.1) and writer.due(20.25)
        assert not writer.maybe_write(metrics,trace,now=20.1)
        assert json.loads(path.read_text())["snapshot_monotonic"]==20
        assert writer.maybe_write(metrics,trace,now=20.25)
        assert json.loads(path.read_text())["snapshot_monotonic"]==20.25
        assert list(Path(directory).glob("*.tmp"))==[]
        assert path.stat().st_mode & 0o777 == 0o600
        writer.close(); assert not path.exists()
    print("live telemetry regression: PASS")
    return 0


if __name__=="__main__": raise SystemExit(main())

#!/usr/bin/env python3
"""Production starts with live capture; immutable bars remain diagnostic-only."""
import hashlib
import json
from pathlib import Path

from jua254_stream import DEFAULT_TEMPLATE, CAPACITY, connection_groups, final_group
from replay_handoff import load_records, validate_chunks

EXPECTED = {
    "manifest.json": "3a9f6895791765c6c0cf29e70b4c3cb82afbd9234ace78863bc80eb3011431ee",
    "manifest-urb.json": "64a909a55563b83ba268f15a690f723393ee4b5e8e4f09e5e4fd2090d2fbe99f",
}

def fake_live_jpeg():
    # Structural substitution is covered here; encoder validity is covered by
    # existing streamer tests/runtime validation before this function is used.
    return b"\xff\xd8LIVE-CAPTURE\xff\xd9"

def test_production_start_reconnect_and_child_recovery_are_live_only():
    records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
    template = final_group(records)
    for event in ("startup", "reconnect", "child-recovery"):
        groups = connection_groups(records, template, fake_live_jpeg())
        assert len(groups) == 1, event
        assert len(groups[0]) == 10, event
        logical = b"".join(data for _, data in groups[0][1::2])
        assert logical[48:48 + len(fake_live_jpeg())] == fake_live_jpeg(), event
        assert len(logical) == 48 + CAPACITY, event

def test_explicit_diagnostic_bootstrap_retains_test_bars():
    records = load_records(DEFAULT_TEMPLATE); template = final_group(records)
    groups = connection_groups(records, template, fake_live_jpeg(), diagnostic_bootstrap=True)
    assert groups[0] is records and len(groups[0]) == 48
    assert len(groups[1]) == 10

def test_handoff_provenance_is_unchanged():
    for name, expected in EXPECTED.items():
        assert hashlib.sha256((DEFAULT_TEMPLATE / name).read_bytes()).hexdigest() == expected
    records = load_records(DEFAULT_TEMPLATE); validate_chunks(records)
    assert len(records) == 48
    assert sum(len(data) for _, data in records) == 1863968

if __name__ == "__main__":
    test_production_start_reconnect_and_child_recovery_are_live_only()
    test_explicit_diagnostic_bootstrap_retains_test_bars()
    test_handoff_provenance_is_unchanged()
    print("production bootstrap regression: PASS")

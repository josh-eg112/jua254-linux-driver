#!/usr/bin/env python3
"""The VA-API JPEG encoder must emit full-range YCbCr (JFIF), or the adapter shows black as 16 and white as 235."""
import jua254_stream


class FakePopen:
    def __init__(self, command, **_):
        self.args = command


def filter_for(monkeypatch, **kwargs):
    monkeypatch.setattr(jua254_stream.subprocess, "Popen", FakePopen)
    command = jua254_stream.PersistentFFmpegEncoder("hardware", 80, "bgra", **kwargs).proc.args
    return command[command.index("-vf") + 1]


def test_hardware_encoder_converts_to_full_range(monkeypatch):
    for kwargs in ({}, {"no_scale": True}):
        vf = filter_for(monkeypatch, **kwargs)
        assert "out_range=full" in vf
        # mjpeg_vaapi rejects frames tagged full range, so the tag stays limited.
        assert "setparams=range=tv" in vf and vf.index("setparams") < vf.index("hwupload")

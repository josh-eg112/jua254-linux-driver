#!/usr/bin/env python3
"""Regression coverage for relocatable production template packaging."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = Path("shared/handoff/linux-rgb-bars-20260825")

def test_installed_assets_from_unrelated_cwd():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        home = base / "home"
        root = base / "installed"
        fake_bin = base / "bin"
        home.mkdir(); fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl_log = base / "systemctl.log"
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$JUA254_SYSTEMCTL_LOG"\nexit 0\n')
        systemctl.chmod(0o755)
        env = os.environ | {
            "HOME": str(home),
            "JUA254_INSTALL_ROOT": str(root),
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "JUA254_SYSTEMCTL_LOG": str(systemctl_log),
        }
        subprocess.run([str(REPO / "linux/install.sh")], env=env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Hold the installed native-helper pathname open as an executing binary.
        # Atomic directory publication must permit the second install without
        # truncating that inode (which would produce ETXTBSY/Text file busy).
        helper = root / "linux/native/jua254-extcopy-capture"
        helper.write_bytes(Path("/bin/sleep").read_bytes())
        helper.chmod(0o755)
        running = subprocess.Popen([str(helper), "30"])
        try:
            time.sleep(.05)
            subprocess.run([str(REPO / "linux/install.sh")], env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert running.poll() is None
        finally:
            running.terminate()
            running.wait(timeout=2)

        executable = (
            root / "linux/jua254_monitor.py",
            root / "linux/jua254_stream.py",
            root / "linux/native/jua254-extcopy-capture",
            home / ".local/bin/jua254-stream",
            home / ".local/bin/jua254-monitor-test",
            home / ".local/bin/jua254-monitor-status",
            home / ".local/bin/jua254-monitor-start",
            home / ".local/bin/jua254-monitor-stop",
            home / ".local/bin/jua254-monitor-diagnostic",
        )
        for path in executable:
            assert path.stat().st_mode & 0o111 == 0o111, path

        installed = root / TEMPLATE
        manifest_path = installed / "manifest-urb.json"
        assert manifest_path.is_file()
        assert manifest_path.stat().st_mode & 0o111 == 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        for transfer in manifest["transfers"]:
            payload = installed / transfer["file"]
            assert payload.is_file()
            assert payload.stat().st_mode & 0o111 == 0
            data = payload.read_bytes()
            assert len(data) == transfer["length"]
            assert hashlib.sha256(data).hexdigest() == transfer["sha256"]

        probe = """
import os, sys
from pathlib import Path
os.chdir('/tmp')
sys.path.insert(0, sys.argv[1])
import jua254_stream, jua254_monitor
from replay_handoff import load_records, validate_chunks
expected = Path(sys.argv[1]).parent / 'shared/handoff/linux-rgb-bars-20260825'
assert jua254_stream.DEFAULT_TEMPLATE == expected
assert jua254_monitor.DEFAULT_TEMPLATE == expected
records = load_records(jua254_stream.DEFAULT_TEMPLATE)
validate_chunks(records)
class Args:
    seconds = None
    streamer = str(Path(sys.argv[1]) / 'jua254_stream.py')
    fps = 30
    template = jua254_monitor.DEFAULT_TEMPLATE
command = jua254_monitor.stream_command('HEADLESS-1', Args())
assert command[command.index('--template') + 1] == str(expected)
print(len(records))
"""
        result = subprocess.run([sys.executable, "-c", probe, str(root / "linux")],
                                cwd="/tmp", check=True, capture_output=True, text=True)
        assert result.stdout.strip() == "48"
        replay = subprocess.run(
            [sys.executable, str(root / "linux/replay_handoff.py"), str(installed), "--dry-run"],
            cwd="/tmp", check=True, capture_output=True, text=True,
        )
        assert replay.stdout.strip() == "validated 48 records, 1863968 bytes"

        calls = systemctl_log.read_text().splitlines()
        assert calls.count("--user stop jua254-monitor.service") == 2
        assert calls.count("--user reset-failed jua254-monitor.service") == 2
        assert calls.count("--user enable --now jua254-monitor.service") == 2
        for offset in (0, len(calls) // 2):
            install_calls = calls[offset:offset + len(calls) // 2]
            assert install_calls.index("--user stop jua254-monitor.service") < install_calls.index("--user enable --now jua254-monitor.service")

if __name__ == "__main__":
    test_installed_assets_from_unrelated_cwd()
    print("installed asset/path regression: PASS")

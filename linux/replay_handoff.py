#!/usr/bin/env python3
"""Replay verified Windows USB URBs with their original boundaries/timing."""

import argparse
import hashlib
import json
import struct
import threading
import time
from pathlib import Path


VID = 0x0711
PID = 0x5600
ENDPOINT = 0x02
INTERRUPT_ENDPOINT = 0x83
INTERFACE = 0
MODE_1080P60 = bytes.fromhex(
    "14 44 02 00 3c 00 98 08 80 07 d8 07 2c 00 65 04 "
    "38 04 3c 04 05 00 bb 02 e8 03 1d 01 01 01 00 00"
)


def load_records(handoff: Path) -> list[tuple[dict, bytes]]:
    manifest_path = handoff / "manifest-urb.json"
    if not manifest_path.exists():
        raise ValueError(
            "missing manifest-urb.json: the existing manifest does not preserve "
            "auditable Windows URB boundaries; run extract_urb_boundaries.py "
            "against the source PCAPNG before any live replay"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    provenance = manifest.get("urb_boundary_provenance", {})
    derived_methods = {
        "derived-jpeg-substitution-v1",
        "derived-type-3-jpeg-substitution-v1",
        "derived-type-7-jpeg-substitution-v1",
    }
    if provenance.get("method") not in {
        "tshark-usbpcap-urb-id", "native-pcapng-usbpcap",
    } | derived_methods:
        raise ValueError("manifest-urb.json has missing or untrusted URB boundary provenance")
    if provenance["method"] in derived_methods:
        if not provenance.get("source_manifest_sha256") or provenance.get("invariant") != (
            "all descriptors, URB lengths, timings, and 48-byte video header unchanged"
        ):
            raise ValueError("derived replay has incomplete invariant provenance")
    elif not provenance.get("source_sha256"):
        raise ValueError("captured replay has incomplete source provenance")
    records = []
    for expected_sequence, item in enumerate(manifest["transfers"], 1):
        if item["sequence"] != expected_sequence or item["endpoint"].lower() != "0x02":
            raise ValueError(f"unexpected record metadata at sequence {expected_sequence}")
        boundary = item.get("urb_boundary")
        if not boundary or boundary.get("pcap_frame") != item.get("pcap_frame"):
            raise ValueError(f"missing/mismatched URB boundary at sequence {expected_sequence}")
        if boundary.get("captured_length") != item["length"]:
            raise ValueError(f"URB length mismatch at sequence {expected_sequence}")
        data = (handoff / item["file"]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != item["length"] or digest != item["sha256"]:
            raise ValueError(f"length/hash mismatch for {item['file']}")
        records.append((item, data))
    if len(records) != manifest["transfer_count"]:
        raise ValueError("transfer count does not match manifest")
    return records


def validate_chunks(records: list[tuple[dict, bytes]]) -> None:
    index = 0
    while index < len(records):
        desc = records[index][1]
        if len(desc) != 32:
            raise ValueError(f"record {index + 1} is not a descriptor")
        words = struct.unpack("<8I", desc)
        if words[0] != 0 or words[4] != 0:
            raise ValueError(f"invalid initial descriptor at record {index + 1}")
        total, stream = words[1], words[2]
        offset = 0
        while offset < total:
            words = struct.unpack("<8I", records[index][1])
            if words[0] != 0 or words[1] != total or words[2] != stream or words[4] != offset:
                raise ValueError(f"inconsistent descriptor at record {index + 1}")
            index += 1
            if index >= len(records) or len(records[index][1]) != words[3]:
                raise ValueError(f"missing/wrong data chunk after record {index}")
            offset += words[3]
            index += 1
            if offset < total and index >= len(records):
                raise ValueError("logical transfer is incomplete")
        if offset != total:
            raise ValueError("logical transfer exceeds descriptor total")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("handoff", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-timing", action="store_true", help="omit captured inter-record delays")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument(
        "--monitor-interrupt", action="store_true",
        help="keep the captured driver's endpoint-0x83 interrupt read pending",
    )
    parser.add_argument(
        "--initialize-1080p", action="store_true",
        help="apply the previously verified non-firmware 1080p60 modeset sequence",
    )
    parser.add_argument(
        "--hold-output-seconds", type=float, default=0.0,
        help="after replay, keep initialized video enabled for visual inspection",
    )
    args = parser.parse_args()
    records = load_records(args.handoff)
    validate_chunks(records)
    print(f"validated {len(records)} records, {sum(len(data) for _, data in records)} bytes")
    if args.dry_run:
        return 0

    import usb.backend.libusb1
    import usb.core
    import usb.util

    backend = usb.backend.libusb1.get_backend(find_library=lambda _: "/usr/lib/libusb-1.0.so")
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise RuntimeError("0711:5600 is not accessible through libusb")
    claimed = False
    detached = False
    interrupt_stop = threading.Event()
    interrupt_events: list[bytes] = []
    interrupt_errors: list[str] = []
    interrupt_thread = None
    output_enabled = False
    try:
        dev.set_configuration()
        if dev.is_kernel_driver_active(INTERFACE):
            dev.detach_kernel_driver(INTERFACE)
            detached = True
        usb.util.claim_interface(dev, INTERFACE)
        claimed = True
        if args.initialize_1080p:
            project = bytes(dev.ctrl_transfer(0xC0, 0xB0, 0, 3, 16, timeout=2000)).rstrip(b"\0")
            if project != b"JUA254H":
                raise RuntimeError(f"unexpected project code {project!r}")
            connector = bytes(dev.ctrl_transfer(0xC0, 0x87, 0, 0, 1, timeout=2000))
            if connector != b"\x01":
                raise RuntimeError(f"HDMI is not connected (status {connector.hex()})")
            mode = bytes(dev.ctrl_transfer(0xC0, 0x89, 0, 0x0340, 32, timeout=2000))
            if mode != MODE_1080P60:
                raise RuntimeError(f"adapter 1080p60 mode mismatch: {mode.hex()}")
            dev.ctrl_transfer(0x40, 0x03, 0, 0, None, timeout=2000)
            time.sleep(0.25)
            written = dev.ctrl_transfer(0x40, 0x12, 0, 0, MODE_1080P60, timeout=2000)
            if written != len(MODE_1080P60):
                raise RuntimeError(f"short mode write: {written}/{len(MODE_1080P60)}")
            time.sleep(0.25)
            dev.ctrl_transfer(0x40, 0x03, 0, 1, None, timeout=2000)
            output_enabled = True
            print("verified JUA254H/HDMI and enabled 1920x1080@60")
        if args.monitor_interrupt:
            def monitor_interrupt() -> None:
                while not interrupt_stop.is_set():
                    try:
                        event = bytes(dev.read(INTERRUPT_ENDPOINT, 64, timeout=100))
                        if event:
                            interrupt_events.append(event)
                            print(f"interrupt-IN {len(event)} bytes: {event.hex()}")
                    except usb.core.USBTimeoutError:
                        pass
                    except Exception as exc:
                        interrupt_errors.append(repr(exc))
                        return

            interrupt_thread = threading.Thread(target=monitor_interrupt, daemon=True)
            interrupt_thread.start()
        previous = records[0][0]["time_relative_seconds"]
        for number, (item, data) in enumerate(records, 1):
            timestamp = item["time_relative_seconds"]
            if number > 1 and not args.no_timing:
                time.sleep(max(0.0, timestamp - previous))
            # Exactly one libusb transfer per captured Windows URB. A short
            # completion is an error; retrying its tail would create a new URB
            # boundary that did not exist in the capture.
            written = dev.write(ENDPOINT, data, timeout=args.timeout_ms)
            if written != len(data):
                raise RuntimeError(
                    f"short USB write at URB {number}: {written}/{len(data)} bytes; "
                    "tail was not retried"
                )
            print(f"{number:02d}/{len(records)} wrote {written:6d} bytes")
            previous = timestamp
        print("replay complete")
        if output_enabled and args.hold_output_seconds > 0:
            print(f"holding video output for {args.hold_output_seconds:g} seconds")
            time.sleep(args.hold_output_seconds)
        return 0
    finally:
        # No confirmed non-firmware video-disable request is recorded yet. Always
        # release the claimed interface and restore its prior kernel ownership.
        interrupt_stop.set()
        if interrupt_thread is not None:
            interrupt_thread.join(timeout=1.0)
            print(
                f"interrupt monitor: {len(interrupt_events)} events, "
                f"{len(interrupt_errors)} errors"
            )
            for error in interrupt_errors:
                print(f"interrupt monitor error: {error}")
        if output_enabled:
            try:
                dev.ctrl_transfer(0x40, 0x03, 0, 0, None, timeout=2000)
                print("disabled video output during cleanup")
            except Exception as exc:
                print(f"warning: could not disable video output: {exc!r}")
        if claimed:
            usb.util.release_interface(dev, INTERFACE)
        usb.util.dispose_resources(dev)
        if detached:
            try:
                dev.attach_kernel_driver(INTERFACE)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(f"replay refused: {exc}") from None

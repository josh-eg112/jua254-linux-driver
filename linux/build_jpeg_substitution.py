#!/usr/bin/env python3
"""Derive a fixed-length JUA254 replay by replacing only the final JPEG body."""

import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jpeg_metadata(data: bytes) -> dict:
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG must have exact SOI/EOI markers and no trailing bytes")
    offset = 2
    sof = None
    sos_components = None
    while offset < len(data):
        if data[offset] != 0xFF:
            raise ValueError(f"invalid JPEG marker at offset {offset}")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker in range(0xD0, 0xD8) or marker in (0x00, 0x01, 0xD8):
            continue
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise ValueError("invalid JPEG segment length")
        segment = data[offset + 2:offset + length]
        if marker == 0xC0:
            count = segment[5]
            components = [
                {"id": segment[6 + 3*i], "sampling": segment[7 + 3*i],
                 "quant_table": segment[8 + 3*i]}
                for i in range(count)
            ]
            sof = {
                "marker": "SOF0", "precision": segment[0],
                "height": int.from_bytes(segment[1:3], "big"),
                "width": int.from_bytes(segment[3:5], "big"),
                "components": components,
            }
        if marker == 0xDA:
            count = segment[0]
            sos_components = [segment[1 + 2*i] for i in range(count)]
            # Entropy-coded bytes run until the final EOI; stuffed marker bytes
            # are deliberately not parsed as ordinary segments.
            break
        offset += length
    if sof is None or sos_components is None:
        raise ValueError("JPEG must contain baseline SOF0 and SOS markers")
    sof["sos_component_ids"] = sos_components
    sof["length"] = len(data)
    sof["sha256"] = sha256(data)
    return sof


def normalize_component_ids(data: bytes) -> bytes:
    """Change conventional JPEG IDs 1/2/3 to the captured encoder's 0/1/2."""
    result = bytearray(data)
    offset = 2
    mapping = {1: 0, 2: 1, 3: 2}
    while offset < len(result):
        while result[offset] == 0xFF:
            offset += 1
        marker = result[offset]
        offset += 1
        if marker == 0xD9:
            break
        length = int.from_bytes(result[offset:offset + 2], "big")
        if marker == 0xC0:
            count = result[offset + 7]
            for i in range(count):
                pos = offset + 8 + 3*i
                result[pos] = mapping[result[pos]]
        elif marker == 0xDA:
            count = result[offset + 2]
            for i in range(count):
                pos = offset + 3 + 2*i
                result[pos] = mapping[result[pos]]
            break
        offset += length
    return bytes(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("jpeg", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--payload-type", type=int, choices=(3, 7), default=3,
                        help="replace the last payload of this type (default: 3/full frame)")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output {args.output}")

    source_manifest_path = args.source / "manifest-urb.json"
    manifest_bytes = source_manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    jpeg = normalize_component_ids(args.jpeg.read_bytes())
    jpeg_info = jpeg_metadata(jpeg)
    expected_components = [
        {"id": 0, "sampling": 0x22, "quant_table": 0},
        {"id": 1, "sampling": 0x11, "quant_table": 1},
        {"id": 2, "sampling": 0x11, "quant_table": 1},
    ]
    expected_geometry = (1920, 1080, 8) if args.payload_type == 3 else (32, 1080, 8)
    if (jpeg_info["width"], jpeg_info["height"], jpeg_info["precision"]) != expected_geometry:
        raise SystemExit(f"wrong JPEG geometry/precision: {jpeg_info}")
    if jpeg_info["components"] != expected_components or jpeg_info["sos_component_ids"] != [0, 1, 2]:
        raise SystemExit(f"wrong JPEG component layout: {jpeg_info}")

    records = []
    for item in manifest["transfers"]:
        records.append([item, (args.source / item["file"]).read_bytes()])

    groups = []
    index = 0
    while index < len(records):
        group = []
        descriptor = struct.unpack("<8I", records[index][1])
        total = descriptor[1]
        offset = 0
        while offset < total:
            desc_index = index
            desc = struct.unpack("<8I", records[index][1])
            index += 1
            data_index = index
            data = records[index][1]
            index += 1
            if desc[4] != offset or len(data) != desc[3]:
                raise SystemExit("source descriptor/chunk validation failed")
            group.append((desc_index, data_index, desc, data))
            offset += len(data)
        groups.append(group)

    matching_groups = []
    for group in groups:
        logical = b"".join(chunk for _, _, _, chunk in group)
        if len(logical) >= 48 and struct.unpack_from("<I", logical)[0] == args.payload_type:
            matching_groups.append(group)
    if not matching_groups:
        raise SystemExit(f"no type-{args.payload_type} logical transfer found")
    target = matching_groups[-1]
    original_logical = b"".join(chunk for _, _, _, chunk in target)
    original_header = original_logical[:48]
    header_words = list(struct.unpack("<12I", original_header))
    if header_words[0] != args.payload_type or header_words[1] != len(original_logical) - 48:
        raise SystemExit(f"target is not the expected fixed-length type-{args.payload_type} envelope")
    capacity = len(original_logical) - 48
    if len(jpeg) > capacity:
        raise SystemExit(f"JPEG {len(jpeg)} exceeds fixed envelope {capacity}")
    modified_logical = original_header + jpeg + bytes(capacity - len(jpeg))

    logical_offset = 0
    modified_sequences = []
    for _desc_index, data_index, _desc, old_chunk in target:
        new_chunk = modified_logical[logical_offset:logical_offset + len(old_chunk)]
        logical_offset += len(old_chunk)
        records[data_index][1] = new_chunk
        if new_chunk != old_chunk:
            modified_sequences.append(records[data_index][0]["sequence"])

    shutil.copytree(args.source, args.output)
    for item, data in records:
        (args.output / item["file"]).write_bytes(data)
        item["sha256"] = sha256(data)
    manifest["urb_boundary_provenance"] = {
        "method": f"derived-type-{args.payload_type}-jpeg-substitution-v1",
        "source_manifest_sha256": sha256(manifest_bytes),
        "source_manifest": str(source_manifest_path),
        "jpeg_source": str(args.jpeg),
        "jpeg_sha256_after_component_normalization": jpeg_info["sha256"],
        "modified_sequences": modified_sequences,
        "invariant": "all descriptors, URB lengths, timings, and 48-byte video header unchanged",
    }
    (args.output / "manifest-urb.json").write_text(json.dumps(manifest, indent=2) + "\n")
    experiment = {
        "payload_type": args.payload_type,
        "jpeg": jpeg_info,
        "fixed_image_capacity": capacity,
        "zero_padding": capacity - len(jpeg),
        "original_header_hex": original_header.hex(" "),
        "modified_header_hex": modified_logical[:48].hex(" "),
        "header_words_original": header_words,
        "header_words_modified": list(struct.unpack("<12I", modified_logical[:48])),
        "changed_header_fields": [],
        "modified_record_sequences": modified_sequences,
        "rollback_handoff": str(args.source),
    }
    (args.output / "experiment.json").write_text(json.dumps(experiment, indent=2) + "\n")
    print(json.dumps(experiment, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# Build and run the Swift protocol tests (Linux or macOS). On Linux set SWIFTC to a toolchain's swiftc.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
"${SWIFTC:-swiftc}" -swift-version 5 -O Protocol.swift JPEG.swift Numbering.swift tests/main.swift -o build/core-tests
build/core-tests tests/golden
if [[ "$(uname)" == Darwin ]]; then
  # Frame preparation (Accelerate/Core Image) exists only on macOS.
  "${SWIFTC:-swiftc}" -swift-version 5 -O Frames.swift tests/frames/main.swift -o build/frames-tests
  build/frames-tests
fi

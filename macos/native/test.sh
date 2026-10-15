#!/usr/bin/env bash
# Build and run the Swift protocol tests (Linux or macOS). On Linux set SWIFTC to a toolchain's swiftc.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
"${SWIFTC:-swiftc}" -swift-version 5 -O Protocol.swift JPEG.swift tests/main.swift -o build/core-tests
build/core-tests tests/golden

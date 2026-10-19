#!/usr/bin/env bash
# Build and run the Swift protocol tests (Linux or macOS). On Linux set SWIFTC to a toolchain's swiftc.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
"${SWIFTC:-swiftc}" -swift-version 5 -O Protocol.swift JPEG.swift Numbering.swift tests/main.swift -o build/core-tests
build/core-tests tests/golden
if [[ "$(uname)" == Darwin ]]; then
  # Frame preparation (Accelerate/Core Image) and the libjpeg-turbo encoder are built only on macOS.
  source ./deps.sh
  clang -O2 -Wall -I build/include/jpeg -c CTurbo/turbo.c -o build/turbo-test.o
  "${SWIFTC:-swiftc}" -swift-version 5 -O -I CTurbo -L build/lib Protocol.swift JPEG.swift Frames.swift Encoder.swift \
    tests/frames/main.swift build/turbo-test.o -o build/frames-tests \
    2> >(grep -v "no platform load command found" >&2)
  build/frames-tests
fi

#!/usr/bin/env bash
# Build the app's C libraries from pinned source tarballs (SHA-256 checked) into build/, once:
#   build/lib/libusb-1.0.a, build/lib/libjpeg.a   universal static libraries (x86_64 + arm64)
#   build/include/jpeg/                            libjpeg-turbo's public headers
# Used (sourced) by build.sh and test.sh. Delete build/ to rebuild from scratch.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
build="$(pwd)/build"
mkdir -p "$build/lib" "$build/dl"

fetch() {  # url, sha256, file: download once, then verify
  local file="$build/dl/$3"
  if [[ ! -f "$file" ]]; then curl -fsSL -o "$file.part" "$1" && mv "$file.part" "$file"; fi
  echo "$2  $file" | shasum -a 256 -c - >/dev/null || { echo "Checksum mismatch for $3"; rm -f "$file"; exit 1; }
}

# 1. libusb, compiled by hand (it needs no configure step on macOS).
libusb_version=1.0.30
src="$build/libusb-$libusb_version"
if [[ ! -f "$build/lib/libusb-1.0.a" ]]; then
  echo "Building libusb $libusb_version..."
  fetch "https://github.com/libusb/libusb/releases/download/v$libusb_version/libusb-$libusb_version.tar.bz2" \
    fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf libusb.tar.bz2
  rm -rf "$src" && tar xjf "$build/dl/libusb.tar.bz2" -C "$build"
  rm -rf "$build/obj" && mkdir -p "$build/obj"
  for file in core descriptor hotplug io strerror sync os/darwin_usb os/events_posix os/threads_posix; do
    clang -arch x86_64 -arch arm64 -mmacosx-version-min=12.3 -O2 -w \
      -I "$src/Xcode" -I "$src/libusb" -I "$src/libusb/os" \
      -c "$src/libusb/$file.c" -o "$build/obj/$(basename "$file").o"
  done
  libtool -static -o "$build/lib/libusb-1.0.a" "$build"/obj/*.o
fi

# 2. libjpeg-turbo with its SIMD code: NEON on arm64 (built in), SSE2/AVX2 on x86_64, which is
# written in assembly for nasm. Without SIMD the encoder is several times slower, so REQUIRE_SIMD=ON
# makes a missing or broken nasm fail the build instead of quietly producing plain C.
# libjpeg-turbo builds only with cmake, and its SIMD code builds for one architecture at a time, so
# each architecture gets its own cmake build and the results are combined with lipo.
# nasm and cmake are build tools only (nothing of them ships); they're pinned too, so the build
# doesn't depend on whatever Homebrew has. nasm publishes no checksums, so its hash is the one of the
# tarball as first downloaded; cmake's is from Kitware's SHA-256.txt; libjpeg-turbo's matches GitHub's.
turbo_version=3.1.2
turbo="$build/libjpeg-turbo-$turbo_version"
if [[ ! -f "$build/lib/libjpeg.a" ]]; then
  echo "Building libjpeg-turbo $turbo_version (and the nasm and cmake it needs)..."
  fetch "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/$turbo_version/libjpeg-turbo-$turbo_version.tar.gz" \
    8f0012234b464ce50890c490f18194f913a7b1f4e6a03d6644179fa0f867d0cf libjpeg-turbo.tar.gz
  fetch "https://www.nasm.us/pub/nasm/releasebuilds/2.16.03/nasm-2.16.03.tar.xz" \
    1412a1c760bbd05db026b6c0d1657affd6631cd0a63cddb6f73cc6d4aa616148 nasm.tar.xz
  fetch "https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6-macos-universal.tar.gz" \
    330b9514f5112e5ed4fb08b8b05803b776fd9b539a6ae12927d14dcc0ee2ba8d cmake.tar.gz
  tools="$build/tools"
  rm -rf "$turbo" "$tools" "$build/nasm-src" "$build/cmake" && mkdir -p "$turbo" "$tools" "$build/nasm-src" "$build/cmake"
  tar xzf "$build/dl/libjpeg-turbo.tar.gz" -C "$turbo" --strip-components 1
  tar xJf "$build/dl/nasm.tar.xz" -C "$build/nasm-src" --strip-components 1
  tar xzf "$build/dl/cmake.tar.gz" -C "$build/cmake" --strip-components 1
  (cd "$build/nasm-src" && ./configure --prefix="$tools" >/dev/null 2>&1 && make -j8 >/dev/null 2>&1 && make install >/dev/null)
  cmake="$build/cmake/CMake.app/Contents/bin/cmake"
  for arch in x86_64 arm64; do
    "$cmake" -S "$turbo" -B "$build/turbo-$arch" -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES="$arch" \
      -DCMAKE_SYSTEM_PROCESSOR="$arch" -DCMAKE_OSX_DEPLOYMENT_TARGET=12.3 -DCMAKE_ASM_NASM_COMPILER="$tools/bin/nasm" \
      -DENABLE_SHARED=OFF -DWITH_TURBOJPEG=OFF -DWITH_TOOLS=OFF -DWITH_TESTS=OFF -DREQUIRE_SIMD=ON \
      -DCMAKE_INSTALL_PREFIX="$build/turbo-$arch/install" >/dev/null
    "$cmake" --build "$build/turbo-$arch" -j8 >/dev/null
    "$cmake" --install "$build/turbo-$arch" >/dev/null
  done
  # The generated headers must agree, since one set serves both architectures.
  diff -r "$build/turbo-x86_64/install/include" "$build/turbo-arm64/install/include" >/dev/null \
    || { echo "libjpeg-turbo's headers differ between x86_64 and arm64"; exit 1; }
  rm -rf "$build/include/jpeg" && mkdir -p "$build/include"
  cp -R "$build/turbo-x86_64/install/include" "$build/include/jpeg"
  lipo -create -output "$build/lib/libjpeg.a" "$build/turbo-x86_64/install/lib/libjpeg.a" "$build/turbo-arm64/install/lib/libjpeg.a"
fi

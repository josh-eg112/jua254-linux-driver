#!/usr/bin/env bash
# Build the self-contained JUA254 Display.app (one binary for Intel and Apple Silicon, no Python),
# sign it, zip it for sharing, and install it to ~/Applications (--no-install to skip).
set -euo pipefail
cd "$(dirname "$0")"
[[ "$(uname)" == Darwin ]] || { echo "This script is for macOS."; exit 1; }
build="$(pwd)/build"
mkdir -p "$build"

# 1. libusb from pinned source, as a universal static library.
libusb_version=1.0.30
libusb_sha256=fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf
src="$build/libusb-$libusb_version"
if [[ ! -f "$build/lib/libusb-1.0.a" ]]; then
  echo "Building libusb $libusb_version..."
  curl -fsSL -o "$build/libusb.tar.bz2" \
    "https://github.com/libusb/libusb/releases/download/v$libusb_version/libusb-$libusb_version.tar.bz2"
  echo "$libusb_sha256  $build/libusb.tar.bz2" | shasum -a 256 -c - >/dev/null
  rm -rf "$src" && tar xjf "$build/libusb.tar.bz2" -C "$build"
  rm -rf "$build/obj" && mkdir -p "$build/obj" "$build/lib"
  for file in core descriptor hotplug io strerror sync os/darwin_usb os/events_posix os/threads_posix; do
    clang -arch x86_64 -arch arm64 -mmacosx-version-min=12.3 -O2 -w \
      -I "$src/Xcode" -I "$src/libusb" -I "$src/libusb/os" \
      -c "$src/libusb/$file.c" -o "$build/obj/$(basename "$file").o"
  done
  libtool -static -o "$build/lib/libusb-1.0.a" "$build"/obj/*.o
fi

# 2. The app binary, once per architecture, then combined.
echo "Compiling JUA254 Display..."
sources=(Protocol.swift JPEG.swift USB.swift Capture.swift Engine.swift main.swift)
for arch in x86_64 arm64; do
  xcrun swiftc -O -swift-version 5 -target "$arch-apple-macos12.3" \
    -import-objc-header ../CGVirtualDisplay.h -I CLibUSB -Xcc -I"$src/libusb" -L "$build/lib" \
    -framework IOKit -framework CoreFoundation -framework Security \
    "${sources[@]}" -o "$build/JUA254Display-$arch"
done
lipo -create -output "$build/JUA254Display" "$build/JUA254Display-x86_64" "$build/JUA254Display-arm64"

# 3. The bundle, with licences.
app="$build/JUA254 Display.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$build/JUA254Display" "$app/Contents/MacOS/JUA254Display"
cp Info.plist "$app/Contents/Info.plist"
cp "$src/COPYING" "$app/Contents/Resources/libusb-COPYING.txt"
cp icon/AppIcon.icns "$app/Contents/Resources/AppIcon.icns"

# 4. Sign (stable local identity; see ../sign_app.sh) and zip for sharing.
bash ../sign_app.sh "$app"
rm -f "$build/JUA254-Display.zip"
ditto -c -k --keepParent "$app" "$build/JUA254-Display.zip"
echo "Built $app ($(lipo -archs "$app/Contents/MacOS/JUA254Display"))"
echo "Shareable zip: $build/JUA254-Display.zip"

# 5. Install, replacing the Python-based app of the same name.
if [[ "${1:-}" != --no-install ]]; then
  # Ask the running app to quit (SIGTERM): it disables the adapter's output and releases it first.
  # Killing it mid-transfer can hang the adapter until it is replugged.
  pkill -TERM -x JUA254Display 2>/dev/null || true
  for _ in $(seq 1 16); do pgrep -x JUA254Display >/dev/null || break; sleep 0.5; done
  pkill -KILL -x JUA254Display 2>/dev/null || true
  rm -rf "$HOME/Applications/JUA254 Display.app"
  mkdir -p "$HOME/Applications"
  ditto "$app" "$HOME/Applications/JUA254 Display.app"
  touch "$HOME/Applications/JUA254 Display.app"  # nudge Finder to pick up a changed icon
  echo "Installed to ~/Applications/JUA254 Display.app"
  open "$HOME/Applications/JUA254 Display.app"
fi

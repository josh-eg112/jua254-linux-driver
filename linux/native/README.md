# Native capture helper

`jua254-extcopy-capture` is a persistent Hyprland/Wayland capture source using
the staging `ext-image-copy-capture-v1` protocol. It reuses one compositor
buffer and emits raw BGRA frames to stdout. Timing diagnostics are emitted as
JSON lines on stderr.

Build with `make -C linux/native`. The Python streamer builds it automatically
when the source is newer than the binary.

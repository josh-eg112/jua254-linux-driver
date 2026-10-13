#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
root="${JUA254_INSTALL_ROOT:-$HOME/.local/share/jua254}"
template=linux-rgb-bars-20260825
if [[ -z "$root" || "$root" == / ]]; then
  echo "Refusing unsafe JUA254 install root: $root" >&2
  exit 2
fi
parent=$(dirname "$root")
mkdir -p "$parent" "$HOME/.local/bin" "$HOME/.config/jua254" "$HOME/.local/state/jua254" "$HOME/.config/systemd/user"

# Build a complete package tree before touching the running installation.
stage=$(mktemp -d "$parent/.jua254-install.XXXXXX")
backup=""
published=false
cleanup_stage() {
  if [[ "$published" != true && -n "$backup" && -e "$backup" && ! -e "$root" ]]; then
    mv "$backup" "$root"
  fi
  [[ ! -e "$stage" ]] || rm -rf "$stage"
}
trap cleanup_stage EXIT
# Native helpers are build products (not tracked); make them current before staging.
make -C "$repo/linux/native"
cp -a "$repo/linux" "$stage/"
mkdir -p "$stage/shared/handoff/$template"
cp -a "$repo/shared/handoff/$template/." "$stage/shared/handoff/$template/"

# Executable production entry points are explicit; manifests and payloads
# retain their non-executable source modes.
chmod 0755 "$stage/linux/jua254_monitor.py" "$stage/linux/jua254_stream.py" \
  "$stage/linux/jua254_service.py" "$stage/linux/jua254_ctl.py" \
  "$stage/linux/native/jua254-extcopy-capture"

# Quiesce the supervisor before replacing its Python files or executing native
# helper. Publishing is then an atomic directory rename on the same filesystem.
systemctl --user stop jua254-monitor.service 2>/dev/null || true
if [[ -e "$root" ]]; then
  backup="$parent/.jua254-previous.$$"
  mv "$root" "$backup"
fi
if ! mv "$stage" "$root"; then
  [[ -z "$backup" || ! -e "$backup" ]] || mv "$backup" "$root"
  exit 1
fi
published=true
[[ -z "$backup" || ! -e "$backup" ]] || rm -rf "$backup"
trap - EXIT

install -Dm644 "$repo/packaging/config.toml" "$HOME/.config/jua254/config.toml"
install -Dm644 "$repo/packaging/jua254-stream.service" "$HOME/.config/systemd/user/jua254-stream.service"
install -Dm644 "$repo/packaging/jua254-monitor.service" "$HOME/.config/systemd/user/jua254-monitor.service"
install -Dm755 "$repo/packaging/jua254-stream" "$HOME/.local/bin/jua254-stream"
install -Dm755 "$repo/packaging/jua254-monitor-test" "$HOME/.local/bin/jua254-monitor-test"
install -Dm755 "$repo/packaging/jua254-monitor-status" "$HOME/.local/bin/jua254-monitor-status"
install -Dm755 "$repo/packaging/jua254-monitor-start" "$HOME/.local/bin/jua254-monitor-start"
install -Dm755 "$repo/packaging/jua254-monitor-stop" "$HOME/.local/bin/jua254-monitor-stop"
install -Dm755 "$repo/packaging/jua254-monitor-diagnostic" "$HOME/.local/bin/jua254-monitor-diagnostic"
systemctl --user daemon-reload
systemctl --user disable --now jua254-stream.service 2>/dev/null || true
systemctl --user reset-failed jua254-monitor.service 2>/dev/null || true
systemctl --user enable --now jua254-monitor.service
if [[ ${1:-} == --udev ]]; then
  sudo install -Dm644 "$repo/packaging/99-jua254.rules" /etc/udev/rules.d/99-jua254.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=0711 --attr-match=idProduct=5600
fi
echo "Installed and enabled JUA254 external-monitor service. Use jua254-monitor-status for diagnostics."

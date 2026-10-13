#!/usr/bin/env bash
# Remove the per-user JUA254 install made by install.sh. --udev also removes the udev rule.
set -euo pipefail
root="${JUA254_INSTALL_ROOT:-$HOME/.local/share/jua254}"
if [[ -z "$root" || "$root" == / ]]; then
  echo "Refusing unsafe JUA254 install root: $root" >&2
  exit 2
fi

systemctl --user disable --now jua254-monitor.service jua254-stream.service 2>/dev/null || true
# Remove the Hyprland output the service created, if any.
[[ -f "$root/linux/jua254_monitor.py" ]] && python3 "$root/linux/jua254_monitor.py" cleanup 2>/dev/null || true

rm -f "$HOME/.config/systemd/user/jua254-monitor.service" "$HOME/.config/systemd/user/jua254-stream.service"
for tool in jua254-stream jua254-monitor-test jua254-monitor-status jua254-monitor-start \
            jua254-monitor-stop jua254-monitor-diagnostic; do
  rm -f "$HOME/.local/bin/$tool"
done
rm -rf "$root"
systemctl --user daemon-reload
systemctl --user reset-failed jua254-monitor.service jua254-stream.service 2>/dev/null || true

if [[ ${1:-} == --udev ]]; then
  sudo rm -f /etc/udev/rules.d/99-jua254.rules
  sudo udevadm control --reload-rules
fi
echo "Removed JUA254. Your settings in ~/.config/jua254 were kept; delete that folder to remove them too."

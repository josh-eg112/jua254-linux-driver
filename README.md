# jua254-linux-driver

An unofficial userspace Linux driver for the **j5create JUA254** USB-to-HDMI display adapter
(MCT Trigger VI, USB ID `0711:5600`). It turns the adapter into an independent extended monitor
on Hyprland.

> **Alpha.** This is a reverse-engineered driver that has been used daily on one machine. Expect
> rough edges. Not affiliated with, endorsed by, or supported by j5create or MCT.

## What works

- An extended Hyprland monitor at 1920x1080@60, about 58-60 FPS, with the adapter's hardware cursor
- Other adapter modes, including 1280x720@60, 1600x900@60 and 1920x1080@50 (`JUA254_MODE`)
- Unplug/replug and suspend/resume recover on their own
- A desktop notification when the adapter's firmware hangs (see [Troubleshooting](#troubleshooting))

## Requirements

- **Hyprland** (the driver creates a Hyprland headless output and captures it). Other desktops are
  not supported yet.
- **Intel graphics with VA-API JPEG encoding** (`intel-media-driver`) for the 60 FPS GPU path.
  Other GPUs can use the slower `--no-gpu` path.
- Python 3.11 or newer, systemd user services, udev.

Arch Linux packages:

```bash
sudo pacman -S --needed python python-pyusb libusb wayland wayland-protocols libva libdrm mesa \
  intel-media-driver ffmpeg libnotify base-devel
```

## Install

```bash
git clone https://github.com/josh-eg112/jua254-linux-driver.git
cd jua254-linux-driver
./linux/install.sh --udev
```

`--udev` installs a rule (`/etc/udev/rules.d/99-jua254.rules`) so the adapter can be used without
root; it asks for your password once. **Unplug and replug the adapter once** after the first install.

The driver installs into `~/.local/share/jua254` and runs as the `jua254-monitor` systemd user
service, which starts with your graphical session. A new monitor (`HEADLESS-N`) appears in Hyprland
whenever the adapter is connected; arrange it like any other monitor.

## Use

| Command | What it does |
| --- | --- |
| `jua254-monitor-status` | Service, adapter and output status |
| `jua254-monitor-stop` | Stop the service and remove the extra monitor |
| `jua254-monitor-start` | Start it again |
| `jua254-monitor-diagnostic` | Write a diagnostic report for bug reports |
| `journalctl --user -u jua254-monitor` | Service log |

To use another output mode, for example 720p:

```bash
mkdir -p ~/.config/systemd/user/jua254-monitor.service.d
printf '[Service]\nEnvironment=JUA254_MODE=1280x720@60\n' > ~/.config/systemd/user/jua254-monitor.service.d/mode.conf
systemctl --user daemon-reload && systemctl --user restart jua254-monitor
```

## Uninstall

```bash
./linux/uninstall.sh          # add --udev to also remove the udev rule
```

## Troubleshooting

- **"JUA254 adapter is not responding"**: the adapter's firmware has hung. Unplug its USB cable and
  plug it back in; the display comes back on its own. Software cannot reset it.
- **No picture, log shows `hdmi=00`**: the adapter sees no HDMI monitor. Check the HDMI cable and
  that the monitor is on.
- **Low frame rate**: check that `vainfo` lists `VAProfileJPEGBaseline` with `VAEntrypointEncPicture`.
  Without it, use the `--no-gpu` path.

When reporting a bug, attach the output of `jua254-monitor-diagnostic` and the journal. Never
attach anything containing passwords or personal files.

## Development

```bash
cd linux && python -m pytest -q
```

Three tests compare against private USB captures from the research project and are skipped here.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

j5create is a trademark of its owner. This project is independent and uses the name only to
identify the hardware it supports.

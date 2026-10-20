# jua254-linux-driver

An unofficial userspace driver for the **j5create JUA254** USB-to-HDMI display adapter
(MCT Trigger VI, USB ID `0711:5600`). It turns the adapter into an independent extended monitor
on **Linux** (Hyprland) and **macOS** (the JUA254 Display menu-bar app, see [macOS](#macos)).

> **Alpha.** This is a reverse-engineered driver that has been used daily on one machine. Expect
> rough edges. Not affiliated with, endorsed by, or supported by j5create or MCT.

## What works

- An extended Hyprland monitor at 1920x1080@60, about 58-60 FPS, with the adapter's hardware cursor
- Other adapter modes, including 1680x1050@60, 1280x720@60, 1600x900@60 and 1920x1080@50, chosen
  per adapter (see [Resolution and rotation](#resolution-and-rotation))
- Several adapters at once, each its own monitor; the j5create A8640H (`0711:5601`) works too
- Detailed screens (dense text) stay within the adapter's limit by lowering JPEG quality briefly
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

### Arch Linux package (recommended)

```bash
git clone https://github.com/josh-eg112/jua254-linux-driver.git
cd jua254-linux-driver/packaging/aur
makepkg -si
```

Unplug and replug the adapter once, then start the driver as your normal user inside Hyprland:

```bash
systemctl --user enable --now jua254-monitor.service
```

The package installs into `/usr/lib/jua254`. Remove it with `jua254-monitor-stop` and then
`sudo pacman -R jua254-linux-driver`.

### Per-user install (any distribution with the requirements above)

```bash
git clone https://github.com/josh-eg112/jua254-linux-driver.git
cd jua254-linux-driver
./linux/install.sh --udev
```

Use one install method, not both: the per-user service takes precedence over the package.

`--udev` installs a rule (`/etc/udev/rules.d/99-jua254.rules`) so the adapter can be used without
root; it asks for your password once. **Unplug and replug the adapter once** after the first install.

The driver installs into `~/.local/share/jua254` and runs as the `jua254-monitor` systemd user
service, which starts with your graphical session. A new monitor (`HEADLESS-N`) appears in Hyprland
whenever the adapter is connected; arrange it like any other monitor.

## Use

| Command | What it does |
| --- | --- |
| `jua254-monitor-status` | Service, adapter and output status |
| `jua254-monitor-adapter` | Each adapter's resolution and rotation (see below) |
| `jua254-monitor-stop` | Stop the service and remove the extra monitor |
| `jua254-monitor-start` | Start it again |
| `jua254-monitor-diagnostic` | Write a diagnostic report for bug reports |
| `journalctl --user -u jua254-monitor` | Service log |

### Resolution and rotation

Each adapter is identified by the USB port it is plugged into. List them, then set a resolution its
monitor supports (for one that says "input out of range") or rotate it for a monitor turned on its
side; the running service applies the change within a second:

```bash
jua254-monitor-adapter                                   # e.g. "3-1: 1920x1080@60"
jua254-monitor-adapter 3-1 --mode 1680x1050@60
jua254-monitor-adapter 3-1 --rotate left                 # upside down? use --rotate right
jua254-monitor-adapter 3-1 --reset                       # back to 1920x1080@60, not rotated
```

Settings are kept in `~/.config/jua254/adapters.json`. Keep each adapter in the same USB port.
`JUA254_MODE` (in the service environment) still sets the default for adapters without a setting.

## Uninstall (per-user install)

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

## macOS

**JUA254 Display** is a menu-bar app that makes the adapter a second display (extended desktop),
or mirrors your main display. It starts when the adapter is plugged in and stops when it is
unplugged; no drivers or extra software to install.

### Install

1. Download `JUA254-Display.zip` from the [latest release](https://github.com/josh-eg112/jua254-linux-driver/releases/latest)
   and unzip it. Move **JUA254 Display** to your Applications folder.
2. Open it. macOS will say it can't verify the app (it is signed, but not by an Apple-registered
   developer). Open **System Settings > Privacy & Security**, scroll down, and click **Open Anyway**.
3. Plug in the adapter (with an HDMI monitor attached). macOS asks to let JUA254 Display record
   the screen: allow it, then quit the app from its menu-bar icon and open it again.
4. The adapter's monitor appears as a display named **JUA254**. Arrange it in System Settings >
   Displays like any other monitor.

The menu-bar icon shows the status and frame rate, and has Start/Stop, Extended Desktop or Mirror,
Start When Adapter Is Plugged In, Open at Login, and the log (`~/Library/Logs/JUA254/display.log`).

- **Several adapters** each get their own display ("JUA254", "JUA254 2", ...). Its name, arrangement
  and resolution follow the monitor (read from its EDID), so moving a monitor to another adapter
  keeps them; a monitor without a readable EDID is remembered by the adapter's USB port.
- **Resolution**: if a monitor says "input out of range", choose a mode it supports for it
  (e.g. 1680 × 1050) in the menu's Resolution submenu.
- **Portrait**: for a monitor turned on its side, choose its portrait size (e.g. 1080 × 1920) in
  System Settings > Displays; if the picture is upside down, switch Portrait Rotation in the menu.
To uninstall, quit the app and move it to the Bin.

### Notes

- Requires macOS 12.3 or newer. One universal app for Intel and Apple Silicon.
- **Tested on an Intel Mac (Hackintosh) with macOS 26.7.1 only.** Apple Silicon and other macOS
  versions are untested; reports are welcome.
- About 50-60 frames/s while the screen changes on the tested machine.
- The extended desktop uses a private macOS API (`CGVirtualDisplay`, as BetterDisplay and DeskPad
  do), so a future macOS update could break it; **Mirror Main Display** uses only public APIs.
- Only one program can use the adapter at a time. Quit the app before running anything else that
  talks to it. If the menu says the adapter is not responding, unplug and replug it.

### Build from source

Needs the Xcode command-line tools (`xcode-select --install`):

```bash
bash macos/native/test.sh    # protocol tests against golden vectors from the Python driver
bash macos/native/build.sh   # builds, signs locally, installs to ~/Applications and opens the app
```

`build.sh` downloads libusb 1.0.30 and libjpeg-turbo 3.1.2, plus nasm 2.16.03 and cmake 3.31.6 to
build libjpeg-turbo's SIMD code (all checked against pinned SHA-256s), and builds both libraries
into the app (`deps.sh`; the first build takes about a minute).
Releases are built by `.github/workflows/macos.yml` on a GitHub macOS runner.

## Development

```bash
cd linux && python -m pytest -q
```

Three tests compare against private USB captures from the research project and are skipped here.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). The macOS app includes libusb (LGPL-2.1-or-later) and
libjpeg-turbo (BSD-3-Clause, IJG and zlib licences); their licences are bundled in the app.
This software is based in part on the work of the Independent JPEG Group.

j5create is a trademark of its owner. This project is independent and uses the name only to
identify the hardware it supports.

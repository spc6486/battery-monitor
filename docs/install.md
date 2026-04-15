# Installation Guide

## Prerequisites

- Raspberry Pi (Pi 3, Pi 4, Pi 5 — see README for compatibility)
- SunFounder PiPower 5 (I2C) or MakerFocus UPSPack V3/V3P (UART)
- Internet connection (for apt packages)

## Quick Install

```bash
tar xzf battery-monitor.tar.gz
cd battery-monitor
./install.sh
```

## What the installer does

### 1. Install system packages

```
python3-gi python3-serial python3-yaml
gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
libayatana-appindicator3-1 libnotify-bin
```

No Python virtual environment needed — all dependencies come from apt.

### 2. Configure UART (auto-detects Pi model)

The installer reads `/proc/device-tree/model` and applies the appropriate
UART configuration:

- **Pi 5:** Adds `dtparam=uart0=on` to redirect UART0 to GPIO14/15
- **Pi 4 / Pi 400:** No overlay needed — PL011 already on GPIO14/15
- **Pi 3 / Zero 2 W:** Adds `dtoverlay=disable-bt` to free PL011 from Bluetooth

Common to all models:
- Removes `console=serial0,...` from cmdline.txt
- Adds user to `dialout` group
- Runs `raspi-config nonint do_serial 2` (disable console, keep hardware)

These changes require a reboot to take effect.

### 3. Remove old installation artifacts

If you previously had the ChatGPT-based `~/ups-v3p` setup, the installer
will offer to remove it along with its systemd service and autostart entries.

### 4. Install application files

- `/opt/battery-monitor/` — application directory
- `/usr/local/bin/battery-monitor` — CLI/GUI launcher script

### 5. Create menu and autostart entries

- `/usr/share/applications/battery-monitor.desktop` — desktop menu entry under Preferences
- `/etc/xdg/autostart/battery-monitor.desktop` — starts the tray app automatically on login (with a 3-second delay to wait for the panel)

### 6. Create default configuration

- `/etc/battery-monitor/battery.conf` — YAML config for serial port, thresholds, and optional MQTT

### 7. Configure passwordless shutdown

- `/etc/sudoers.d/battery-monitor` — allows the monitor to call `shutdown` without a password prompt

## File locations

### Application

| Path | Contents |
|------|----------|
| `/opt/battery-monitor/` | Application files |
| `/usr/local/bin/battery-monitor` | Launcher script |
| `/usr/share/applications/battery-monitor.desktop` | Menu entry |
| `/etc/xdg/autostart/battery-monitor.desktop` | Autostart entry |
| `/etc/sudoers.d/battery-monitor` | Sudo rules |
| `/etc/battery-monitor/battery.conf` | Configuration |

### UART configuration (written by installer)

| Path | Contents | Needs reboot |
|------|----------|:------------:|
| `/boot/firmware/config.txt` | `dtparam=uart0=on` | Yes |
| `/boot/firmware/cmdline.txt` | Serial console removal | Yes |

### Settings written by the app

| Path | Contents | Needs reboot |
|------|----------|:------------:|
| `/etc/battery-monitor/battery.conf` | Thresholds, power saver options | No |

## Uninstall

Three ways to uninstall:

**From the application menu:** Right-click "Battery Monitor" → **Uninstall Battery Monitor**.

**From the terminal:**

```bash
sudo /opt/battery-monitor/install.sh --uninstall
```

This removes the application, launcher, menu entry, autostart, and sudoers rule. It preserves:

- UART configuration in `/boot/firmware/config.txt`
- Configuration file (you'll be asked)

## Updating

```bash
pkill -f battery-monitor
cd ~/battery-monitor
git pull   # or extract new tarball
./install.sh
battery-monitor
```

The installer overwrites application files but preserves configuration.

## Troubleshooting

### No tray icon

The system tray widget must be enabled in wf-panel-pi (it is by default). The app uses AppIndicator/StatusNotifierItem — it's not a panel plugin.

### Tray shows "not connected"

Check the serial port:

```bash
ls -l /dev/ttyAMA0
battery-monitor --cli
```

If the device doesn't exist, verify UART is enabled:

```bash
grep 'dtparam=uart0=on' /boot/firmware/config.txt
grep 'console=serial0' /boot/firmware/cmdline.txt   # should return nothing
```

### Permission denied on serial port

```bash
groups    # should include 'dialout'
sudo usermod -a -G dialout $USER
sudo reboot
```

### Something else using the port

```bash
sudo fuser -v /dev/ttyAMA0
```

Kill whatever process is holding it, then restart the monitor.

# Battery Monitor

System tray indicator for the MakerFocus UPSPack V3/V3P on Raspberry Pi.

## Features

- **Tray icon** with standard battery icons (charging/discharging/full/low)
- **Auto-shutdown** with configurable threshold and hysteresis
- **Low battery notification** at configurable warning level
- **Settings dialog** for thresholds, UPS info, and voltage display
- **CLI mode** for headless/SSH monitoring
- **Power saver** — independently toggle CPU governor, frequency cap, and Bluetooth on battery
- **Dynamic CPU capping** — set separate max CPU frequencies for AC and battery
- **Optional MQTT** publishing (disabled by default)
- **Single process** — no MQTT broker required for basic operation
- **Pi 5 UART** setup handled automatically by installer

## Compatibility

### UPS

This monitor supports the **MakerFocus UPSPack V3 and V3P** only. These use
a text-based UART protocol at 9600 8N1:

```
$ SmartUPS V3.2P,Vin GOOD,BATCAP 48,Vout 5250 $
```

Other UPS boards (Geekworm, Waveshare, PiSugar, CyberPower, APC) use
different protocols or I2C and are not supported.

### Raspberry Pi

| Model | Status | UART Setup |
|-------|--------|------------|
| Pi 5 | Tested | `dtparam=uart0=on` (auto) |
| Pi 4 / Pi 400 | Supported | PL011 already on GPIO14/15 |
| Pi 3 / Zero 2 W | Supported | `dtoverlay=disable-bt` (auto, disables Bluetooth) |
| Pi Zero / Pi 2 / older | Untested | May need manual UART configuration |

The installer auto-detects the Pi model and applies the correct UART
configuration. On Pi 3 and Zero 2 W, the installer disables the Bluetooth
overlay to free the PL011 UART for the UPS — Bluetooth will not be available.

### Operating System

Requires Raspberry Pi OS Bookworm (or later) with a GUI desktop.

## Hardware

- MakerFocus RPi V3P UPS on UART (GPIO14/15, /dev/ttyAMA0)
- Protocol: `$ SmartUPS V3.2P,Vin GOOD,BATCAP 48,Vout 5250 $` at 9600 8N1
- Wiring: UPS TX → Pi RXD0 (pin 10), UPS RX → Pi TXD0 (pin 8), GND → GND

## Install

```bash
tar xzf battery-monitor.tar.gz
cd battery-monitor
./install.sh
```

The installer will:
1. Install system packages (python3-gi, python3-serial, etc.)
2. Configure UART for your Pi model (auto-detected)
3. Disable serial console on UART0
4. Install to `/opt/battery-monitor/`
5. Create CLI launcher (`battery-monitor`)
6. Set up autostart on login
7. Configure passwordless shutdown
8. Prompt to reboot if UART changes were made

## Usage

### Tray indicator (default)

```bash
battery-monitor
```

Starts automatically on login. The tray icon shows:
- Battery level with standard system icons
- Charging bolt overlay when on AC power
- Menu with status and Settings access

### CLI mode

```bash
battery-monitor --cli
```

Prints live UPS data to terminal — useful over SSH.

### Settings

Click the tray icon → **Battery Settings…** to open the settings window.

**Settings tab:**
- UPS model and output voltage
- Shutdown threshold and confirm delay
- Low battery warning level

**Power Saver tab:**
- CPU governor switching on battery
- CPU max frequency for AC and battery (dynamic, no reboot)
- Bluetooth disable on battery
- Current governor and frequency status

## Uninstall

From the tray menu: **Uninstall Battery Monitor** (confirms before removing).

From the application menu: right-click Battery Monitor → **Uninstall**.

Or from terminal:

```bash
sudo /opt/battery-monitor/install.sh --uninstall
```

This removes the application, launcher, autostart, and sudoers rule.
Configuration and UART settings are preserved unless you choose to remove them.

## Configuration

Config file: `/etc/battery-monitor/battery.conf`

```yaml
serial:
  port: /dev/ttyAMA0
  baud: 9600
  timeout_s: 2

shutdown:
  enable: true
  low_percent: 10        # Shutdown below this % (on battery only)
  confirm_seconds: 30    # Must stay below for this long
  clear_percent: 25      # Reset trip once battery rises above this

notifications:
  enable: true
  warn_percent: 20       # Desktop notification at this %

power_saver:
  cpu_governor: false    # Switch governor on battery
  governor_ac: ondemand
  governor_battery: powersave
  max_freq_ac: 0         # CPU max kHz on AC (0 = hardware max)
  max_freq_battery: 0    # CPU max kHz on battery (0 = hardware max)
  disable_bluetooth: false  # rfkill bluetooth on battery

mqtt:
  enable: false          # Set true to publish to MQTT
  host: 127.0.0.1
  port: 1883
  topic: raspberrypi/ups/status
  client_id: rp5-ups
```

Changes made via the Settings dialog are saved automatically.
Manual edits take effect after restarting the tray.


### Changing the serial port

The default port `/dev/ttyAMA0` is correct for a Pi 5 with the UPS wired to
GPIO14/15. If using a USB-serial adapter, edit the config file:

```bash
sudo nano /etc/battery-monitor/battery.conf
```

Change `port:` to `/dev/ttyUSB0` (or whichever device), then restart the tray.

## File Locations

| Path | Contents |
|------|----------|
| `/opt/battery-monitor/` | Application files |
| `/usr/local/bin/battery-monitor` | Launcher script |
| `/usr/share/applications/battery-monitor.desktop` | Menu entry |
| `/etc/xdg/autostart/battery-monitor.desktop` | Login autostart |
| `/etc/sudoers.d/battery-monitor` | Passwordless shutdown |
| `/etc/battery-monitor/battery.conf` | Configuration |
| `$XDG_RUNTIME_DIR/battery-monitor-status.json` | Live UPS status (for external tools) |

## Advanced: Hardware Power Tuning

The power saver settings in this app control dynamic, runtime behavior — the
CPU governor and frequency cap change instantly when you unplug/plug in the
charger, no reboot needed.

For permanent hardware-level power tuning, edit `/boot/firmware/config.txt`
directly. These settings require a reboot and apply regardless of AC/battery
state:

```
arm_freq=1200        # Absolute CPU ceiling (MHz)
arm_freq_min=600     # Absolute CPU floor (MHz)
gpu_freq=300         # GPU max frequency (MHz)
gpu_freq_min=250     # GPU min frequency (safe floor)
#over_voltage=-2     # Undervolt by 50mV (saves ~100-200mW, risk of instability)
dtoverlay=disable-bt # Kernel-level Bluetooth disable (saves ~10-20mW)
```

Safe ranges for Raspberry Pi 5: CPU 600–2400 MHz, GPU 250–500 MHz,
over_voltage -2 to +2 (each step is 25mV, negative values below -2 risk
lockups under load).

These hardware caps define the envelope within which the dynamic power saver
operates. For example, if `arm_freq=1200` is set in config.txt, the "CPU max
on AC" dropdown in Settings will cap at 1200 MHz instead of 2400 MHz.

## License

MIT — see LICENSE file.

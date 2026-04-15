# Battery Monitor

System tray indicator for UPS battery monitoring on Raspberry Pi.
Supports SunFounder PiPower 5 (I2C) and MakerFocus V3/V3P (UART) with
automatic hardware detection.

## Features

- **Dual UPS support** — auto-detects PiPower 5 or V3/V3P at startup
- **Tray icon** with standard battery icons (charging/discharging/full/low)
- **Auto-shutdown** with configurable threshold and hysteresis
- **Low battery notification** at configurable warning level
- **Settings dialog** for thresholds, UPS info, and voltage display
- **CLI mode** for headless/SSH monitoring
- **Power saver** — independently toggle CPU governor, frequency cap, Bluetooth, Wi-Fi, and refresh rate on battery
- **Dynamic CPU capping** — set separate max CPU frequencies for AC and battery
- **Refresh rate switching** — drops HDMI from 60Hz to 30Hz for power/thermal savings
- **Live CPU monitoring** — real-time frequency display in the settings window
- **Predictive runtime** — estimated remaining runtime (PiPower 5 only)
- **Optional MQTT** publishing (disabled by default)
- **Single process** — no MQTT broker required for basic operation

## Compatibility

### Supported UPS Hardware

| UPS | Connection | Data Available |
|-----|-----------|----------------|
| SunFounder PiPower 5 | I2C (GPIO2/3, addr 0x5C) | Voltage, current, power (in/out/bat), SOC%, runtime est. |
| MakerFocus V3/V3P | UART (GPIO14/15, 9600 8N1) | SOC%, AC status, output voltage |

The app auto-detects which hardware is present — no manual configuration needed.

### Raspberry Pi

| Model | Status | Notes |
|-------|--------|-------|
| Pi 5 | Tested | UART auto-configured for V3P |
| Pi 4 / Pi 400 | Supported | PL011 on GPIO14/15 for V3P |
| Pi 3 / Zero 2 W | Supported | Disables Bluetooth overlay for V3P UART |
| Pi Zero / Pi 2 / older | Untested | May need manual UART configuration |

### Operating System

Requires Raspberry Pi OS Bookworm (or later) with a GUI desktop.

## Install

```bash
tar xzf battery-monitor.tar.gz
cd battery-monitor
./install.sh
```

The installer will:
1. Install system packages (python3-gi, python3-serial, smbus2, etc.)
2. Configure UART for your Pi model (auto-detected, for V3P support)
3. Optionally enable extended CPU frequency range (arm_freq_min=600)
4. Install to `/opt/battery-monitor/`
5. Create CLI launcher (`battery-monitor`)
6. Set up autostart on login
7. Configure passwordless shutdown
8. Prompt to reboot if config.txt changes were made

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
- UPS type, model, and output voltage
- Input/output voltage, current, and power (PiPower 5)
- Estimated runtime (PiPower 5)
- Shutdown threshold and confirm delay
- Low battery warning level

**Power Saver tab:**
- CPU governor switching on battery
- CPU max frequency for AC and battery (dynamic, no reboot)
- Bluetooth disable on battery
- Wi-Fi disable on battery
- Refresh rate switching on battery (60→30 Hz)
- Live CPU frequency and governor display
- Current refresh rate

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
  disable_wifi: false       # rfkill wifi on battery
  reduce_refresh_rate: false # HDMI 60→30 Hz

pipower5:
  battery_capacity_wh: 59.2  # For runtime estimation

mqtt:
  enable: false          # Set true to publish to MQTT
  host: 127.0.0.1
  port: 1883
  topic: raspberrypi/ups/status
  client_id: rp5-ups
```

Changes made via the Settings dialog are saved automatically.
Manual edits take effect after restarting the tray.

### Changing the serial port (V3P only)

The default port `/dev/ttyAMA0` is correct for a Pi 5 with the V3P wired to
GPIO14/15. If using a USB-serial adapter, edit the config file:

```bash
sudo nano /etc/battery-monitor/battery.conf
```

Change `port:` to `/dev/ttyUSB0` (or whichever device), then restart the tray.
PiPower 5 users do not need to configure a serial port.

### Battery capacity calibration (PiPower 5)

The runtime estimate depends on the `battery_capacity_wh` setting in the
UPS tab. The default is 59.2 Wh (two 10Ah MakerFocus packs in 2S at 7.4V,
derated to ~80% usable). To calibrate for your actual packs:

1. Fully charge the battery
2. Disconnect external power and use the device normally
3. Note the runtime and average output power shown in the tray or CLI
4. When the device shuts down, calculate: `capacity = runtime_hours × average_watts`
5. Enter the result in Battery Settings → UPS → Capacity

For example, if the device ran for 3.5 hours at an average of 10W:
`3.5 × 10 = 35 Wh`

Battery capacity decreases with age, temperature, and charge cycles.
Re-calibrate periodically for accurate estimates.

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

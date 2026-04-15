# Changelog

## 2.0.0

- **Dual UPS support:** auto-detects SunFounder PiPower 5 (I2C) or MakerFocus V3/V3P (UART)
- Backend abstraction: `UPSBackend` base class with `V3PBackend` and `PiPower5Backend`
- PiPower 5 provides: input/output/battery voltage and current, power draw,
  charge state, and estimated runtime
- Predictive runtime estimation (PiPower 5 only, rolling 30-sample power average)
- PiPower 5 `shutdown_request` register honored immediately
- Settings window adapts to show backend-specific information
- CLI shows UPS type, connection info, and PiPower 5 power/runtime data
- Status file extended with PiPower 5 fields (V3P fields remain for backward compat)
- Installer adds `smbus2` dependency (apt with pip fallback)
- Battery capacity (Wh) configurable for runtime estimation accuracy
- UPS settings tab: battery capacity (PiPower 5) or serial port (V3P)
- PiPower 5 hardware info display: firmware, shutdown %, charge current
- Installer removes wf-panel-pi `batt` widget (shows 0% with PiPower 5)
- Installer suppresses pipower5.service (re-enabled on uninstall)
- Refresh rate change includes safety revert (auto-recovers if display goes dark)

## 1.1.1

- Refresh rate is now persistent — set once at startup and on Apply, no toggling
  on AC↔battery transitions (eliminates screen flash on power state changes)
- Refresh rate display updates live in the settings window
- Display section separated from CPU Frequency in Power Saver tab

## 1.1.0

- Refresh rate switching: drops HDMI from 60Hz to 30Hz (~0.3–0.5W saved)
- Wi-Fi disable on battery via rfkill (~0.1–0.3W saved)
- Installer prompts to enable extended CPU frequency range (arm_freq_min=600)
- Live CPU frequency display in Power Saver tab (updates every second)
- Current refresh rate shown in Display section of Power Saver tab
- CPU frequency and refresh rate included in status file for external tools

## 1.0.3

- CLI mode falls back to status file when serial port is held by tray app
- Added "Reboot instead of shutdown" checkbox for auto-restart on AC return
- CLI shows helpful error when neither serial nor status file is available

## 1.0.2

- Unified Settings and Power Saver into single tabbed window
  (matches kiosk-manager UI pattern)
- Added Uninstall option to tray menu (with confirmation dialog)
- Window uses Gtk.Window with Notebook tabs instead of separate Dialogs
- Header shows version, battery status, and serial port status
- Bottom bar with Close + Apply buttons

## 1.0.1

- Added status file (`$XDG_RUNTIME_DIR/battery-monitor-status.json`) for
  external tool integration
- Fixed `AyatanaAppIndicator3.Indicator.set_icon` deprecation warning
- Removed screen blanking from Power Saver (compositor-dependent, better
  handled by Pi OS's built-in screen blanking settings)
- Split Settings and Power Saver into separate dialogs

## 1.0.0 — Initial Release

- System tray indicator with standard battery icons (charging/discharging/full/low)
- UPS serial reader for MakerFocus UPSPack V3/V3P (9600 8N1 UART)
- Auto-shutdown with configurable threshold and hysteresis
- Low battery desktop notification (configurable warning level)
- Settings dialog: UPS info, shutdown thresholds, notification settings
- Power Saver dialog:
  - CPU governor switching on battery (ondemand ↔ powersave)
  - Dynamic CPU frequency capping (separate AC/battery maximums)
  - Bluetooth disable on battery (via rfkill)
- CLI mode for headless/SSH monitoring (`battery-monitor --cli`)
- Optional MQTT publishing (disabled by default)
- FHS-compliant install: `/opt/battery-monitor/`, `/etc/battery-monitor/`
- Pi model auto-detection for UART configuration (Pi 3/4/5)
- Old `~/ups-v3p` install migration/cleanup built into installer
- Uninstaller with interactive config cleanup

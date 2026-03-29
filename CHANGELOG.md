# Changelog

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

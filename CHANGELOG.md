# Changelog

## 1.0.0 — Initial Release

- System tray indicator with standard battery icons (charging/discharging/full/low)
- UPS serial reader for MakerFocus UPSPack V3P (9600 8N1 UART)
- Auto-shutdown with configurable threshold and hysteresis
- Low battery desktop notification (configurable warning level)
- Settings dialog: UPS info, shutdown thresholds, notification settings
- Power Saver dialog:
  - CPU governor switching on battery (ondemand ↔ powersave)
  - Dynamic CPU frequency capping (separate AC/battery maximums)
  - Bluetooth disable on battery (via rfkill)
  - Screen blanking timeout (writes to Wayfire idle config)
- CLI mode for headless/SSH monitoring (`battery-monitor --cli`)
- Optional MQTT publishing (disabled by default)
- FHS-compliant install: `/opt/battery-monitor/`, `/etc/battery-monitor/`
- Pi 5 UART setup automated by installer
- Old `~/ups-v3p` install migration/cleanup built into installer
- Uninstaller with interactive config cleanup

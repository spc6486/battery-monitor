#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────
# Battery Monitor — Installer
#
# Installs to /opt/battery-monitor with:
#   • System tray indicator (GTK3 + AppIndicator)
#   • Panel autostart on login
#   • CLI access via 'battery-monitor'
#   • Automatic low-battery shutdown
#   • UART serial configuration (auto-detects Pi model)
#
# Uninstall:  sudo /opt/battery-monitor/install.sh --uninstall
# ────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/battery-monitor"
LAUNCHER="/usr/local/bin/battery-monitor"
DESKTOP_FILE="/usr/share/applications/battery-monitor.desktop"
AUTOSTART_SYS="/etc/xdg/autostart/battery-monitor.desktop"
SUDOERS_FILE="/etc/sudoers.d/battery-monitor"
CONFIG_DIR="/etc/battery-monitor"
CONFIG_FILE="$CONFIG_DIR/battery.conf"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_REAL="${SUDO_USER:-$USER}"

# ── Colours ──────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}▸${NC} $*"; }
ok()    { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
err()   { echo -e "${RED}✗${NC} $*"; }

# ── Uninstall ────────────────────────────────────────────

if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "remove" ]; then
    echo ""
    echo "Removing Battery Monitor..."
    echo ""

    # Stop any running instances
    pkill -f "battery-monitor.py" 2>/dev/null || true
    sleep 1

    # Remove systemd leftovers from old ChatGPT install
    if systemctl is-active --quiet ups-daemon.service 2>/dev/null; then
        info "Stopping old ups-daemon service..."
        sudo systemctl stop ups-daemon.service 2>/dev/null || true
        sudo systemctl disable ups-daemon.service 2>/dev/null || true
    fi
    sudo rm -f /etc/systemd/system/ups-daemon.service
    sudo systemctl daemon-reload 2>/dev/null || true

    # Remove old ChatGPT install artifacts
    if [ -d "$HOME/ups-v3p" ]; then
        if [ -t 0 ]; then
            echo -n "Remove old ~/ups-v3p directory? [y/N] "
            read -r ans
            if [[ "$ans" =~ ^[Yy] ]]; then
                rm -rf "$HOME/ups-v3p"
                ok "Removed ~/ups-v3p"
            fi
        fi
    fi
    rm -f "$HOME/.config/autostart/ups-tray.desktop"
    rm -f "$HOME/.config/autostart/ups-tray-ayatana.desktop"

    # Remove battery-monitor install
    sudo rm -rf "$INSTALL_DIR"
    sudo rm -f  "$LAUNCHER"
    sudo rm -f  "$DESKTOP_FILE"
    sudo rm -f  "$AUTOSTART_SYS"
    sudo rm -f  "$SUDOERS_FILE"

    # Ask about config
    if [ -f "$CONFIG_FILE" ] && [ -t 0 ]; then
        echo -n "Remove configuration ($CONFIG_FILE)? [y/N] "
        read -r ans
        if [[ "$ans" =~ ^[Yy] ]]; then
            sudo rm -f "$CONFIG_FILE"
            # Remove dir if empty
            sudo rmdir "$CONFIG_DIR" 2>/dev/null || true
            ok "Configuration removed"
        else
            ok "Configuration preserved"
        fi
    fi

    echo ""
    ok "Battery Monitor removed."
    echo "   UART settings in /boot/firmware/config.txt are preserved."
    echo "   Sudoers shutdown rule removed."
    echo ""
    exit 0
fi

# ── Pre-flight checks ───────────────────────────────────

echo ""
echo "╔════════════════════════════════════════╗"
echo "║     Battery Monitor — Installer        ║"
echo "╚════════════════════════════════════════╝"
echo ""

if [ "$EUID" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    info "This installer needs sudo for system-wide installation."
    info "You may be prompted for your password."
    echo ""
fi

# ── 1. Install system dependencies ──────────────────────

info "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-gi \
    python3-serial \
    python3-yaml \
    gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    libnotify-bin \
    > /dev/null 2>&1
ok "System packages installed"

# ── 2. Detect Pi model and configure UART ────────────────

BOOT_CFG="/boot/firmware/config.txt"
CMDLINE="/boot/firmware/cmdline.txt"

# Fall back to legacy path on older Pi OS
[ -f "$BOOT_CFG" ] || BOOT_CFG="/boot/config.txt"
[ -f "$CMDLINE" ] || CMDLINE="/boot/cmdline.txt"

PI_MODEL="unknown"
if [ -f /proc/device-tree/model ]; then
    PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
fi
info "Detected: $PI_MODEL"

# Determine UART strategy based on model
# Pi 5:  UART0 defaults to debug header — needs dtparam=uart0=on
# Pi 4:  PL011 already on GPIO14/15 — just disable serial console
# Pi 3:  PL011 assigned to Bluetooth — needs dtoverlay=disable-bt
#         to free PL011 for the UPS (mini-UART moves to Bluetooth)
# Other: warn and proceed with minimal changes

case "$PI_MODEL" in
    *"Pi 5"*)
        if [ -f "$BOOT_CFG" ]; then
            if ! grep -q '^dtparam=uart0=on' "$BOOT_CFG" 2>/dev/null; then
                info "Pi 5: Enabling UART0 on GPIO14/15..."
                sudo sed -i '/^dtparam=uart0/d' "$BOOT_CFG"
                echo 'dtparam=uart0=on' | sudo tee -a "$BOOT_CFG" > /dev/null
                ok "UART0 enabled in config.txt"
                NEED_REBOOT=1
            else
                ok "UART0 already enabled"
            fi
        fi
        ;;
    *"Pi 4"*|*"Pi 400"*|*"Compute Module 4"*)
        ok "Pi 4: PL011 UART already on GPIO14/15"
        ;;
    *"Pi 3"*|*"Pi Zero 2"*)
        if [ -f "$BOOT_CFG" ]; then
            if ! grep -q '^dtoverlay=disable-bt' "$BOOT_CFG" 2>/dev/null && \
               ! grep -q '^dtoverlay=miniuart-bt' "$BOOT_CFG" 2>/dev/null; then
                info "Pi 3/Zero 2: Freeing PL011 UART from Bluetooth..."
                echo 'dtoverlay=disable-bt' | sudo tee -a "$BOOT_CFG" > /dev/null
                ok "Bluetooth overlay disabled — PL011 available on GPIO14/15"
                NEED_REBOOT=1
            else
                ok "PL011 UART already configured for GPIO14/15"
            fi
        fi
        # Disable Bluetooth system service (matches the overlay)
        sudo systemctl disable --now hciuart.service 2>/dev/null || true
        ;;
    *"Pi Zero"*|*"Pi 2"*|*"Pi Model"*)
        warn "Untested on this model ($PI_MODEL)."
        warn "UART may need manual configuration — see docs/install.md"
        ;;
    *)
        warn "Unknown board: $PI_MODEL"
        warn "UART may need manual configuration — see docs/install.md"
        ;;
esac

# Common: remove serial console from kernel cmdline (all models)
if [ -f "$CMDLINE" ]; then
    if grep -q 'console=serial0' "$CMDLINE" 2>/dev/null; then
        info "Removing serial console from cmdline.txt..."
        sudo cp "$CMDLINE" "${CMDLINE}.bak.$(date +%Y%m%d%H%M%S)"
        sudo sed -i 's/console=serial0,[0-9]\+ //g' "$CMDLINE"
        ok "Serial console removed"
        NEED_REBOOT=1
    else
        ok "Serial console already disabled"
    fi
fi

# Add user to dialout group
if ! groups "$USER_REAL" | grep -q dialout; then
    info "Adding $USER_REAL to dialout group..."
    sudo usermod -a -G dialout "$USER_REAL"
    ok "Added to dialout group"
    NEED_REBOOT=1
fi

# ── 2b. Extended CPU frequency range ────────────────────

if [ -f "$BOOT_CFG" ]; then
    if ! grep -q '^arm_freq_min=' "$BOOT_CFG" 2>/dev/null; then
        if [ -t 0 ]; then
            echo ""
            info "Enable extended CPU frequency range for better battery life?"
            echo "  Allows CPU to idle at 600 MHz instead of 1500 MHz."
            echo "  Improves battery life by ~0.5W at idle. No performance impact"
            echo "  under load — the CPU still scales up when needed."
            echo -n "  Enable? [Y/n] "
            read -r ans
            if [[ ! "$ans" =~ ^[Nn] ]]; then
                echo 'arm_freq_min=600' | sudo tee -a "$BOOT_CFG" > /dev/null
                ok "arm_freq_min=600 added to config.txt"
                NEED_REBOOT=1
            else
                ok "Skipped arm_freq_min"
            fi
        fi
    else
        ok "arm_freq_min already set"
    fi
fi

# ── 3. Disable serial login console ─────────────────────

# raspi-config nonint do_serial 2 = disable console, keep hardware
if command -v raspi-config &>/dev/null; then
    sudo raspi-config nonint do_serial 2 2>/dev/null || true
fi

# ── 4. Stop old installations ───────────────────────────

pkill -f "battery-monitor.py" 2>/dev/null || true
pkill -f "ups_tray" 2>/dev/null || true

# Stop old ChatGPT systemd service if present
if systemctl is-active --quiet ups-daemon.service 2>/dev/null; then
    info "Stopping old ups-daemon service..."
    sudo systemctl stop ups-daemon.service 2>/dev/null || true
    sudo systemctl disable ups-daemon.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/ups-daemon.service
    sudo systemctl daemon-reload
    ok "Old ups-daemon service removed"
fi

# Remove old autostart entries
rm -f "$HOME/.config/autostart/ups-tray.desktop" 2>/dev/null || true
rm -f "$HOME/.config/autostart/ups-tray-ayatana.desktop" 2>/dev/null || true

# ── 5. Install application files ────────────────────────

info "Installing to $INSTALL_DIR..."
sudo mkdir -p "$INSTALL_DIR"
sudo cp "$SRC/battery-monitor.py" "$INSTALL_DIR/"
sudo cp "$SRC/install.sh" "$INSTALL_DIR/"
sudo cp "$SRC/VERSION" "$INSTALL_DIR/" 2>/dev/null || \
    echo "1.0.0" | sudo tee "$INSTALL_DIR/VERSION" > /dev/null
sudo cp "$SRC/README.md" "$INSTALL_DIR/" 2>/dev/null || true
sudo cp "$SRC/LICENSE" "$INSTALL_DIR/" 2>/dev/null || true
sudo chmod 755 "$INSTALL_DIR/battery-monitor.py"
sudo chmod 755 "$INSTALL_DIR/install.sh"
ok "Application files installed"

# ── 6. Create launcher ──────────────────────────────────

info "Creating launcher..."
sudo tee "$LAUNCHER" > /dev/null <<'LAUNCHER_SH'
#!/usr/bin/env bash
# Battery Monitor launcher
exec python3 /opt/battery-monitor/battery-monitor.py "$@"
LAUNCHER_SH
sudo chmod 755 "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# ── 7. Create desktop menu entry ────────────────────────

info "Creating menu entry..."
sudo tee "$DESKTOP_FILE" > /dev/null <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Battery Monitor
Comment=UPS battery status indicator
Exec=battery-monitor
Icon=battery-good
Categories=Settings;HardwareSettings;
Terminal=false
StartupNotify=false

[Desktop Action Uninstall]
Name=Uninstall Battery Monitor
Exec=bash -c 'pkexec /opt/battery-monitor/install.sh --uninstall'
DESKTOP
ok "Menu entry: Preferences → Battery Monitor"

# ── 8. Create autostart entry ───────────────────────────

info "Setting up autostart..."
sudo tee "$AUTOSTART_SYS" > /dev/null <<'AUTOSTART'
[Desktop Entry]
Type=Application
Name=Battery Monitor
Comment=UPS battery status tray indicator
Exec=bash -c 'sleep 3 && exec battery-monitor'
Icon=battery-good
X-GNOME-Autostart-enabled=true
NoDisplay=true
AUTOSTART
ok "Autostart on login (3s delay for panel)"

# ── 9. Create default config ────────────────────────────

if [ ! -f "$CONFIG_FILE" ]; then
    info "Creating default configuration..."
    sudo mkdir -p "$CONFIG_DIR"
    sudo tee "$CONFIG_FILE" > /dev/null <<'CONF'
# Battery Monitor configuration
# See README.md for details

serial:
  port: /dev/ttyAMA0
  baud: 9600
  timeout_s: 2

shutdown:
  enable: true
  low_percent: 10
  confirm_seconds: 30
  clear_percent: 25

notifications:
  enable: true
  warn_percent: 20

power_saver:
  cpu_governor: false
  governor_ac: ondemand
  governor_battery: powersave
  max_freq_ac: 0
  max_freq_battery: 0
  disable_bluetooth: false
  disable_wifi: false
  reduce_refresh_rate: false

mqtt:
  enable: false
  host: 127.0.0.1
  port: 1883
  topic: raspberrypi/ups/status
  client_id: rp5-ups
CONF
    sudo chmod 644 "$CONFIG_FILE"
    ok "Config: $CONFIG_FILE"
else
    ok "Existing config preserved: $CONFIG_FILE"
fi

# ── 10. Passwordless shutdown ────────────────────────────

info "Configuring passwordless shutdown..."
echo "$USER_REAL ALL=(ALL) NOPASSWD: /sbin/shutdown" | \
    sudo tee "$SUDOERS_FILE" > /dev/null
sudo chown root:root "$SUDOERS_FILE"
sudo chmod 0440 "$SUDOERS_FILE"
sudo visudo -c -q
ok "Sudoers rule: $SUDOERS_FILE"

# Remove old-style sudoers if present
sudo rm -f /etc/sudoers.d/pi-shutdown 2>/dev/null || true

# ── 11. Update icon cache ───────────────────────────────

if command -v gtk-update-icon-cache &>/dev/null; then
    sudo gtk-update-icon-cache -f /usr/share/icons/hicolor/ 2>/dev/null || true
fi

# ── Done ─────────────────────────────────────────────────

echo ""
echo "╔════════════════════════════════════════╗"
echo "║     Installation complete!             ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "  Launcher:   battery-monitor"
echo "  CLI mode:   battery-monitor --cli"
echo "  Config:     $CONFIG_FILE"
echo "  Uninstall:  sudo $INSTALL_DIR/install.sh --uninstall"
echo ""
echo "  The battery indicator will appear in the system tray"
echo "  after login. Access settings from the tray menu."
echo ""

if [ "${NEED_REBOOT:-0}" = "1" ]; then
    warn "A reboot is needed for UART changes to take effect."
    echo ""
    if [ -t 0 ]; then
        echo -n "Reboot now? [y/N] "
        read -r ans
        if [[ "$ans" =~ ^[Yy] ]]; then
            sudo reboot
        else
            echo "  Run 'sudo reboot' when ready."
        fi
    fi
else
    info "Start the tray now with: battery-monitor"
fi
echo ""

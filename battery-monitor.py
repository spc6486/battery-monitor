#!/usr/bin/env python3
"""
Battery Monitor — MakerFocus UPSPack V3/V3P tray indicator for Raspberry Pi.

Single-process design: reads UART directly, drives the tray icon,
handles low-battery shutdown. No MQTT required.

Install location: /opt/battery-monitor/
Config location:  /etc/battery-monitor/battery.conf
"""

import os
import sys
import re
import glob
import time
import json
import threading
import subprocess
import signal
import configparser

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    AppIndicator3 = None

from gi.repository import Gtk, GLib

try:
    import serial
except ImportError:
    serial = None

try:
    import yaml
except ImportError:
    yaml = None


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

APP_ID = "battery-monitor"
VERSION = "1.0.0"
CONFIG_PATH = "/etc/battery-monitor/battery.conf"

# Status file for external consumers (e.g. serial bridge)
# Written each poll cycle with latest UPS data as JSON
_runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
STATUS_FILE = os.path.join(_runtime, "battery-monitor-status.json")

# UPS protocol parsing
LINE_PAT = re.compile(
    r"(?i)\$?\s*SmartUPS\s+([^,]+),\s*Vin\s+(\w+)\s*,"
    r"\s*BATCAP\s+(\d+)\s*,\s*Vout\s+(\d+)"
)

VIN_AC = {"GOOD", "OK"}
VIN_BAT = {"NG", "BAD"}

# sysfs paths
GOVERNOR_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
AVAIL_FREQ_PATH = (
    "/sys/devices/system/cpu/cpufreq/policy0/scaling_available_frequencies"
)
MAX_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq"
CUR_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"

WAYFIRE_INI = os.path.expanduser("~/.config/wayfire.ini")

# Default config
DEFAULT_CONFIG = {
    "serial": {
        "port": "/dev/ttyAMA0",
        "baud": 9600,
        "timeout_s": 2,
    },
    "shutdown": {
        "enable": True,
        "low_percent": 10,
        "confirm_seconds": 30,
        "clear_percent": 25,
    },
    "notifications": {
        "enable": True,
        "warn_percent": 20,
    },
    "power_saver": {
        "cpu_governor": False,
        "governor_ac": "ondemand",
        "governor_battery": "powersave",
        "max_freq_ac": 0,
        "max_freq_battery": 0,
        "disable_bluetooth": False,
    },
    "mqtt": {
        "enable": False,
        "host": "127.0.0.1",
        "port": 1883,
        "topic": "raspberrypi/ups/status",
        "client_id": "rp5-ups",
    },
}


# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

def load_config():
    """Load config from file, falling back to defaults."""
    cfg = _deep_copy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                if yaml:
                    user = yaml.safe_load(f) or {}
                else:
                    user = json.load(f)
                _deep_merge(cfg, user)
        except Exception as e:
            print(f"Warning: could not read {CONFIG_PATH}: {e}",
                  file=sys.stderr)
    return cfg


def save_config(cfg):
    """Write config back to file."""
    conf_dir = os.path.dirname(CONFIG_PATH)
    try:
        if not os.path.exists(conf_dir):
            subprocess.run(["sudo", "mkdir", "-p", conf_dir], check=True)
        tmp = f"/tmp/{APP_ID}-conf-{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            if yaml:
                yaml.safe_dump(cfg, f, sort_keys=False,
                               default_flow_style=False)
            else:
                json.dump(cfg, f, indent=2)
        subprocess.run(["sudo", "cp", tmp, CONFIG_PATH], check=True)
        subprocess.run(["sudo", "chmod", "644", CONFIG_PATH], check=True)
        os.unlink(tmp)
        return True
    except Exception as e:
        print(f"Error saving config: {e}", file=sys.stderr)
        return False


def _deep_copy(d):
    return json.loads(json.dumps(d))


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ═══════════════════════════════════════════════════════════════
# CPU Frequency Detection
# ═══════════════════════════════════════════════════════════════

def get_available_frequencies():
    """Return sorted list of available CPU frequencies in kHz."""
    try:
        with open(AVAIL_FREQ_PATH, "r") as f:
            freqs = [int(x) for x in f.read().strip().split()]
            return sorted(freqs)
    except Exception:
        return []


def get_current_max_freq():
    """Read current scaling_max_freq in kHz."""
    try:
        with open(MAX_FREQ_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def get_current_freq():
    """Read current CPU frequency in kHz."""
    try:
        with open(CUR_FREQ_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def freq_khz_to_mhz(khz):
    """Convert kHz to MHz for display."""
    return khz // 1000


def freq_mhz_to_khz(mhz):
    """Convert MHz to kHz for sysfs."""
    return mhz * 1000


# ═══════════════════════════════════════════════════════════════
# Wayfire idle (screen blanking)
# ═══════════════════════════════════════════════════════════════

def read_wayfire_idle_timeout():
    """Read dpms_timeout from wayfire.ini [idle] section. Returns minutes."""
    if not os.path.exists(WAYFIRE_INI):
        return 0
    try:
        cp = configparser.ConfigParser(strict=False)
        cp.read(WAYFIRE_INI)
        if cp.has_section("idle") and cp.has_option("idle", "dpms_timeout"):
            val = int(cp.get("idle", "dpms_timeout"))
            return max(0, val)
    except Exception:
        pass
    return 0


def write_wayfire_idle_timeout(minutes):
    """Write dpms_timeout to wayfire.ini [idle] section (targeted write)."""
    minutes = max(0, int(minutes))
    if not os.path.exists(WAYFIRE_INI):
        return False
    try:
        with open(WAYFIRE_INI, "r") as f:
            lines = f.readlines()

        in_idle = False
        found_dpms = False
        found_idle = False
        result = []

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("["):
                if in_idle and not found_dpms:
                    result.append(f"dpms_timeout = {minutes}\n")
                    found_dpms = True
                in_idle = (stripped == "[idle]")
                if in_idle:
                    found_idle = True

            if in_idle and stripped.startswith("dpms_timeout"):
                result.append(f"dpms_timeout = {minutes}\n")
                found_dpms = True
                continue

            result.append(line)

        if in_idle and not found_dpms:
            result.append(f"dpms_timeout = {minutes}\n")
            found_dpms = True

        if not found_idle:
            result.append("\n[idle]\n")
            result.append(f"dpms_timeout = {minutes}\n")

        with open(WAYFIRE_INI, "w") as f:
            f.writelines(result)
        return True
    except Exception as e:
        print(f"Error writing wayfire.ini: {e}", file=sys.stderr)
        return False


# ═══════════════════════════════════════════════════════════════
# UPS Serial Reader
# ═══════════════════════════════════════════════════════════════

class UPSReader:
    """Reads MakerFocus V3P UPS data from UART."""

    def __init__(self, port, baud, timeout):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None
        self._lock = threading.Lock()

    def open(self):
        if serial is None:
            print("pyserial not installed", file=sys.stderr)
            return False
        try:
            self._ser = serial.Serial(
                self.port, self.baud, timeout=self.timeout
            )
            return True
        except Exception as e:
            print(f"Cannot open {self.port}: {e}", file=sys.stderr)
            return False

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    def read_once(self):
        """Read and parse one UPS status line. Returns dict or None."""
        if not self._ser:
            return None
        with self._lock:
            try:
                raw = self._ser.readline().decode(errors="ignore").strip()
            except Exception:
                return None
        if not raw:
            return None
        m = LINE_PAT.search(raw)
        if not m:
            return None
        ups_ver, vin_str, bat_str, vout_str = m.groups()
        vin = vin_str.upper()
        return {
            "ups_version": ups_ver.strip(),
            "vin_state": vin,
            "ac_power": vin in VIN_AC,
            "bat_percent": int(bat_str),
            "vout_volts": int(vout_str) / 1000.0,
            "raw": raw,
            "timestamp": int(time.time()),
        }


# ═══════════════════════════════════════════════════════════════
# Power Saver
# ═══════════════════════════════════════════════════════════════

class PowerSaver:
    """Switches CPU governor, frequency cap, and Bluetooth on AC change."""

    def __init__(self, cfg):
        self._prev_ac = None
        self.update_config(cfg)

    def update_config(self, cfg):
        ps = cfg.get("power_saver", {})
        self.cpu_gov = ps.get("cpu_governor", False)
        self.gov_ac = ps.get("governor_ac", "ondemand")
        self.gov_bat = ps.get("governor_battery", "powersave")
        self.bt_toggle = ps.get("disable_bluetooth", False)

        # Frequency caps (kHz). 0 = use hardware max.
        self.max_freq_ac = int(ps.get("max_freq_ac", 0))
        self.max_freq_bat = int(ps.get("max_freq_battery", 0))

    def tick(self, data):
        """Called each poll cycle. Switches profile on AC state change."""
        if data is None:
            return
        if not self.cpu_gov and not self.bt_toggle:
            return
        ac = data["ac_power"]
        if ac == self._prev_ac:
            return
        self._prev_ac = ac
        if ac:
            self._apply_ac()
        else:
            self._apply_battery()

    def _apply_ac(self):
        if self.cpu_gov:
            self._set_governor(self.gov_ac)
        if self.max_freq_ac > 0:
            self._set_max_freq(self.max_freq_ac)
        elif self.cpu_gov:
            # Restore hardware max
            freqs = get_available_frequencies()
            if freqs:
                self._set_max_freq(freqs[-1])
        if self.bt_toggle:
            self._rfkill_bluetooth(block=False)

    def _apply_battery(self):
        if self.cpu_gov:
            self._set_governor(self.gov_bat)
        if self.max_freq_bat > 0:
            self._set_max_freq(self.max_freq_bat)
        if self.bt_toggle:
            self._rfkill_bluetooth(block=True)

    def _set_governor(self, gov):
        for policy in glob.glob(
            "/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"
        ):
            try:
                try:
                    with open(policy, "w") as f:
                        f.write(gov)
                except PermissionError:
                    subprocess.run(
                        f"echo {gov} | sudo tee {policy}",
                        shell=True, check=True,
                        stdout=subprocess.DEVNULL,
                    )
            except Exception as e:
                print(f"Governor set failed: {e}", file=sys.stderr)

    def _set_max_freq(self, freq_khz):
        """Set scaling_max_freq for all CPU policies."""
        for policy in glob.glob(
            "/sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq"
        ):
            try:
                try:
                    with open(policy, "w") as f:
                        f.write(str(freq_khz))
                except PermissionError:
                    subprocess.run(
                        f"echo {freq_khz} | sudo tee {policy}",
                        shell=True, check=True,
                        stdout=subprocess.DEVNULL,
                    )
            except Exception as e:
                print(f"Max freq set failed: {e}", file=sys.stderr)

    def _rfkill_bluetooth(self, block):
        action = "block" if block else "unblock"
        try:
            subprocess.run(
                ["rfkill", action, "bluetooth"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    def get_current_governor(self):
        try:
            with open(GOVERNOR_PATH, "r") as f:
                return f.read().strip()
        except Exception:
            return "unknown"


# ═══════════════════════════════════════════════════════════════
# Shutdown Guard
# ═══════════════════════════════════════════════════════════════

class ShutdownGuard:
    """Auto-shutdown with hysteresis when battery is critically low."""

    def __init__(self, cfg):
        self._trip_start = None
        self._tripped = False
        self.update_config(cfg)

    def tick(self, data):
        if not self.enabled or data is None:
            return
        if self._tripped:
            if data["bat_percent"] >= self.clear_pct:
                self._tripped = False
            return
        critical = (
            data["bat_percent"] <= self.low_pct
            and not data["ac_power"]
        )
        now = time.time()
        if critical:
            if self._trip_start is None:
                self._trip_start = now
            elif now - self._trip_start >= self.confirm_s:
                self._tripped = True
                print(f"Shutdown: battery at {data['bat_percent']}%",
                      file=sys.stderr)
                os.system(self.command)
        else:
            self._trip_start = None

    def update_config(self, cfg):
        sd = cfg.get("shutdown", {})
        self.enabled = sd.get("enable", True)
        self.low_pct = int(sd.get("low_percent", 10))
        self.confirm_s = int(sd.get("confirm_seconds", 30))
        self.clear_pct = int(sd.get("clear_percent", 25))
        self.command = sd.get(
            "command",
            'sudo /sbin/shutdown -h now "UPS low battery"'
        )


# ═══════════════════════════════════════════════════════════════
# Optional MQTT Publisher
# ═══════════════════════════════════════════════════════════════

class MQTTPublisher:
    """Publishes UPS status to MQTT if enabled."""

    def __init__(self, cfg):
        self._client = None
        self._enabled = False
        self._topic = "raspberrypi/ups/status"
        self.update_config(cfg)

    def update_config(self, cfg):
        mq = cfg.get("mqtt", {})
        self._enabled = mq.get("enable", False)
        if not self._enabled:
            self._disconnect()
            return
        try:
            import paho.mqtt.client as mqtt_mod
        except ImportError:
            print("paho-mqtt not installed, MQTT disabled",
                  file=sys.stderr)
            self._enabled = False
            return
        self._topic = mq.get("topic", "raspberrypi/ups/status")
        try:
            try:
                cbv = mqtt_mod.CallbackAPIVersion.VERSION2
                self._client = mqtt_mod.Client(
                    callback_api_version=cbv,
                    client_id=mq.get("client_id", "rp5-ups"),
                )
            except (AttributeError, TypeError):
                self._client = mqtt_mod.Client(
                    client_id=mq.get("client_id", "rp5-ups"),
                )
            host = mq.get("host", "127.0.0.1")
            port = int(mq.get("port", 1883))
            self._client.connect(host, port, keepalive=30)
            self._client.loop_start()
        except Exception as e:
            print(f"MQTT connect failed: {e}", file=sys.stderr)
            self._enabled = False

    def publish(self, data):
        if not self._enabled or not self._client or data is None:
            return
        try:
            payload = json.dumps(data)
            self._client.publish(self._topic, payload, retain=True)
        except Exception:
            pass

    def _disconnect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None


# ═══════════════════════════════════════════════════════════════
# Battery Icon Selection
# ═══════════════════════════════════════════════════════════════

def battery_icon_name(percent, ac_power):
    if ac_power:
        if percent >= 95:
            return "battery-full-charged"
        elif percent >= 70:
            return "battery-full-charging"
        elif percent >= 40:
            return "battery-good-charging"
        elif percent >= 15:
            return "battery-low-charging"
        else:
            return "battery-caution-charging"
    else:
        if percent >= 70:
            return "battery-full"
        elif percent >= 40:
            return "battery-good"
        elif percent >= 15:
            return "battery-low"
        else:
            return "battery-caution"


def battery_icon_fallback(percent, ac_power):
    if percent >= 70:
        return "battery-full"
    elif percent >= 40:
        return "battery-good"
    elif percent >= 15:
        return "battery-low"
    else:
        return "battery-caution"


def get_best_icon(percent, ac_power):
    name = battery_icon_name(percent, ac_power)
    theme = Gtk.IconTheme.get_default()
    if theme.has_icon(name):
        return name
    return battery_icon_fallback(percent, ac_power)


# ═══════════════════════════════════════════════════════════════
# Settings Dialog
# ═══════════════════════════════════════════════════════════════

class SettingsDialog(Gtk.Dialog):
    """Settings dialog for UPS info, shutdown thresholds, notifications."""

    def __init__(self, parent_data, cfg, reader, on_save):
        super().__init__(
            title="Battery Monitor Settings",
            flags=Gtk.DialogFlags.MODAL,
        )
        self.set_resizable(False)
        self.cfg = _deep_copy(cfg)
        self.on_save = on_save

        box = self.get_content_area()
        box.set_spacing(12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(8)

        # ── UPS Info section ──
        info_frame = Gtk.Frame(label="  UPS Information  ")
        info_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        info_grid.set_margin_start(12)
        info_grid.set_margin_end(12)
        info_grid.set_margin_top(8)
        info_grid.set_margin_bottom(8)

        d = parent_data or {}
        self._add_info_row(info_grid, 0, "UPS Model:",
                           d.get("ups_version", "—"))
        self._add_info_row(info_grid, 1, "Output Voltage:",
                           f"{d.get('vout_volts', 0):.2f} V"
                           if d.get("vout_volts") else "—")
        self._add_info_row(info_grid, 2, "Power Source:",
                           "Connected" if d.get("ac_power") is True
                           else "Battery" if d.get("ac_power") is False
                           else "—")

        port = cfg["serial"]["port"]
        if reader and reader.is_connected():
            port_status = f"{port}  (connected)"
        elif d:
            port_status = f"{port}  (connected)"
        else:
            port_status = f"{port}  (not responding)"
        self._add_info_row(info_grid, 3, "Serial Port:", port_status)

        info_frame.add(info_grid)
        box.pack_start(info_frame, False, False, 0)

        # ── Shutdown section ──
        sd_frame = Gtk.Frame(label="  Low Battery Shutdown  ")
        sd_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        sd_grid.set_margin_start(12)
        sd_grid.set_margin_end(12)
        sd_grid.set_margin_top(8)
        sd_grid.set_margin_bottom(8)

        sd = cfg.get("shutdown", {})

        self.sd_enable = Gtk.CheckButton(label="Enable auto-shutdown")
        self.sd_enable.set_active(sd.get("enable", True))
        sd_grid.attach(self.sd_enable, 0, 0, 2, 1)

        sd_grid.attach(Gtk.Label(label="Shutdown at:", xalign=0),
                       0, 1, 1, 1)
        self.sd_low = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.sd_low.set_value(sd.get("low_percent", 10))
        low_box = Gtk.Box(spacing=4)
        low_box.pack_start(self.sd_low, False, False, 0)
        low_box.pack_start(Gtk.Label(label="%"), False, False, 0)
        sd_grid.attach(low_box, 1, 1, 1, 1)

        sd_grid.attach(Gtk.Label(label="Confirm delay:", xalign=0),
                       0, 2, 1, 1)
        self.sd_confirm = Gtk.SpinButton.new_with_range(5, 300, 5)
        self.sd_confirm.set_value(sd.get("confirm_seconds", 30))
        confirm_box = Gtk.Box(spacing=4)
        confirm_box.pack_start(self.sd_confirm, False, False, 0)
        confirm_box.pack_start(Gtk.Label(label="sec"), False, False, 0)
        sd_grid.attach(confirm_box, 1, 2, 1, 1)

        sd_frame.add(sd_grid)
        box.pack_start(sd_frame, False, False, 0)

        # ── Notification section ──
        nf_frame = Gtk.Frame(label="  Notifications  ")
        nf_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        nf_grid.set_margin_start(12)
        nf_grid.set_margin_end(12)
        nf_grid.set_margin_top(8)
        nf_grid.set_margin_bottom(8)

        nf = cfg.get("notifications", {})

        self.nf_enable = Gtk.CheckButton(label="Enable low battery warning")
        self.nf_enable.set_active(nf.get("enable", True))
        nf_grid.attach(self.nf_enable, 0, 0, 2, 1)

        nf_grid.attach(Gtk.Label(label="Warn at:", xalign=0), 0, 1, 1, 1)
        self.nf_warn = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.nf_warn.set_value(nf.get("warn_percent", 20))
        warn_box = Gtk.Box(spacing=4)
        warn_box.pack_start(self.nf_warn, False, False, 0)
        warn_box.pack_start(Gtk.Label(label="%"), False, False, 0)
        nf_grid.attach(warn_box, 1, 1, 1, 1)

        nf_frame.add(nf_grid)
        box.pack_start(nf_frame, False, False, 0)

        # ── Buttons ──
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)

        self.connect("response", self._on_response)
        self.show_all()

    def _add_info_row(self, grid, row, label, value):
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_markup(f"<b>{label}</b>")
        grid.attach(lbl, 0, row, 1, 1)
        val = Gtk.Label(label=str(value), xalign=0, selectable=True)
        grid.attach(val, 1, row, 1, 1)

    def _on_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            self.cfg["shutdown"]["enable"] = self.sd_enable.get_active()
            self.cfg["shutdown"]["low_percent"] = int(
                self.sd_low.get_value()
            )
            self.cfg["shutdown"]["confirm_seconds"] = int(
                self.sd_confirm.get_value()
            )
            self.cfg["notifications"]["enable"] = self.nf_enable.get_active()
            self.cfg["notifications"]["warn_percent"] = int(
                self.nf_warn.get_value()
            )
            self.on_save(self.cfg)
        self.destroy()


class PowerSaverDialog(Gtk.Dialog):
    """Power saver dialog for CPU governor, freq caps, BT, screen blanking."""

    def __init__(self, cfg, on_save):
        super().__init__(
            title="Power Saver",
            flags=Gtk.DialogFlags.MODAL,
        )
        self.set_resizable(False)
        self.cfg = _deep_copy(cfg)
        self.on_save = on_save

        box = self.get_content_area()
        box.set_spacing(12)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(8)

        ps = cfg.get("power_saver", {})

        # ── On Battery section ──
        bat_frame = Gtk.Frame(label="  On Battery  ")
        bat_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        bat_grid.set_margin_start(12)
        bat_grid.set_margin_end(12)
        bat_grid.set_margin_top(8)
        bat_grid.set_margin_bottom(8)

        self.ps_cpu = Gtk.CheckButton(
            label="Switch CPU to power saving on battery"
        )
        self.ps_cpu.set_active(ps.get("cpu_governor", False))
        bat_grid.attach(self.ps_cpu, 0, 0, 2, 1)

        self.ps_bt = Gtk.CheckButton(
            label="Disable Bluetooth on battery"
        )
        self.ps_bt.set_active(ps.get("disable_bluetooth", False))
        bat_grid.attach(self.ps_bt, 0, 1, 2, 1)

        bat_frame.add(bat_grid)
        box.pack_start(bat_frame, False, False, 0)

        # ── CPU Frequency section ──
        freq_frame = Gtk.Frame(label="  CPU Frequency  ")
        freq_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        freq_grid.set_margin_start(12)
        freq_grid.set_margin_end(12)
        freq_grid.set_margin_top(8)
        freq_grid.set_margin_bottom(8)

        avail = get_available_frequencies()
        avail_mhz = [freq_khz_to_mhz(f) for f in avail] if avail else []
        self._avail_mhz = avail_mhz
        hw_max = avail_mhz[-1] if avail_mhz else 0

        freq_grid.attach(Gtk.Label(label="Max on AC:", xalign=0),
                         0, 0, 1, 1)
        self.freq_ac_combo = Gtk.ComboBoxText()
        self.freq_ac_combo.append_text(
            f"Default ({hw_max} MHz)" if hw_max else "Default"
        )
        cfg_ac_khz = int(ps.get("max_freq_ac", 0))
        cfg_ac_mhz = freq_khz_to_mhz(cfg_ac_khz) if cfg_ac_khz else 0
        ac_active = 0
        for i, mhz in enumerate(avail_mhz):
            self.freq_ac_combo.append_text(f"{mhz} MHz")
            if cfg_ac_mhz == mhz:
                ac_active = i + 1
        self.freq_ac_combo.set_active(ac_active)
        freq_grid.attach(self.freq_ac_combo, 1, 0, 1, 1)

        freq_grid.attach(Gtk.Label(label="Max on battery:", xalign=0),
                         0, 1, 1, 1)
        self.freq_bat_combo = Gtk.ComboBoxText()
        self.freq_bat_combo.append_text(
            f"Default ({hw_max} MHz)" if hw_max else "Default"
        )
        cfg_bat_khz = int(ps.get("max_freq_battery", 0))
        cfg_bat_mhz = freq_khz_to_mhz(cfg_bat_khz) if cfg_bat_khz else 0
        bat_active = 0
        for i, mhz in enumerate(avail_mhz):
            self.freq_bat_combo.append_text(f"{mhz} MHz")
            if cfg_bat_mhz == mhz:
                bat_active = i + 1
        self.freq_bat_combo.set_active(bat_active)
        freq_grid.attach(self.freq_bat_combo, 1, 1, 1, 1)

        # Current status
        gov = PowerSaver(cfg).get_current_governor()
        cur_mhz = freq_khz_to_mhz(get_current_freq())
        max_mhz = freq_khz_to_mhz(get_current_max_freq())
        status = f"{gov}, {cur_mhz}/{max_mhz} MHz"
        freq_grid.attach(Gtk.Label(label="Current:", xalign=0), 0, 2, 1, 1)
        freq_grid.attach(Gtk.Label(label=status, xalign=0), 1, 2, 1, 1)

        freq_frame.add(freq_grid)
        box.pack_start(freq_frame, False, False, 0)

        # ── Display section ──
        disp_frame = Gtk.Frame(label="  Display  ")
        disp_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        disp_grid.set_margin_start(12)
        disp_grid.set_margin_end(12)
        disp_grid.set_margin_top(8)
        disp_grid.set_margin_bottom(8)

        disp_grid.attach(Gtk.Label(label="Screen blanking:", xalign=0),
                         0, 0, 1, 1)
        self.ps_blank = Gtk.SpinButton.new_with_range(0, 60, 1)
        self.ps_blank.set_value(read_wayfire_idle_timeout())
        blank_box = Gtk.Box(spacing=4)
        blank_box.pack_start(self.ps_blank, False, False, 0)
        blank_box.pack_start(
            Gtk.Label(label="min  (0 = never)"), False, False, 0
        )
        disp_grid.attach(blank_box, 1, 0, 1, 1)

        disp_frame.add(disp_grid)
        box.pack_start(disp_frame, False, False, 0)

        # ── Buttons ──
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)

        self.connect("response", self._on_response)
        self.show_all()

    def _parse_freq_combo(self, combo):
        """Parse a freq combo selection. Returns kHz or 0 for default."""
        idx = combo.get_active()
        if idx <= 0:
            return 0
        mhz = self._avail_mhz[idx - 1]
        return freq_mhz_to_khz(mhz)

    def _on_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            self.cfg["power_saver"]["cpu_governor"] = (
                self.ps_cpu.get_active()
            )
            self.cfg["power_saver"]["disable_bluetooth"] = (
                self.ps_bt.get_active()
            )
            self.cfg["power_saver"]["max_freq_ac"] = (
                self._parse_freq_combo(self.freq_ac_combo)
            )
            self.cfg["power_saver"]["max_freq_battery"] = (
                self._parse_freq_combo(self.freq_bat_combo)
            )

            write_wayfire_idle_timeout(int(self.ps_blank.get_value()))

            self.on_save(self.cfg)
        self.destroy()


# ═══════════════════════════════════════════════════════════════
# Tray Application
# ═══════════════════════════════════════════════════════════════

class BatteryTray:
    """System tray indicator for UPS battery status."""

    def __init__(self):
        self.cfg = load_config()
        self.data = None
        self._warned = False

        self.reader = UPSReader(
            self.cfg["serial"]["port"],
            self.cfg["serial"]["baud"],
            self.cfg["serial"]["timeout_s"],
        )
        self.guard = ShutdownGuard(self.cfg)
        self.mqtt = MQTTPublisher(self.cfg)
        self.power = PowerSaver(self.cfg)

        self._build_indicator()
        self._build_menu()

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self._reader_thread.start()

        GLib.timeout_add_seconds(2, self._update_ui)

    # ── Indicator ────────────────────────────────────────────

    def _build_indicator(self):
        if AppIndicator3:
            self.indicator = AppIndicator3.Indicator.new(
                APP_ID,
                "battery-missing",
                AppIndicator3.IndicatorCategory.HARDWARE,
            )
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Battery Monitor")
        else:
            self.indicator = None
            self.status_icon = Gtk.StatusIcon.new_from_icon_name(
                "battery-missing"
            )
            self.status_icon.set_tooltip_text("Battery Monitor")
            self.status_icon.connect("popup-menu", self._on_status_popup)
            self.status_icon.connect("activate", self._on_status_activate)

    def _on_status_popup(self, icon, button, time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu,
                        icon, button, time)

    def _on_status_activate(self, icon):
        self.menu.popup(None, None, None, None, 0,
                        Gtk.get_current_event_time())

    # ── Menu ─────────────────────────────────────────────────

    def _build_menu(self):
        self.menu = Gtk.Menu()

        self.mi_status = Gtk.MenuItem(label="Battery: —")
        self.mi_status.set_sensitive(False)
        self.menu.append(self.mi_status)

        self.menu.append(Gtk.SeparatorMenuItem())

        self._add_item("Settings…", self._on_settings)
        self._add_item("Power Saver…", self._on_power_saver)

        self.menu.append(Gtk.SeparatorMenuItem())

        self._add_item("Quit", self._on_quit)

        self.menu.show_all()

        if self.indicator:
            self.indicator.set_menu(self.menu)

    def _add_item(self, label, callback):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        self.menu.append(item)
        return item

    # ── Serial reader (background thread) ────────────────────

    def _reader_loop(self):
        while True:
            if not self.reader._ser:
                if not self.reader.open():
                    time.sleep(5)
                    continue
            d = self.reader.read_once()
            if d:
                self.data = d
                self.guard.tick(d)
                self.mqtt.publish(d)
                self.power.tick(d)
                self._write_status_file(d)
            else:
                time.sleep(0.5)

    # ── UI update (GLib main loop) ───────────────────────────

    def _update_ui(self):
        d = self.data
        if d is None:
            self._set_icon("battery-missing")
            self.mi_status.set_label("Battery: not connected")
            return True

        pct = d["bat_percent"]
        ac = d["ac_power"]

        icon_name = get_best_icon(pct, ac)
        self._set_icon(icon_name)

        if ac:
            if pct >= 95:
                status = f"Full — AC Power ({pct}%)"
            else:
                status = f"Charging: {pct}%"
        else:
            status = f"Battery: {pct}%"

        self.mi_status.set_label(status)

        nf = self.cfg.get("notifications", {})
        if nf.get("enable", True) and not ac:
            warn_pct = nf.get("warn_percent", 20)
            if pct <= warn_pct and not self._warned:
                self._warned = True
                self._notify(
                    "Low Battery",
                    f"Battery at {pct}%. Connect charger.",
                    "battery-caution",
                )
        if ac:
            self._warned = False

        return True

    def _set_icon(self, name):
        if self.indicator:
            self.indicator.set_icon_full(name, "Battery status")
        else:
            self.status_icon.set_from_icon_name(name)

    def _notify(self, title, body, icon):
        try:
            subprocess.Popen(
                ["notify-send", "-i", icon, "-u", "critical", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    # ── Callbacks ────────────────────────────────────────────

    def _on_settings(self, _widget):
        SettingsDialog(self.data, self.cfg, self.reader, self._save_settings)

    def _on_power_saver(self, _widget):
        PowerSaverDialog(self.cfg, self._save_settings)

    def _save_settings(self, new_cfg):
        self.cfg = new_cfg
        if save_config(new_cfg):
            self.guard.update_config(new_cfg)
            self.mqtt.update_config(new_cfg)
            self.power.update_config(new_cfg)

    def _write_status_file(self, data):
        """Write latest UPS data to a JSON file for external consumers."""
        try:
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass

    def _on_quit(self, _widget):
        self.reader.close()
        try:
            os.unlink(STATUS_FILE)
        except FileNotFoundError:
            pass
        Gtk.main_quit()


# ═══════════════════════════════════════════════════════════════
# CLI Status
# ═══════════════════════════════════════════════════════════════

def cli_status():
    cfg = load_config()
    ps = PowerSaver(cfg)
    print(f"Battery Monitor v{VERSION}")
    print(f"Serial port: {cfg['serial']['port']}")
    print(f"Shutdown at: {cfg['shutdown']['low_percent']}%"
          f" (confirm {cfg['shutdown']['confirm_seconds']}s)")
    print(f"Warning at:  {cfg['notifications']['warn_percent']}%")
    print(f"Governor:    {ps.get_current_governor()}")
    cur = freq_khz_to_mhz(get_current_freq())
    cap = freq_khz_to_mhz(get_current_max_freq())
    print(f"CPU freq:    {cur}/{cap} MHz")
    print()

    reader = UPSReader(
        cfg["serial"]["port"],
        cfg["serial"]["baud"],
        cfg["serial"]["timeout_s"],
    )
    if not reader.open():
        print("ERROR: Cannot open serial port")
        return

    print("Reading UPS (Ctrl+C to stop)...")
    try:
        while True:
            d = reader.read_once()
            if d:
                ac = "AC" if d["ac_power"] else "BAT"
                print(f"  {d['vin_state']} BAT={d['bat_percent']}%"
                      f" V={d['vout_volts']:.2f}V [{ac}]"
                      f"  ({d['ups_version']})")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        reader.close()


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════

def main():
    if "--cli" in sys.argv or "--status" in sys.argv:
        cli_status()
        return

    if "--help" in sys.argv or "-h" in sys.argv:
        print(f"Battery Monitor v{VERSION}")
        print("Usage: battery-monitor [--cli] [--help]")
        print("  --cli    Print UPS status to terminal (no GUI)")
        print("  (none)   Launch system tray indicator")
        return

    if "--version" in sys.argv:
        print(f"Battery Monitor v{VERSION}")
        return

    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())

    app = BatteryTray()
    Gtk.main()


if __name__ == "__main__":
    main()

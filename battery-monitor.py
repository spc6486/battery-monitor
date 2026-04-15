#!/usr/bin/env python3
"""
Battery Monitor — UPS tray indicator for Raspberry Pi.

Supports both SunFounder PiPower 5 (I2C) and MakerFocus V3/V3P (UART).
Auto-detects which hardware is present at startup.

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
import collections

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    AppIndicator3 = None

from gi.repository import Gtk, GLib, Gdk

try:
    import serial
except ImportError:
    serial = None

try:
    import smbus2
except ImportError:
    smbus2 = None

try:
    import yaml
except ImportError:
    yaml = None


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

APP_ID = "battery-monitor"
VERSION = "2.0.0"
CONFIG_PATH = "/etc/battery-monitor/battery.conf"

# Status file for external consumers (e.g. serial bridge)
_runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
STATUS_FILE = os.path.join(_runtime, "battery-monitor-status.json")

# V3P protocol parsing
LINE_PAT = re.compile(
    r"(?i)\$?\s*SmartUPS\s+([^,]+),\s*Vin\s+(\w+)\s*,"
    r"\s*BATCAP\s+(\d+)\s*,\s*Vout\s+(\d+)"
)

VIN_AC = {"GOOD", "OK"}
VIN_BAT = {"NG", "BAD"}

# PiPower 5 I2C
PIPOWER5_ADDR = 0x5C
PIPOWER5_BUS = 1

# sysfs paths
GOVERNOR_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
AVAIL_FREQ_PATH = (
    "/sys/devices/system/cpu/cpufreq/policy0/scaling_available_frequencies"
)
MAX_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq"
CUR_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"

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
        "disable_wifi": False,
        "reduce_refresh_rate": False,
    },
    "pipower5": {
        "battery_capacity_wh": 59.2,
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
# Config Management
# ═══════════════════════════════════════════════════════════════

def load_config():
    cfg = _deep_copy(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "r") as f:
            if yaml:
                user = yaml.safe_load(f) or {}
            else:
                user = {}
                for line in f:
                    line = line.strip()
                    if ":" in line and not line.startswith("#"):
                        k, v = line.split(":", 1)
                        user[k.strip()] = v.strip()
        _deep_merge(cfg, user)
    except Exception as e:
        print(f"Config load error: {e}", file=sys.stderr)
    return cfg


def save_config(cfg):
    if not yaml:
        return False
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        print(f"Config save error: {e}", file=sys.stderr)
        return False


def _deep_copy(d):
    return json.loads(json.dumps(d))


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ═══════════════════════════════════════════════════════════════
# CPU Frequency Helpers
# ═══════════════════════════════════════════════════════════════

def get_available_frequencies():
    try:
        with open(AVAIL_FREQ_PATH, "r") as f:
            return sorted(int(x) for x in f.read().split())
    except Exception:
        return []


def get_current_max_freq():
    try:
        with open(MAX_FREQ_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def get_current_freq():
    try:
        with open(CUR_FREQ_PATH, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def freq_khz_to_mhz(khz):
    return khz // 1000


def freq_mhz_to_khz(mhz):
    return mhz * 1000


# ═══════════════════════════════════════════════════════════════
# Display Refresh Rate (wlr-randr)
# ═══════════════════════════════════════════════════════════════

def detect_hdmi_output():
    """Detect the HDMI output name from wlr-randr. Returns name or None."""
    try:
        out = subprocess.check_output(
            ["wlr-randr"], text=True, timeout=3,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("HDMI-"):
                return line.split()[0]
    except Exception:
        pass
    return None


def get_display_description():
    """Get the full display description string from wlr-randr.
    Returns e.g. 'XXX HDMI' or 'TYT HDMI HDMI', or None."""
    try:
        out = subprocess.check_output(
            ["wlr-randr"], text=True, timeout=3,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("HDMI-"):
                # Format: HDMI-A-1 "Description (HDMI-A-1)"
                m = re.match(r'HDMI-\S+\s+"([^"]+)"', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def get_current_refresh_rate():
    """Read current refresh rate in Hz from wlr-randr. Returns int or 0."""
    try:
        out = subprocess.check_output(
            ["wlr-randr"], text=True, timeout=3,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "current" in line and "Hz" in line:
                m = re.search(r"([\d.]+)\s*Hz", line)
                if m:
                    return int(float(m.group(1)))
    except Exception:
        pass
    return 0


def set_refresh_rate(hz):
    """Set HDMI refresh rate via wlr-randr custom mode.
    Returns True on success. Does NOT verify display health —
    caller should use confirm_or_revert_refresh() for user-initiated changes."""
    current_hz = get_current_refresh_rate()
    if current_hz == hz:
        return True
    if current_hz == 0:
        return False
    output = detect_hdmi_output()
    if not output:
        return False
    try:
        out = subprocess.check_output(
            ["wlr-randr"], text=True, timeout=3,
            stderr=subprocess.DEVNULL,
        )
        res = None
        for line in out.splitlines():
            if "current" in line and "px" in line:
                m = re.match(r"\s*(\d+x\d+)\s+px", line)
                if m:
                    res = m.group(1)
                    break
        if not res:
            return False
        subprocess.run(
            ["wlr-randr", "--output", output,
             "--custom-mode", f"{res}@{hz}Hz"],
            check=True, timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def revert_refresh_rate():
    """Revert display to EDID default by restarting kanshi."""
    try:
        subprocess.run(["pkill", "kanshi"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        time.sleep(1)
        subprocess.Popen(["kanshi"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# UPS Backend Abstraction
# ═══════════════════════════════════════════════════════════════

def detect_ups_type():
    """Detect which UPS hardware is present. Returns 'pipower5', 'v3p', or None."""
    # Try I2C first (PiPower 5) — read status block and verify sane values
    if smbus2:
        try:
            bus = smbus2.SMBus(PIPOWER5_BUS)
            # Read 25-byte status block from register 0 (matches SPC read_all)
            raw = bus.read_i2c_block_data(PIPOWER5_ADDR, 0, 25)
            bus.close()
            # Output voltage at offset 4 (little-endian word)
            out_mv = raw[4] | (raw[5] << 8)
            # Battery percentage at offset 12 (single byte)
            pct = raw[12]
            # Sanity: output voltage 3000-6000 mV and percent 0-100
            if 3000 <= out_mv <= 6000 and 0 <= pct <= 100:
                return "pipower5"
        except Exception:
            pass
    # Try UART (V3P)
    if serial:
        try:
            ports = ["/dev/ttyAMA0", "/dev/serial0"]
            for port in ports:
                if os.path.exists(port):
                    ser = serial.Serial(port, 9600, timeout=2)
                    data = ser.read(200)
                    ser.close()
                    if b"SmartUPS" in data:
                        return "v3p"
        except Exception:
            pass
    return None


class UPSBackend:
    """Abstract base for UPS communication backends."""

    def open(self):
        """Open the connection. Returns True on success."""
        raise NotImplementedError

    def close(self):
        """Close the connection."""
        pass

    def is_connected(self):
        """Returns True if backend is connected."""
        return False

    def read_status(self):
        """Read UPS status. Returns standardized dict or None."""
        raise NotImplementedError

    def get_type_name(self):
        """Human-readable UPS type."""
        return "Unknown"

    def get_connection_info(self):
        """Human-readable connection info."""
        return "—"

    def get_hardware_info(self):
        """Hardware-specific details dict."""
        return {}


class V3PBackend(UPSBackend):
    """UART-based MakerFocus V3/V3P communication."""

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

    def read_status(self):
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
            "ups_type": "v3p",
            "ups_version": ups_ver.strip(),
            "vin_state": vin,
            "ac_power": vin in VIN_AC,
            "bat_percent": int(bat_str),
            "vout_volts": int(vout_str) / 1000.0,
            "raw": raw,
            # PiPower5-only fields (None for V3P)
            "input_voltage_mv": None,
            "input_current_ma": None,
            "output_current_ma": None,
            "battery_voltage_mv": None,
            "battery_current_ma": None,
            "input_power_w": None,
            "output_power_w": None,
            "battery_power_w": None,
            "is_charging": None,
            "estimated_runtime_min": None,
            "timestamp": int(time.time()),
        }

    def get_type_name(self):
        return "MakerFocus V3P"

    def get_connection_info(self):
        if self.is_connected():
            return f"{self.port} connected"
        return f"{self.port} not responding"

    def get_hardware_info(self):
        return {
            "port": self.port,
            "baud": self.baud,
        }


class PiPower5Backend(UPSBackend):
    """I2C-based SunFounder PiPower 5 communication."""

    # Register offsets within 25-byte block read (from spc library)
    _OFF_INPUT_VOLTAGE = 0       # word (2 bytes)
    _OFF_INPUT_CURRENT = 2       # word
    _OFF_OUTPUT_VOLTAGE = 4      # word
    _OFF_OUTPUT_CURRENT = 6      # word
    _OFF_BATTERY_VOLTAGE = 8     # word
    _OFF_BATTERY_CURRENT = 10    # word (signed)
    _OFF_BATTERY_PERCENTAGE = 12 # byte
    _OFF_BATTERY_CAPACITY = 13   # word
    _OFF_POWER_SOURCE = 15       # byte
    _OFF_IS_PLUGGED_IN = 16      # byte
    _OFF_IS_CHARGING = 18        # byte
    _OFF_SHUTDOWN_REQUEST = 20   # byte
    _BLOCK_LENGTH = 25

    def __init__(self, battery_capacity_wh=59.2):
        self._bus = None
        self._lock = threading.Lock()
        self._battery_capacity_wh = battery_capacity_wh
        self._power_history = collections.deque(maxlen=30)

    def open(self):
        if smbus2 is None:
            print("smbus2 not installed", file=sys.stderr)
            return False
        try:
            self._bus = smbus2.SMBus(PIPOWER5_BUS)
            # Verify device responds
            self._bus.read_byte(PIPOWER5_ADDR)
            return True
        except Exception as e:
            print(f"Cannot open I2C: {e}", file=sys.stderr)
            return False

    def close(self):
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def is_connected(self):
        if not self._bus:
            return False
        try:
            self._bus.read_byte(PIPOWER5_ADDR)
            return True
        except Exception:
            return False

    def _read_block(self):
        """Read the full 25-byte status block (matches SPC read_all)."""
        with self._lock:
            try:
                return self._bus.read_i2c_block_data(
                    PIPOWER5_ADDR, 0, self._BLOCK_LENGTH
                )
            except Exception:
                return None

    def _u16(self, data, offset):
        """Unpack little-endian unsigned 16-bit value."""
        return data[offset] | (data[offset + 1] << 8)

    def _i16(self, data, offset):
        """Unpack little-endian signed 16-bit value."""
        val = self._u16(data, offset)
        return val if val < 32768 else val - 65536

    def read_status(self):
        if not self._bus:
            return None
        raw = self._read_block()
        if raw is None or len(raw) < self._BLOCK_LENGTH:
            return None
        try:
            input_v = self._u16(raw, self._OFF_INPUT_VOLTAGE)
            input_a = self._u16(raw, self._OFF_INPUT_CURRENT)
            output_v = self._u16(raw, self._OFF_OUTPUT_VOLTAGE)
            output_a = self._u16(raw, self._OFF_OUTPUT_CURRENT)
            bat_v = self._u16(raw, self._OFF_BATTERY_VOLTAGE)
            bat_a = self._i16(raw, self._OFF_BATTERY_CURRENT)
            bat_pct = raw[self._OFF_BATTERY_PERCENTAGE]
            power_src = raw[self._OFF_POWER_SOURCE]
            plugged = raw[self._OFF_IS_PLUGGED_IN]
            charging = raw[self._OFF_IS_CHARGING]
            shutdown_req = raw[self._OFF_SHUTDOWN_REQUEST]

            on_ac = (power_src == 0) and bool(plugged)
            output_w = (output_v * output_a) / 1_000_000.0
            input_w = (input_v * input_a) / 1_000_000.0
            bat_w = (bat_v * abs(bat_a)) / 1_000_000.0

            # Rolling average for runtime estimation
            if output_w > 0:
                self._power_history.append(output_w)
            avg_power = (
                sum(self._power_history) / len(self._power_history)
                if self._power_history else 0
            )
            runtime_min = None
            if avg_power > 0 and bat_pct > 0:
                remaining_wh = (
                    (bat_pct / 100.0) * self._battery_capacity_wh
                )
                runtime_min = int((remaining_wh / avg_power) * 60)

            return {
                "ups_type": "pipower5",
                "ups_version": "PiPower 5",
                "vin_state": "GOOD" if on_ac else "NG",
                "ac_power": on_ac,
                "bat_percent": min(100, max(0, bat_pct)),
                "vout_volts": output_v / 1000.0,
                # PiPower5-specific
                "input_voltage_mv": input_v,
                "input_current_ma": input_a,
                "output_current_ma": output_a,
                "battery_voltage_mv": bat_v,
                "battery_current_ma": bat_a,
                "input_power_w": round(input_w, 3),
                "output_power_w": round(output_w, 3),
                "battery_power_w": round(bat_w, 3),
                "is_charging": bool(charging),
                "estimated_runtime_min": runtime_min,
                "shutdown_request": shutdown_req,
                "timestamp": int(time.time()),
            }
        except Exception as e:
            print(f"PiPower5 read failed: {e}", file=sys.stderr)
            return None

    def get_type_name(self):
        return "SunFounder PiPower 5"

    def get_connection_info(self):
        if self.is_connected():
            return f"I2C 0x{PIPOWER5_ADDR:02X} connected"
        return f"I2C 0x{PIPOWER5_ADDR:02X} not responding"

    def get_hardware_info(self):
        """Read hardware info from extended registers."""
        info = {}
        if not self._bus:
            return info
        try:
            with self._lock:
                # Firmware version (regs 128, 129, 130)
                fw = self._bus.read_i2c_block_data(
                    PIPOWER5_ADDR, 128, 3
                )
                info["firmware"] = f"{fw[0]}.{fw[1]}.{fw[2]}"
                # Shutdown percentage (reg 143)
                info["shutdown_pct"] = self._bus.read_byte_data(
                    PIPOWER5_ADDR, 143
                )
                # Max charge current (reg 155, N*100mA)
                raw = self._bus.read_byte_data(PIPOWER5_ADDR, 155)
                info["max_charge_ma"] = raw * 100
                # Default on (reg 139)
                info["default_on"] = bool(
                    self._bus.read_byte_data(PIPOWER5_ADDR, 139)
                )
        except Exception as e:
            print(f"PiPower5 hw info read failed: {e}",
                  file=sys.stderr)
        return info


def create_backend(cfg):
    """Auto-detect UPS hardware and create appropriate backend."""
    ups_type = detect_ups_type()
    if ups_type == "pipower5":
        pp = cfg.get("pipower5", {})
        return PiPower5Backend(
            battery_capacity_wh=float(pp.get("battery_capacity_wh", 59.2))
        )
    elif ups_type == "v3p":
        sc = cfg.get("serial", {})
        return V3PBackend(
            sc.get("port", "/dev/ttyAMA0"),
            int(sc.get("baud", 9600)),
            int(sc.get("timeout_s", 2)),
        )
    else:
        # Default to V3P backend (will fail gracefully if no hardware)
        sc = cfg.get("serial", {})
        return V3PBackend(
            sc.get("port", "/dev/ttyAMA0"),
            int(sc.get("baud", 9600)),
            int(sc.get("timeout_s", 2)),
        )


# ═══════════════════════════════════════════════════════════════
# Power Saver
# ═══════════════════════════════════════════════════════════════

class PowerSaver:
    """Switches CPU governor, frequency cap, Wi-Fi/BT, and refresh rate."""

    def __init__(self, cfg):
        self._prev_ac = None
        self.update_config(cfg)

    def update_config(self, cfg):
        ps = cfg.get("power_saver", {})
        self.cpu_gov = ps.get("cpu_governor", False)
        self.gov_ac = ps.get("governor_ac", "ondemand")
        self.gov_bat = ps.get("governor_battery", "powersave")
        self.bt_toggle = ps.get("disable_bluetooth", False)
        self.wifi_toggle = ps.get("disable_wifi", False)
        self.refresh_toggle = ps.get("reduce_refresh_rate", False)
        self.max_freq_ac = int(ps.get("max_freq_ac", 0))
        self.max_freq_bat = int(ps.get("max_freq_battery", 0))

    def has_any_action(self):
        return (self.cpu_gov or self.bt_toggle or self.wifi_toggle
                or self.refresh_toggle)

    def tick(self, data):
        if data is None:
            return
        if not self.has_any_action():
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
            freqs = get_available_frequencies()
            if freqs:
                self._set_max_freq(freqs[-1])
        if self.bt_toggle:
            self._rfkill("bluetooth", block=False)
        if self.wifi_toggle:
            self._rfkill("wifi", block=False)

    def _apply_battery(self):
        if self.cpu_gov:
            self._set_governor(self.gov_bat)
        if self.max_freq_bat > 0:
            self._set_max_freq(self.max_freq_bat)
        if self.bt_toggle:
            self._rfkill("bluetooth", block=True)
        if self.wifi_toggle:
            self._rfkill("wifi", block=True)

    def apply_refresh_rate(self):
        if self.refresh_toggle:
            return set_refresh_rate(30)
        else:
            return set_refresh_rate(60)

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

    def _rfkill(self, device, block):
        action = "block" if block else "unblock"
        try:
            subprocess.run(
                ["rfkill", action, device],
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
        # PiPower5: honor its own shutdown request immediately
        if data.get("shutdown_request", 0) != 0:
            print("PiPower5 shutdown request received", file=sys.stderr)
            os.system(self.command)
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
# Settings Window (tabbed)
# ═══════════════════════════════════════════════════════════════

class BatterySettingsWindow(Gtk.Window):
    """Tabbed settings window — adapts display based on UPS backend type."""

    def __init__(self, parent_data, cfg, backend, on_save):
        super().__init__(title="Battery Monitor", default_width=420)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_resizable(False)

        self.cfg = _deep_copy(cfg)
        self.on_save = on_save
        self.parent_data = parent_data or {}
        self.backend = backend

        self._build_ui()

    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        self.add(outer)

        # ── Header ──
        header = Gtk.Label()
        header.set_markup(
            f"<big><b>Battery Monitor</b></big>  <small>v{VERSION}</small>"
        )
        header.set_xalign(0)
        outer.pack_start(header, False, False, 0)

        d = self.parent_data
        if d.get("ac_power") is True:
            pct = d.get("bat_percent", 0)
            state = f"Charging ({pct}%)" if pct < 95 else f"Full ({pct}%)"
        elif d.get("ac_power") is False:
            pct = d.get("bat_percent", 0)
            rt = d.get("estimated_runtime_min")
            if rt:
                state = f"Battery ({pct}%, ~{rt} min)"
            else:
                state = f"Battery ({pct}%)"
        else:
            state = "Not connected"

        conn = self.backend.get_connection_info() if self.backend else "—"

        info = Gtk.Label()
        info.set_markup(
            f"<small>UPS: <b>{self.backend.get_type_name()}</b>    "
            f"Status: <b>{state}</b>    "
            f"<b>{conn}</b></small>"
        )
        info.set_xalign(0)
        outer.pack_start(info, False, False, 0)

        # ── Notebook (tabs) ──
        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 4)

        # ═══ TAB 1: UPS ═══
        ups_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        ups_page.set_margin_start(8)
        ups_page.set_margin_end(8)
        ups_page.set_margin_top(8)
        ups_page.set_margin_bottom(8)
        notebook.append_page(ups_page, Gtk.Label(label="UPS"))

        hw = self.backend.get_hardware_info() if self.backend else {}
        ups_type = d.get("ups_type", "")

        # Status
        st_frame = Gtk.Frame(label="  Status  ")
        st_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        st_grid.set_margin_start(12)
        st_grid.set_margin_end(12)
        st_grid.set_margin_top(4)
        st_grid.set_margin_bottom(4)

        row = 0
        if ups_type == "pipower5":
            in_v = d.get("input_voltage_mv")
            in_w = d.get("input_power_w")
            self._add_info_row(st_grid, row, "Input:",
                               f"{in_v / 1000.0:.1f} V, "
                               f"{d.get('input_current_ma', 0)} mA"
                               f" ({in_w:.1f} W)"
                               if in_v else "—")
            row += 1
            out_v = d.get("vout_volts", 0)
            out_w = d.get("output_power_w")
            self._add_info_row(st_grid, row, "Output:",
                               f"{out_v:.2f} V, "
                               f"{d.get('output_current_ma', 0)} mA"
                               f" ({out_w:.1f} W)"
                               if out_w else "—")
            row += 1
            bat_v = d.get("battery_voltage_mv")
            rt = d.get("estimated_runtime_min")
            rt_str = f", ~{rt} min" if rt else ""
            self._add_info_row(st_grid, row, "Battery:",
                               f"{bat_v / 1000.0:.2f} V, "
                               f"{d.get('battery_current_ma', 0)} mA"
                               f"{rt_str}"
                               if bat_v else "—")
        else:
            self._add_info_row(st_grid, row, "Model:",
                               d.get("ups_version", "—"))
            row += 1
            self._add_info_row(st_grid, row, "Output:",
                               f"{d.get('vout_volts', 0):.2f} V"
                               if d.get("vout_volts") else "—")

        st_frame.add(st_grid)
        ups_page.pack_start(st_frame, False, False, 0)

        # Shutdown & Warnings (merged)
        sd_frame = Gtk.Frame(label="  Shutdown & Warnings  ")
        sd_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        sd_grid.set_margin_start(12)
        sd_grid.set_margin_end(12)
        sd_grid.set_margin_top(4)
        sd_grid.set_margin_bottom(4)

        sd = self.cfg.get("shutdown", {})
        nf = self.cfg.get("notifications", {})

        self.sd_enable = Gtk.CheckButton(label="Auto-shutdown on low battery")
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

        self.sd_reboot = Gtk.CheckButton(label="Reboot instead of shutdown")
        self.sd_reboot.set_active(
            "shutdown -r" in sd.get("command", "")
        )
        sd_grid.attach(self.sd_reboot, 0, 3, 2, 1)

        self.nf_enable = Gtk.CheckButton(label="Low battery warning")
        self.nf_enable.set_active(nf.get("enable", True))
        sd_grid.attach(self.nf_enable, 0, 4, 1, 1)
        self.nf_warn = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.nf_warn.set_value(nf.get("warn_percent", 20))
        warn_box = Gtk.Box(spacing=4)
        warn_box.pack_start(self.nf_warn, False, False, 0)
        warn_box.pack_start(Gtk.Label(label="%"), False, False, 0)
        sd_grid.attach(warn_box, 1, 4, 1, 1)

        sd_frame.add(sd_grid)
        ups_page.pack_start(sd_frame, False, False, 0)

        # Hardware / Connection
        if ups_type == "pipower5":
            hw_frame = Gtk.Frame(label="  Hardware  ")
            hw_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
            hw_grid.set_margin_start(12)
            hw_grid.set_margin_end(12)
            hw_grid.set_margin_top(4)
            hw_grid.set_margin_bottom(4)

            hw_grid.attach(
                Gtk.Label(label="Capacity:", xalign=0), 0, 0, 1, 1
            )
            pp = self.cfg.get("pipower5", {})
            self.ups_capacity = Gtk.SpinButton.new_with_range(
                1, 200, 0.1
            )
            self.ups_capacity.set_digits(1)
            self.ups_capacity.set_value(
                float(pp.get("battery_capacity_wh", 59.2))
            )
            cap_box = Gtk.Box(spacing=4)
            cap_box.pack_start(self.ups_capacity, False, False, 0)
            cap_box.pack_start(Gtk.Label(label="Wh"), False, False, 0)
            hw_grid.attach(cap_box, 1, 0, 1, 1)

            # Compact hardware info on two rows
            fw = hw.get("firmware", "—")
            sd_pct = hw.get("shutdown_pct", "—")
            self._add_info_row(hw_grid, 1, "Firmware:",
                               f"{fw}    Shutdown: {sd_pct}%")
            charge = hw.get("max_charge_ma", "—")
            default = "on" if hw.get("default_on") else "off"
            self._add_info_row(hw_grid, 2, "Charge:",
                               f"{charge} mA    Default on: {default}")

            hw_frame.add(hw_grid)
            ups_page.pack_start(hw_frame, False, False, 0)
        else:
            conn_frame = Gtk.Frame(label="  Connection  ")
            conn_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
            conn_grid.set_margin_start(12)
            conn_grid.set_margin_end(12)
            conn_grid.set_margin_top(4)
            conn_grid.set_margin_bottom(4)

            conn_grid.attach(
                Gtk.Label(label="Serial port:", xalign=0), 0, 0, 1, 1
            )
            self.ups_port = Gtk.Entry()
            self.ups_port.set_text(
                self.cfg.get("serial", {}).get("port", "/dev/ttyAMA0")
            )
            conn_grid.attach(self.ups_port, 1, 0, 1, 1)

            self._add_info_row(
                conn_grid, 1, "Baud rate:",
                str(hw.get("baud", 9600))
            )

            conn_frame.add(conn_grid)
            ups_page.pack_start(conn_frame, False, False, 0)

        # ═══ TAB 2: Power Saver ═══
        power_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6
        )
        power_page.set_margin_start(8)
        power_page.set_margin_end(8)
        power_page.set_margin_top(8)
        power_page.set_margin_bottom(8)
        notebook.append_page(power_page, Gtk.Label(label="Power Saver"))

        ps = self.cfg.get("power_saver", {})

        # On Battery
        bat_frame = Gtk.Frame(label="  On Battery  ")
        bat_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        bat_grid.set_margin_start(12)
        bat_grid.set_margin_end(12)
        bat_grid.set_margin_top(4)
        bat_grid.set_margin_bottom(4)

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

        self.ps_wifi = Gtk.CheckButton(
            label="Disable Wi-Fi on battery"
        )
        self.ps_wifi.set_active(ps.get("disable_wifi", False))
        bat_grid.attach(self.ps_wifi, 0, 2, 2, 1)

        bat_frame.add(bat_grid)
        power_page.pack_start(bat_frame, False, False, 0)

        # CPU Frequency
        freq_frame = Gtk.Frame(label="  CPU Frequency  ")
        freq_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        freq_grid.set_margin_start(12)
        freq_grid.set_margin_end(12)
        freq_grid.set_margin_top(4)
        freq_grid.set_margin_bottom(4)

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

        # Live CPU status
        freq_grid.attach(Gtk.Label(label="Current:", xalign=0),
                         0, 2, 1, 1)
        self._freq_label = Gtk.Label(xalign=0)
        self._update_freq_label()
        freq_grid.attach(self._freq_label, 1, 2, 1, 1)

        freq_frame.add(freq_grid)
        power_page.pack_start(freq_frame, False, False, 0)

        # Display
        disp_frame = Gtk.Frame(label="  Display  ")
        disp_grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        disp_grid.set_margin_start(12)
        disp_grid.set_margin_end(12)
        disp_grid.set_margin_top(4)
        disp_grid.set_margin_bottom(4)

        self.ps_refresh = Gtk.CheckButton(
            label="Reduce refresh rate (60→30 Hz)"
        )
        self.ps_refresh.set_tooltip_text(
            "Lowers HDMI refresh rate to save ~0.3–0.5W.\n"
            "Applied at startup while battery-monitor is running.\n"
            "No visual impact on LCD panels."
        )
        self.ps_refresh.set_active(ps.get("reduce_refresh_rate", False))
        disp_grid.attach(self.ps_refresh, 0, 0, 2, 1)

        disp_grid.attach(Gtk.Label(label="Current:", xalign=0),
                         0, 1, 1, 1)
        refresh_hz = get_current_refresh_rate()
        self._refresh_label = Gtk.Label(
            label=f"{refresh_hz} Hz" if refresh_hz else "—", xalign=0
        )
        disp_grid.attach(self._refresh_label, 1, 1, 1, 1)

        disp_frame.add(disp_grid)
        power_page.pack_start(disp_frame, False, False, 0)

        # Start live update timer
        self._freq_timer = GLib.timeout_add(1000, self._update_freq_label)
        self.connect("destroy", self._on_destroy)

        # ── Bottom button bar ──
        btn_box = Gtk.Box(spacing=8)
        btn_box.set_margin_top(4)

        spacer = Gtk.Label()
        btn_box.pack_start(spacer, True, True, 0)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda _: self.destroy())
        btn_box.pack_start(close_btn, False, False, 0)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.get_style_context().add_class("suggested-action")
        apply_btn.connect("clicked", self._on_apply)
        btn_box.pack_start(apply_btn, False, False, 0)

        outer.pack_start(btn_box, False, False, 0)

    def _add_info_row(self, grid, row, label, value):
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.set_markup(f"<b>{label}</b>")
        grid.attach(lbl, 0, row, 1, 1)
        val = Gtk.Label(label=str(value), xalign=0, selectable=True)
        grid.attach(val, 1, row, 1, 1)

    def _update_freq_label(self):
        gov = PowerSaver(self.cfg).get_current_governor()
        cur = freq_khz_to_mhz(get_current_freq())
        cap = freq_khz_to_mhz(get_current_max_freq())
        self._freq_label.set_text(f"{gov}, {cur}/{cap} MHz")
        if hasattr(self, "_refresh_label"):
            hz = get_current_refresh_rate()
            self._refresh_label.set_text(f"{hz} Hz" if hz else "—")
        return True

    def _on_destroy(self, _widget):
        if hasattr(self, "_freq_timer"):
            GLib.source_remove(self._freq_timer)

    def _parse_freq_combo(self, combo):
        idx = combo.get_active()
        if idx <= 0:
            return 0
        mhz = self._avail_mhz[idx - 1]
        return freq_mhz_to_khz(mhz)

    def _on_apply(self, _btn):
        # Settings tab
        self.cfg["shutdown"]["enable"] = self.sd_enable.get_active()
        self.cfg["shutdown"]["low_percent"] = int(self.sd_low.get_value())
        self.cfg["shutdown"]["confirm_seconds"] = int(
            self.sd_confirm.get_value()
        )
        if self.sd_reboot.get_active():
            self.cfg["shutdown"]["command"] = (
                'sudo /sbin/shutdown -r now "UPS low battery — rebooting"'
            )
        else:
            self.cfg["shutdown"]["command"] = (
                'sudo /sbin/shutdown -h now "UPS low battery"'
            )
        self.cfg["notifications"]["enable"] = self.nf_enable.get_active()
        self.cfg["notifications"]["warn_percent"] = int(
            self.nf_warn.get_value()
        )
        # UPS tab
        ups_type = self.parent_data.get("ups_type", "")
        if ups_type == "pipower5" and hasattr(self, "ups_capacity"):
            if "pipower5" not in self.cfg:
                self.cfg["pipower5"] = {}
            self.cfg["pipower5"]["battery_capacity_wh"] = round(
                self.ups_capacity.get_value(), 1
            )
        elif hasattr(self, "ups_port"):
            self.cfg["serial"]["port"] = self.ups_port.get_text().strip()
        # Power Saver tab
        self.cfg["power_saver"]["cpu_governor"] = self.ps_cpu.get_active()
        self.cfg["power_saver"]["disable_bluetooth"] = (
            self.ps_bt.get_active()
        )
        self.cfg["power_saver"]["disable_wifi"] = (
            self.ps_wifi.get_active()
        )
        self.cfg["power_saver"]["max_freq_ac"] = (
            self._parse_freq_combo(self.freq_ac_combo)
        )
        self.cfg["power_saver"]["max_freq_battery"] = (
            self._parse_freq_combo(self.freq_bat_combo)
        )

        new_refresh = self.ps_refresh.get_active()

        # If 30Hz is checked, handle with confirmation/revert
        if new_refresh:
            # Save everything EXCEPT the refresh rate change
            self.cfg["power_saver"]["reduce_refresh_rate"] = False
            self.on_save(self.cfg)
            # Now run confirmation with manual 30Hz apply
            self._confirm_refresh_change()
        else:
            self.cfg["power_saver"]["reduce_refresh_rate"] = False
            self.cfg["power_saver"].pop("refresh_confirmed_display", None)
            self.on_save(self.cfg)
            self.destroy()

    def _confirm_refresh_change(self):
        """Apply 30Hz with a background safety revert.
        Writes a revert script and runs it via nohup — fully independent
        of the compositor and GTK process."""

        # Write revert script
        revert_script = "/tmp/battery-monitor-revert.sh"
        revert_log = "/tmp/battery-monitor-revert.log"
        wayland_disp = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
        xdg_dir = os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")
        with open(revert_script, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"exec >> {revert_log} 2>&1\n")
            f.write("echo \"$(date): revert script started\"\n")
            f.write("sleep 12\n")
            f.write("echo \"$(date): killing kanshi\"\n")
            f.write("pkill -9 kanshi\n")
            f.write("sleep 2\n")
            f.write(f"export WAYLAND_DISPLAY={wayland_disp}\n")
            f.write(f"export XDG_RUNTIME_DIR={xdg_dir}\n")
            f.write("echo \"$(date): starting kanshi\"\n")
            f.write("kanshi &\n")
            f.write("echo \"$(date): done\"\n")
        os.chmod(revert_script, 0o755)

        # Launch via os.system with shell backgrounding — most reliable
        # way to create a fully detached process
        os.system(f"nohup {revert_script} > /dev/null 2>&1 &")

        # Verify revert process is running before changing display
        time.sleep(0.5)
        result = subprocess.run(
            ["pgrep", "-f", "battery-monitor-revert"],
            stdout=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            print("WARNING: revert process failed to start",
                  file=sys.stderr)

        # Now apply the mode change
        set_refresh_rate(30)

        # Show confirmation dialog — if user can see it, display works
        self._revert_seconds = 10
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.QUESTION,
            text="Keep this refresh rate?",
        )
        dlg.format_secondary_text(
            f"Reverting to 60 Hz in {self._revert_seconds} seconds..."
        )
        dlg.add_button("Revert Now", Gtk.ResponseType.CANCEL)
        keep_btn = dlg.add_button("Keep 30 Hz", Gtk.ResponseType.OK)
        keep_btn.get_style_context().add_class("suggested-action")

        def _countdown():
            self._revert_seconds -= 1
            if self._revert_seconds <= 0:
                dlg.response(Gtk.ResponseType.CANCEL)
                return False
            dlg.format_secondary_text(
                f"Reverting to 60 Hz in {self._revert_seconds} seconds..."
            )
            return True

        timer_id = GLib.timeout_add(1000, _countdown)
        response = dlg.run()
        GLib.source_remove(timer_id)
        dlg.destroy()

        if response == Gtk.ResponseType.OK:
            # User confirmed — kill the revert script
            subprocess.run(
                ["pkill", "-f", "battery-monitor-revert"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                os.unlink(revert_script)
            except FileNotFoundError:
                pass
            # Re-apply 30Hz in case revert already started
            set_refresh_rate(30)
            # NOW save config with refresh rate enabled
            self.cfg["power_saver"]["reduce_refresh_rate"] = True
            desc = get_display_description()
            if desc:
                self.cfg["power_saver"]["refresh_confirmed_display"] = desc
            self.on_save(self.cfg)
            self.destroy()
        else:
            # Wait for revert script to finish, or trigger manually
            if os.path.exists(revert_script):
                revert_refresh_rate()
                try:
                    os.unlink(revert_script)
                except FileNotFoundError:
                    pass
            self.cfg["power_saver"]["reduce_refresh_rate"] = False
            self.cfg["power_saver"].pop("refresh_confirmed_display", None)
            self.on_save(self.cfg)
            self.ps_refresh.set_active(False)
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

        print(f"Detecting UPS hardware...", file=sys.stderr)
        self.backend = create_backend(self.cfg)
        print(f"  UPS: {self.backend.get_type_name()}", file=sys.stderr)

        self.guard = ShutdownGuard(self.cfg)
        self.mqtt = MQTTPublisher(self.cfg)
        self.power = PowerSaver(self.cfg)
        self._cached_refresh_hz = get_current_refresh_rate()

        # Apply refresh rate after compositor settles
        GLib.timeout_add_seconds(3, self._apply_startup_refresh)

        self._build_indicator()
        self._build_menu()

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self._reader_thread.start()

        GLib.timeout_add_seconds(2, self._update_ui)
        GLib.timeout_add_seconds(30, self._update_refresh_cache)

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

        item = Gtk.MenuItem(label="Battery Settings…")
        item.connect("activate", self._on_settings)
        self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())

        uninstall = Gtk.MenuItem(label="Uninstall Battery Monitor")
        uninstall.connect("activate", self._on_uninstall)
        self.menu.append(uninstall)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()

        if self.indicator:
            self.indicator.set_menu(self.menu)

    # ── Reader (background thread) ───────────────────────────

    def _reader_loop(self):
        while True:
            if not self.backend.is_connected():
                if not self.backend.open():
                    time.sleep(5)
                    continue
            d = self.backend.read_status()
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
            rt = d.get("estimated_runtime_min")
            if rt:
                status = f"Battery: {pct}% (~{rt} min)"
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

    def _update_refresh_cache(self):
        self._cached_refresh_hz = get_current_refresh_rate()
        return True

    def _apply_startup_refresh(self):
        """Apply refresh rate at startup — only if this display was
        previously confirmed to work at 30Hz."""
        if not self.power.refresh_toggle:
            return False
        # Only apply if the current display matches what was confirmed
        confirmed = self.cfg.get("power_saver", {}).get(
            "refresh_confirmed_display"
        )
        if not confirmed:
            return False  # never confirmed, skip
        current_display = get_display_description()
        if not current_display or confirmed not in current_display:
            print(f"Skipping 30Hz: display '{current_display}' "
                  f"doesn't match confirmed '{confirmed}'",
                  file=sys.stderr)
            return False
        if self.power.apply_refresh_rate():
            self._cached_refresh_hz = get_current_refresh_rate()
            return False  # success
        self._refresh_retries = getattr(self, "_refresh_retries", 0) + 1
        if self._refresh_retries >= 5:
            return False
        return True  # retry

    def _write_status_file(self, data):
        try:
            enriched = dict(data)
            enriched["cpu_freq_mhz"] = freq_khz_to_mhz(get_current_freq())
            enriched["refresh_hz"] = self._cached_refresh_hz
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(enriched, f)
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass

    # ── Callbacks ────────────────────────────────────────────

    def _on_settings(self, _widget):
        win = BatterySettingsWindow(
            self.data, self.cfg, self.backend, self._save_settings
        )
        win.connect("destroy", lambda _: None)
        win.show_all()

    def _save_settings(self, new_cfg):
        self.cfg = new_cfg
        if save_config(new_cfg):
            self.guard.update_config(new_cfg)
            self.mqtt.update_config(new_cfg)
            self.power.update_config(new_cfg)
            self.power.apply_refresh_rate()
            # Update backend capacity if changed
            if hasattr(self.backend, '_battery_capacity_wh'):
                pp = new_cfg.get("pipower5", {})
                self.backend._battery_capacity_wh = float(
                    pp.get("battery_capacity_wh", 59.2)
                )

    def _on_uninstall(self, _widget):
        dlg = Gtk.MessageDialog(
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Uninstall Battery Monitor?",
        )
        dlg.format_secondary_text(
            "This will remove the application, launcher, autostart, "
            "and sudoers rule. Configuration can be kept or removed."
        )
        response = dlg.run()
        dlg.destroy()
        if response == Gtk.ResponseType.YES:
            self.backend.close()
            try:
                os.unlink(STATUS_FILE)
            except FileNotFoundError:
                pass
            subprocess.Popen(
                ["pkexec", "/opt/battery-monitor/install.sh", "--uninstall"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Gtk.main_quit()

    def _on_quit(self, _widget):
        self.backend.close()
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
    ups_type = detect_ups_type() or "unknown"
    print(f"Battery Monitor v{VERSION}")
    print(f"UPS type:    {ups_type}")
    if ups_type == "v3p":
        print(f"Serial port: {cfg['serial']['port']}")
    elif ups_type == "pipower5":
        print(f"I2C bus:     {PIPOWER5_BUS}, addr 0x{PIPOWER5_ADDR:02X}")
    print(f"Shutdown at: {cfg['shutdown']['low_percent']}%"
          f" (confirm {cfg['shutdown']['confirm_seconds']}s)")
    print(f"Warning at:  {cfg['notifications']['warn_percent']}%")
    print(f"Governor:    {ps.get_current_governor()}")
    cur = freq_khz_to_mhz(get_current_freq())
    cap = freq_khz_to_mhz(get_current_max_freq())
    print(f"CPU freq:    {cur}/{cap} MHz")
    hz = get_current_refresh_rate()
    print(f"Refresh:     {hz} Hz" if hz else "Refresh:     —")
    print()

    # If tray app is running, read from its status file
    if os.path.exists(STATUS_FILE):
        print("Reading from tray app (Ctrl+C to stop)...")
        try:
            while True:
                try:
                    with open(STATUS_FILE, "r") as f:
                        d = json.load(f)
                    ac = "AC" if d.get("ac_power") else "BAT"
                    pct = d.get("bat_percent", 0)
                    ups = d.get("ups_type", "?")
                    line = f"  BAT={pct}% [{ac}] ({ups})"
                    # Add PiPower5 details
                    out_w = d.get("output_power_w")
                    if out_w is not None:
                        line += f" {out_w:.1f}W"
                    rt = d.get("estimated_runtime_min")
                    if rt is not None:
                        line += f" ~{rt}min"
                    print(line)
                    time.sleep(2)
                except (json.JSONDecodeError, KeyError):
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    # No tray running — try backend directly
    backend = create_backend(cfg)
    if backend.open():
        print(f"Reading {backend.get_type_name()} directly "
              f"(Ctrl+C to stop)...")
        try:
            while True:
                d = backend.read_status()
                if d:
                    ac = "AC" if d["ac_power"] else "BAT"
                    pct = d["bat_percent"]
                    line = f"  BAT={pct}% [{ac}] ({d.get('ups_version','?')})"
                    out_w = d.get("output_power_w")
                    if out_w is not None:
                        line += f" {out_w:.1f}W"
                    rt = d.get("estimated_runtime_min")
                    if rt is not None:
                        line += f" ~{rt}min"
                    print(line)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            backend.close()
        return

    print("ERROR: No status file and cannot open UPS connection.")
    print("       Start the tray app or check hardware.")


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

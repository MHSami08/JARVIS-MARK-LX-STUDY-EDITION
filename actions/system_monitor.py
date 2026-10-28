# -*- coding: utf-8 -*-
"""
Minimal Health-Warning-Only System Diagnostics (`actions/system_monitor.py`)
Does NOT list running user processes, windows, or personal files.
Only checks RAM, Disk, and CPU/GPU health thresholds to protect the PC during study sessions.
"""

import psutil
import time

TOOL_SCHEMA = {
    "name": "system_monitor",
    "description": "Check critical PC health metrics (RAM %, CPU %, Disk free %, GPU temp) and report if any warning threshold is exceeded.",
    "parameters": {
        "type": "object",
        "properties": {}
    }
}

def check_health_warnings() -> dict:
    ram = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=0.2)
    disk = psutil.disk_usage("/")
    gpu_temp = None

    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        gpu_temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
    except Exception:
        pass

    warnings = []
    if ram.percent >= 88.0:
        warnings.append(f"⚠️ High RAM usage warning: {ram.percent:.1f}% used.")
    if cpu_pct >= 90.0:
        warnings.append(f"⚠️ High CPU load warning: {cpu_pct:.1f}%.")
    if disk.percent >= 92.0:
        warnings.append(f"⚠️ Low Disk Space warning: {disk.percent:.1f}% full.")
    if gpu_temp is not None and gpu_temp >= 83:
        warnings.append(f"⚠️ High GPU Temperature warning: {gpu_temp}°C.")

    return {
        "status": "warning" if warnings else "healthy",
        "ram_percent": ram.percent,
        "cpu_percent": cpu_pct,
        "disk_percent": disk.percent,
        "gpu_temp_c": gpu_temp,
        "warnings": warnings
    }

def execute(**kwargs) -> dict:
    return check_health_warnings()


# ── Backwards-compatible session API (used by main.py's inline "system_status"
#    tool and the background health-alert task) ────────────────────────────────

_ALERT_COOLDOWN = 900.0  # seconds: repeat the same warning at most every 15 min


class SystemMonitor:
    """Persistent wrapper around check_health_warnings() with a cooldown, so a
    stuck resource does not re-alert on every 10-second poll."""

    def __init__(self):
        self._last_alert = ""
        self._last_time = 0.0

    def check(self) -> str:
        """Return ONE verbal alert ("") when nothing needs saying."""
        info = check_health_warnings()
        if not info["warnings"]:
            return ""
        alert = info["warnings"][0]          # one sentence; the rest repeat later
        now = time.monotonic()
        if alert == self._last_alert and (now - self._last_time) < _ALERT_COOLDOWN:
            return ""
        self._last_alert, self._last_time = alert, now
        return alert


def get_system_status() -> dict:
    return check_health_warnings()

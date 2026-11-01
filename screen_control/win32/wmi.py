"""Brightness of built-in laptop panels, which use WMI instead of DDC/CI.

Talking to WMI natively needs COM (pywin32 or a lot of ctypes vtable code), so
we shell out to PowerShell instead. It is slower (~0.5 s) but dependency-free,
and only used when an internal panel is actually present.
"""

from __future__ import annotations

import logging
import os
import subprocess

from .common import Win32Error

__all__ = ("get_internal_brightness", "set_internal_brightness")

log = logging.getLogger(__name__)

_TIMEOUT = 10
_CREATE_NO_WINDOW = 0x08000000


def _powershell_exe() -> str:
    """By full path: a bare name is also searched for in Python's and the current folder."""
    system_root = os.environ.get("SystemRoot")
    path = os.path.join(system_root or "", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if system_root and os.path.isfile(path) else "powershell.exe"


def _powershell(script: str) -> str:
    try:
        proc = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Win32Error(f"Could not query the built-in display: {e}") from e
    if proc.returncode != 0:
        raise Win32Error(
            "Windows does not expose brightness control for the built-in display"
        )
    return proc.stdout


def _normalize(instance_name: str) -> str:
    # WMI: DISPLAY\BOE0A1D\4&2e1c5e9&0&UID8388688_0 -> DISPLAY\BOE0A1D\4&2e1c5e9&0&UID8388688
    name = instance_name.strip().upper()
    head, sep, tail = name.rpartition("_")
    return head if sep and tail.isdigit() else name


def get_internal_brightness() -> dict[str, int]:
    """Map of device instance id (upper case) -> brightness percent."""

    out = _powershell(
        "Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
        "-ErrorAction Stop | ForEach-Object { $_.InstanceName + '|' + $_.CurrentBrightness }"
    )
    levels: dict[str, int] = {}
    for line in out.splitlines():
        name, sep, value = line.rpartition("|")
        if sep and value.strip().isdigit():
            levels[_normalize(name)] = int(value)
    return levels


def set_internal_brightness(instance_id: str | None, value: int) -> None:
    value = max(0, min(100, int(value)))
    # Instance ids only contain [A-Za-z0-9&_\\]; refuse anything else rather
    # than risk breaking out of the PowerShell string.
    if instance_id and not all(c.isalnum() or c in "&_\\" for c in instance_id):
        instance_id = None
    selector = ""
    if instance_id:
        selector = f" | Where-Object {{ $_.InstanceName -like '{instance_id}*' }}"
    _powershell(
        "Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
        f"-ErrorAction Stop{selector} | Invoke-CimMethod -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=1; Brightness=[byte]{value}}} -ErrorAction Stop | Out-Null"
    )

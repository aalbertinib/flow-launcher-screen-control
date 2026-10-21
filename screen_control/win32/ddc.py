"""DDC/CI (monitor control over the video cable) through dxva2.dll.

DDC/CI is slow (tens of milliseconds per command) and flaky: commands sent too
close together, or while the monitor is busy, fail transiently. Every call here
therefore retries with a short back-off, and callers must serialise access
(the monitor service runs everything on a single worker thread).
"""

from __future__ import annotations

import ctypes
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes as wt
from dataclasses import dataclass

from .common import Win32Error

__all__ = (
    "VCP_BRIGHTNESS",
    "VCP_CONTRAST",
    "DdcError",
    "describe_error",
    "PhysicalMonitor",
    "get_vcp",
    "open_physical_monitors",
    "set_vcp",
)

log = logging.getLogger(__name__)

VCP_BRIGHTNESS = 0x10
VCP_CONTRAST = 0x12

_ATTEMPTS = 3
# MCCS asks hosts to wait 40-50 ms between commands; give flaky monitors more.
_RETRY_DELAYS = (0.06, 0.15)

_dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)


class _PHYSICAL_MONITOR(ctypes.Structure):
    _pack_ = 1  # physicalmonitorenumerationapi.h uses #pragma pack(1)
    _fields_ = (
        ("hPhysicalMonitor", wt.HANDLE),
        ("szPhysicalMonitorDescription", wt.WCHAR * 128),
    )


_GetNumberOfPhysicalMonitorsFromHMONITOR = _dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR
_GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = (wt.HMONITOR, ctypes.POINTER(wt.DWORD))
_GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wt.BOOL

_GetPhysicalMonitorsFromHMONITOR = _dxva2.GetPhysicalMonitorsFromHMONITOR
_GetPhysicalMonitorsFromHMONITOR.argtypes = (
    wt.HMONITOR,
    wt.DWORD,
    ctypes.POINTER(_PHYSICAL_MONITOR),
)
_GetPhysicalMonitorsFromHMONITOR.restype = wt.BOOL

_DestroyPhysicalMonitors = _dxva2.DestroyPhysicalMonitors
_DestroyPhysicalMonitors.argtypes = (wt.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR))
_DestroyPhysicalMonitors.restype = wt.BOOL

_GetVCPFeatureAndVCPFeatureReply = _dxva2.GetVCPFeatureAndVCPFeatureReply
_GetVCPFeatureAndVCPFeatureReply.argtypes = (
    wt.HANDLE,
    wt.BYTE,
    ctypes.c_void_p,
    ctypes.POINTER(wt.DWORD),
    ctypes.POINTER(wt.DWORD),
)
_GetVCPFeatureAndVCPFeatureReply.restype = wt.BOOL

_SetVCPFeature = _dxva2.SetVCPFeature
_SetVCPFeature.argtypes = (wt.HANDLE, wt.BYTE, wt.DWORD)
_SetVCPFeature.restype = wt.BOOL


class DdcError(Win32Error):
    pass


_NOT_RESPONDING = (
    "The monitor did not answer over DDC/CI. Make sure DDC/CI is enabled in "
    "its on-screen menu (and that it is not connected through a dock or adapter "
    "that blocks it)"
)
_GARBLED = (
    "The monitor answered with corrupted DDC/CI data: it may be busy or stuck. "
    "If this persists, turn it off and on with its power button"
)

# Error codes from winerror.h (ERROR_GRAPHICS_*), grouped by what the user can do.
_NO_DDC_CODES = {
    0xC0262580,  # I2C_NOT_SUPPORTED: no I2C bus on that output
    0xC0262581,  # I2C_DEVICE_DOES_NOT_EXIST: nothing answering at the DDC/CI address
}
_GARBLED_CODES = {
    0xC0262582,  # I2C_ERROR_TRANSMITTING_DATA
    0xC0262583,  # I2C_ERROR_RECEIVING_DATA
    0xC0262585,  # DDCCI_INVALID_DATA
    0xC0262589,  # DDCCI_INVALID_MESSAGE_COMMAND
    0xC026258A,  # DDCCI_INVALID_MESSAGE_LENGTH
    0xC026258B,  # DDCCI_INVALID_MESSAGE_CHECKSUM
}
_VCP_NOT_SUPPORTED = 0xC0262584
_GONE_CODES = {
    0xC026258C,  # INVALID_PHYSICAL_MONITOR_HANDLE
    0xC026258D,  # MONITOR_NO_LONGER_EXISTS (mode change, monitor unplugged...)
}
_CONSOLE_ONLY = 0xC02625E0
_INVALID_VALUE = 0xC02625D8  # current value greater than maximum
_FEATURE_NAMES = {VCP_BRIGHTNESS: "brightness", VCP_CONTRAST: "contrast"}


def describe_error(code: int, vcp_code: int | None = None) -> str:
    """User-facing advice for a DDC/CI failure code."""
    code &= 0xFFFFFFFF
    if code == _VCP_NOT_SUPPORTED:
        feature = _FEATURE_NAMES.get(vcp_code or -1, "this setting")
        return f"This monitor does not allow changing {feature} over DDC/CI"
    if code in _GARBLED_CODES:
        return _GARBLED
    if code in _GONE_CODES:
        return "The display configuration just changed: try again"
    if code == _CONSOLE_ONLY:
        return "Monitor control is not available over Remote Desktop"
    if code == _INVALID_VALUE:
        return "The monitor reported an invalid value for this setting"
    return _NOT_RESPONDING


@dataclass(frozen=True)
class PhysicalMonitor:
    handle: int
    description: str


@contextmanager
def open_physical_monitors(hmonitor: int) -> Iterator[list[PhysicalMonitor]]:
    """Yield the physical monitors behind an HMONITOR and always release them."""

    count = wt.DWORD()
    if not _GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) or not count.value:
        yield []
        return

    arr = (_PHYSICAL_MONITOR * count.value)()
    if not _GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, arr):
        yield []
        return
    try:
        yield [
            PhysicalMonitor(int(m.hPhysicalMonitor or 0), m.szPhysicalMonitorDescription)
            for m in arr
        ]
    finally:
        if not _DestroyPhysicalMonitors(count.value, arr):
            log.warning("DestroyPhysicalMonitors failed: %s", ctypes.get_last_error())


def get_vcp(handle: int, code: int) -> tuple[int, int]:
    """Return ``(current, maximum)`` for a VCP feature."""

    current, maximum = wt.DWORD(), wt.DWORD()
    last_error = 0
    for attempt in range(_ATTEMPTS):
        if _GetVCPFeatureAndVCPFeatureReply(
            handle, code, None, ctypes.byref(current), ctypes.byref(maximum)
        ):
            if maximum.value == 0:
                raise DdcError("The monitor reported an invalid range for this setting")
            return int(current.value), int(maximum.value)
        last_error = ctypes.get_last_error()
        if (last_error & 0xFFFFFFFF) == _VCP_NOT_SUPPORTED:
            break  # a definite answer: retrying will not change it
        if attempt < len(_RETRY_DELAYS):
            time.sleep(_RETRY_DELAYS[attempt])
    raise DdcError(describe_error(last_error, code), last_error)


def set_vcp(handle: int, code: int, value: int) -> None:
    last_error = 0
    for attempt in range(_ATTEMPTS):
        if _SetVCPFeature(handle, code, value):
            return
        last_error = ctypes.get_last_error()
        if (last_error & 0xFFFFFFFF) == _VCP_NOT_SUPPORTED:
            break
        if attempt < len(_RETRY_DELAYS):
            time.sleep(_RETRY_DELAYS[attempt])
    raise DdcError(describe_error(last_error, code), last_error)

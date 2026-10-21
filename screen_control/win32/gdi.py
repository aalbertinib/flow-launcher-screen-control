"""GDI monitor enumeration: desktop rectangles, primary flag, DPI scale."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes as wt
from dataclasses import dataclass

from .common import RECT, Win32Error

__all__ = (
    "GdiMonitor",
    "enable_dpi_awareness",
    "enum_attached_monitor_interfaces",
    "enum_monitors",
)

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)

MONITORINFOF_PRIMARY = 0x1
EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DISPLAY_DEVICE_ACTIVE = 0x1
DISPLAY_DEVICE_ATTACHED = 0x2


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    )


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = (
        ("cb", wt.DWORD),
        ("DeviceName", wt.WCHAR * 32),
        ("DeviceString", wt.WCHAR * 128),
        ("StateFlags", wt.DWORD),
        ("DeviceID", wt.WCHAR * 128),
        ("DeviceKey", wt.WCHAR * 128),
    )


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM
)

_EnumDisplayMonitors = _user32.EnumDisplayMonitors
_EnumDisplayMonitors.argtypes = (wt.HDC, ctypes.POINTER(RECT), _MONITORENUMPROC, wt.LPARAM)
_EnumDisplayMonitors.restype = wt.BOOL

_GetMonitorInfoW = _user32.GetMonitorInfoW
_GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(_MONITORINFOEXW))
_GetMonitorInfoW.restype = wt.BOOL

_EnumDisplayDevicesW = _user32.EnumDisplayDevicesW
_EnumDisplayDevicesW.argtypes = (
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.POINTER(_DISPLAY_DEVICEW),
    wt.DWORD,
)
_EnumDisplayDevicesW.restype = wt.BOOL


@dataclass(frozen=True)
class GdiMonitor:
    hmonitor: int
    device_name: str
    """e.g. ``\\\\.\\DISPLAY1``"""
    left: int
    top: int
    right: int
    bottom: int
    primary: bool
    scale_percent: int | None

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def enable_dpi_awareness() -> None:
    """Make coordinates physical pixels so monitor rectangles are not virtualised."""

    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        fn = _user32.SetProcessDpiAwarenessContext
        fn.argtypes = (ctypes.c_void_p,)
        fn.restype = wt.BOOL
        if fn(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        _user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        log.debug("Could not enable DPI awareness", exc_info=True)


def _scale_for(hmonitor: int) -> int | None:
    try:
        shcore = ctypes.WinDLL("shcore")
        fn = shcore.GetDpiForMonitor
        fn.argtypes = (
            wt.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wt.UINT),
            ctypes.POINTER(wt.UINT),
        )
        fn.restype = ctypes.c_long
        x, y = wt.UINT(), wt.UINT()
        if fn(hmonitor, 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
            return round(x.value * 100 / 96)
    except (AttributeError, OSError):
        pass
    return None


def enum_monitors() -> list[GdiMonitor]:
    handles: list[int] = []

    def _callback(hmon, _hdc, _rect, _lparam):  # noqa: ANN001
        handles.append(int(hmon))
        return True

    cb = _MONITORENUMPROC(_callback)  # keep a reference for the duration of the call
    if not _EnumDisplayMonitors(None, None, cb, 0):
        raise Win32Error("Could not list monitors", ctypes.get_last_error())

    monitors: list[GdiMonitor] = []
    for hmon in handles:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not _GetMonitorInfoW(hmon, ctypes.byref(info)):
            continue  # monitor went away between the two calls
        r = info.rcMonitor
        monitors.append(
            GdiMonitor(
                hmonitor=hmon,
                device_name=info.szDevice,
                left=r.left,
                top=r.top,
                right=r.right,
                bottom=r.bottom,
                primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                scale_percent=_scale_for(hmon),
            )
        )
    return monitors


def enum_attached_monitor_interfaces(gdi_device_name: str) -> list[str]:
    """Device interface paths of the monitors attached to a GDI source, in order.

    This order is the one ``GetPhysicalMonitorsFromHMONITOR`` uses, which lets us
    pair DDC/CI handles with CCD targets when a source drives several monitors
    (duplicate/clone mode).
    """

    paths: list[str] = []
    i = 0
    while i < 16:
        dev = _DISPLAY_DEVICEW()
        dev.cb = ctypes.sizeof(dev)
        if not _EnumDisplayDevicesW(gdi_device_name, i, ctypes.byref(dev), EDD_GET_DEVICE_INTERFACE_NAME):
            break
        if dev.StateFlags & (DISPLAY_DEVICE_ACTIVE | DISPLAY_DEVICE_ATTACHED):
            paths.append(dev.DeviceID)
        i += 1
    return paths

"""Connecting and Configuring Displays (CCD) API: active display paths and HDR.

Struct layouts mirror wingdi.h from the Windows 10.0.26100 SDK.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass

from .common import LUID, POINTL, RECT, Win32Error, format_winerror

__all__ = (
    "DisplayPath",
    "HdrState",
    "OUTPUT_TECHNOLOGY_NAMES",
    "get_hdr_state",
    "is_internal_output",
    "query_active_paths",
    "set_hdr",
)

_user32 = ctypes.WinDLL("user32")

ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
QDC_ONLY_ACTIVE_PATHS = 0x00000002

# DISPLAYCONFIG_DEVICE_INFO_TYPE
_GET_SOURCE_NAME = 1
_GET_TARGET_NAME = 2
_GET_ADVANCED_COLOR_INFO = 9
_SET_ADVANCED_COLOR_STATE = 10
_GET_ADVANCED_COLOR_INFO_2 = 15  # Windows 11 24H2+
_SET_HDR_STATE = 16  # Windows 11 24H2+

_MODE_IDX_INVALID = 0xFFFFFFFF
_ADVANCED_COLOR_MODE_HDR = 2

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY
_OUTPUT_INTERNAL = 0x80000000
OUTPUT_TECHNOLOGY_NAMES: dict[int, str] = {
    0: "VGA",
    4: "DVI",
    5: "HDMI",
    6: "LVDS",
    10: "DisplayPort",
    11: "eDP",
    12: "UDI",
    13: "UDI",
    15: "Miracast",
    16: "Wired (indirect)",
    17: "Virtual",
    18: "USB-C",
    _OUTPUT_INTERNAL: "Internal",
}
_INTERNAL_OUTPUTS = frozenset({6, 11, 13, _OUTPUT_INTERNAL})


def is_internal_output(output_technology: int) -> bool:
    return output_technology in _INTERNAL_OUTPUTS


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #


class _RATIONAL(ctypes.Structure):
    _fields_ = (("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32))


class _REGION2D(ctypes.Structure):
    _fields_ = (("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32))


class _VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = (
        ("pixelRate", ctypes.c_uint64),
        ("hSyncFreq", _RATIONAL),
        ("vSyncFreq", _RATIONAL),
        ("activeSize", _REGION2D),
        ("totalSize", _REGION2D),
        ("videoStandard", ctypes.c_uint32),
        ("scanLineOrdering", ctypes.c_uint32),
    )


class _SOURCE_MODE(ctypes.Structure):
    _fields_ = (
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelFormat", ctypes.c_uint32),
        ("position", POINTL),
    )


class _DESKTOP_IMAGE_INFO(ctypes.Structure):
    _fields_ = (
        ("PathSourceSize", POINTL),
        ("DesktopImageRegion", RECT),
        ("DesktopImageClip", RECT),
    )


class _MODE_UNION(ctypes.Union):
    _fields_ = (
        ("targetMode", _VIDEO_SIGNAL_INFO),
        ("sourceMode", _SOURCE_MODE),
        ("desktopImageInfo", _DESKTOP_IMAGE_INFO),
    )


class _MODE_INFO(ctypes.Structure):
    _fields_ = (
        ("infoType", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("adapterId", LUID),
        ("u", _MODE_UNION),
    )


class _PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = (
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("statusFlags", ctypes.c_uint32),
    )


class _PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = (
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", _RATIONAL),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wt.BOOL),
        ("statusFlags", ctypes.c_uint32),
    )


class _PATH_INFO(ctypes.Structure):
    _fields_ = (
        ("sourceInfo", _PATH_SOURCE_INFO),
        ("targetInfo", _PATH_TARGET_INFO),
        ("flags", ctypes.c_uint32),
    )


class _HEADER(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
    )


class _SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = (("header", _HEADER), ("viewGdiDeviceName", wt.WCHAR * 32))


class _TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = (
        ("header", _HEADER),
        ("flags", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", wt.WCHAR * 64),
        ("monitorDevicePath", wt.WCHAR * 128),
    )


class _ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = (
        ("header", _HEADER),
        ("value", ctypes.c_uint32),
        ("colorEncoding", ctypes.c_uint32),
        ("bitsPerColorChannel", ctypes.c_uint32),
    )


class _ADVANCED_COLOR_INFO_2(ctypes.Structure):
    _fields_ = (
        ("header", _HEADER),
        ("value", ctypes.c_uint32),
        ("colorEncoding", ctypes.c_uint32),
        ("bitsPerColorChannel", ctypes.c_uint32),
        ("activeColorMode", ctypes.c_uint32),
    )


class _SET_STATE(ctypes.Structure):
    """Shared layout of DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE / _SET_HDR_STATE."""

    _fields_ = (("header", _HEADER), ("value", ctypes.c_uint32))


# Catch layout mistakes at import time instead of corrupting memory later.
for _struct, _size in (
    (_MODE_INFO, 64),
    (_PATH_INFO, 72),
    (_HEADER, 20),
    (_SOURCE_DEVICE_NAME, 84),
    (_TARGET_DEVICE_NAME, 420),
    (_ADVANCED_COLOR_INFO, 32),
    (_ADVANCED_COLOR_INFO_2, 36),
    (_SET_STATE, 24),
):
    if ctypes.sizeof(_struct) != _size:
        raise ImportError(
            f"{_struct.__name__} is {ctypes.sizeof(_struct)} bytes, expected {_size}"
        )

_GetDisplayConfigBufferSizes = _user32.GetDisplayConfigBufferSizes
_GetDisplayConfigBufferSizes.argtypes = (
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint32),
)
_GetDisplayConfigBufferSizes.restype = wt.LONG

_QueryDisplayConfig = _user32.QueryDisplayConfig
_QueryDisplayConfig.argtypes = (
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(_PATH_INFO),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(_MODE_INFO),
    ctypes.c_void_p,
)
_QueryDisplayConfig.restype = wt.LONG

_DisplayConfigGetDeviceInfo = _user32.DisplayConfigGetDeviceInfo
_DisplayConfigGetDeviceInfo.argtypes = (ctypes.c_void_p,)
_DisplayConfigGetDeviceInfo.restype = wt.LONG

_DisplayConfigSetDeviceInfo = _user32.DisplayConfigSetDeviceInfo
_DisplayConfigSetDeviceInfo.argtypes = (ctypes.c_void_p,)
_DisplayConfigSetDeviceInfo.restype = wt.LONG


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DisplayPath:
    adapter_id: tuple[int, int]
    source_id: int
    target_id: int
    gdi_device_name: str
    """GDI name of the source, e.g. ``\\\\.\\DISPLAY1``. Matches MONITORINFOEX.szDevice."""
    friendly_name: str
    """Model name from the EDID, e.g. ``LG ULTRAGEAR``. May be empty."""
    device_path: str
    """Monitor device interface path, unique per connected monitor."""
    output_technology: int
    connector_instance: int
    width: int | None
    height: int | None
    refresh_hz: float | None


@dataclass(frozen=True)
class HdrState:
    supported: bool
    enabled: bool
    """HDR is switched on in Windows (it may still be inactive, see ``active``)."""
    active: bool
    limited_by_policy: bool
    """Windows is restricting advanced color, e.g. on battery saver."""
    bits_per_channel: int
    modern_api: bool
    """True when the Windows 11 24H2+ HDR-specific API is available."""


def _header(struct: ctypes.Structure, kind: int, adapter: LUID, ident: int) -> None:
    struct.header.type = kind
    struct.header.size = ctypes.sizeof(struct)
    struct.header.adapterId = adapter
    struct.header.id = ident


def query_active_paths() -> list[DisplayPath]:
    """Return one entry per active display path (i.e. per lit-up monitor)."""

    paths_arr = modes_arr = None
    n_paths = ctypes.c_uint32()
    n_modes = ctypes.c_uint32()

    # The topology can change between the two calls (a monitor waking up),
    # in which case Windows asks for bigger buffers: just try again.
    for _ in range(5):
        rc = _GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)
        )
        if rc != ERROR_SUCCESS:
            raise Win32Error(
                f"Could not read the display configuration: {format_winerror(rc)}", rc
            )
        paths_arr = (_PATH_INFO * max(n_paths.value, 1))()
        modes_arr = (_MODE_INFO * max(n_modes.value, 1))()
        rc = _QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(n_paths),
            paths_arr,
            ctypes.byref(n_modes),
            modes_arr,
            None,
        )
        if rc == ERROR_SUCCESS:
            break
        if rc != ERROR_INSUFFICIENT_BUFFER:
            raise Win32Error(
                f"Could not read the display configuration: {format_winerror(rc)}", rc
            )
    else:
        raise Win32Error("The display configuration kept changing, try again")

    assert paths_arr is not None and modes_arr is not None
    result: list[DisplayPath] = []
    for i in range(n_paths.value):
        path = paths_arr[i]
        src, tgt = path.sourceInfo, path.targetInfo

        source_name = _SOURCE_DEVICE_NAME()
        _header(source_name, _GET_SOURCE_NAME, src.adapterId, src.id)
        if _DisplayConfigGetDeviceInfo(ctypes.byref(source_name)) != ERROR_SUCCESS:
            continue  # path vanished while we were looking at it

        target_name = _TARGET_DEVICE_NAME()
        _header(target_name, _GET_TARGET_NAME, tgt.adapterId, tgt.id)
        if _DisplayConfigGetDeviceInfo(ctypes.byref(target_name)) != ERROR_SUCCESS:
            continue

        width = height = None
        idx = src.modeInfoIdx
        if idx != _MODE_IDX_INVALID and idx < n_modes.value:
            mode = modes_arr[idx]
            if mode.infoType == 1:  # DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE
                width = int(mode.u.sourceMode.width)
                height = int(mode.u.sourceMode.height)

        refresh = None
        if tgt.refreshRate.Denominator:
            refresh = tgt.refreshRate.Numerator / tgt.refreshRate.Denominator

        result.append(
            DisplayPath(
                adapter_id=tgt.adapterId.as_tuple(),
                source_id=int(src.id),
                target_id=int(tgt.id),
                gdi_device_name=source_name.viewGdiDeviceName,
                friendly_name=target_name.monitorFriendlyDeviceName.strip(),
                device_path=target_name.monitorDevicePath,
                output_technology=int(target_name.outputTechnology),
                connector_instance=int(target_name.connectorInstance),
                width=width,
                height=height,
                refresh_hz=refresh,
            )
        )
    return result


def get_hdr_state(adapter_id: tuple[int, int], target_id: int) -> HdrState | None:
    """Read the HDR state of a display target, or ``None`` if Windows won't say."""

    adapter = LUID.from_tuple(adapter_id)

    info2 = _ADVANCED_COLOR_INFO_2()
    _header(info2, _GET_ADVANCED_COLOR_INFO_2, adapter, target_id)
    if _DisplayConfigGetDeviceInfo(ctypes.byref(info2)) == ERROR_SUCCESS:
        v = info2.value
        return HdrState(
            supported=bool(v & (1 << 4)),
            enabled=bool(v & (1 << 5)),
            active=info2.activeColorMode == _ADVANCED_COLOR_MODE_HDR,
            limited_by_policy=bool(v & (1 << 3)),
            bits_per_channel=int(info2.bitsPerColorChannel),
            modern_api=True,
        )

    # Pre-24H2 fallback. "Advanced color" with wide color enforced is the
    # SDR wide-gamut mode of some laptop panels, not HDR.
    info = _ADVANCED_COLOR_INFO()
    _header(info, _GET_ADVANCED_COLOR_INFO, adapter, target_id)
    if _DisplayConfigGetDeviceInfo(ctypes.byref(info)) != ERROR_SUCCESS:
        return None
    v = info.value
    wide_color_enforced = bool(v & (1 << 2))
    enabled = bool(v & (1 << 1)) and not wide_color_enforced
    return HdrState(
        supported=bool(v & 1) and not wide_color_enforced,
        enabled=enabled,
        active=enabled,
        limited_by_policy=bool(v & (1 << 3)),
        bits_per_channel=int(info.bitsPerColorChannel),
        modern_api=False,
    )


def set_hdr(
    adapter_id: tuple[int, int], target_id: int, enable: bool, modern_api: bool
) -> None:
    state = _SET_STATE()
    kind = _SET_HDR_STATE if modern_api else _SET_ADVANCED_COLOR_STATE
    _header(state, kind, LUID.from_tuple(adapter_id), target_id)
    state.value = 1 if enable else 0
    rc = _DisplayConfigSetDeviceInfo(ctypes.byref(state))
    if rc != ERROR_SUCCESS:
        verb = "enable" if enable else "disable"
        raise Win32Error(f"Windows refused to {verb} HDR: {format_winerror(rc)}", rc)

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

__all__ = ("LUID", "POINTL", "RECT", "Win32Error", "format_winerror")


class Win32Error(Exception):
    """A Windows API call failed. ``message`` is meant to be shown to the user."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        if self.code is None:
            return self.message
        return f"{self.message} (error 0x{self.code & 0xFFFFFFFF:08X})"


def format_winerror(code: int) -> str:
    try:
        text = ctypes.FormatError(code).strip()
    except Exception:  # FormatError can fail for NTSTATUS-style codes
        text = ""
    return text or "Unknown error"


class LUID(ctypes.Structure):
    _fields_ = (("LowPart", wt.DWORD), ("HighPart", wt.LONG))

    def as_tuple(self) -> tuple[int, int]:
        return (int(self.LowPart), int(self.HighPart))

    @classmethod
    def from_tuple(cls, value: tuple[int, int]) -> LUID:
        return cls(value[0], value[1])


class POINTL(ctypes.Structure):
    _fields_ = (("x", wt.LONG), ("y", wt.LONG))


class RECT(ctypes.Structure):
    _fields_ = (
        ("left", wt.LONG),
        ("top", wt.LONG),
        ("right", wt.LONG),
        ("bottom", wt.LONG),
    )

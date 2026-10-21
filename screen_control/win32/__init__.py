"""Thin, dependency-free ctypes bindings for the Windows display APIs we need.

Everything in this package is blocking and must be called from the monitor
worker thread (see ``screen_control.monitors``), never from the asyncio loop.
"""

from .common import Win32Error

__all__ = ("Win32Error",)

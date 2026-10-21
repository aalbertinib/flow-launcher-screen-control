"""Show a big number on each screen for a few seconds ("which screen is which?").

Runs as its own short-lived process (see ``identify.py``) so a GDI problem can
never take the plugin down, and only uses the standard library because it is
started with the bare interpreter.

Usage: overlay.py <json>  where json is
    {"duration_ms": 3000, "screens": [{"device": "\\\\.\\DISPLAY1", "number": 1,
      "title": "FQ320", "detail": "Left · Primary", "color": "#3B82F6"}]}
"""

from __future__ import annotations

import ctypes
import json
import sys
import winreg
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2
SW_SHOWNOACTIVATE = 4
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
HTTRANSPARENT = -1
DT_CENTER = 0x1
DT_VCENTER = 0x4
DT_SINGLELINE = 0x20
DT_END_ELLIPSIS = 0x8000
TRANSPARENT = 1
CLEARTYPE_QUALITY = 5
FW_NORMAL = 400
FW_SEMIBOLD = 600
FW_BOLD = 700


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    )


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = (
        ("hdc", wt.HDC),
        ("fErase", wt.BOOL),
        ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL),
        ("fIncUpdate", wt.BOOL),
        ("rgbReserved", wt.BYTE * 32),
    )


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    )


def _sig(fn, argtypes, restype):  # noqa: ANN001, ANN202
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


GetModuleHandleW = _sig(kernel32.GetModuleHandleW, (wt.LPCWSTR,), wt.HMODULE)
RegisterClassExW = _sig(user32.RegisterClassExW, (ctypes.POINTER(WNDCLASSEXW),), wt.ATOM)
CreateWindowExW = _sig(
    user32.CreateWindowExW,
    (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID),
    wt.HWND,
)
DefWindowProcW = _sig(user32.DefWindowProcW, (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM), LRESULT)
DestroyWindow = _sig(user32.DestroyWindow, (wt.HWND,), wt.BOOL)
ShowWindow = _sig(user32.ShowWindow, (wt.HWND, ctypes.c_int), wt.BOOL)
UpdateWindow = _sig(user32.UpdateWindow, (wt.HWND,), wt.BOOL)
SetTimer = _sig(user32.SetTimer, (wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p), ctypes.c_size_t)
KillTimer = _sig(user32.KillTimer, (wt.HWND, ctypes.c_size_t), wt.BOOL)
GetMessageW = _sig(user32.GetMessageW, (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT), wt.BOOL)
TranslateMessage = _sig(user32.TranslateMessage, (ctypes.POINTER(wt.MSG),), wt.BOOL)
DispatchMessageW = _sig(user32.DispatchMessageW, (ctypes.POINTER(wt.MSG),), LRESULT)
PostQuitMessage = _sig(user32.PostQuitMessage, (ctypes.c_int,), None)
SetLayeredWindowAttributes = _sig(
    user32.SetLayeredWindowAttributes, (wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD), wt.BOOL
)
BeginPaint = _sig(user32.BeginPaint, (wt.HWND, ctypes.POINTER(PAINTSTRUCT)), wt.HDC)
EndPaint = _sig(user32.EndPaint, (wt.HWND, ctypes.POINTER(PAINTSTRUCT)), wt.BOOL)
GetClientRect = _sig(user32.GetClientRect, (wt.HWND, ctypes.POINTER(wt.RECT)), wt.BOOL)
FillRect = _sig(user32.FillRect, (wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH), ctypes.c_int)
DrawTextW = _sig(user32.DrawTextW, (wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT), ctypes.c_int)
EnumDisplayMonitors = _sig(
    user32.EnumDisplayMonitors, (wt.HDC, ctypes.POINTER(wt.RECT), MONITORENUMPROC, wt.LPARAM), wt.BOOL
)
GetMonitorInfoW = _sig(user32.GetMonitorInfoW, (wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW)), wt.BOOL)
CreateSolidBrush = _sig(gdi32.CreateSolidBrush, (wt.COLORREF,), wt.HBRUSH)
CreateFontW = _sig(
    gdi32.CreateFontW,
    (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD,
     wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR),
    wt.HFONT,
)
SelectObject = _sig(gdi32.SelectObject, (wt.HDC, wt.HGDIOBJ), wt.HGDIOBJ)
DeleteObject = _sig(gdi32.DeleteObject, (wt.HGDIOBJ,), wt.BOOL)
SetTextColor = _sig(gdi32.SetTextColor, (wt.HDC, wt.COLORREF), wt.COLORREF)
SetBkMode = _sig(gdi32.SetBkMode, (wt.HDC, ctypes.c_int), ctypes.c_int)
RoundRect = _sig(
    gdi32.RoundRect,
    (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int),
    wt.BOOL,
)
GetStockObject = _sig(gdi32.GetStockObject, (ctypes.c_int,), wt.HGDIOBJ)
FrameRect = _sig(user32.FrameRect, (wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH), ctypes.c_int)
NULL_PEN = 8


def windows_uses_light_theme() -> bool:
    """Windows' app colour mode (Settings > Personalisation > Colours)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return True  # Windows' default when the value is absent


def rgb(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 0x3B, 0x82, 0xF6
    return r | (g << 8) | (b << 16)  # COLORREF is 0x00BBGGRR


def enable_dpi_awareness() -> None:
    try:
        fn = _sig(user32.SetProcessDpiAwarenessContext, (ctypes.c_void_p,), wt.BOOL)
        if fn(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def monitor_rects() -> dict[str, tuple[int, int, int, int]]:
    rects: dict[str, tuple[int, int, int, int]] = {}

    def cb(hmon, _hdc, _rect, _lp):  # noqa: ANN001
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            rects[info.szDevice.upper()] = (r.left, r.top, r.right, r.bottom)
        return True

    proc = MONITORENUMPROC(cb)
    EnumDisplayMonitors(None, None, proc, 0)
    return rects


# Follow Windows light/dark mode. Text contrast: 16:1 (main) and >= 6.4:1 (details).
THEMES = {
    "dark": {"bg": "#202020", "text": "#FFFFFF", "muted": "#C8C8C8", "border": "#3D3D3D"},
    "light": {"bg": "#F9F9F9", "text": "#1B1B1B", "muted": "#5C5C5C", "border": "#C9C9C9"},
}


class Overlay:

    def __init__(self, screens: list[dict], duration_ms: int) -> None:
        self.screens_by_hwnd: dict[int, dict] = {}
        self.duration_ms = max(500, min(int(duration_ms), 15000))
        theme = THEMES["light" if windows_uses_light_theme() else "dark"]
        self.BG, self.TEXT, self.MUTED = rgb(theme["bg"]), rgb(theme["text"]), rgb(theme["muted"])
        self.border = CreateSolidBrush(rgb(theme["border"]))
        self.open_windows = 0
        self.brush = CreateSolidBrush(self.BG)
        self.fonts: list[int] = []
        self.wndproc = WNDPROC(self._wndproc)  # must outlive every window
        self.hinstance = GetModuleHandleW(None)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self.wndproc
        wc.hInstance = self.hinstance
        wc.lpszClassName = "FlowScreenControlIdentify"
        if not RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())

        rects = monitor_rects()
        for screen in screens:
            rect = rects.get(str(screen.get("device", "")).upper())
            if rect is not None:
                self._create(screen, rect)

    def _font(self, height: int, weight: int, face: str = "Segoe UI") -> int:
        font = CreateFontW(-height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, CLEARTYPE_QUALITY, 0, face)
        self.fonts.append(font)
        return font

    def _create(self, screen: dict, rect: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = rect
        mw, mh = right - left, bottom - top
        w = max(320, min(int(mw * 0.30), 900))
        h = int(w * 0.72)
        x = left + (mw - w) // 2
        y = top + (mh - h) // 2

        hwnd = CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            "FlowScreenControlIdentify",
            "Screen identification",
            WS_POPUP,
            x, y, w, h,
            None, None, self.hinstance, None,
        )
        if not hwnd:
            return
        screen = dict(screen)
        screen["_fonts"] = (
            self._font(int(h * 0.36), FW_BOLD),
            self._font(max(14, int(h * 0.085)), FW_SEMIBOLD),
            self._font(max(12, int(h * 0.058)), FW_NORMAL),
        )
        self.screens_by_hwnd[int(hwnd)] = screen
        self.open_windows += 1

        SetLayeredWindowAttributes(hwnd, 0, 250, LWA_ALPHA)  # nearly opaque: the desktop must not bleed into text
        self._round_corners(hwnd)
        ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        UpdateWindow(hwnd)
        SetTimer(hwnd, 1, self.duration_ms, None)

    @staticmethod
    def _round_corners(hwnd: int) -> None:
        try:
            dwmapi = ctypes.WinDLL("dwmapi")
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except (AttributeError, OSError):
            pass  # Windows 10: square corners

    def _paint(self, hwnd: int) -> None:
        ps = PAINTSTRUCT()
        hdc = BeginPaint(hwnd, ctypes.byref(ps))
        try:
            screen = self.screens_by_hwnd.get(int(hwnd))
            rc = wt.RECT()
            GetClientRect(hwnd, ctypes.byref(rc))
            FillRect(hdc, ctypes.byref(rc), self.brush)
            if not screen:
                return
            w, h = rc.right, rc.bottom
            big, medium, small = screen["_fonts"]
            SetBkMode(hdc, TRANSPARENT)

            FrameRect(hdc, ctypes.byref(rc), self.border)  # edge visible on any wallpaper

            # Number on a tile in the screen's colour: white digit, >= 4.5:1 on every
            # palette colour, same look as the Flow icon.
            tile = int(min(h * 0.50, w * 0.42))
            tx, ty = (w - tile) // 2, int(h * 0.07)
            accent = CreateSolidBrush(rgb(str(screen.get("color", ""))))
            old_brush = SelectObject(hdc, accent)
            old_pen = SelectObject(hdc, GetStockObject(NULL_PEN))
            RoundRect(hdc, tx, ty, tx + tile, ty + tile, tile // 4, tile // 4)
            SelectObject(hdc, old_pen)
            SelectObject(hdc, old_brush)
            DeleteObject(accent)

            pad = int(w * 0.05)
            lines = (
                (str(screen.get("number", "?")), big, rgb("#FFFFFF"), wt.RECT(tx, ty, tx + tile, ty + tile)),
                (str(screen.get("title", "")), medium, self.TEXT, wt.RECT(pad, int(h * 0.62), w - pad, int(h * 0.78))),
                (str(screen.get("detail", "")), small, self.MUTED, wt.RECT(pad, int(h * 0.78), w - pad, int(h * 0.93))),
            )
            for text, font, color, r in lines:
                old = SelectObject(hdc, font)
                SetTextColor(hdc, color)
                DrawTextW(hdc, text, -1, ctypes.byref(r), DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS)
                SelectObject(hdc, old)
        finally:
            EndPaint(hwnd, ctypes.byref(ps))

    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001, ANN202
        try:
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_TIMER:
                KillTimer(hwnd, wparam)
                DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.screens_by_hwnd.pop(int(hwnd), None)
                self.open_windows -= 1
                if self.open_windows <= 0:
                    PostQuitMessage(0)
                return 0
        except Exception:
            # Never let a Python exception unwind through a Win32 callback.
            PostQuitMessage(1)
            return 0
        return DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        if self.open_windows == 0:
            return 1
        msg = wt.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))
        for font in self.fonts:
            DeleteObject(font)
        DeleteObject(self.brush)
        DeleteObject(self.border)
        return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    try:
        payload = json.loads(argv[1])
        screens = [s for s in payload.get("screens", []) if isinstance(s, dict)]
        duration = int(payload.get("duration_ms", 3000))
    except (ValueError, TypeError, AttributeError):
        return 2
    enable_dpi_awareness()
    return Overlay(screens, duration).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

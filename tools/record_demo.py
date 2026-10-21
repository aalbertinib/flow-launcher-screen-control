"""Record a GIF of Flow Launcher driving ScreenControl.

Keystrokes are only sent while Flow is the foreground window, and frames come
from PrintWindow on Flow's own window (not a screen copy), so nothing else on
the desktop is captured.

Needs Pillow (`pip install pillow`, recording only) and the plugin's shortcut
(Ctrl+Alt+Shift+C). Don't touch the keyboard or mouse while it runs (~20 s).

    python tools/record_demo.py probe probe.png     # open Flow, grab one frame, close
    python tools/record_demo.py record docs/demo.gif
    python tools/record_demo.py stills docs/images
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

for fn, args, res in (
    (user32.GetForegroundWindow, (), wt.HWND),
    (user32.GetWindowThreadProcessId, (wt.HWND, ctypes.POINTER(wt.DWORD)), wt.DWORD),
    (user32.GetWindowRect, (wt.HWND, ctypes.POINTER(wt.RECT)), wt.BOOL),
    (user32.IsWindowVisible, (wt.HWND,), wt.BOOL),
    (user32.PrintWindow, (wt.HWND, wt.HDC, wt.UINT), wt.BOOL),
    (user32.GetDC, (wt.HWND,), wt.HDC),
    (user32.ReleaseDC, (wt.HWND, wt.HDC), ctypes.c_int),
    (gdi32.CreateCompatibleDC, (wt.HDC,), wt.HDC),
    (gdi32.CreateCompatibleBitmap, (wt.HDC, ctypes.c_int, ctypes.c_int), wt.HBITMAP),
    (gdi32.SelectObject, (wt.HDC, wt.HGDIOBJ), wt.HGDIOBJ),
    (gdi32.DeleteObject, (wt.HGDIOBJ,), wt.BOOL),
    (gdi32.DeleteDC, (wt.HDC,), wt.BOOL),
    (gdi32.GetDIBits, (wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT), ctypes.c_int),
):
    fn.argtypes, fn.restype = args, res

ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def flow_pids() -> set[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-c", "(Get-Process Flow.Launcher -ErrorAction SilentlyContinue).Id"],
        capture_output=True, text=True,
    ).stdout
    return {int(x) for x in out.split() if x.isdigit()}


PIDS = flow_pids()


def pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def flow_window() -> int | None:
    """Largest visible top-level window belonging to Flow."""
    best: list[tuple[int, int]] = []

    def cb(hwnd, _lp):  # noqa: ANN001
        if user32.IsWindowVisible(hwnd) and pid_of(hwnd) in PIDS:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > 0:
                best.append((area, hwnd))
        return True

    user32.EnumWindows(ENUMPROC(cb), 0)
    return max(best)[1] if best else None


def flow_in_front() -> bool:
    fg = user32.GetForegroundWindow()
    return bool(fg) and pid_of(fg) in PIDS


# ---------------------------------------------------------------- keyboard

class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t))


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t))


class _U(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wt.DWORD), ("u", _U))


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
KEYUP, UNICODE = 0x2, 0x4
VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "enter": 0x0D, "esc": 0x1B, "down": 0x28, "a": 0x41, "c": 0x43}


def _send(*events: INPUT) -> None:
    arr = (INPUT * len(events))(*events)
    if user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) != len(events):
        raise OSError(ctypes.get_last_error())


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(1, _U(ki=KEYBDINPUT(vk, 0, KEYUP if up else 0, 0, 0)))


def chord(*names: str) -> None:
    keys = [VK[n] for n in names]
    _send(*[_key(k) for k in keys], *[_key(k, True) for k in reversed(keys)])


def type_text(text: str, delay: float = 0.07) -> None:
    for ch in text:
        require_flow()
        code = ord(ch)
        _send(INPUT(1, _U(ki=KEYBDINPUT(0, code, UNICODE, 0, 0))), INPUT(1, _U(ki=KEYBDINPUT(0, code, UNICODE | KEYUP, 0, 0))))
        time.sleep(delay)


def require_flow() -> None:
    if not flow_in_front():
        raise SystemExit("Flow is not the foreground window: stopped without sending more keys")


def set_query(text: str) -> None:
    require_flow()
    chord("ctrl", "a")
    time.sleep(0.08)
    type_text(text, delay=0.012)


# ----------------------------------------------------------------- capture

def grab(hwnd: int) -> Image.Image | None:
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT: the window itself, not the screen
        header = (ctypes.c_byte * 40)()
        ctypes.memmove(header, (40).to_bytes(4, "little") + w.to_bytes(4, "little", signed=True)
                       + (-h).to_bytes(4, "little", signed=True) + (1).to_bytes(2, "little")
                       + (32).to_bytes(2, "little"), 16)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, header, 0)
        return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def open_flow() -> int:
    chord("ctrl", "alt", "shift", "c")  # the plugin's shortcut: opens Flow on "sc "
    for _ in range(30):
        time.sleep(0.1)
        hwnd = flow_window()
        if hwnd and flow_in_front():
            return hwnd
    raise SystemExit("Flow did not open on the shortcut: nothing typed")


def probe(out: str) -> None:
    hwnd = open_flow()
    time.sleep(1.0)
    img = grab(hwnd)
    chord("esc")
    if img is None:
        raise SystemExit("capture failed")
    img.save(out)
    print("frame", img.size)


def record(out: str) -> None:
    hwnd = open_flow()
    time.sleep(0.9)  # skip Flow's own keyword list shown for an instant on open
    frames: list[tuple[float, Image.Image]] = []
    recording = True

    def capture() -> None:
        while recording:
            t = time.perf_counter()
            img = grab(hwnd)
            if img is not None:
                frames.append((t, img))
            time.sleep(max(0.0, 0.1 - (time.perf_counter() - t)))

    th = threading.Thread(target=capture)
    th.start()
    try:
        time.sleep(1.5)                      # overview: screens + command menu
        set_query("sc 50"); time.sleep(2.0)  # set brightness: all screens or one
        set_query("sc c 40"); time.sleep(2.0)  # compare screens before picking one
        set_query("sc b"); time.sleep(1.2)
        require_flow(); chord("down"); time.sleep(0.4)
        require_flow(); chord("shift", "enter"); time.sleep(2.2)  # quick actions
        require_flow(); chord("esc"); time.sleep(0.5)
        set_query("sc hdr"); time.sleep(2.0)  # Windows HDR switch
        set_query("sc night"); time.sleep(2.0)  # profiles
        set_query("sc ?"); time.sleep(2.2)  # every command
    finally:
        recording = False
        th.join()
        if flow_in_front():
            chord("esc")

    # Same size for every frame (the window grows and shrinks with the list).
    W = max(f.width for _, f in frames)
    H = max(f.height for _, f in frames)
    tallest = max((f for _, f in frames), key=lambda f: f.height)
    bg = tallest.getpixel((tallest.width // 2, tallest.height - 12))
    canvas = []
    for _, f in frames:
        c = Image.new("RGB", (W, H), bg)
        f = f.copy()
        # PrintWindow leaves see-through parts black: paint them with the background.
        px = f.load()
        for y in range(f.height):
            for x in (0, 1, f.width - 2, f.width - 1):
                if px[x, y] == (0, 0, 0):
                    px[x, y] = bg
        c.paste(f, ((W - f.width) // 2, 0))
        canvas.append(c)
    scale = 640 / W
    canvas = [c.resize((640, int(H * scale)), Image.LANCZOS) for c in canvas]
    pal = [c.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for c in canvas]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=100, loop=0, optimize=True)
    print(f"{len(pal)} frames, {W}x{H} -> 640x{int(H * scale)}")


def stills(out_dir: str) -> None:
    """README screenshots: one PNG per view, from Flow's window only."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    hwnd = open_flow()
    time.sleep(1.2)

    def shot(name: str, wait: float = 1.0) -> None:
        time.sleep(wait)
        img = grab(hwnd)
        if img is not None:
            img.save(out / f"{name}.png", optimize=True)
            print("saved", name, img.size)

    try:
        shot("screens", 0.3)
        set_query("sc c 40"); shot("set-contrast")
        set_query("sc b"); time.sleep(0.8)
        require_flow(); chord("down"); time.sleep(0.3)
        require_flow(); chord("shift", "enter"); shot("quick-actions", 1.2)
        require_flow(); chord("esc"); time.sleep(0.4)
        set_query("sc hdr"); shot("hdr")
        set_query("sc profile"); shot("profiles")
        set_query("sc ?"); shot("help")
    finally:
        if flow_in_front():
            chord("esc")
    identify_still(out / "identify.png")


def identify_still(path) -> None:  # noqa: ANN001
    """The on-screen number, captured within the overlay's own rectangle only."""
    import json
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(root, "screen_control"))
    from screen_control.screens import _Hardware  # noqa: PLC0415
    from screen_control import identify  # noqa: PLC0415
    import overlay  # noqa: PLC0415

    screen = [s for s in _Hardware().topology() if s.number == 1][0]
    payload = {"duration_ms": 2500, "screens": [{
        "device": screen.gdi_device_name, "number": 1, "title": screen.name,
        "detail": " · ".join(screen.tags), "color": identify.screen_color(1)}]}
    th = threading.Thread(target=overlay.main, args=(["overlay", json.dumps(payload)],))
    th.start()
    time.sleep(1.2)
    left, top, right, bottom = screen.rect
    mw, mh = right - left, bottom - top
    w = max(320, min(int(mw * 0.30), 900)); h = int(w * 0.72)  # same geometry as overlay.py
    x, y = left + (mw - w) // 2, top + (mh - h) // 2
    sdc = user32.GetDC(None); mem = gdi32.CreateCompatibleDC(sdc); bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD)
    gdi32.BitBlt(mem, 0, 0, w, h, sdc, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
    header = (ctypes.c_byte * 40)()
    ctypes.memmove(header, (40).to_bytes(4, "little") + w.to_bytes(4, "little", signed=True)
                   + (-h).to_bytes(4, "little", signed=True) + (1).to_bytes(2, "little") + (32).to_bytes(2, "little"), 16)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, header, 0)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mem); user32.ReleaseDC(None, sdc)
    th.join()
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    # The overlay is 98% opaque: flatten its flat colours so nothing of the desktop
    # behind it (faint text...) survives in a published image.
    flat = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in ("#202020", identify.screen_color(1))]
    px = img.load()
    for yy in range(img.height):
        for xx in range(img.width):
            c = px[xx, yy]
            for f in flat:
                if max(abs(c[0] - f[0]), abs(c[1] - f[1]), abs(c[2] - f[2])) <= 18:
                    px[xx, yy] = f
                    break
    img.thumbnail((480, 480))
    img.save(path, optimize=True)
    print("saved identify", img.size)


if __name__ == "__main__":
    if not PIDS:
        raise SystemExit("Flow Launcher is not running")
    {"probe": probe, "record": record, "stills": stills}[sys.argv[1]](sys.argv[2])

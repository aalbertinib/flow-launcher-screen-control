"""Launch the identification overlay (``overlay.py``) in its own process."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from .screens import Screen

__all__ = ("SCREEN_COLORS", "icon_for", "identify", "screen_color")

log = logging.getLogger(__name__)

# One colour per screen number, shared by the overlay and the Flow icons
# (assets/screen_N.png) so a screen can be matched at a glance. Each passes
# 4.5:1 contrast with its white digit (WCAG AA) and 3:1 against light and dark backgrounds.
SCREEN_COLORS = (
    "#2563EB",  # 1 blue
    "#B45309",  # 2 amber
    "#047857",  # 3 green
    "#DC2626",  # 4 red
    "#7C3AED",  # 5 violet
    "#BE185D",  # 6 pink
    "#0E7490",  # 7 cyan
    "#4D7C0F",  # 8 lime
    "#C2410C",  # 9 orange
)
MAX_NUMBERED_ICON = len(SCREEN_COLORS)

_OVERLAY_SCRIPT = Path(__file__).with_name("overlay.py")
_CREATE_NO_WINDOW = 0x08000000
_process: subprocess.Popen[bytes] | None = None


def screen_color(number: int) -> str:
    return SCREEN_COLORS[(number - 1) % len(SCREEN_COLORS)]


def icon_for(screen: Screen) -> str:
    if 1 <= screen.number <= MAX_NUMBERED_ICON:
        return f"assets/screen_{screen.number}.png"
    return "assets/screen.png"


def _detail(screen: Screen) -> str:
    return " · ".join(screen.tags)


def identify(screens: Iterable[Screen], duration_ms: int = 3000) -> None:
    """Flash each screen's number on the screen itself. Never raises."""

    global _process

    payload = {
        "duration_ms": duration_ms,
        "screens": [
            {
                "device": s.gdi_device_name,
                "number": s.number,
                "title": f"{s.nickname} ({s.name})" if s.nickname else s.name,
                "detail": _detail(s),
                "color": screen_color(s.number),
            }
            for s in screens
        ],
    }
    if not payload["screens"]:
        return

    # A new request replaces the previous overlay instead of stacking on it.
    if _process is not None and _process.poll() is None:
        try:
            _process.kill()
        except OSError:
            pass

    try:
        _process = subprocess.Popen(
            [sys.executable, str(_OVERLAY_SCRIPT), json.dumps(payload)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError:
        log.exception("Could not start the identification overlay")

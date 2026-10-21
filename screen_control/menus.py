"""Quick-action submenus (Flow's context menu, opened with Shift+Enter)."""

from __future__ import annotations

from flogin import Result

from .results import (
    ICON_ERROR,
    ICON_HDR,
    ICON_IDENTIFY,
    HdrResult,
    IdentifyResult,
    InfoResult,
    LevelResult,
    feature_icon,
    finalize,
    level_bar,
)
from .screens import Feature, Screen

__all__ = ("PRESETS", "quick_actions")

PRESETS = (0, 25, 50, 75, 100)
_TOP_SCORE = 1_000_000
_SCORE_STEP = 10_000


def quick_actions(screens: list[Screen]) -> list[Result]:
    """Presets and toggles for one screen, or for several at once."""

    rows: list[Result] = []
    multi = len(screens) > 1
    scope = f"all {len(screens)} screens" if multi else screens[0].title

    for feature in Feature:
        usable = [s for s in screens if s.level(feature).ok]
        if not usable:
            reason = screens[0].level(feature).error or "Not available"
            rows.append(InfoResult(title=f"{feature.label} unavailable — {scope}", sub=reason, icon=ICON_ERROR))
            continue
        values = {s.level(feature).value for s in usable}
        if multi:
            now = "Now " + " · ".join(f"{s.number}: {s.level(feature).value}%" for s in usable)
        else:
            now = f"Now {usable[0].level(feature).value}%"
        for preset in PRESETS:
            current = values == {preset}
            rows.append(
                LevelResult(
                    usable,
                    feature,
                    value=preset,
                    title=f"{feature.label} {preset}% — {scope}" + ("   (current)" if current else ""),
                    sub=now,
                    icon=feature_icon(feature),
                    progress_bar=level_bar(feature, preset),
                )
            )

    capable = [s for s in screens if s.hdr and s.hdr.supported]
    if capable:
        if multi:
            want = not any(s.hdr and s.hdr.enabled for s in capable)
            now = "Now " + " · ".join(f"{s.number}: {'on' if s.hdr and s.hdr.enabled else 'off'}" for s in capable)
            rows.append(
                HdrResult(capable, want, title=f"Turn HDR {'on' if want else 'off'} — {scope}", sub=now, icon=ICON_HDR)
            )
        else:
            s = capable[0]
            enabled = bool(s.hdr and s.hdr.enabled)
            rows.append(
                HdrResult(
                    [s],
                    None,
                    title=f"Turn HDR {'off' if enabled else 'on'} — {s.title}",
                    sub=f"HDR is {'on' if enabled else 'off'}",
                    icon=ICON_HDR,
                )
            )

    rows.append(
        IdentifyResult(
            screens,
            title=f"Identify {scope}",
            sub="Show the screen number on the screen itself for 3 seconds",
            icon=ICON_IDENTIFY,
        )
    )

    for i, row in enumerate(rows):
        row.score = _TOP_SCORE - i * _SCORE_STEP
    return finalize(rows)

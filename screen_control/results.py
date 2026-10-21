"""Flow results and what happens when one is picked."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Unpack

from flogin import ExecuteResponse, Glyph, ProgressBar, Result, ResultConstructorKwargs

from . import identify
from .screens import Feature, Screen
from .win32 import Win32Error

if TYPE_CHECKING:
    from .plugin import ScreenControlPlugin

__all__ = (
    "ChangeQueryResult",
    "HdrResult",
    "IdentifyResult",
    "InfoResult",
    "LevelResult",
    "level_bar",
)

log = logging.getLogger(__name__)

ICON_APP = "assets/app.png"
ICON_ERROR = "assets/error.png"
ICON_BRIGHTNESS = "assets/brightness.png"
ICON_CONTRAST = "assets/contrast.png"
ICON_HDR = "assets/hdr.png"
ICON_IDENTIFY = "assets/identify.png"
ICON_ALL = "assets/screens.png"

FLOW_CALL_TIMEOUT = 3.0
"""Calls back into Flow must never keep an action from answering."""

# The value is written over the bar in the theme's text colour (white or near
# black), so each colour keeps >= 3.6:1 with both, and with both backgrounds.
_BAR_COLORS = {Feature.BRIGHTNESS: "#C2620A", Feature.CONTRAST: "#71809A"}

# Flow's built-in icon font (works without the font installed, e.g. Windows 10).
# Glyphs follow the Flow theme colour and are shown when "Use Segoe Fluent Icons"
# is on; screen rows keep their coloured number icons (they match the overlay).
GLYPH_FONT = "/Resources/#Segoe Fluent Icons"
_GLYPHS = {
    ICON_BRIGHTNESS: "",  # sun
    ICON_CONTRAST: "",  # half sun
    ICON_HDR: "",  # sun in a frame
    ICON_IDENTIFY: "",  # monitor
    ICON_APP: "",  # info
}
SUBTITLE_FIT = 72
"""Characters that fit in a subtitle at Flow's default width; longer ones get a tooltip."""


def finalize(results: list[Result]) -> list[Result]:
    """Readability pass applied to every list sent to Flow.

    - Flow draws a result's progress bar in the title row, under the title text.
      Long titles over a bar are hard to read, so rows with a bar show only the
      bar's value ("50%") there; the full title moves into the subtitle (and
      stays available as a tooltip).
    - Action rows get theme-aware glyphs.
    - Subtitles too long for the window keep their full text in a tooltip.
    """
    for r in results:
        if r.progress_bar is not None:
            value = f"{r.progress_bar.percentage}%"
            if r.title and r.title != value:
                r.title_tooltip = r.title_tooltip or r.title
                r.sub = f"{r.title}   ·   {r.sub}" if r.sub else r.title
            r.title = value
        if r.glyph is None and r.icon in _GLYPHS:
            r.glyph = Glyph(_GLYPHS[r.icon], GLYPH_FONT)
        if r.sub and len(r.sub) > SUBTITLE_FIT and not r.sub_tooltip:
            r.sub_tooltip = r.sub
    return results


def level_bar(feature: Feature, value: int) -> ProgressBar:
    return ProgressBar(max(0, min(100, value)), _BAR_COLORS[feature])


def feature_icon(feature: Feature) -> str:
    return ICON_BRIGHTNESS if feature is Feature.BRIGHTNESS else ICON_CONTRAST


class _PluginResult(Result["ScreenControlPlugin"]):
    """Base result: every failure becomes a readable message, never a crash."""

    menu_screens: list[Screen] | None = None
    """Screens the Shift+Enter quick-action submenu applies to (none: no submenu)."""

    def with_menu(self, screens: list[Screen]) -> _PluginResult:
        self.menu_screens = list(screens)
        return self

    async def context_menu(self) -> list[Result]:
        if not self.menu_screens:
            return []
        from .menus import quick_actions  # menus imports this module

        assert self.plugin is not None
        # Build from fresh values: the row may have been shown a while ago.
        try:
            current = {s.key: s for s in await self.plugin.service.screens()}
        except Win32Error:
            current = {}
        screens = [current.get(s.key, s) for s in self.menu_screens]
        return quick_actions(screens)

    async def on_context_menu_error(self, error: Exception) -> list[Result]:
        log.exception("Could not build the quick-action menu", exc_info=error)
        return [InfoResult(title="Quick actions unavailable", sub=str(error), icon=ICON_ERROR)]

    async def _notify(self, message: str) -> None:
        assert self.plugin is not None
        try:
            await asyncio.wait_for(self.plugin.api.show_notification("ScreenControl", message), FLOW_CALL_TIMEOUT)
        except Exception:
            log.exception("Could not show notification: %s", message)

    async def _requery(self, delay: float = 0.0) -> None:
        """Re-run the current query so the list shows the new values."""
        assert self.plugin is not None
        if delay:
            await asyncio.sleep(delay)
        query = self.plugin.last_query
        if query is None:
            return
        try:
            await asyncio.wait_for(self.plugin.api.change_query(query.raw_text, requery=True), FLOW_CALL_TIMEOUT)
        except Exception:
            log.exception("Could not refresh the query")

    async def on_error(self, error: Exception) -> ExecuteResponse:
        if isinstance(error, Win32Error):
            log.warning("Action failed: %s", error)
            await self._notify(error.message)
        else:
            log.exception("Unexpected error in result action", exc_info=error)
            await self._notify(f"Something went wrong: {error}")
        return ExecuteResponse(hide=False)


class InfoResult(_PluginResult):
    """A row that only displays information; picking it does nothing."""

    async def callback(self) -> ExecuteResponse:
        return ExecuteResponse(hide=False)


class ChangeQueryResult(_PluginResult):
    """Picking the row navigates to another query (and can flash screens)."""

    def __init__(
        self,
        new_query: str,
        flash: list[Screen] | None = None,
        **kwargs: Unpack[ResultConstructorKwargs],
    ) -> None:
        super().__init__(**kwargs)
        self.new_query = new_query
        self.flash = flash or []

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        if self.flash:
            identify.identify(self.flash, duration_ms=1500)
        try:
            await asyncio.wait_for(self.plugin.api.change_query(self.new_query, requery=True), FLOW_CALL_TIMEOUT)
        except Exception:
            log.exception("Could not change the query")
        return ExecuteResponse(hide=False)


class IdentifyResult(_PluginResult):
    def __init__(self, screens: list[Screen], **kwargs: Unpack[ResultConstructorKwargs]) -> None:
        super().__init__(**kwargs)
        self.screens = screens

    async def callback(self) -> ExecuteResponse:
        identify.identify(self.screens)
        return ExecuteResponse(hide=False)


class LevelResult(_PluginResult):
    def __init__(
        self,
        screens: list[Screen],
        feature: Feature,
        *,
        value: int | None = None,
        delta: int | None = None,
        **kwargs: Unpack[ResultConstructorKwargs],
    ) -> None:
        super().__init__(**kwargs)
        self.screens = screens
        self.feature = feature
        self.value = value
        self.delta = delta

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        results = await self.plugin.service.set_levels(
            [s.key for s in self.screens], self.feature, value=self.value, delta=self.delta
        )
        failures: list[str] = []
        for screen in self.screens:
            outcome = results.get(screen.key)
            if isinstance(outcome, Win32Error):
                log.warning("Setting %s on %s failed: %s", self.feature.value, screen.name, outcome)
                failures.append(f"{screen.title}: {outcome.message}")
        if failures:
            await self._notify(f"Could not change {self.feature.value}:\n" + "\n".join(failures))
        await self._requery()
        return ExecuteResponse(hide=False)


class HdrResult(_PluginResult):
    def __init__(
        self,
        screens: list[Screen],
        enable: bool | None,
        **kwargs: Unpack[ResultConstructorKwargs],
    ) -> None:
        super().__init__(**kwargs)
        self.screens = screens
        self.enable = enable

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        failures: list[str] = []
        for screen in self.screens:
            try:
                state = await self.plugin.service.set_hdr(screen.key, self.enable)
            except Win32Error as e:
                log.warning("HDR change on %s failed: %s", screen.name, e)
                failures.append(f"{screen.title}: {e.message}")
                continue
            wanted = (not (screen.hdr and screen.hdr.enabled)) if self.enable is None else self.enable
            if state.enabled != wanted:
                failures.append(f"{screen.title}: Windows did not apply the change")
        if failures:
            await self._notify("HDR change failed:\n" + "\n".join(failures))
        # The screen flickers and changes mode; give Flow a moment before refreshing.
        await self._requery(delay=0.5)
        return ExecuteResponse(hide=False)


class NameResult(_PluginResult):
    """Give a screen a nickname (``None``/empty removes it)."""

    def __init__(self, screen: Screen, nickname: str | None, **kwargs: Unpack[ResultConstructorKwargs]) -> None:
        super().__init__(**kwargs)
        self.screens = [screen]
        self.nickname = nickname

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        self.plugin.store.set_nickname(self.screens[0].stable_id, self.nickname)
        identify.identify(self.screens, duration_ms=1500)  # show it on the screen it applies to
        query = self.plugin.last_query
        keyword = query.keyword if query else "*"
        await self._change_query("" if keyword == "*" else f"{keyword} ")
        return ExecuteResponse(hide=False)

    async def _change_query(self, text: str) -> None:
        assert self.plugin is not None
        try:
            await asyncio.wait_for(self.plugin.api.change_query(text, requery=True), FLOW_CALL_TIMEOUT)
        except Exception:
            log.exception("Could not change the query")


def profile_plan(profile: dict, screens: list[Screen]) -> dict[str, dict]:
    """Settings to apply per screen key: its own entry, else the "*" entry."""
    plan = {}
    for s in screens:
        settings = profile.get(s.stable_id) or profile.get("*")
        if settings:
            plan[s.key] = {k: settings[k] for k in ("brightness", "contrast", "hdr") if settings.get(k) is not None}
    return {k: v for k, v in plan.items() if v}


class ProfileResult(_PluginResult):
    def __init__(
        self, name: str, profile: dict, screens: list[Screen], **kwargs: Unpack[ResultConstructorKwargs]
    ) -> None:
        super().__init__(**kwargs)
        self.name = name
        self.profile = profile
        self.screens = screens

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        plan = profile_plan(self.profile, self.screens)
        errors = await self.plugin.service.apply_settings(plan)
        if errors:
            by_key = {s.key: s.title for s in self.screens}
            lines = [f"{by_key.get(k, k)}: {'; '.join(v)}" for k, v in errors.items()]
            await self._notify(f"Profile '{self.name}' applied with errors:\n" + "\n".join(lines))
        await self._requery(delay=0.5 if any("hdr" in p for p in plan.values()) else 0.0)
        return ExecuteResponse(hide=False)


class SaveProfileResult(_PluginResult):
    """Store every screen's current brightness, contrast and HDR state."""

    def __init__(self, name: str, screens: list[Screen], **kwargs: Unpack[ResultConstructorKwargs]) -> None:
        super().__init__(**kwargs)
        self.name = name
        self.screens = screens

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        current = await self.plugin.service.screens(max_age=0)  # fresh values, not the cache
        settings: dict[str, dict] = {}
        for s in current:
            entry: dict = {}
            for feature in Feature:
                if s.level(feature).ok:
                    entry[feature.value] = s.level(feature).value
            if s.hdr and s.hdr.supported:
                entry["hdr"] = s.hdr.enabled
            if entry:
                settings[s.stable_id] = entry
        if not settings:
            await self._notify("Nothing to save: no screen reported its settings")
            return ExecuteResponse(hide=False)
        self.plugin.store.save_profile(self.name, settings)
        await self._notify(f"Saved the current settings of {len(settings)} screen(s) as '{self.name}'")
        await self._requery()
        return ExecuteResponse(hide=False)


class DeleteProfileResult(_PluginResult):
    def __init__(self, name: str, **kwargs: Unpack[ResultConstructorKwargs]) -> None:
        super().__init__(**kwargs)
        self.name = name

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        self.plugin.store.delete_profile(self.name)
        await self._requery()
        return ExecuteResponse(hide=False)


class ResetResult(_PluginResult):
    """Start over: fresh workers, monitor handles and caches."""

    async def callback(self) -> ExecuteResponse:
        assert self.plugin is not None
        self.plugin.service.reset()
        self.plugin.service.warm_up()
        query = self.plugin.last_query
        keyword = query.keyword if query else "*"
        try:
            await asyncio.wait_for(
                self.plugin.api.change_query("" if keyword == "*" else f"{keyword} ", requery=True), FLOW_CALL_TIMEOUT
            )
        except Exception:
            log.exception("Could not refresh after reset")
        return ExecuteResponse(hide=False)

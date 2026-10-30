"""Build the list of Flow results for a parsed query and a screen snapshot."""

from __future__ import annotations

from flogin import Result

from .feedback import Feedback
from .identify import icon_for
from .query_parser import Action, HdrMode, ParsedQuery
from .results import (
    ICON_ALL,
    ICON_APP,
    ICON_BRIGHTNESS,
    ICON_CONTRAST,
    ICON_ERROR,
    ICON_HDR,
    ICON_IDENTIFY,
    GLYPH_DONE,
    GLYPH_SETTINGS,
    GLYPH_UNDO,
    ChangeQueryResult,
    ResetResult,
    SettingsResult,
    UndoResult,
    HdrResult,
    IdentifyResult,
    InfoResult,
    LevelResult,
    feature_icon,
    finalize,
    level_bar,
    _PluginResult,
)
from .screens import Feature, Screen
from .store import DEFAULT_PROFILES
from .views_extra import active_profiles, apply_summary, matching_names, name_view, profile_view

__all__ = ("build_results", "error_result")

_TOP_SCORE = 1_000_000
_SCORE_STEP = 10_000  # wide gaps so Flow's "frequently used" boost can't reorder rows


def error_result(title: str, sub: str = "") -> Result:
    return InfoResult(title=title, sub=sub, icon=ICON_ERROR)


def reset_result(title: str, sub: str = "") -> Result:
    """An error row whose Enter resets ScreenControl (for failures the user can't fix by typing)."""
    return finalize([ResetResult(title=title, sub=f"{sub} · Enter: reset ScreenControl" if sub else "Enter: reset ScreenControl", icon=ICON_ERROR)])[0]


def _status(s: Screen) -> str:
    def pct(level) -> str:  # noqa: ANN001
        return f"{level.value}%" if level.ok else "n/a"

    parts = [f"☀ {pct(s.brightness)}", f"◑ {pct(s.contrast)}"]
    if s.hdr and s.hdr.supported:
        parts.append("HDR on" if s.hdr.enabled else "HDR off")
    return "   ".join(parts)


def _hdr_label(s: Screen) -> str:
    if s.hdr is None:
        return "HDR state unknown"
    if not s.hdr.supported:
        return "HDR not supported"
    text = "HDR is on" if s.hdr.enabled else "HDR is off"
    if s.hdr.enabled and not s.hdr.active:
        text += " (not active right now)"
    if s.hdr.limited_by_policy:
        text += " (limited by Windows power settings)"
    return text


def _details(s: Screen) -> str:
    lines = [
        f"Screen {s.number}: {s.name}",
        f"Vendor: {s.vendor or 'unknown'}" + (f" · Serial: {s.serial}" if s.serial else ""),
        " · ".join(s.tags),
        f"Brightness: {s.brightness.value if s.brightness.ok else s.brightness.error}",
        f"Contrast: {s.contrast.value if s.contrast.ok else s.contrast.error}",
        _hdr_label(s),
        f"Windows: {s.gdi_device_name} · {s.device_path}",
    ]
    return "\n".join(lines)


class _Builder:
    def __init__(self, keyword: str, results: list[Result] | None = None) -> None:
        self.keyword = keyword
        self.results: list[Result] = [] if results is None else results

    def q(self, text: str) -> str:
        """Full query text for Flow's search box (keyword included)."""
        return text if self.keyword in ("*", "") else f"{self.keyword} {text}"

    def add(self, result: Result) -> None:
        result.score = _TOP_SCORE - len(self.results) * _SCORE_STEP
        self.results.append(result)

    def error(self, title: str, sub: str = "") -> None:
        self.add(error_result(title, sub))


# Command words a half-typed word may be heading to (shown instead of an error),
# after the profiles it may be heading to.
_COMPLETIONS = (
    ("profile ", "List, apply or save profiles"),
    ("name ", "Give a screen a nickname"),
    ("settings", "Open the settings panel: profiles, nicknames, values"),
    ("reset", "Restart monitor communication and clear caches"),
    ("help", "Every command with examples"),
)


def _suggest(b: _Builder, partial: str, profiles: dict[str, dict], screens: list[Screen]) -> bool:
    names = [n for n in profiles if n.startswith(partial) and n != partial]
    for n in names:
        b.add(
            ChangeQueryResult(
                b.q(n),
                title=f"sc {n}",
                sub=f"Profile · {apply_summary(profiles[n], screens)} · Enter or Tab to complete",
                icon=ICON_ALL,
                auto_complete_text=b.q(n),
            )
        )
    matches = [(word, what) for word, what in _COMPLETIONS if word.startswith(partial) and word.strip() != partial]
    for word, what in matches:
        b.add(ChangeQueryResult(b.q(word), title=f"sc {word.strip()}", sub=f"{what} · Enter or Tab to complete",
                                icon=ICON_APP, auto_complete_text=b.q(word)))
    return bool(names or matches)


def _feedback(b: _Builder, feedback: Feedback) -> None:
    """The banner reporting the last action, on the list it was picked from."""
    if feedback.can_undo:
        sub = f"{feedback.sub} · Enter: undo" if feedback.sub else "Enter: undo"
        b.add(UndoResult(title=f"✓ {feedback.title}", sub=sub, icon=ICON_APP, glyph=GLYPH_UNDO,
                         title_tooltip="Enter: undo this change"))
    else:
        b.add(InfoResult(title=f"✓ {feedback.title}", sub=feedback.sub, icon=ICON_APP, glyph=GLYPH_DONE))


def _select(
    parsed: ParsedQuery, screens: list[Screen], b: _Builder, profiles: dict[str, dict]
) -> list[Screen] | None:
    selected = screens
    if parsed.screen_number is not None:
        selected = [s for s in selected if s.number == parsed.screen_number]
        if not selected:
            numbers = ", ".join(f"#{s.number}" for s in screens) or "none"
            b.error(f"There is no screen #{parsed.screen_number}", f"Connected screens: {numbers}")
            return None
    for word in parsed.filters:
        selected = [s for s in selected if word in s.search_text]
    if not selected:
        if _suggest(b, " ".join(parsed.filters), profiles, screens):
            return None  # a command word or profile name being typed, not a screen name
        names = " · ".join(s.title for s in screens)
        b.error(f"No screen matches “{' '.join(parsed.filters)}”", f"Connected: {names}")
        return None
    return selected


def build_results(parsed: ParsedQuery, screens: list[Screen], keyword: str, *args, **kwargs) -> list[Result]:
    results = _build_results(parsed, screens, keyword, *args, **kwargs)
    if parsed.shortened:
        _also_screens(_Builder(keyword, results), parsed.shortened, screens)
    return finalize(results)


def _also_screens(b: _Builder, typed: str, screens: list[Screen]) -> None:
    """"sc r" means reset, but may have been heading to the right screen: keep it one row away."""
    for s in screens:
        if any(word.startswith(typed) for word in s.search_text.split()):
            b.add(
                ChangeQueryResult(
                    b.q(f"#{s.number} "),
                    title=s.title,
                    sub=f"{_status(s)}   —   {' · '.join(s.tags)}",
                    icon=icon_for(s),
                    auto_complete_text=b.q(f"#{s.number} "),
                ).with_menu([s])
            )


def _build_results(
    parsed: ParsedQuery,
    screens: list[Screen],
    keyword: str,
    profiles: dict[str, dict] | None = None,
    customized: frozenset[str] = frozenset(),
    feedback: Feedback | None = None,
) -> list[Result]:
    b = _Builder(keyword)
    if not screens:
        b.error("No screens found", "Windows did not report any active display")
        return b.results
    if profiles is None:
        profiles = DEFAULT_PROFILES
    if feedback is not None:
        _feedback(b, feedback)
    if parsed.error:
        b.error(parsed.error, "Examples: 50 · +10 · c 70 · hdr on · #2 80 · id")
        return b.results

    if parsed.action is Action.HELP:
        _help(b)
        return b.results
    if parsed.action is Action.RESET:
        b.add(
            ResetResult(
                title="Reset ScreenControl",
                sub="Restarts monitor communication, clears caches and retries screens marked unresponsive",
                icon=ICON_APP,
            )
        )
        return b.results
    if parsed.action is Action.NAME:
        name_view(b, parsed, screens)
        return b.results
    if parsed.action is Action.SETTINGS:
        b.add(_settings_result(b))
        return b.results
    if parsed.action is Action.PROFILE:
        profile_view(b, parsed, screens, profiles, customized)
        return b.results

    selected = _select(parsed, screens, b, profiles)
    if selected is None:
        return b.results

    if parsed.action is Action.IDENTIFY:
        _identify(b, selected, screens)
    elif parsed.action is Action.LEVEL:
        assert parsed.feature is not None
        _level(b, parsed, selected)
    elif parsed.action is Action.HDR:
        _hdr(b, parsed, selected)
    elif parsed.has_selector and len(selected) == 1:
        _detail(b, selected[0])
    else:
        _overview(b, parsed, selected, profiles)
    if parsed.action is Action.OVERVIEW and parsed.filters:
        _suggest(b, " ".join(parsed.filters), profiles, screens)  # "sc n": several commands fit

    # Every row acting on screens gets the Shift+Enter quick-action submenu.
    for result in b.results:
        if isinstance(result, _PluginResult) and result.menu_screens is None:
            targets = getattr(result, "screens", None)
            if targets:
                result.with_menu(targets)
    return b.results


def _overview(b: _Builder, parsed: ParsedQuery, selected: list[Screen], profiles: dict[str, dict]) -> None:
    for s in selected:
        b.add(
            ChangeQueryResult(
                b.q(f"#{s.number} "),
                flash=[s],
                title=s.title,
                sub=f"{_status(s)}   —   {' · '.join(s.tags)}",
                icon=icon_for(s),
                auto_complete_text=b.q(f"#{s.number} "),
                title_tooltip="Enter: this screen's settings · Shift+Enter: quick actions · Ctrl+C: copy details",
                copy_text=_details(s),
            ).with_menu([s])
        )
    if not parsed.has_selector:
        b.add(_settings_result(b, sub="Edit profiles screen by screen, nicknames and values in a window"))
    b.add(
        IdentifyResult(
            selected,
            title=f"Identify — all {len(selected)} screens" if len(selected) > 1 else f"Identify {selected[0].title}",
            sub="Show each screen's number on the screen itself",
            icon=ICON_IDENTIFY,
            auto_complete_text=b.q("identify"),
        )
    )
    if not parsed.has_selector:
        menu = (
            ("brightness ", "Brightness", "Set all screens or one, exact or relative · sc 50 · sc +10 · sc #2 30", ICON_BRIGHTNESS),
            ("contrast ", "Contrast", "Set all screens or one, exact or relative · sc c 70 · sc c -10", ICON_CONTRAST),
            ("hdr ", "HDR", f"{_windows_hdr_state(selected)} · Windows HDR or one screen · sc hdr · sc hdr off", ICON_HDR),
            ("profile ", "Profiles", _profiles_hint(profiles, selected), ICON_ALL),
            ("name ", "Nicknames", "Give screens your own names · sc name #2 Gaming", ICON_IDENTIFY),
            ("? ", "All commands", "Every command with examples · tip: Shift+Enter on a screen for quick actions", ICON_APP),
        )
        for text, title, sub_text, icon in menu:
            b.add(ChangeQueryResult(b.q(text), title=title, sub=sub_text, icon=icon, auto_complete_text=b.q(text)))


def _settings_result(b: _Builder, title: str = "Open the settings panel", sub: str = "") -> Result:
    return SettingsResult(
        title=title,
        sub=sub or "Profiles screen by screen with live preview, nicknames, brightness, contrast and HDR",
        icon=ICON_APP,
        glyph=GLYPH_SETTINGS,
        auto_complete_text=b.q("settings"),
    )


def _profiles_hint(profiles: dict[str, dict], screens: list[Screen]) -> str:
    active = active_profiles(profiles, screens)
    names = ", ".join(profiles)
    if active:
        return f"Active: {', '.join(active)} · {names} · brightness, contrast and HDR in one go"
    return f"{names} · brightness, contrast and HDR in one go"


def _windows_hdr_state(screens: list[Screen]) -> str:
    supported = [s for s in screens if s.hdr and s.hdr.supported]
    if not supported:
        return "No HDR screen"
    on = sum(1 for s in supported if s.hdr and s.hdr.enabled)
    if on == 0:
        return "Windows HDR is off"
    if on == len(supported):
        return "Windows HDR is on"
    return f"HDR on {on} of {len(supported)} screens"


def _level_rows(b: _Builder, s: Screen, feature: Feature) -> None:
    level = s.level(feature)
    word = feature.value
    if not level.ok:
        b.error(f"{feature.label} unavailable on {s.title}", level.error or "")
        return
    assert level.value is not None
    b.add(
        ChangeQueryResult(
            b.q(f"#{s.number} {word} "),
            title=f"{feature.label} {level.value}%",
            sub=f"{s.title}   —   type a value: 70, +10, -10 · Shift+Enter: presets",
            icon=feature_icon(feature),
            progress_bar=level_bar(feature, level.value),
            auto_complete_text=b.q(f"#{s.number} {word} "),
        ).with_menu([s])
    )


def _detail(b: _Builder, s: Screen) -> None:
    for feature in Feature:
        _level_rows(b, s, feature)

    if s.hdr and s.hdr.supported:
        action = "off" if s.hdr.enabled else "on"
        b.add(
            HdrResult(
                [s],
                None,
                title=f"{_hdr_label(s)} — press Enter to turn it {action}",
                sub=f"{s.title} · {s.hdr.bits_per_channel}-bit colour",
                icon=ICON_HDR,
                auto_complete_text=b.q(f"#{s.number} hdr "),
            )
        )
    else:
        b.add(InfoResult(title=_hdr_label(s), sub=s.title, icon=ICON_HDR))

    b.add(
        IdentifyResult(
            [s],
            title=f"Identify {s.title}",
            sub=" · ".join(s.tags + ([s.serial] if s.serial else [])),
            icon=icon_for(s),
        )
    )


def _level(b: _Builder, parsed: ParsedQuery, selected: list[Screen]) -> None:
    feature = parsed.feature
    assert feature is not None
    if not parsed.has_level_change:
        readable = [s for s in selected if s.level(feature).ok]
        if len(readable) > 1:
            prefix = " ".join([*parsed.filters, feature.value])
            now = " · ".join(f"{s.number}: {s.level(feature).value}%" for s in readable)
            b.add(
                ChangeQueryResult(
                    b.q(f"{prefix} "),
                    title=f"{feature.label} — all {len(readable)} screens",
                    sub=f"Now: {now}   —   type a value: 70, +10, -10 · Shift+Enter: presets",
                    icon=ICON_ALL,
                    auto_complete_text=b.q(f"{prefix} "),
                ).with_menu(readable)
            )
        for s in selected:
            _level_rows(b, s, feature)
        return

    def target(current: int) -> int:
        if parsed.value is not None:
            return parsed.value
        assert parsed.delta is not None
        return max(0, min(100, current + parsed.delta))

    def describe() -> str:
        if parsed.value is not None:
            return f"to {parsed.value}%"
        return f"by {parsed.delta:+d}%"

    usable = [s for s in selected if s.level(feature).ok]
    if len(usable) > 1:
        changes = " · ".join(
            f"{s.number}: {s.level(feature).value}% → {target(s.level(feature).value or 0)}%" for s in usable
        )
        b.add(
            LevelResult(
                usable,
                feature,
                value=parsed.value,
                delta=parsed.delta,
                title=f"Set {feature.value} {describe()} — all {len(usable)} screens",
                sub=changes,
                icon=ICON_ALL,
            )
        )
    for s in selected:
        level = s.level(feature)
        if not level.ok:
            b.error(f"{feature.label} unavailable on {s.title}", level.error or "")
            continue
        assert level.value is not None
        b.add(
            LevelResult(
                [s],
                feature,
                value=parsed.value,
                delta=parsed.delta,
                title=f"Set {feature.value} {describe()} — {s.title}",
                sub=f"now {level.value}% → {target(level.value)}%",
                icon=icon_for(s),
                # Current value, so screens can be compared before picking one.
                progress_bar=level_bar(feature, level.value),
            )
        )


def _windows_hdr(b: _Builder, supported: list[Screen], mode: HdrMode) -> None:
    """System-wide HDR switch: every HDR screen at once, like Windows' Win+Alt+B."""

    on = [s for s in supported if s.hdr and s.hdr.enabled]
    if not on:
        state = "off"
    elif len(on) == len(supported):
        state = "on"
    else:
        state = "on for " + ", ".join(f"screen {s.number}" for s in on)
    sub = f"Windows HDR is {state} · applies to every HDR screen at once (like Win+Alt+B)"

    if mode is HdrMode.TOGGLE:
        # Windows' own rule: anything in HDR -> all off, otherwise all on.
        wanted = [not on]
        if on and len(on) < len(supported):
            wanted.append(True)  # mixed: also offer "everywhere on"
    else:
        wanted = [mode is HdrMode.ON]
    for want in wanted:
        word = "on" if want else "off"
        b.add(
            HdrResult(
                supported,
                want,
                title=f"Windows HDR: turn {word}" + (" everywhere" if len(supported) > 1 else ""),
                sub=sub,
                icon=ICON_HDR,
                auto_complete_text=b.q(f"hdr {word}"),
            )
        )


def _hdr(b: _Builder, parsed: ParsedQuery, selected: list[Screen]) -> None:
    supported = [s for s in selected if s.hdr and s.hdr.supported]
    mode = parsed.hdr_mode or HdrMode.TOGGLE
    if supported and not parsed.has_selector:
        _windows_hdr(b, supported, mode)

    if mode is HdrMode.TOGGLE:
        for s in supported:
            assert s.hdr is not None
            action = "off" if s.hdr.enabled else "on"
            b.add(
                HdrResult(
                    [s],
                    None,
                    title=f"Turn HDR {action} — {s.title}",
                    sub=f"{_hdr_label(s)}   ·   {' · '.join(s.tags)}",
                    icon=icon_for(s),
                )
            )
    else:
        want = mode is HdrMode.ON
        for s in supported:
            if s.hdr and s.hdr.enabled != want:
                b.add(
                    HdrResult(
                        [s],
                        want,
                        title=f"Turn HDR {mode.value} — {s.title}",
                        sub=f"{_hdr_label(s)}   ·   {' · '.join(s.tags)}",
                        icon=icon_for(s),
                    )
                )
            else:
                b.add(InfoResult(title=f"HDR already {mode.value} — {s.title}", sub=" · ".join(s.tags), icon=icon_for(s)))

    for s in selected:
        if s not in supported:
            b.add(InfoResult(title=f"{_hdr_label(s)} — {s.title}", sub=" · ".join(s.tags), icon=ICON_HDR))


def _identify(b: _Builder, selected: list[Screen], all_screens: list[Screen]) -> None:
    title = f"Identify — all {len(selected)} screens" if len(selected) > 1 else f"Identify {selected[0].title}"
    b.add(
        IdentifyResult(
            selected,
            title=title,
            sub="Shows each screen's number on the screen itself for 3 seconds",
            icon=ICON_IDENTIFY,
        )
    )
    for s in selected:
        b.add(
            IdentifyResult(
                [s],
                title=s.title,
                sub=f"{_status(s)}   —   {' · '.join(s.tags)}",
                icon=icon_for(s),
            )
        )


_HELP: tuple[tuple[str, str, str, str], ...] = (
    # (query to fill in, title, description, icon)
    ("", "sc", "List your screens with brightness, contrast, HDR, position, size and connection", ICON_APP),
    ("brightness ", "sc 50 · sc +10 · sc -10", "Brightness: exact or relative value, then pick all screens or one", ICON_BRIGHTNESS),
    ("contrast ", "sc c 70 · sc c +5", "Contrast: same as brightness (c, con, contrast)", ICON_CONTRAST),
    ("hdr ", "sc hdr · sc hdr on · sc hdr off", "HDR: switch all screens at once (like Win+Alt+B) or one screen", ICON_HDR),
    ("id", "sc id", "Identify: show a big coloured number on each screen for 3 seconds", ICON_IDENTIFY),
    ("#1 ", "sc #2 · sc lg · sc left · sc hdmi", "Pick a screen by number, name, brand, position (left, right, top, primary) or connection (hdmi, dp)", ICON_ALL),
    ("#1 50", "sc #2 80 · sc lg c 40 · sc left hdr on", "Combine a screen with any command", ICON_ALL),
    ("night", "sc day · sc night · sc movie · sc profile", "Profiles: apply saved brightness, contrast and HDR to all screens", ICON_ALL),
    ("profile save ", "sc night save · sc profile save gaming", "Save the current settings of every screen as a profile; then sc gaming applies it", ICON_ALL),
    ("settings", "sc settings", "Open the settings panel: edit profiles screen by screen with live preview, nicknames and values", ICON_APP),
    ("reset", "sc reset", "Something looks wrong or stuck? Restart monitor communication and clear caches", ICON_APP),
    ("name #1 ", "sc name #2 Gaming", "Nickname a screen (shown everywhere, follows the monitor); sc name #2 alone removes it", ICON_IDENTIFY),
)
_HELP_KEYS: tuple[tuple[str, str], ...] = (
    ("Shift+Enter on a screen or an 'all screens' row", "Quick actions: brightness and contrast presets, HDR on/off, identify"),
    ("Ctrl+C on a screen", "Copy its details (model, serial, connection, current values)"),
    ("Enter on the ✓ row after a change", "Undo it: values, profiles and nicknames go back to what they were"),
)


def _help(b: _Builder) -> None:
    for text, title, description, icon in _HELP:
        b.add(ChangeQueryResult(b.q(text), title=title, sub=description, icon=icon, auto_complete_text=b.q(text)))
    for title, description in _HELP_KEYS:
        b.add(InfoResult(title=title, sub=description, icon=ICON_APP))

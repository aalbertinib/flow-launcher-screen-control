"""Views for nicknames and profiles (kept apart from views.py for readability)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .identify import icon_for
from .query_parser import ParsedQuery, profile_name_problem
from .results import (
    ICON_ALL,
    ICON_APP,
    ICON_ERROR,
    ChangeQueryResult,
    DeleteProfileResult,
    InfoResult,
    NameResult,
    ProfileResult,
    SaveProfileResult,
    current_settings,
    profile_plan,
    settings_text,
)
from .screens import Feature, Screen
from .store import DEFAULT_PROFILES

if TYPE_CHECKING:
    from .views import _Builder

__all__ = ("active_profiles", "apply_summary", "describe_profile", "matching_names", "name_view", "profile_view")


def name_view(b: _Builder, parsed: ParsedQuery, screens: list[Screen]) -> None:
    if parsed.screen_number is None:
        for s in screens:
            current = f"Nickname: {s.nickname}" if s.nickname else "No nickname yet"
            b.add(
                ChangeQueryResult(
                    b.q(f"name #{s.number} "),
                    flash=[s],
                    title=f"Rename {s.title}",
                    sub=f"{current} · then type the name: sc name #{s.number} Gaming",
                    icon=icon_for(s),
                    auto_complete_text=b.q(f"name #{s.number} "),
                ).with_menu([])
            )
        return

    s = next((x for x in screens if x.number == parsed.screen_number), None)
    if s is None:
        connected = ", ".join(f"#{x.number}" for x in screens)
        b.error(f"There is no screen #{parsed.screen_number}", f"Connected: {connected}")
        return
    nickname = (parsed.nickname or "").strip()
    if nickname:
        b.add(
            NameResult(
                s,
                nickname,
                title=f"Name screen {s.number} “{nickname}”",
                sub=f"{s.name} · {' · '.join(s.tags)} · the name follows this monitor",
                icon=icon_for(s),
            )
        )
        return
    b.add(InfoResult(title=f"Type a name for {s.title}", sub=f"e.g. sc name #{s.number} Gaming", icon=icon_for(s)))
    if s.nickname:
        b.add(NameResult(s, None, title=f"Remove the nickname “{s.nickname}”", sub=f"Back to {s.name}", icon=ICON_ERROR))


def describe_profile(profile: dict, screens: list[Screen]) -> str:
    plan = profile_plan(profile, screens)
    parts = []
    for s in screens:
        settings = dict(plan.get(s.key) or {})
        if not (s.hdr and s.hdr.supported):
            settings.pop("hdr", None)
        if settings:
            parts.append(f"{s.number}: {settings_text(settings)}")
    return "   ·   ".join(parts) or "Changes nothing on the connected screens"


_SYMBOLS = {Feature.BRIGHTNESS: "☀", Feature.CONTRAST: "◑"}


def _changes(before: dict[str, dict], after: dict[str, dict], screens: list[Screen]) -> list[str]:
    """What going from ``before`` to ``after`` changes, per screen ("1: ☀ 40→10%")."""
    parts = []
    for s in screens:
        old, new = before.get(s.key) or {}, after.get(s.key) or {}
        bits = []
        for feature, symbol in _SYMBOLS.items():
            key = feature.value
            if new.get(key) is None or new[key] == old.get(key) or not s.level(feature).ok:
                continue  # unchanged, or a screen without DDC/CI: it can't be set
            bits.append(f"{symbol} {old[key]}→{new[key]}%" if old.get(key) is not None else f"{symbol} {new[key]}%")
        if new.get("hdr") is not None and new["hdr"] != old.get("hdr") and s.hdr and s.hdr.supported:
            word = "on" if new["hdr"] else "off"
            bits.append(f"HDR {'on' if old['hdr'] else 'off'}→{word}" if old.get("hdr") is not None else f"HDR {word}")
        if bits:
            parts.append(f"{s.number}: " + " ".join(bits))
    return parts


def _now(screens: list[Screen]) -> dict[str, dict]:
    return {s.key: current_settings(s) for s in screens}


def profile_changes(profile: dict, screens: list[Screen]) -> list[str]:
    """What applying ``profile`` would change right now (empty: it is already active)."""
    return _changes(_now(screens), profile_plan(profile, screens), screens)


def is_active(profile: dict, screens: list[Screen]) -> bool:
    """The screens already have every value the profile sets."""
    return bool(profile_plan(profile, screens)) and not profile_changes(profile, screens)


def active_profiles(profiles: dict[str, dict], screens: list[Screen]) -> list[str]:
    return [n for n, p in profiles.items() if is_active(p, screens)]


def apply_summary(profile: dict, screens: list[Screen]) -> str:
    changes = profile_changes(profile, screens)
    if changes:
        return "Changes " + "   ·   ".join(changes)
    if profile_plan(profile, screens):
        return "Already active: your screens match it"
    return "Changes nothing on the connected screens"


def matching_names(partial: str, names: list[str]) -> list[str]:
    """Names starting with ``partial``, then names containing it (never ``partial`` itself)."""
    partial = partial.strip()
    if not partial:
        return [n for n in names]
    starts = [n for n in names if n.startswith(partial) and n != partial]
    inside = [n for n in names if partial in n and n not in starts and n != partial]
    return starts + inside


def _current_profile(screens: list[Screen]) -> dict[str, dict]:
    """Current settings in saved-profile form (by stable identity)."""
    current: dict[str, dict] = {}
    for s in screens:
        entry = current_settings(s)
        if entry:
            current[s.stable_id] = entry
    return current


def _save_sub(name: str, profiles: dict[str, dict], screens: list[Screen]) -> str:
    current = _current_profile(screens)
    if name not in profiles:
        return "Saves " + describe_profile(current, screens)
    changes = _changes(profile_plan(profiles[name], screens), profile_plan(current, screens), screens)
    if not changes:
        return "Same as the saved settings: nothing would change"
    return "Updates " + "   ·   ".join(changes)


def _shadows_screen(name: str, screens: list[Screen]) -> Screen | None:
    """A screen that ``sc <name>`` used to pick, before the profile takes the word."""
    words = name.split()
    return next((s for s in screens if all(w.lower() in s.search_text.split() for w in words)), None)


def _deletable(profiles: dict[str, dict], customized: frozenset[str]) -> list[str]:
    return [n for n in profiles if n not in DEFAULT_PROFILES or n in customized]


def _profile_row(b: _Builder, name: str, profile: dict, screens: list[Screen], customized: frozenset[str]) -> None:
    tags = []
    if is_active(profile, screens):
        tags.append("✓ active")
    if name in customized and name in DEFAULT_PROFILES:
        tags.append("customised")
    b.add(
        ProfileResult(
            name,
            profile,
            screens,
            title=f"Apply profile “{name}”" + (f"   ({', '.join(tags)})" if tags else ""),
            sub=apply_summary(profile, screens),
            sub_tooltip="Sets " + describe_profile(profile, screens),
            icon=ICON_ALL,
            auto_complete_text=b.q(name),
        ).with_menu([])
    )


def _replace_rows(b: _Builder, names: list[str], profiles: dict[str, dict], screens: list[Screen]) -> None:
    for n in names:
        b.add(
            ChangeQueryResult(
                b.q(f"profile save {n}"),
                title=f"Replace “{n}” with the current settings",
                sub=_save_sub(n, profiles, screens) + " · Enter or Tab to pick",
                icon=ICON_ALL,
                auto_complete_text=b.q(f"profile save {n}"),
            )
        )


def profile_view(
    b: _Builder,
    parsed: ParsedQuery,
    screens: list[Screen],
    profiles: dict[str, dict],
    customized: frozenset[str],
) -> None:
    name = parsed.profile_name

    if parsed.profile_op == "save":
        if not name:
            b.add(InfoResult(title="Type a profile name to save", sub="e.g. sc profile save gaming · sc night save", icon=ICON_ALL))
            _replace_rows(b, list(profiles), profiles, screens)
            return
        problem = profile_name_problem(name)
        if problem:
            b.error(problem, "Profile names are used as commands: sc gaming applies “gaming”")
            _replace_rows(b, matching_names(name, list(profiles)), profiles, screens)
            return
        sub = _save_sub(name, profiles, screens)
        shadowed = _shadows_screen(name, screens) if name not in profiles else None
        if shadowed is not None:
            sub += f" · note: {b.q(name)} will apply this profile instead of picking {shadowed.title}"
        replaces = " (replaces it)" if name in profiles else ""
        b.add(
            SaveProfileResult(
                name,
                screens,
                title=f"Save current settings as “{name}”{replaces}",
                sub=sub,
                icon=ICON_ALL,
            ).with_menu([])
        )
        _replace_rows(b, matching_names(name, list(profiles)), profiles, screens)
        return

    if parsed.profile_op == "delete":
        deletable = _deletable(profiles, customized)
        if not name or name not in profiles:
            suggestions = matching_names(name or "", deletable)
            for n in suggestions:
                verb = "Reset" if n in DEFAULT_PROFILES else "Delete"
                b.add(
                    ChangeQueryResult(
                        b.q(f"profile delete {n}"),
                        title=f"{verb} “{n}”",
                        sub=describe_profile(profiles[n], screens) + " · Enter or Tab to pick",
                        icon=ICON_ERROR,
                        auto_complete_text=b.q(f"profile delete {n}"),
                    )
                )
            if suggestions:
                return
            if not name:
                b.add(InfoResult(title="No profile of your own to delete yet",
                                 sub="Save one with sc profile save <name> · changed default profiles can be reset here",
                                 icon=ICON_ALL))
            else:
                what = "Your profiles: " + ", ".join(deletable) if deletable else "Only the default profiles exist"
                b.error(f"No profile “{name}” to delete", what)
            return
        is_builtin = name in DEFAULT_PROFILES
        if is_builtin and name not in customized:
            b.add(
                InfoResult(
                    title=f"“{name}” already has its default settings",
                    sub=describe_profile(profiles[name], screens),
                    icon=ICON_ALL,
                )
            )
            return
        if is_builtin:
            sub = "Back to the defaults: " + describe_profile(DEFAULT_PROFILES[name], screens)
        else:
            sub = "Removes " + describe_profile(profiles[name], screens) + " · you can undo it right after"
        b.add(
            DeleteProfileResult(
                name,
                title=f"{'Reset' if is_builtin else 'Delete'} profile “{name}”",
                sub=sub,
                icon=ICON_ERROR,
            ).with_menu([])
        )
        return

    if name and name not in profiles:
        suggestions = matching_names(name, list(profiles))
        if not suggestions:
            b.error(f"No profile “{name}”", f"Save one with: sc profile save {name}")
            return
        for n in suggestions:
            _profile_row(b, n, profiles[n], screens, customized)
        b.add(
            ChangeQueryResult(
                b.q(f"profile save {name}"),
                title=f"Save the current settings as “{name}”",
                sub="New profile: remembers each screen's brightness, contrast and HDR",
                icon=ICON_ALL,
            )
        )
        return
    for n in [name] if name else list(profiles):
        _profile_row(b, n, profiles[n], screens, customized)
    if name:
        b.add(
            ChangeQueryResult(
                b.q(f"profile save {name}"),
                title=f"Save the current settings as “{name}”",
                sub=_save_sub(name, profiles, screens),
                icon=ICON_ALL,
            )
        )
        if name in customized or name not in DEFAULT_PROFILES:
            b.add(
                ChangeQueryResult(
                    b.q(f"profile delete {name}"),
                    title=f"{'Reset' if name in DEFAULT_PROFILES else 'Delete'} “{name}”",
                    sub="Back to the default settings" if name in DEFAULT_PROFILES else "Remove this profile",
                    icon=ICON_ERROR,
                )
            )
    else:
        b.add(
            InfoResult(
                title="Save your own: sc profile save <name>",
                sub="e.g. sc profile save gaming, then sc gaming applies it · sc night save replaces night",
                icon=ICON_APP,
            )
        )

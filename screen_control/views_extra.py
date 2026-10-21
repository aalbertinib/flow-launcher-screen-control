"""Views for nicknames and profiles (kept apart from views.py for readability)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .identify import icon_for
from .query_parser import ParsedQuery
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
    profile_plan,
)
from .screens import Feature, Screen
from .store import DEFAULT_PROFILES

if TYPE_CHECKING:
    from .views import _Builder

__all__ = ("describe_profile", "name_view", "profile_view")


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
        settings = plan.get(s.key)
        if not settings:
            continue
        bits = []
        if "brightness" in settings:
            bits.append(f"☀ {settings['brightness']}%")
        if "contrast" in settings:
            bits.append(f"◑ {settings['contrast']}%")
        if "hdr" in settings and s.hdr and s.hdr.supported:
            bits.append("HDR on" if settings["hdr"] else "HDR off")
        if bits:
            parts.append(f"{s.number}: " + " ".join(bits))
    return "   ·   ".join(parts) or "Changes nothing on the connected screens"


def _current_settings(screens: list[Screen]) -> dict[str, dict]:
    current: dict[str, dict] = {}
    for s in screens:
        entry = {f.value: s.level(f).value for f in Feature if s.level(f).ok}
        if s.hdr and s.hdr.supported:
            entry["hdr"] = s.hdr.enabled
        current[s.stable_id] = entry
    return current


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
            return
        replaces = " (replaces it)" if name in profiles else ""
        b.add(
            SaveProfileResult(
                name,
                screens,
                title=f"Save current settings as “{name}”{replaces}",
                sub=describe_profile(_current_settings(screens), screens),
                icon=ICON_ALL,
            ).with_menu([])
        )
        return

    if parsed.profile_op == "delete":
        if not name or name not in profiles:
            b.error(f"No profile “{name or ''}”", "Profiles: " + ", ".join(profiles))
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
        b.add(
            DeleteProfileResult(
                name,
                title=f"{'Reset' if is_builtin else 'Delete'} profile “{name}”",
                sub="Back to the default settings" if is_builtin else "This cannot be undone",
                icon=ICON_ERROR,
            ).with_menu([])
        )
        return

    if name and name not in profiles:
        b.error(f"No profile “{name}”", f"Save one with: sc profile save {name}")
        return
    for n in [name] if name else list(profiles):
        b.add(
            ProfileResult(
                n,
                profiles[n],
                screens,
                title=f"Apply profile “{n}”" + ("   (customised)" if n in customized else ""),
                sub=describe_profile(profiles[n], screens),
                icon=ICON_ALL,
                auto_complete_text=b.q(n),
            ).with_menu([])
        )
    if name:
        b.add(
            ChangeQueryResult(
                b.q(f"profile save {name}"),
                title=f"Save the current settings as “{name}”",
                sub="Remembers each screen's brightness, contrast and HDR",
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
                sub="e.g. sc profile save gaming · sc night save replaces the night profile",
                icon=ICON_APP,
            )
        )

"""Turn the text typed after the action keyword into a structured request.

Grammar (words in any order, case-insensitive)::

    [setting] [value] [screen selector...]

    setting   brightness | contrast | hdr | identify   (any prefix: b, c, h, i)
              help | ?                                 (list every command)

Special forms (first word decides):

    name #2 My Desk      nickname a screen (no text: remove)
    settings             open the settings panel (profiles, nicknames, values)
    day | night | movie  apply a profile; add "save" to store current values
    gaming               your own profiles work the same way (exact name)
    profile [save|delete] [name]
    value     50 | 50% | +10 | -10                     (brightness/contrast)
              on | off | toggle                        (hdr)
    selector  #2 | all | any word matched against the screen's name,
              vendor, position, connection... (lg, left, primary, hdmi)

A bare number means brightness (``sc 50``). Commands can be shortened as long as
the letters fit only one of them (``sc p``, ``sc se``, ``sc ni``); see ``_shortened``.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .screens import Feature

__all__ = ("Action", "HdrMode", "ParsedQuery", "parse_query", "profile_name_problem")


class Action(enum.Enum):
    OVERVIEW = "overview"
    LEVEL = "level"
    HDR = "hdr"
    IDENTIFY = "identify"
    HELP = "help"
    NAME = "name"
    PROFILE = "profile"
    RESET = "reset"
    SETTINGS = "settings"


class HdrMode(enum.Enum):
    ON = "on"
    OFF = "off"
    TOGGLE = "toggle"


@dataclass
class ParsedQuery:
    action: Action = Action.OVERVIEW
    feature: Feature | None = None
    value: int | None = None
    delta: int | None = None
    hdr_mode: HdrMode | None = None
    screen_number: int | None = None
    filters: list[str] = field(default_factory=list)
    typed_filters: list[str] = field(default_factory=list)
    """The same words as typed: profile names are case-sensitive."""
    error: str | None = None
    nickname: str | None = None
    """For NAME: the new name, original capitalisation ("" means remove)."""
    profile_name: str | None = None
    profile_op: str = "apply"
    """For PROFILE: apply | save | delete."""
    shortened: str | None = None
    """The letters typed when they stood for a whole command ("p" for profile)."""

    @property
    def has_selector(self) -> bool:
        return self.screen_number is not None or bool(self.filters)

    @property
    def has_level_change(self) -> bool:
        return self.value is not None or self.delta is not None


_SETTINGS: dict[str, str] = {
    "brightness": "brightness",
    "contrast": "contrast",
    "hdr": "hdr",
    "identify": "identify",
}
# Exact aliases, including a few French ones.
_ALIASES: dict[str, str] = {
    "br": "brightness",
    "lum": "brightness",
    "luminosity": "brightness",
    "luminosite": "brightness",
    "luminosité": "brightness",
    "contraste": "contrast",
    "id": "identify",
    "which": "identify",
    "where": "identify",
    # Exact words only, so "h" keeps meaning HDR.
    "help": "help",
    "?": "help",
    "aide": "help",
}
_HDR_WORDS: dict[str, HdrMode] = {
    "on": HdrMode.ON,
    "off": HdrMode.OFF,
    "toggle": HdrMode.TOGGLE,
}
_NUMBER = re.compile(r"^([+-]?)(\d{1,4})%?$")
_SCREEN = re.compile(r"^#(\d{1,2})$")


def _setting_for(token: str) -> str | None:
    if token in _ALIASES:
        return _ALIASES[token]
    matches = [name for name in _SETTINGS if name.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def _hdr_word_for(token: str) -> HdrMode | None | bool:
    """HdrMode for a full or unambiguous prefix, True if ambiguous ("o"), else None."""

    matches = [mode for word, mode in _HDR_WORDS.items() if word.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    return True if matches else None


NAME_WORDS = frozenset({"name", "rename", "nick", "nickname", "nom", "renommer"})
PROFILE_WORDS = frozenset({"profile", "profiles", "mode", "modes", "profil", "profils"})
BUILTIN_PROFILES = frozenset({"day", "night", "movie"})
_PROFILE_OPS = {"save": "save", "store": "save", "delete": "delete", "remove": "delete", "reset": "delete"}


RESET_WORDS = frozenset({"reset", "repair", "fix", "restart", "reinit"})
SETTINGS_WORDS = frozenset(
    {"settings", "setting", "config", "configure", "panel", "options", "preferences", "prefs",
     "parametres", "paramètres", "reglages", "réglages"}
)


# What a first word can be short for. Brightness, contrast, HDR and identify have
# their own prefixes (_setting_for), but count here: "h" stays HDR rather than help.
_COMMANDS = ("name", "profile", "reset", "settings", "help", "day", "night", "movie")
_OTHER_WORDS = ("brightness", "contrast", "hdr", "identify")


def _shortened(typed: str, profile_names: frozenset[str]) -> str | None:
    """The one command ``typed`` starts, None if it is complete or fits several.

    Command words ignore case; profile names, built-in ones included, don't.
    """
    token = typed.lower()
    commands = [w for w in (*_COMMANDS, *_OTHER_WORDS) if w not in BUILTIN_PROFILES and w.startswith(token)]
    profiles = [n for n in (*BUILTIN_PROFILES, *profile_names) if n.startswith(typed)]
    fits = commands + profiles
    return fits[0] if len(fits) == 1 and fits[0] != token and fits[0] in _COMMANDS else None


def _expand_command(raw: list[str], low: list[str], profile_names: frozenset[str]) -> str | None:
    """Replace a shortened first word by its command; returns the letters it replaced."""
    word = _shortened(raw[0], profile_names)
    if word is None:
        return None
    rest = low[1:]
    if word == "profile":
        # "p 50" and "pr #2" keep picking screens ("primary").
        usable = not rest or not (_NUMBER.match(rest[0]) or _SCREEN.match(rest[0]))
    elif word in BUILTIN_PROFILES:
        usable = not rest or (len(rest) == 1 and rest[0] in _PROFILE_OPS)
    else:
        usable = word == "name" or not rest
    if not usable:
        return None
    typed = low[0]
    raw[0] = low[0] = word
    return typed


def _parse_special(raw: list[str], profile_names: frozenset[str] = frozenset()) -> ParsedQuery | None:
    low = [t.lower() for t in raw]
    if not low:
        return None
    typed = _expand_command(raw, low, profile_names)
    q = _parse_command(raw, low, profile_names)
    if q is not None and typed is not None and len(low) == 1:
        q.shortened = typed
    return q


def _parse_command(raw: list[str], low: list[str], profile_names: frozenset[str]) -> ParsedQuery | None:
    if low == ["help"]:
        return ParsedQuery(action=Action.HELP)
    if len(low) == 1 and low[0] in RESET_WORDS:
        return ParsedQuery(action=Action.RESET)
    if len(low) == 1 and low[0] in SETTINGS_WORDS:
        return ParsedQuery(action=Action.SETTINGS)

    # gaming  /  gaming setup save: the user's own profiles, by their exact name
    # (profile_name_problem keeps saved names from clashing with commands).
    joined = " ".join(raw)
    if joined in profile_names:
        return ParsedQuery(action=Action.PROFILE, profile_name=joined)
    if len(low) > 1 and low[-1] in _PROFILE_OPS and " ".join(raw[:-1]) in profile_names:
        return ParsedQuery(action=Action.PROFILE, profile_name=" ".join(raw[:-1]), profile_op=_PROFILE_OPS[low[-1]])

    # name #2 My Desk  /  #2 name My Desk
    name_at = next((i for i in (0, 1) if i < len(low) and low[i] in NAME_WORDS), None)
    if name_at is not None and (name_at == 0 or _SCREEN.match(low[0])):
        q = ParsedQuery(action=Action.NAME)
        words = []
        for i, (token, original) in enumerate(zip(low, raw)):
            if i == name_at:
                continue
            m = _SCREEN.match(token)
            if m and q.screen_number is None:
                q.screen_number = int(m.group(1))
            else:
                words.append(original)
        q.nickname = " ".join(words)
        return q

    # profile [save|delete] [name]  /  night [save]
    if low[0] in PROFILE_WORDS or raw[0] in BUILTIN_PROFILES:
        q = ParsedQuery(action=Action.PROFILE)
        start = 1 if low[0] in PROFILE_WORDS else 0
        rest, names = low[start:], raw[start:]  # names keep their case
        if rest and rest[0] in _PROFILE_OPS:
            q.profile_op = _PROFILE_OPS[rest[0]]
            names = names[1:]
        elif len(rest) > 1 and rest[1] in _PROFILE_OPS:  # "night save"
            q.profile_op = _PROFILE_OPS[rest[1]]
            names = names[:1] + names[2:]
        elif rest and low[0] in PROFILE_WORDS and not any(n.startswith(names[0]) for n in BUILTIN_PROFILES | profile_names):
            # "p sa gaming": short for save, unless a profile starts with those letters.
            ops = {op for word, op in _PROFILE_OPS.items() if word.startswith(rest[0])}
            if len(ops) == 1:
                q.profile_op = ops.pop()
                names = names[1:]
        q.profile_name = " ".join(names) or None
        return q
    return None


def parse_query(text: str, profile_names: Iterable[str] = ()) -> ParsedQuery:
    """``profile_names``: saved profiles, so ``sc gaming`` applies the "gaming" profile."""
    special = _parse_special(text.split(), frozenset(profile_names) - BUILTIN_PROFILES)
    if special is not None:
        return special

    q = ParsedQuery()
    setting: str | None = None
    hdr_pending = False

    for token, typed in zip(text.lower().split(), text.split()):
        if token in ("all", "*", "everywhere"):
            continue
        if token in ("windows", "win", "system", "global"):
            continue  # "windows hdr off": the system-wide switch is the default target

        m = _SCREEN.match(token)
        if m:
            q.screen_number = int(m.group(1))
            continue

        m = _NUMBER.match(token)
        if m:
            if q.has_level_change:
                q.error = "Type a single value, e.g. 50, +10 or -10"
                continue
            sign, digits = m.groups()
            number = int(digits)
            if sign:
                q.delta = number if sign == "+" else -number
            elif number > 100:
                q.error = f"{number}% is out of range: use a value from 0 to 100"
            else:
                q.value = number
            continue

        candidate = _setting_for(token)
        if candidate is not None and (setting is None or candidate == setting):
            setting = candidate
            continue

        if setting in (None, "hdr"):
            hdr_word = _hdr_word_for(token)
            if hdr_word is not None and (setting == "hdr" or len(token) >= 2):
                if hdr_word is True:
                    hdr_pending = True
                else:
                    q.hdr_mode = hdr_word  # type: ignore[assignment]
                    setting = "hdr"
                continue

        if candidate is not None:
            q.error = "Change one setting at a time"
            continue

        q.filters.append(token)
        q.typed_filters.append(typed)

    if setting == "help":
        q.action = Action.HELP
    elif setting == "identify":
        q.action = Action.IDENTIFY
    elif setting == "hdr" or q.hdr_mode is not None or hdr_pending:
        q.action = Action.HDR
        if q.has_level_change and q.error is None:
            q.error = "HDR is on or off: use 'hdr on', 'hdr off' or just 'hdr' to toggle"
    elif setting in ("brightness", "contrast") or q.has_level_change:
        q.action = Action.LEVEL
        q.feature = Feature.CONTRAST if setting == "contrast" else Feature.BRIGHTNESS
    return q


def profile_name_problem(name: str) -> str | None:
    """Why ``name`` can't be a profile name (it already means something), else None."""
    if name in BUILTIN_PROFILES:
        return None
    q = parse_query(name)
    words = name.split()
    if not words:
        return "Type a profile name"
    if q.action is Action.OVERVIEW and q.screen_number is None and q.error is None and q.filters == [w.lower() for w in words]:
        return None
    return f"“{name}” is already a ScreenControl command: pick another name"

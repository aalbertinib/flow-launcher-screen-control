"""Turn the text typed after the action keyword into a structured request.

Grammar (words in any order, case-insensitive)::

    [setting] [value] [screen selector...]

    setting   brightness | contrast | hdr | identify   (any prefix: b, c, h, i)
              help | ?                                 (list every command)

Special forms (first word decides):

    name #2 My Desk      nickname a screen (no text: remove)
    day | night | movie  apply a profile; add "save" to store current values
    profile [save|delete] [name]
    value     50 | 50% | +10 | -10                     (brightness/contrast)
              on | off | toggle                        (hdr)
    selector  #2 | all | any word matched against the screen's name,
              vendor, position, connection... (lg, left, primary, hdmi)

A bare number means brightness (``sc 50``).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

from .screens import Feature

__all__ = ("Action", "HdrMode", "ParsedQuery", "parse_query")


class Action(enum.Enum):
    OVERVIEW = "overview"
    LEVEL = "level"
    HDR = "hdr"
    IDENTIFY = "identify"
    HELP = "help"
    NAME = "name"
    PROFILE = "profile"
    RESET = "reset"


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
    error: str | None = None
    nickname: str | None = None
    """For NAME: the new name, original capitalisation ("" means remove)."""
    profile_name: str | None = None
    profile_op: str = "apply"
    """For PROFILE: apply | save | delete."""

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


def _parse_special(raw: list[str]) -> ParsedQuery | None:
    low = [t.lower() for t in raw]
    if not low:
        return None
    if len(low) == 1 and low[0] in RESET_WORDS:
        return ParsedQuery(action=Action.RESET)

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
    if low[0] in PROFILE_WORDS or low[0] in BUILTIN_PROFILES:
        q = ParsedQuery(action=Action.PROFILE)
        rest = low[1:] if low[0] in PROFILE_WORDS else low
        if rest and rest[0] in _PROFILE_OPS:
            q.profile_op = _PROFILE_OPS[rest[0]]
            rest = rest[1:]
        elif len(rest) > 1 and rest[1] in _PROFILE_OPS:  # "night save"
            q.profile_op = _PROFILE_OPS[rest[1]]
            rest = rest[:1] + rest[2:]
        q.profile_name = " ".join(rest) or None
        return q
    return None


def parse_query(text: str) -> ParsedQuery:
    special = _parse_special(text.split())
    if special is not None:
        return special

    q = ParsedQuery()
    setting: str | None = None
    hdr_pending = False

    for token in text.lower().split():
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

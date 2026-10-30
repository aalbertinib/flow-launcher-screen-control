"""Persistent user data: screen nicknames and profiles.

Stored as JSON in Flow's settings folder (``Settings/Plugins/ScreenControl``),
which survives plugin updates. Only touched from the asyncio thread.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

__all__ = ("DEFAULT_PROFILES", "Store", "data_dir")

log = logging.getLogger(__name__)

# Settings per screen identity, "*" applying to screens a profile does not name.
# Keys: brightness / contrast (0-100) and hdr (bool). Missing keys are left alone.
DEFAULT_PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    "day": {"*": {"brightness": 80}},
    "night": {"*": {"brightness": 10, "hdr": False}},
    "movie": {"*": {"brightness": 70, "hdr": True}},
}
_FILE_NAME = "screen_control.json"
_MAX_NAME = 40


_LEGACY_DIR_NAMES = ("Screen Control",)  # before the plugin was renamed ScreenControl


def data_dir(plugin_root: Path) -> Path:
    """Flow's per-plugin settings folder, or the plugin folder when run elsewhere."""
    if plugin_root.parent.name.lower() != "plugins":
        return plugin_root / "data"
    settings = plugin_root.parent.parent / "Settings" / "Plugins"
    current = settings / "ScreenControl"
    _move_legacy_data(settings, current)
    return current


def _move_legacy_data(settings: Path, current: Path) -> None:
    """Keep nicknames and profiles saved under the plugin's previous name."""
    if (current / _FILE_NAME).exists():
        return
    for name in _LEGACY_DIR_NAMES:
        old = settings / name / _FILE_NAME
        if old.is_file():
            try:
                current.mkdir(parents=True, exist_ok=True)
                os.replace(old, current / _FILE_NAME)
                log.info("Moved saved nicknames and profiles from %s", old.parent)
            except OSError:
                log.exception("Could not move %s", old)
            return


class Store:
    def __init__(self, directory: Path) -> None:
        self.path = directory / _FILE_NAME
        self._data: dict[str, Any] = {"version": 1, "nicknames": {}, "profiles": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Keep the unreadable file for inspection rather than losing it silently.
            log.exception("Could not read %s, starting fresh", self.path)
            try:
                self.path.replace(self.path.with_suffix(".bad.json"))
            except OSError:
                pass
            return
        if isinstance(raw, dict):
            if isinstance(raw.get("nicknames"), dict):
                self._data["nicknames"] = {str(k): str(v) for k, v in raw["nicknames"].items() if v}
            if isinstance(raw.get("profiles"), dict):
                self._data["profiles"] = {
                    str(k): v for k, v in raw["profiles"].items() if isinstance(v, dict)
                }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    # -- nicknames ---------------------------------------------------------

    def nickname(self, identity: str) -> str | None:
        return self._data["nicknames"].get(identity)

    def set_nickname(self, identity: str, name: str | None) -> None:
        name = " ".join((name or "").split())[:_MAX_NAME]
        if name:
            self._data["nicknames"][identity] = name
        else:
            self._data["nicknames"].pop(identity, None)
        self._save()

    # -- profiles ----------------------------------------------------------

    def profiles(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Built-in profiles (possibly overridden) followed by custom ones."""
        merged = copy.deepcopy(DEFAULT_PROFILES)
        merged.update(copy.deepcopy(self._data["profiles"]))
        return merged

    def save_profile(self, name: str, settings: dict[str, dict[str, Any]]) -> None:
        self._data["profiles"][normalize_profile_name(name)] = settings
        self._save()

    def delete_profile(self, name: str) -> bool:
        """Remove a custom profile or reset a built-in one. True if anything changed."""
        removed = self._data["profiles"].pop(normalize_profile_name(name), None) is not None
        if removed:
            self._save()
        return removed

    def is_customized(self, name: str) -> bool:
        return normalize_profile_name(name) in self._data["profiles"]

    def saved_profile(self, name: str) -> dict[str, dict[str, Any]] | None:
        """What the user saved under ``name`` (None for a default or unknown profile)."""
        saved = self._data["profiles"].get(normalize_profile_name(name))
        return copy.deepcopy(saved) if saved is not None else None

    def restore_profile(self, name: str, saved: dict[str, dict[str, Any]] | None) -> None:
        """Put back what :meth:`saved_profile` returned (undo of a save or delete)."""
        if saved is None:
            self.delete_profile(name)
        else:
            self.save_profile(name, saved)


def normalize_profile_name(name: str) -> str:
    return " ".join(name.lower().split())[:_MAX_NAME]

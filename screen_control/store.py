"""Persistent user data: screen nicknames and profiles.

Stored as JSON in Flow's settings folder (``Settings/Plugins/ScreenControl``),
which survives plugin updates. Only touched from the asyncio thread.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import shutil
import time
from collections.abc import Callable
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
_BACKUP_NAME = "screen_control.bak.json"
_LOCK_RETRIES = 10
_LOCK_WAIT = 0.05
_TEMP_FILE_AGE = 60  # seconds: newer ones may be another process saving
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


def _valid_settings(raw: Any) -> dict[str, dict[str, Any]]:
    """The usable part of a saved profile: the file may have been edited by hand."""
    settings: dict[str, dict[str, Any]] = {}
    for screen, values in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(values, dict):
            continue
        entry: dict[str, Any] = {}
        for feature in ("brightness", "contrast"):
            value = values.get(feature)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                entry[feature] = max(0, min(100, round(value)))
        if isinstance(values.get("hdr"), bool):
            entry["hdr"] = values["hdr"]
        settings[screen] = entry
    return settings


def _patiently(action: Callable[[], Any]) -> Any:
    """Run a file operation, waiting out a short lock (antivirus, backup, another process)."""
    for _ in range(_LOCK_RETRIES):
        try:
            return action()
        except PermissionError:
            time.sleep(_LOCK_WAIT)
    return action()


class Store:
    """The file is the truth: a restarted or second process never overwrites newer data."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / _FILE_NAME
        self.backup = directory / _BACKUP_NAME
        self._data: dict[str, Any] = {"version": 1, "nicknames": {}, "profiles": {}}
        self._stamp: tuple[int, int] | None = None
        self._current()
        self._remove_old_temp_files()

    def _remove_old_temp_files(self) -> None:
        """What a crash in the middle of a save left behind."""
        for tmp in self.path.parent.glob(f"{self.path.stem}.*.tmp"):
            try:
                if time.time() - tmp.stat().st_mtime > _TEMP_FILE_AGE:
                    tmp.unlink()
            except OSError:
                pass

    def _read(self, path: Path) -> dict[str, Any]:
        raw = json.loads(_patiently(lambda: path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name} does not hold an object")
        return raw

    def _use(self, raw: dict[str, Any]) -> None:
        self._data = {**raw, "nicknames": {}, "profiles": {}}  # keeps what a newer version added
        if isinstance(raw.get("nicknames"), dict):
            self._data["nicknames"] = {str(k): str(v) for k, v in raw["nicknames"].items() if v}
        if isinstance(raw.get("profiles"), dict):
            # Kept as they are, so a typo fixed in the file is not lost by the next save.
            self._data["profiles"] = {str(k): v for k, v in raw["profiles"].items()}
            for name, settings in self._data["profiles"].items():
                if _valid_settings(settings) != settings:
                    log.warning("Profile %r has values that can't be used, ignoring them", name)

    def _refresh(self) -> None:
        """Load the file if it changed. Raises OSError if it exists but can't be read."""
        try:
            stat = _patiently(self.path.stat)
        except FileNotFoundError:
            if self._stamp is None:
                self._recover()  # first load: a crash may have left only the backup
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            self._use(self._read(self.path))
            self._stamp = stamp
        except ValueError:
            # Keep the unreadable file for inspection rather than losing it silently.
            log.exception("Could not read %s, using the backup", self.path)
            try:
                self.path.replace(self.path.with_suffix(".bad.json"))
            except OSError:
                log.exception("Could not set %s aside", self.path)
            self._recover()

    def _recover(self) -> None:
        try:
            self._use(self._read(self.backup))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("Could not read %s", self.backup)
            return
        log.warning("Restored nicknames and profiles from %s", self.backup)
        try:
            self._save()
        except OSError:
            log.exception("Could not write %s", self.path)

    def _current(self) -> None:
        """Before reading: an unreadable file must not break the search, what's in memory will do."""
        try:
            self._refresh()
        except OSError:
            log.exception("Could not read %s, keeping what is in memory", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # on disk before it replaces anything: survives a power cut
        try:
            if self.path.exists():
                shutil.copyfile(self.path, self.backup)
        except OSError:
            log.exception("Could not back up %s", self.path)
        try:
            _patiently(lambda: os.replace(tmp, self.path))  # atomic: never leaves a half-written file
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        stat = self.path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    # -- nicknames ---------------------------------------------------------

    def nickname(self, identity: str) -> str | None:
        self._current()
        return self._data["nicknames"].get(identity)

    def set_nickname(self, identity: str, name: str | None) -> None:
        self._refresh()
        name = " ".join((name or "").split())[:_MAX_NAME]
        if name:
            self._data["nicknames"][identity] = name
        else:
            self._data["nicknames"].pop(identity, None)
        self._save()

    # -- profiles ----------------------------------------------------------

    def profiles(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Built-in profiles (possibly overridden) followed by custom ones."""
        self._current()
        merged = copy.deepcopy(DEFAULT_PROFILES)
        merged.update({name: _valid_settings(saved) for name, saved in self._data["profiles"].items()})
        return merged

    def save_profile(self, name: str, settings: dict[str, dict[str, Any]]) -> None:
        self._refresh()
        self._data["profiles"][normalize_profile_name(name)] = settings
        self._save()

    def delete_profile(self, name: str) -> bool:
        """Remove a custom profile or reset a built-in one. True if anything changed."""
        self._refresh()
        removed = self._data["profiles"].pop(normalize_profile_name(name), None) is not None
        if removed:
            self._save()
        return removed

    def is_customized(self, name: str) -> bool:
        self._current()
        return normalize_profile_name(name) in self._data["profiles"]

    def saved_profile(self, name: str) -> dict[str, dict[str, Any]] | None:
        """What the user saved under ``name`` (None for a default or unknown profile)."""
        self._current()
        saved = self._data["profiles"].get(normalize_profile_name(name))
        return copy.deepcopy(saved) if saved is not None else None

    def restore_profile(self, name: str, saved: dict[str, dict[str, Any]] | None) -> None:
        """Put back what :meth:`saved_profile` returned (undo of a save or delete)."""
        if saved is None:
            self.delete_profile(name)
        else:
            self.save_profile(name, saved)


def normalize_profile_name(name: str) -> str:
    return " ".join(name.split())[:_MAX_NAME]

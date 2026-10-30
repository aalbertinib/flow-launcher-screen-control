"""What the last action did, shown at the top of the list it was picked from.

Replaces success notifications: the list refreshes in place, so the result of an
action (and a way to undo it) belongs there rather than in a Windows toast.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

__all__ = ("FEEDBACK_TTL", "Feedback", "merge_restore")

FEEDBACK_TTL = 30.0
"""Seconds the banner stays on the list the action was picked from."""


@dataclass
class Feedback:
    title: str
    sub: str
    query: str
    """Full query text (keyword included) of the list the banner belongs to."""
    restore: dict[str, dict] | None = None
    """Screen settings that undo the action ({screen key: settings}, as for profiles)."""
    undo: Callable[[], Awaitable[tuple[str, str | None]]] | None = None
    """Undo for actions on saved data; returns (message, query to show or None)."""
    at: float = field(default_factory=time.monotonic)

    @property
    def can_undo(self) -> bool:
        return self.restore is not None or self.undo is not None

    def shown_for(self, query: str) -> bool:
        return time.monotonic() - self.at < FEEDBACK_TTL and query.strip() == self.query.strip()


def merge_restore(previous: Feedback | None, query: str, restore: dict[str, dict]) -> dict[str, dict]:
    """Keep the oldest values when actions follow each other on the same list,
    so pressing +10 three times and then undo goes back to where it started."""
    if previous is None or previous.restore is None or not previous.shown_for(query):
        return restore
    merged = {key: dict(settings) for key, settings in restore.items()}
    for key, settings in previous.restore.items():
        merged.setdefault(key, {}).update(settings)
    return merged

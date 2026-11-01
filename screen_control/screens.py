"""Screen discovery and control.

All hardware access goes through :class:`ScreenService`, which runs every
blocking call on one dedicated worker thread. That keeps Flow's asyncio loop
responsive and guarantees DDC/CI commands never overlap (overlapping commands
are the main cause of random "monitor did not answer" failures).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TypeVar

from .win32 import Win32Error, ddc, displayconfig, edid, gdi, wmi

__all__ = ("Feature", "Level", "Screen", "ScreenService", "describe_positions")

log = logging.getLogger(__name__)

T = TypeVar("T")

CACHE_TTL = 4.0
"""Seconds a snapshot is reused. Flow sends a query on every keystroke."""
DDC_FAILURE_TTL = 60.0
"""Seconds to skip a monitor whose DDC/CI did not answer (failed reads are slow)."""
READ_TIMEOUT = 10.0
STUCK_AFTER = 20.0
MAX_STUCK_THREADS = 16
"""A hardware call running longer than this is abandoned and the workers replaced."""
KEEP_FRESH_EVERY = 30.0
"""Background refresh interval: DDC/CI reads cost ~60 ms each and Windows runs
them one at a time even across monitors, so they are kept off the path the user
waits on. Opening the list then shows values at most this old, instantly."""
STALE_OK = 120.0
"""Snapshots younger than this are shown at once while a refresh runs in the background."""
STALE_WAIT = 1.5
"""How long to wait for a refresh before falling back to an older snapshot."""
WRITE_TIMEOUT = 10.0


class Feature(enum.Enum):
    BRIGHTNESS = "brightness"
    CONTRAST = "contrast"

    @property
    def vcp_code(self) -> int:
        return ddc.VCP_BRIGHTNESS if self is Feature.BRIGHTNESS else ddc.VCP_CONTRAST

    @property
    def label(self) -> str:
        return self.value.capitalize()


@dataclass(frozen=True)
class Level:
    value: int | None = None
    """Percent 0-100, ``None`` when unknown/unsupported (see ``error``)."""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class Screen:
    key: str
    """Stable identity: the monitor device path, lower-cased."""
    number: int
    """1-based, ordered left to right then top to bottom."""
    name: str
    vendor: str
    gdi_device_name: str
    hmonitor: int
    adapter_id: tuple[int, int]
    target_id: int
    device_path: str
    connection: str
    internal: bool
    width: int | None
    height: int | None
    refresh_hz: float | None
    scale_percent: int | None
    rect: tuple[int, int, int, int]
    primary: bool
    position: str
    """``left``, ``right``, ``top-left``... or ``""`` when there is only one screen."""
    diagonal_inches: float | None
    serial: str | None
    hdr: displayconfig.HdrState | None
    brightness: Level = field(default_factory=Level)
    contrast: Level = field(default_factory=Level)
    identity: str = ""
    """Survives port changes when the EDID has a serial number (nicknames, profiles)."""
    nickname: str | None = None

    def level(self, feature: Feature) -> Level:
        return self.brightness if feature is Feature.BRIGHTNESS else self.contrast

    @property
    def title(self) -> str:
        if self.nickname:
            return f"{self.number} · {self.nickname} ({self.name})"
        return f"{self.number} · {self.name}"

    @property
    def stable_id(self) -> str:
        return self.identity or self.key

    @property
    def tags(self) -> list[str]:
        """Short human hints that tell identical-looking screens apart."""

        tags: list[str] = []
        if self.position:
            tags.append(self.position.capitalize())
        if self.primary:
            tags.append("Primary")
        if self.diagonal_inches:
            tags.append(f'{self.diagonal_inches:.0f}"')
        if self.width and self.height:
            tags.append(f"{self.width}×{self.height}")
        if self.refresh_hz:
            tags.append(f"{self.refresh_hz:.0f} Hz")
        tags.append(_SHORT_CONNECTION.get(self.connection, self.connection))
        return tags

    @property
    def search_text(self) -> str:
        words = [
            f"#{self.number}",
            self.name,
            self.nickname or "",
            self.vendor,
            self.position,
            self.connection,
            "primary" if self.primary else "secondary",
            "internal builtin laptop" if self.internal else "external",
        ]
        if self.connection == "DisplayPort":
            words.append("dp")
        if self.serial:
            words.append(self.serial)
        return " ".join(w for w in words if w).lower()


_SHORT_CONNECTION = {"DisplayPort": "DP", "Wired (indirect)": "Indirect"}


def describe_positions(rects: Sequence[tuple[int, int, int, int]]) -> list[str]:
    """Name each rectangle by where it sits in the desktop layout.

    Horizontal words (left/middle/right) are used when screens sit side by
    side, vertical ones (top/bottom) when some are stacked, both if needed.
    """

    n = len(rects)
    if n < 2:
        return [""] * n

    def overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
        return min(a1, b1) - max(a0, b0) > 0

    def axis(lo: int, hi: int) -> list[str]:
        # lo/hi index into (left, top, right, bottom)
        centers = [(r[lo] + r[hi]) / 2 for r in rects]
        # If every pair of screens overlaps on this axis, the axis is meaningless.
        spread = any(
            not overlap(rects[i][lo], rects[i][hi], rects[j][lo], rects[j][hi])
            for i in range(n)
            for j in range(i + 1, n)
        )
        if not spread:
            return [""] * n
        cmin, cmax = min(centers), max(centers)
        words = []
        for c in centers:
            if c == cmin:
                words.append("start")
            elif c == cmax:
                words.append("end")
            else:
                words.append("middle")
        return words

    horizontal = [{"start": "left", "end": "right"}.get(w, w) for w in axis(0, 2)]
    vertical = [{"start": "top", "end": "bottom"}.get(w, w) for w in axis(1, 3)]

    result = []
    for h, v in zip(horizontal, vertical):
        if h and v:
            if h == v == "middle":
                result.append("center")
            elif v == "middle":
                result.append(h)
            elif h == "middle":
                result.append(v)
            else:
                result.append(f"{v}-{h}")
        else:
            result.append(h or v)
    return result


# --------------------------------------------------------------------------- #
# Blocking implementation (worker thread only)
# --------------------------------------------------------------------------- #


class _Preempted(Exception):
    """A background refresh stopped early so a user command could run first."""


class _Hardware:
    def __init__(self) -> None:
        self._edid_cache: dict[str, edid.EdidInfo | None] = {}
        self._ddc_failures: dict[str, tuple[float, str]] = {}
        # Each monitor has its own DDC/CI bus: talk to them in parallel, one
        # thread per monitor at a time (callers hold the single service worker).
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="screen-ddc")
        # Set when a user command is waiting: background refreshes stop at the
        # next DDC/CI read so the command does not queue behind them.
        self.preempt = threading.Event()
        # VCP maximum per (screen, code). Fixed per monitor, so absolute writes
        # can skip the read that would otherwise precede them.
        self._vcp_max: dict[tuple[str, int], int] = {}

    def _edid(self, device_path: str) -> edid.EdidInfo | None:
        if device_path not in self._edid_cache:
            try:
                self._edid_cache[device_path] = edid.read_edid(device_path)
            except Exception:
                log.exception("Could not read EDID for %s", device_path)
                self._edid_cache[device_path] = None
        return self._edid_cache[device_path]

    def topology(self) -> list[Screen]:
        paths = displayconfig.query_active_paths()
        gdi_monitors = {m.device_name.upper(): m for m in gdi.enum_monitors()}

        rows = []
        for p in paths:
            g = gdi_monitors.get(p.gdi_device_name.upper())
            if g is None:
                continue  # not part of the desktop (e.g. a headset)
            rows.append((p, g))

        # Number screens left to right, then top to bottom.
        rows.sort(key=lambda pg: (pg[1].left, pg[1].top, pg[0].device_path))
        positions = describe_positions([(g.left, g.top, g.right, g.bottom) for _, g in rows])

        screens: list[Screen] = []
        for number, ((p, g), position) in enumerate(zip(rows, positions), start=1):
            info = self._edid(p.device_path)
            pnp_id = info.manufacturer_id if info else _pnp_from_path(p.device_path)
            vendor = edid.vendor_name(pnp_id) if pnp_id else ""
            name = p.friendly_name or (info.model_name if info else None) or ""
            internal = displayconfig.is_internal_output(p.output_technology)
            if not name:
                name = "Built-in display" if internal else f"{vendor} monitor".strip()
            try:
                hdr = displayconfig.get_hdr_state(p.adapter_id, p.target_id)
            except Exception:
                log.exception("Could not read HDR state of %s", name)
                hdr = None
            screens.append(
                Screen(
                    key=p.device_path.lower(),
                    number=number,
                    name=name,
                    vendor=vendor,
                    gdi_device_name=p.gdi_device_name,
                    hmonitor=g.hmonitor,
                    adapter_id=p.adapter_id,
                    target_id=p.target_id,
                    device_path=p.device_path,
                    connection=displayconfig.OUTPUT_TECHNOLOGY_NAMES.get(p.output_technology, "Other"),
                    internal=internal,
                    width=p.width or g.width,
                    height=p.height or g.height,
                    refresh_hz=p.refresh_hz,
                    scale_percent=g.scale_percent,
                    rect=(g.left, g.top, g.right, g.bottom),
                    primary=g.primary,
                    position=position,
                    diagonal_inches=info.diagonal_inches if info else None,
                    serial=info.serial_number if info else None,
                    hdr=hdr,
                    identity=(
                        f"{info.manufacturer_id}{info.product_code:04X}#{info.serial_number}"
                        if info and info.serial_number
                        else p.device_path.lower()
                    ),
                )
            )
        return screens

    def snapshot(self, preemptible: bool = False) -> list[Screen]:
        screens = self.topology()
        internal_levels: dict[str, int] | None = None
        internal_error: str | None = None
        if any(s.internal for s in screens):
            try:
                internal_levels = wmi.get_internal_brightness()
            except Win32Error as e:
                internal_error = e.message

        ddc_reads = {
            s.key: self._pool.submit(self._with_ddc_levels, s, preemptible) for s in screens if not s.internal
        }
        return [
            self._with_internal_levels(s, internal_levels, internal_error) if s.internal else ddc_reads[s.key].result()
            for s in screens
        ]

    def _with_internal_levels(
        self, s: Screen, levels: dict[str, int] | None, error: str | None
    ) -> Screen:
        contrast = Level(error="Built-in displays do not expose a contrast control")
        if levels is None:
            return replace(s, brightness=Level(error=error), contrast=contrast)
        instance = (edid.device_instance_id(s.device_path) or "").upper()
        value = levels.get(instance)
        if value is None and len(levels) == 1:
            value = next(iter(levels.values()))
        if value is None:
            brightness = Level(error="Windows does not expose brightness for this display")
        else:
            brightness = Level(value)
        return replace(s, brightness=brightness, contrast=contrast)

    def _ddc_skip_reason(self, key: str) -> str | None:
        failure = self._ddc_failures.get(key)
        if failure and time.monotonic() - failure[0] < DDC_FAILURE_TTL:
            return failure[1]
        return None

    def _with_ddc_levels(self, s: Screen, preemptible: bool = False) -> Screen:
        skip = self._ddc_skip_reason(s.key)
        if skip:
            return replace(s, brightness=Level(error=skip), contrast=Level(error=skip))
        try:
            with self._physical_handle(s) as handle:
                levels: dict[Feature, Level] = {}
                for feature in Feature:
                    if preemptible and self.preempt.is_set():
                        raise _Preempted
                    try:
                        cur, mx = ddc.get_vcp(handle, feature.vcp_code)
                        self._vcp_max[(s.key, feature.vcp_code)] = mx
                        levels[feature] = Level(_to_percent(cur, mx))
                    except ddc.DdcError as e:
                        levels[feature] = Level(error=e.message)
                        if feature is Feature.BRIGHTNESS:
                            # Silent on the first feature (every DDC monitor has
                            # brightness): it is not answering at all, so don't
                            # spend another round of retries on contrast.
                            levels[Feature.CONTRAST] = Level(error=e.message)
                            break
        except Win32Error as e:
            levels = {f: Level(error=e.message) for f in Feature}

        if not any(level.ok for level in levels.values()):
            # Remember total failures so we don't stall every keystroke on them.
            self._ddc_failures[s.key] = (time.monotonic(), levels[Feature.BRIGHTNESS].error or "")
        else:
            self._ddc_failures.pop(s.key, None)
        return replace(s, brightness=levels[Feature.BRIGHTNESS], contrast=levels[Feature.CONTRAST])

    def _physical_handle(self, s: Screen):  # noqa: ANN202 - context manager
        return _PhysicalHandle(s)

    def _find(self, key: str) -> Screen:
        for s in self.topology():
            if s.key == key:
                return s
        raise Win32Error("That screen is no longer connected")

    def set_levels(
        self, keys: list[str], feature: Feature, value: int | None, delta: int | None
    ) -> dict[str, int | Win32Error]:
        """Apply a change to several screens in parallel. Never raises per screen."""

        by_key = {s.key: s for s in self.topology()}

        def one(key: str) -> int | Win32Error:
            s = by_key.get(key)
            if s is None:
                return Win32Error("That screen is no longer connected")
            try:
                return self._set_level_on(s, feature, value, delta)
            except Win32Error as e:
                return e

        futures = {key: self._pool.submit(one, key) for key in keys}
        return {key: f.result() for key, f in futures.items()}

    def set_level(self, key: str, feature: Feature, value: int | None, delta: int | None) -> int:
        result = self.set_levels([key], feature, value, delta)[key]
        if isinstance(result, Win32Error):
            raise result
        return result

    def _set_level_on(self, s: Screen, feature: Feature, value: int | None, delta: int | None) -> int:
        key = s.key
        if s.internal:
            if feature is Feature.CONTRAST:
                raise Win32Error("Built-in displays do not expose a contrast control")
            if delta is not None:
                levels = wmi.get_internal_brightness()
                instance = (edid.device_instance_id(s.device_path) or "").upper()
                current = levels.get(instance, next(iter(levels.values()), None) if len(levels) == 1 else None)
                if current is None:
                    raise Win32Error("Could not read the current brightness")
                value = current + delta
            assert value is not None
            value = _clamp(value)
            wmi.set_internal_brightness(edid.device_instance_id(s.device_path), value)
            return value

        code = feature.vcp_code
        maximum = self._vcp_max.get((key, code))
        with self._physical_handle(s) as handle:
            if delta is not None or maximum is None:
                try:
                    current_raw, maximum = ddc.get_vcp(handle, code)
                except ddc.DdcError:
                    self._ddc_failures.pop(key, None)
                    raise
                self._vcp_max[(key, code)] = maximum
                if delta is not None:
                    value = _to_percent(current_raw, maximum) + delta
            assert value is not None
            value = _clamp(value)
            ddc.set_vcp(handle, code, round(value * maximum / 100))
        self._ddc_failures.pop(key, None)
        return value

    def apply_settings(self, plan: dict[str, dict]) -> dict[str, list[str]]:
        """Apply {screen key: {"brightness", "contrast", "hdr"}}; return errors per key."""

        by_key = {s.key: s for s in self.topology()}
        errors: dict[str, list[str]] = {key: [] for key in plan}

        def levels(key: str) -> None:
            s = by_key.get(key)
            if s is None:
                errors[key].append("no longer connected")
                return
            for feature in Feature:
                value = plan[key].get(feature.value)
                if value is None:
                    continue
                try:
                    self._set_level_on(s, feature, int(value), None)
                except Win32Error as e:
                    errors[key].append(f"{feature.value}: {e.message}")

        for f in [self._pool.submit(levels, key) for key in plan]:
            f.result()
        # HDR changes reconfigure the displays: one at a time, after the DDC work.
        for key, settings in plan.items():
            want = settings.get("hdr")
            s = by_key.get(key)
            if want is None or s is None or not (s.hdr and s.hdr.supported):
                continue
            if s.hdr.enabled != bool(want):
                try:
                    self.set_hdr(key, bool(want))
                except Win32Error as e:
                    errors[key].append(f"HDR: {e.message}")
        return {k: v for k, v in errors.items() if v}

    def set_hdr(self, key: str, enable: bool | None) -> displayconfig.HdrState:
        s = self._find(key)
        state = s.hdr
        if state is None or not state.supported:
            raise Win32Error(f"{s.name} does not support HDR")
        target = (not state.enabled) if enable is None else enable
        if target != state.enabled:
            displayconfig.set_hdr(s.adapter_id, s.target_id, target, state.modern_api)
        # Windows applies the change asynchronously; poll briefly for it.
        deadline = time.monotonic() + 3.0
        new_state = state
        while time.monotonic() < deadline:
            new_state = displayconfig.get_hdr_state(s.adapter_id, s.target_id) or state
            if new_state.enabled == target:
                break
            time.sleep(0.1)
        return new_state


class _PhysicalHandle:
    """Context manager yielding the DDC/CI handle of one screen."""

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        self._cm = None

    def __enter__(self) -> int:
        s = self.screen
        self._cm = ddc.open_physical_monitors(s.hmonitor)
        physical = self._cm.__enter__()
        if not physical:
            self._cm.__exit__(None, None, None)
            raise Win32Error("Windows gave no DDC/CI handle for this screen")
        if len(physical) == 1:
            return physical[0].handle
        # Duplicate/clone mode: several monitors share one desktop. Pair them up
        # using the attached-monitor order, which dxva2 follows.
        try:
            interfaces = [p.lower() for p in gdi.enum_attached_monitor_interfaces(s.gdi_device_name)]
            if len(interfaces) == len(physical) and s.key in interfaces:
                return physical[interfaces.index(s.key)].handle
        except Exception:
            log.exception("Could not pair physical monitors")
        return physical[0].handle

    def __exit__(self, *exc: object) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)


def _pnp_from_path(device_path: str) -> str | None:
    instance = edid.device_instance_id(device_path)
    if not instance:
        return None
    hardware_id = instance.split("\\")[1]
    return hardware_id[:3] if len(hardware_id) >= 3 else None


def _to_percent(current: int, maximum: int) -> int:
    return _clamp(round(current * 100 / maximum)) if maximum else 0


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


# --------------------------------------------------------------------------- #
# Async facade
# --------------------------------------------------------------------------- #


class ScreenService:
    nickname_for: Callable[[str], str | None] = staticmethod(lambda identity: None)  # type: ignore[assignment]
    """Set by the plugin to decorate screens with user nicknames."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-io")
        self._hw = _Hardware()
        self._cache: tuple[float, list[Screen]] | None = None
        self._pending: asyncio.Future[list[Screen]] | None = None
        self._busy_since: float | None = None
        self.resets = 0
        self._keeper: asyncio.Task[None] | None = None

    def _timed(self, fn: Callable[..., T], *args: object) -> T:
        started = time.monotonic()
        self._busy_since = started
        try:
            return fn(*args)
        finally:
            # An abandoned call finishing late must not clear the timer of a newer one.
            if self._busy_since == started:
                self._busy_since = None

    def _submit(self, fn: Callable[..., T], *args: object) -> asyncio.Future[T]:
        self._recover_if_stuck()
        return asyncio.get_running_loop().run_in_executor(self._executor, self._timed, fn, *args)

    def _recover_if_stuck(self) -> None:
        busy = self._busy_since
        if busy is not None and time.monotonic() - busy > STUCK_AFTER:
            log.error("A hardware call has been stuck for %.0f s: replacing the workers", time.monotonic() - busy)
            self.reset(clear_cache=False)
            stuck = sum(t.name.startswith("screen-") for t in threading.enumerate())
            if stuck > MAX_STUCK_THREADS:
                # They can't be stopped from here. The supervisor starts a clean process.
                log.critical("%s threads are stuck in hardware calls, restarting the plugin", stuck)
                logging.shutdown()
                os._exit(1)

    def reset(self, clear_cache: bool = True) -> None:
        """Start over: fresh worker threads, monitor handles and failure memory.

        A stuck call keeps its (abandoned) thread; everything new runs on new ones.
        """
        old_executor, old_hw = self._executor, self._hw
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-io")
        self._hw = _Hardware()
        self._pending = None
        self._busy_since = None
        if clear_cache:
            self._cache = None
        self.resets += 1
        old_executor.shutdown(wait=False, cancel_futures=True)
        old_hw._pool.shutdown(wait=False, cancel_futures=True)
        log.info("Screen service reset (#%s)", self.resets)

    def _user_command(self, fn: Callable[..., T], *args: object) -> T:
        self._hw.preempt.clear()
        return fn(*args)

    async def _run(self, fn: Callable[..., T], *args: object, timeout: float) -> T:
        """Run a user command (a write) ahead of any background refresh."""
        if self._pending is not None and not self._pending.done():
            self._hw.preempt.set()
        fut = self._submit(self._user_command, fn, *args)
        # shield: a cancelled Flow query must not orphan an in-flight hardware
        # call; it finishes on the worker and its result is simply dropped.
        fut.add_done_callback(_consume_exception)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            self._recover_if_stuck()
            raise Win32Error("The screen took too long to answer") from None

    def _start_refresh(self) -> asyncio.Future[list[Screen]]:
        # A stuck refresh must not be shared forever: check before reusing it.
        self._recover_if_stuck()
        # Coalesce concurrent refreshes (Flow fires a query per keystroke).
        if self._pending is None or self._pending.done():
            # With values already on screen, this refresh may yield to user commands.
            self._pending = self._submit(self._hw.snapshot, self._cache is not None)
            self._pending.add_done_callback(self._store_snapshot)
        return self._pending

    def warm_up(self) -> None:
        """Read the screens now and keep them fresh in the background."""
        self._start_refresh()
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.get_running_loop().create_task(self._keep_fresh(), name="keep-fresh")

    async def _keep_fresh(self) -> None:
        while True:
            await asyncio.sleep(KEEP_FRESH_EVERY / 2)
            cache = self._cache
            if cache and time.monotonic() - cache[0] < KEEP_FRESH_EVERY:
                continue  # a query or a write refreshed it recently
            try:
                await asyncio.wait_for(asyncio.shield(self._start_refresh()), READ_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Background refresh failed", exc_info=True)

    async def screens(self, max_age: float = CACHE_TTL) -> list[Screen]:
        return [self._decorate(s) for s in await self._raw_screens(max_age)]

    def _decorate(self, s: Screen) -> Screen:
        nickname = self.nickname_for(s.stable_id)
        return replace(s, nickname=nickname) if nickname != s.nickname else s

    async def apply_settings(self, plan: dict[str, dict]) -> dict[str, list[str]]:
        try:
            return await self._run(self._hw.apply_settings, plan, timeout=WRITE_TIMEOUT * 2)
        finally:
            self.invalidate()

    async def _raw_screens(self, max_age: float = CACHE_TTL) -> list[Screen]:
        cache = self._cache
        age = time.monotonic() - cache[0] if cache else None
        if cache and age is not None and age < max_age:
            return cache[1]

        pending = self._start_refresh()
        if cache and age is not None and age < STALE_OK:
            # Recent enough: answer now, the refresh lands for the next keystroke.
            return cache[1]

        # Old or no values: wait for fresh ones, but never long when we have
        # something to show (a stuck monitor must not freeze the list).
        timeout = STALE_WAIT if cache else READ_TIMEOUT
        try:
            return await asyncio.wait_for(asyncio.shield(pending), timeout)
        except _Preempted:
            assert cache is not None  # only refreshes with values to fall back on yield
            return cache[1]
        except asyncio.TimeoutError:
            if cache:
                log.info("Screen refresh is slow, showing values from %.0f s ago", time.monotonic() - cache[0])
                return cache[1]
            raise Win32Error("The screens took too long to answer") from None

    def _store_snapshot(self, fut: asyncio.Future[list[Screen]]) -> None:
        if fut.cancelled():
            return
        if fut.exception() is None:
            self._cache = (time.monotonic(), fut.result())

    def invalidate(self) -> None:
        self._cache = None

    def _patch_cache(self, key: str, **changes: object) -> None:
        if not self._cache:
            return
        taken, screens = self._cache
        self._cache = (taken, [replace(s, **changes) if s.key == key else s for s in screens])  # type: ignore[arg-type]

    async def set_level(
        self, key: str, feature: Feature, *, value: int | None = None, delta: int | None = None
    ) -> int:
        if (value is None) == (delta is None):
            raise ValueError("Pass exactly one of value or delta")
        new_value = await self._run(self._hw.set_level, key, feature, value, delta, timeout=WRITE_TIMEOUT)
        # Monitors often report the old value for a moment after a write, so
        # trust what we wrote instead of re-reading immediately.
        self._patch_cache(key, **{feature.value: Level(new_value)})
        return new_value

    async def set_levels(
        self, keys: list[str], feature: Feature, *, value: int | None = None, delta: int | None = None
    ) -> dict[str, int | Win32Error]:
        """Change several screens at once (in parallel). Per-screen failures are returned, not raised."""
        if (value is None) == (delta is None):
            raise ValueError("Pass exactly one of value or delta")
        results = await self._run(self._hw.set_levels, keys, feature, value, delta, timeout=WRITE_TIMEOUT)
        for key, result in results.items():
            if not isinstance(result, Win32Error):
                self._patch_cache(key, **{feature.value: Level(result)})
        return results

    async def set_hdr(self, key: str, enable: bool | None) -> displayconfig.HdrState:
        try:
            return await self._run(self._hw.set_hdr, key, enable, timeout=WRITE_TIMEOUT)
        finally:
            self.invalidate()  # HDR changes can alter modes, bit depth...

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._hw._pool.shutdown(wait=False, cancel_futures=True)


def _consume_exception(fut: asyncio.Future[object]) -> None:
    # Mark the exception as retrieved so asyncio does not log
    # "Future exception was never retrieved" when every waiter gave up.
    if not fut.cancelled():
        fut.exception()

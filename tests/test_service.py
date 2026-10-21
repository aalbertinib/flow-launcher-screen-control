"""Failure-path tests for the screen service (no hardware needed)."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from screen_control import screens as screens_mod
from screen_control.screens import Feature, Level, ScreenService, _Hardware
from screen_control.win32 import Win32Error, ddc

from .test_logic import _screen


class ServiceTests(unittest.TestCase):
    def test_concurrent_queries_share_one_hardware_read(self) -> None:
        calls = 0
        lock = threading.Lock()

        def slow_snapshot(*_: object) -> list:
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.2)
            return [_screen(1, "A", "")]

        async def scenario() -> list:
            service = ScreenService()
            service._hw.snapshot = slow_snapshot  # type: ignore[method-assign]
            try:
                return await asyncio.gather(*(service.screens() for _ in range(10)))
            finally:
                service.close()

        results = asyncio.run(scenario())
        self.assertEqual(calls, 1)
        self.assertTrue(all(r == results[0] for r in results))

    def test_cancelled_query_does_not_break_the_next_one(self) -> None:
        async def scenario() -> list:
            service = ScreenService()
            service._hw.snapshot = lambda *_: (time.sleep(0.2), [_screen(1, "A", "")])[1]  # type: ignore[method-assign]
            try:
                first = asyncio.ensure_future(service.screens())
                await asyncio.sleep(0.05)
                first.cancel()  # Flow cancels the previous query on each keystroke
                return await service.screens()
            finally:
                service.close()

        self.assertEqual(len(asyncio.run(scenario())), 1)

    def test_snapshot_error_is_reported_then_retried(self) -> None:
        attempts = 0

        def flaky(*_: object) -> list:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Win32Error("boom")
            return [_screen(1, "A", "")]

        async def scenario() -> list:
            service = ScreenService()
            service._hw.snapshot = flaky  # type: ignore[method-assign]
            try:
                with self.assertRaises(Win32Error):
                    await service.screens()
                return await service.screens()
            finally:
                service.close()

        self.assertEqual(len(asyncio.run(scenario())), 1)

    def test_hung_hardware_times_out_instead_of_freezing(self) -> None:
        release = threading.Event()

        async def scenario() -> None:
            service = ScreenService()
            service._hw.snapshot = lambda *_: release.wait(5) and []  # type: ignore[method-assign]
            try:
                with mock.patch.object(screens_mod, "READ_TIMEOUT", 0.1):
                    with self.assertRaises(Win32Error):
                        await service.screens()
            finally:
                release.set()
                service.close()

        asyncio.run(scenario())

    def test_slow_refresh_falls_back_to_previous_values(self) -> None:
        release = threading.Event()
        calls = 0

        def snapshot(*_: object) -> list:
            nonlocal calls
            calls += 1
            if calls > 1:
                release.wait(5)  # a monitor stalls on the second refresh
            return [_screen(1, "A", "")]

        async def scenario() -> list:
            service = ScreenService()
            service._hw.snapshot = snapshot  # type: ignore[method-assign]
            try:
                await service.screens()
                with mock.patch.object(screens_mod, "STALE_WAIT", 0.1):
                    return await service.screens(max_age=0)
            finally:
                release.set()
                service.close()

        self.assertEqual(len(asyncio.run(scenario())), 1)

    def test_write_updates_cache_without_rereading(self) -> None:
        async def scenario() -> Level:
            service = ScreenService()
            service._hw.snapshot = lambda *_: [_screen(1, "A", "")]  # type: ignore[method-assign]
            service._hw.set_level = lambda key, feature, value, delta: 77  # type: ignore[method-assign]
            try:
                await service.screens()
                await service.set_level("path1", Feature.CONTRAST, value=77)
                return (await service.screens())[0].contrast
            finally:
                service.close()

        self.assertEqual(asyncio.run(scenario()), Level(77))


class PerformanceTests(unittest.TestCase):
    def test_recent_cache_answers_at_once_while_refreshing(self) -> None:
        release = threading.Event()
        calls = 0

        def snapshot(*_: object) -> list:
            nonlocal calls
            calls += 1
            if calls > 1:
                release.wait(5)
            return [_screen(1, "A", "")]

        async def scenario() -> float:
            service = ScreenService()
            service._hw.snapshot = snapshot  # type: ignore[method-assign]
            try:
                await service.screens()
                t = time.perf_counter()
                await service.screens(max_age=0)  # expired, but recent: no waiting
                return time.perf_counter() - t
            finally:
                release.set()
                service.close()

        self.assertLess(asyncio.run(scenario()), 0.05)

    def test_set_levels_runs_screens_in_parallel_and_reports_each(self) -> None:
        hw = _Hardware()
        screens = [_screen(1, "A", ""), _screen(2, "B", "")]

        def set_on(s, feature, value, delta):  # noqa: ANN001, ANN202
            time.sleep(0.2)
            if s.number == 2:
                raise Win32Error("no answer")
            return value

        with mock.patch.object(hw, "topology", lambda: screens), mock.patch.object(hw, "_set_level_on", set_on):
            t = time.perf_counter()
            results = hw.set_levels(["path1", "path2", "gone"], Feature.BRIGHTNESS, 30, None)
            elapsed = time.perf_counter() - t
        hw._pool.shutdown()
        self.assertLess(elapsed, 0.35)  # parallel, not 0.4 s
        self.assertEqual(results["path1"], 30)
        self.assertIsInstance(results["path2"], Win32Error)
        self.assertIsInstance(results["gone"], Win32Error)

    def test_absolute_write_skips_read_once_maximum_is_known(self) -> None:
        hw = _Hardware()
        screen = _screen(1, "A", "")
        reads: list[int] = []
        writes: list[tuple[int, int]] = []

        @contextmanager
        def fake_handle(_screen: object):  # noqa: ANN202
            yield 1

        def get_vcp(handle: int, code: int) -> tuple[int, int]:
            reads.append(code)
            return (10, 200)

        with mock.patch.object(ddc, "get_vcp", get_vcp), mock.patch.object(
            ddc, "set_vcp", lambda h, code, raw: writes.append((code, raw))
        ), mock.patch.object(hw, "_physical_handle", fake_handle):
            hw._set_level_on(screen, Feature.BRIGHTNESS, 50, None)  # max unknown: read
            hw._set_level_on(screen, Feature.BRIGHTNESS, 60, None)  # max cached: no read
            hw._set_level_on(screen, Feature.BRIGHTNESS, None, 5)  # relative: must read
        hw._pool.shutdown()
        self.assertEqual(len(reads), 2)
        self.assertEqual(writes, [(ddc.VCP_BRIGHTNESS, 100), (ddc.VCP_BRIGHTNESS, 120), (ddc.VCP_BRIGHTNESS, 20)])


class DdcFailureTests(unittest.TestCase):
    def test_unresponsive_monitor_is_skipped_for_a_while(self) -> None:
        hw = _Hardware()
        screen = _screen(1, "A", "")
        calls = 0

        def failing_get_vcp(handle: int, code: int) -> tuple[int, int]:
            nonlocal calls
            calls += 1
            raise ddc.DdcError("no answer")

        @contextmanager
        def fake_handle(_screen: object):  # noqa: ANN202
            yield 1

        with mock.patch.object(ddc, "get_vcp", failing_get_vcp), mock.patch.object(
            hw, "_physical_handle", fake_handle
        ):
            first = hw._with_ddc_levels(screen)
            second = hw._with_ddc_levels(screen)

        self.assertFalse(first.brightness.ok)
        self.assertEqual(first.brightness.error, "no answer")
        self.assertEqual(second.brightness.error, "no answer")
        self.assertEqual(calls, 1)  # brightness failed: contrast not tried, then skipped while cached

    def test_partial_support_keeps_working_feature(self) -> None:
        hw = _Hardware()

        def get_vcp(handle: int, code: int) -> tuple[int, int]:
            if code == ddc.VCP_CONTRAST:
                raise ddc.DdcError("unsupported")
            return (50, 100)

        @contextmanager
        def fake_handle(_screen: object):  # noqa: ANN202
            yield 1

        with mock.patch.object(ddc, "get_vcp", get_vcp), mock.patch.object(hw, "_physical_handle", fake_handle):
            s = hw._with_ddc_levels(_screen(1, "A", ""))
        self.assertEqual(s.brightness, Level(50))
        self.assertFalse(s.contrast.ok)


if __name__ == "__main__":
    unittest.main()


class PreemptionTests(unittest.TestCase):
    def test_background_refresh_yields_to_a_user_command(self) -> None:
        hw = _Hardware()
        screens = [_screen(1, "A", ""), _screen(2, "B", "")]

        @contextmanager
        def fake_handle(_screen: object):  # noqa: ANN202
            yield 1

        def slow_read(handle: int, code: int) -> tuple[int, int]:
            time.sleep(0.06)  # one DDC/CI read
            return (50, 100)

        with mock.patch.object(hw, "topology", lambda: screens), mock.patch.object(
            hw, "_physical_handle", fake_handle
        ), mock.patch.object(ddc, "get_vcp", slow_read):
            t = time.perf_counter()
            threading.Timer(0.03, hw.preempt.set).start()  # the user presses Enter mid-refresh
            with self.assertRaises(screens_mod._Preempted):
                hw.snapshot(preemptible=True)
            yielded_after = time.perf_counter() - t
            hw.preempt.set()
            self.assertEqual(len(hw.snapshot(preemptible=False)), 2)  # a refresh someone waits on never yields
        hw._pool.shutdown()
        self.assertLess(yielded_after, 0.2)  # instead of 4 reads x 60 ms

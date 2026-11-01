"""Self-healing inside the plugin process: stuck hardware, failing handlers, resets."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from unittest import mock

from screen_control import screens as screens_mod
from screen_control.query_parser import Action, parse_query
from screen_control.screens import ScreenService
from screen_control.views import build_results
from screen_control.win32 import Win32Error

from .test_logic import SCREENS, _screen


class FakeHardware:
    hang = threading.Event()  # set: snapshot blocks until released
    release = threading.Event()

    def __init__(self) -> None:
        self._pool = mock.Mock()

    def snapshot(self, preemptible: bool = False) -> list:
        if FakeHardware.hang.is_set():
            FakeHardware.release.wait(5)
        return [_screen(1, "A", "")]


class WatchdogTests(unittest.TestCase):
    def test_stuck_hardware_call_is_abandoned_and_service_recovers(self) -> None:
        FakeHardware.hang.set()
        FakeHardware.release.clear()

        async def scenario() -> list:
            with mock.patch.object(screens_mod, "_Hardware", FakeHardware), mock.patch.object(
                screens_mod, "READ_TIMEOUT", 0.3
            ), mock.patch.object(screens_mod, "STUCK_AFTER", 0.5):
                service = ScreenService()
                try:
                    with self.assertRaises(Win32Error):
                        await service.screens()  # the monitor driver never returns
                    FakeHardware.hang.clear()  # later calls behave
                    await asyncio.sleep(0.6)  # now past STUCK_AFTER
                    result = await service.screens()  # watchdog resets, fresh worker answers
                    self.assertEqual(service.resets, 1)
                    return result
                finally:
                    FakeHardware.release.set()
                    service.close()

        self.assertEqual(len(asyncio.run(scenario())), 1)

    def test_manual_reset_clears_cache_and_workers(self) -> None:
        async def scenario() -> None:
            with mock.patch.object(screens_mod, "_Hardware", FakeHardware):
                FakeHardware.hang.clear()
                service = ScreenService()
                try:
                    await service.screens()
                    old_executor = service._executor
                    service.reset()
                    self.assertIsNone(service._cache)
                    self.assertIsNot(service._executor, old_executor)
                    self.assertEqual(len(await service.screens()), 1)
                finally:
                    service.close()

        asyncio.run(scenario())


class PluginGuardTests(unittest.TestCase):
    def _plugin(self):  # noqa: ANN202
        from screen_control.plugin import ScreenControlPlugin

        plugin = ScreenControlPlugin()
        written: list[dict] = []

        async def write(data: bytes, drain: bool = True) -> None:
            written.append(json.loads(data))

        plugin.jsonrpc.write = write  # type: ignore[method-assign]
        return plugin, written

    def test_a_crashing_query_still_gets_an_answer(self) -> None:
        plugin, written = self._plugin()

        async def boom(line: str) -> None:
            raise RuntimeError("bug")

        plugin.jsonrpc.process_input = boom  # type: ignore[method-assign]
        line = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "query", "params": []})
        asyncio.run(plugin._process_safely(line))
        plugin.service.close()
        self.assertEqual(written[0]["id"], 7)
        self.assertIn("unexpected error", written[0]["result"]["result"][0]["title"])

    def test_a_crashing_action_still_gets_an_answer(self) -> None:
        plugin, written = self._plugin()

        async def boom(line: str) -> None:
            raise RuntimeError("bug")

        plugin.jsonrpc.process_input = boom  # type: ignore[method-assign]
        asyncio.run(plugin._process_safely(json.dumps({"id": 8, "method": "flogin.action.x", "params": []})))
        asyncio.run(plugin._process_safely("not json at all"))  # ignored, no exception
        plugin.service.close()
        self.assertEqual(written, [{"jsonrpc": "2.0", "id": 8, "result": {"hide": False}}])

    def test_outdated_action_is_answered_quietly(self) -> None:
        plugin, _ = self._plugin()

        async def scenario() -> object:
            task = plugin.process_action("flogin.action.does-not-exist")
            assert task is not None
            return await task

        response = asyncio.run(scenario())
        plugin.service.close()
        self.assertFalse(response.hide)  # type: ignore[attr-defined]


class ResetCommandTests(unittest.TestCase):
    def test_reset_command(self) -> None:
        for word in ("reset", "repair", "fix", "restart"):
            self.assertIs(parse_query(word).action, Action.RESET, word)
        self.assertIsNot(parse_query("profile reset night").action, Action.RESET)
        results = build_results(parse_query("reset"), SCREENS, "sc")
        self.assertEqual(results[0].title, "Reset ScreenControl")

    def test_screen_read_failure_row_resets_on_enter(self) -> None:
        from screen_control.results import ResetResult
        from screen_control.views import reset_result

        row = reset_result("Could not read your screens", "timeout")
        self.assertIsInstance(row, ResetResult)
        self.assertIn("Enter: reset", row.sub or "")


class StuckThreadLimitTests(unittest.TestCase):
    def _recover(self, threads: int) -> mock.Mock:
        async def scenario() -> None:
            service = screens_mod.ScreenService()
            service._busy_since = -1e9
            service._recover_if_stuck()
            service.close()

        fake = [threading.Thread(name=f"screen-io_{i}") for i in range(threads)]
        with mock.patch.object(screens_mod.threading, "enumerate", return_value=fake), mock.patch.object(
            screens_mod.os, "_exit"
        ) as leave, mock.patch.object(screens_mod.logging, "shutdown"):
            asyncio.run(scenario())
        return leave

    def test_a_few_stuck_threads_are_tolerated(self) -> None:
        self.assertFalse(self._recover(screens_mod.MAX_STUCK_THREADS).called)

    def test_too_many_restart_the_process(self) -> None:
        self._recover(screens_mod.MAX_STUCK_THREADS + 1).assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()


class KeepFreshTests(unittest.TestCase):
    def test_background_refresh_keeps_the_cache_young(self) -> None:
        calls = 0

        class CountingHardware(FakeHardware):
            def snapshot(self, preemptible: bool = False) -> list:
                nonlocal calls
                calls += 1
                return [_screen(1, "A", "")]

        async def scenario() -> float:
            with mock.patch.object(screens_mod, "_Hardware", CountingHardware), mock.patch.object(
                screens_mod, "KEEP_FRESH_EVERY", 0.2
            ):
                service = ScreenService()
                try:
                    service.warm_up()
                    await asyncio.sleep(1.0)  # no queries at all meanwhile
                    assert service._cache is not None
                    return time.monotonic() - service._cache[0]
                finally:
                    service.close()

        age = asyncio.run(scenario())
        self.assertGreaterEqual(calls, 3)  # warm-up + background refreshes
        self.assertLess(age, 0.5)

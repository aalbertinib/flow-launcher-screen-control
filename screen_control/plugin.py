from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from flogin import ErrorResponse, ExecuteResponse, Plugin, Query, QueryResponse, Result, SearchHandler

from .feedback import Feedback
from .panel import PanelServer
from .query_parser import parse_query
from .screens import ScreenService
from .store import Store, data_dir
from .views import build_results, error_result, reset_result
from .win32 import Win32Error

__all__ = ("ScreenControlPlugin",)

log = logging.getLogger(__name__)


def keyword_variants(keywords: list[str]) -> list[str]:
    """Case variants of ``keywords`` that are not registered yet."""
    variants: list[str] = []
    for keyword in keywords:
        if keyword == "*":
            continue  # no keyword: Flow sends every query
        if len(keyword) <= 4:
            forms = ["".join(chars) for chars in itertools.product(*((c.lower(), c.upper()) for c in keyword))]
        else:
            forms = [keyword.lower(), keyword.upper(), keyword.title()]
        variants += [f for f in forms if f not in keywords and f not in variants]
    return variants


class ScreenSearchHandler(SearchHandler["ScreenControlPlugin"]):
    """Single entry point: every query is parsed and turned into results."""

    async def callback(self, query: Query[Any]) -> list[Result]:
        assert self.plugin is not None
        store = self.plugin.store
        profiles = store.profiles()
        parsed = parse_query(query.text, profiles)
        try:
            screens = await self.plugin.service.screens()
        except Win32Error as e:
            log.warning("Could not read screens: %s", e)
            return [reset_result("Could not read your screens", e.message)]
        customized = frozenset(n for n in profiles if store.is_customized(n))
        feedback = self.plugin.feedback
        if feedback is not None and not feedback.shown_for(query.raw_text):
            feedback = None
        return build_results(parsed, screens, query.keyword, profiles, customized, feedback)

    async def on_error(self, query: Query[Any], error: Exception) -> list[Result]:
        # Show the problem in Flow instead of returning a JSON-RPC error,
        # which Flow reports as a plugin crash.
        log.exception("Search failed for %r", query.text, exc_info=error)
        return [
            reset_result(
                "ScreenControl hit an unexpected error",
                f"{type(error).__name__}: {error} — details in screen_control.log",
            )
        ]


class ScreenControlPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(
            # We configure logging ourselves (see main.py).
            disable_log_override_files=True,
            # Flow cancels the previous query on every keystroke. flogin turns a
            # cancelled search into ``None`` and then crashes serialising it, so
            # Flow never gets an answer. Queries are cheap (cached, and hardware
            # reads are coalesced), so just let them finish.
            ignore_cancellation_requests=True,
        )
        self.store = Store(data_dir(Path(__file__).resolve().parent.parent))
        self.feedback: Feedback | None = None
        """The last action, shown (with undo) at the top of the list it came from."""
        self.panel = PanelServer(self)
        self.service = ScreenService()
        self.service.nickname_for = self.store.nickname
        self.register_search_handler(ScreenSearchHandler())
        self.register_event(self._warm_up, "on_initialization")
        self.register_event(self._on_close, "on_close")  # Flow sends "close" when it exits

    def open_panel(self) -> None:
        """Show the settings window (served by this process while it is open)."""
        self.panel.open(asyncio.get_running_loop())

    async def _on_close(self, *_: Any) -> ExecuteResponse:
        self.panel.stop()
        self.service.close()
        return ExecuteResponse(hide=False)

    async def start(self) -> None:
        """Replaces flogin's loop, which spins forever once Flow closes the pipe and
        leaves a request unanswered when handling it raises. Standard library only
        (flogin's version needs aioconsole, which is GPL)."""
        reader = _StdinLines(asyncio.get_running_loop())
        self.jsonrpc.reader, self.jsonrpc.writer = reader, _StdoutWriter()  # type: ignore[assignment]
        tasks: set[asyncio.Task[None]] = set()
        async for raw in reader:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            task = asyncio.create_task(self._process_safely(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        log.info("Flow closed the connection, stopping")
        self.panel.stop()
        self.service.close()

    async def _process_safely(self, line: str) -> None:
        try:
            await self.jsonrpc.process_input(line)
        except Exception as error:
            log.exception("Could not handle message from Flow: %.300s", line)
            try:
                msg = json.loads(line)
            except ValueError:
                return  # not JSON: there is nothing to answer
            if not (isinstance(msg, dict) and "id" in msg and "method" in msg):
                return
            # Always answer, so Flow never waits on a request.
            response: QueryResponse | ExecuteResponse
            if msg["method"] == "query":
                response = QueryResponse(
                    [error_result("ScreenControl hit an unexpected error", f"{type(error).__name__}: {error}")]
                )
            elif msg["method"] == "context_menu":
                response = QueryResponse([])
            else:
                response = ExecuteResponse(hide=False)
            try:
                await self.jsonrpc.write(response.to_message(id=msg["id"]))
            except Exception:
                log.exception("Could not send the error answer")

    def process_action(self, method: str) -> asyncio.Task[ExecuteResponse] | None:
        task = super().process_action(method)
        if task is None and method.startswith("flogin.action."):
            # A result from an older list (e.g. a submenu opened before the last
            # keystroke): flogin would answer with an error. Do nothing, quietly.
            log.info("Ignored action from an outdated result list")
            return asyncio.create_task(_stay_open())
        return task

    async def _warm_up(self) -> None:
        # Read the screens while Flow finishes starting, so the first `sc` is instant.
        self.service.warm_up()
        await self._register_keyword_variants()

    async def _register_keyword_variants(self) -> None:
        """Flow matches action keywords exactly: make "SC" and "sC" reach the plugin too."""
        try:
            keywords = list(self.metadata.keywords or [])
            # Flow would take a keyword away from another plugin without a word, and list
            # one twice if a restarted plugin process registered it again.
            taken: set[str] = set()
            try:
                plugins = await asyncio.wait_for(self.api.get_all_plugins(), 3.0)
                taken.update(k for plugin in plugins for k in plugin.keywords or [])
            except Exception:
                log.warning("Could not read the keywords in use", exc_info=True)
            for keyword in keyword_variants(keywords):
                if keyword not in taken:
                    await asyncio.wait_for(self.api.add_keyword(self.metadata.id, keyword), 3.0)
        except Exception:
            log.exception("Could not register the keyword's upper-case variants")

    async def process_search_handlers(self, query: Query[Any]) -> QueryResponse | ErrorResponse:
        response = await super().process_search_handlers(query)
        # Belt and braces for the flogin bug above: never send a None result list.
        if isinstance(response, QueryResponse) and response.results is None:
            response.results = []
        return response

    async def process_context_menus(self, data: list[Any]) -> QueryResponse | ErrorResponse:
        response = await super().process_context_menus(data)
        if isinstance(response, QueryResponse) and response.results is None:
            response.results = []
        return response


async def _stay_open() -> ExecuteResponse:
    return ExecuteResponse(hide=False)


class _StdinLines:
    """Flow's messages, one line each, read on a thread.

    Flow's pipes are anonymous (not overlapped), which asyncio's Windows event
    loop cannot read directly.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        threading.Thread(target=self._pump, name="flow-stdin", daemon=True).start()

    def _push(self, item: bytes | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._lines.put_nowait, item)
        except RuntimeError:
            pass  # the loop is already closed: we are shutting down

    def _pump(self) -> None:
        try:
            for line in sys.stdin.buffer:
                self._push(line)
        except (OSError, ValueError, AttributeError):
            log.exception("Reading from Flow failed")
        finally:
            self._push(None)  # end of input

    def __aiter__(self) -> _StdinLines:
        return self

    async def __anext__(self) -> bytes:
        line = await self._lines.get()
        if line is None:
            raise StopAsyncIteration
        return line


class _StdoutWriter:
    """What flogin's JSON-RPC client needs from a stream writer."""

    def __init__(self) -> None:
        self._out = sys.stdout.buffer
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self._out.write(data)
            self._out.flush()

    async def drain(self) -> None:
        return None  # write() already flushed

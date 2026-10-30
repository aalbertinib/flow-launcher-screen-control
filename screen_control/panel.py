"""The settings panel (``sc settings``): profiles, nicknames and live values in a window.

Flow only offers flat text fields to Python plugins, so the panel is a page the
plugin serves itself while it is open, shown in a chromeless browser window
(Edge, Chrome, Brave or Vivaldi in "app mode", else a normal tab): nothing to
install. The server:

- listens on 127.0.0.1 only, on a random port, and checks the Host header;
- requires a token that is new every time the panel starts;
- stops by itself a little after the window is closed (the page pings it).

Requests are handled on the server's threads; everything that touches the
screens or the saved data runs on the plugin's asyncio loop (:class:`PanelApi`).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import webbrowser
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from . import identify
from .query_parser import profile_name_problem
from .results import current_settings, profile_plan
from .screens import Feature
from .store import DEFAULT_PROFILES, normalize_profile_name
from .views_extra import is_active
from .win32 import Win32Error

if TYPE_CHECKING:
    from .plugin import ScreenControlPlugin

__all__ = ("PanelApi", "PanelError", "PanelServer")

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("panel.html")
IDLE_STOP = 60.0
"""Seconds without a request before the server stops (the page pings every 10 s)."""
CALL_TIMEOUT = 30.0
MAX_BODY = 64 * 1024
WINDOW_SIZE = "1020,780"


class PanelError(Exception):
    """A request the panel should show as a message (bad input, unknown profile...)."""


def _level(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PanelError("Values must be numbers from 0 to 100")
    return max(0, min(100, round(value)))


def clean_settings(raw: Any) -> dict[str, dict[str, Any]]:
    """A profile as sent by the page: {screen id or "*": {brightness, contrast, hdr}}."""
    if not isinstance(raw, dict):
        raise PanelError("A profile is a list of screens and their values")
    settings: dict[str, dict[str, Any]] = {}
    for screen_id, values in raw.items():
        if not isinstance(screen_id, str) or not isinstance(values, dict):
            raise PanelError("A profile is a list of screens and their values")
        entry: dict[str, Any] = {}
        for feature in Feature:
            if values.get(feature.value) is not None:
                entry[feature.value] = _level(values[feature.value])
        if values.get("hdr") is not None:
            if not isinstance(values["hdr"], bool):
                raise PanelError("HDR is on or off")
            entry["hdr"] = values["hdr"]
        if entry:
            settings[screen_id[:512]] = entry
    return settings


class PanelApi:
    """What the panel can do. Every method runs on the plugin's asyncio loop."""

    def __init__(self, plugin: ScreenControlPlugin) -> None:
        self.plugin = plugin

    async def state(self, fresh: bool = False) -> dict[str, Any]:
        store = self.plugin.store
        try:
            screens = await self.plugin.service.screens(max_age=0 if fresh else 4.0)
        except Win32Error as e:
            raise PanelError(e.message) from None
        profiles = store.profiles()

        def level(s, feature: Feature) -> dict[str, Any]:  # noqa: ANN001
            lv = s.level(feature)
            return {"value": lv.value, "error": None if lv.ok else (lv.error or "Not available")}

        return {
            "screens": [
                {
                    "key": s.key,
                    "id": s.stable_id,
                    "number": s.number,
                    "name": s.name,
                    "nickname": s.nickname or "",
                    "title": s.title,
                    "color": identify.screen_color(s.number),
                    "tags": s.tags,
                    "brightness": level(s, Feature.BRIGHTNESS),
                    "contrast": level(s, Feature.CONTRAST),
                    "hdr": {"enabled": s.hdr.enabled} if s.hdr and s.hdr.supported else None,
                    "current": current_settings(s),
                }
                for s in screens
            ],
            "profiles": [
                {
                    "name": name,
                    "settings": settings,
                    "builtin": name in DEFAULT_PROFILES,
                    "customized": store.is_customized(name),
                    "defaults": DEFAULT_PROFILES.get(name),
                    "active": is_active(settings, screens),
                }
                for name, settings in profiles.items()
            ],
            "keyword": query.keyword if (query := self.plugin.last_query) and query.keyword != "*" else "",
        }

    async def save_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        store = self.plugin.store
        name = normalize_profile_name(str(body.get("name") or ""))
        original = normalize_profile_name(str(body.get("original") or "")) or None
        if not name:
            raise PanelError("Give the profile a name")
        problem = profile_name_problem(name)
        if problem:
            raise PanelError(problem)
        settings = clean_settings(body.get("settings"))
        if not settings:
            raise PanelError("The profile sets nothing: turn on at least one value")
        profiles = store.profiles()
        if original and original != name:
            if original in DEFAULT_PROFILES:
                raise PanelError("Default profiles keep their name: save a copy under a new name instead")
            if name in profiles and not body.get("overwrite"):
                raise PanelError(f"A profile “{name}” already exists")
        elif not original and name in profiles and not body.get("overwrite"):
            raise PanelError(f"A profile “{name}” already exists")
        store.save_profile(name, settings)
        if original and original != name:
            store.delete_profile(original)
        return {"saved": name, "state": await self.state()}

    async def delete_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        name = normalize_profile_name(str(body.get("name") or ""))
        if name not in self.plugin.store.profiles():
            raise PanelError(f"No profile “{name}”")
        self.plugin.store.delete_profile(name)
        return {"state": await self.state()}

    async def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply a profile's settings ({"settings": ...}, by screen id) or a plan ({"plan": ...}, by key)."""
        screens = await self.plugin.service.screens()
        if "plan" in body:
            keys = {s.key for s in screens}
            raw = body["plan"]
            plan = {k: v for k, v in clean_settings(raw).items() if k in keys}
        else:
            plan = profile_plan(clean_settings(body.get("settings")), screens)
        errors = await self.plugin.service.apply_settings(plan) if plan else {}
        titles = {s.key: s.title for s in screens}
        return {
            "errors": [f"{titles.get(k, k)}: {'; '.join(v)}" for k, v in errors.items()],
            "state": await self.state(),
        }

    async def set_level(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            feature = Feature(body.get("feature"))
        except ValueError:
            raise PanelError("Unknown setting") from None
        key = str(body.get("key") or "")
        result = (await self.plugin.service.set_levels([key], feature, value=_level(body.get("value")))).get(key)
        if isinstance(result, Win32Error):
            raise PanelError(result.message)
        if result is None:
            raise PanelError("That screen is no longer connected")
        return {"value": result}

    async def nickname(self, body: dict[str, Any]) -> dict[str, Any]:
        screen_id = str(body.get("id") or "")
        screens = await self.plugin.service.screens()
        if not any(s.stable_id == screen_id for s in screens):
            raise PanelError("That screen is no longer connected")
        self.plugin.store.set_nickname(screen_id, str(body.get("name") or ""))
        return {"state": await self.state()}

    async def identify(self, body: dict[str, Any]) -> dict[str, Any]:
        keys = body.get("keys")
        screens = await self.plugin.service.screens()
        identify.identify([s for s in screens if not keys or s.key in keys])
        return {}

    async def reset(self, _body: dict[str, Any]) -> dict[str, Any]:
        self.plugin.service.reset()
        self.plugin.service.warm_up()
        return {"state": await self.state(fresh=True)}


_ROUTES = {
    "/api/profile/save": "save_profile",
    "/api/profile/delete": "delete_profile",
    "/api/apply": "apply",
    "/api/level": "set_level",
    "/api/nickname": "nickname",
    "/api/identify": "identify",
    "/api/reset": "reset",
}


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("panel: " + format, *args)

    # -- checks ------------------------------------------------------------

    def _allowed(self, token: str | None) -> bool:
        panel = self.server.panel
        if self.headers.get("Host") != panel.host:
            return False  # a web page reaching 127.0.0.1 through its own domain name
        return token is not None and hmac.compare_digest(token, panel.token)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # -- requests ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self.server.panel.touch()
        url = urlsplit(self.path)
        if url.path == "/":
            token = (parse_qs(url.query).get("t") or [None])[0]
            if not self._allowed(token):
                self._send(403, "Open the panel from Flow Launcher: sc settings".encode(), "text/plain; charset=utf-8")
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if not self._allowed(self.headers.get("X-Token")):
            self._json(403, {"error": "This panel has expired: open it again from Flow (sc settings)"})
            return
        if url.path == "/api/ping":
            self._json(200, {"ok": True})
        elif url.path == "/api/state":
            fresh = parse_qs(url.query).get("fresh") == ["1"]
            self._call(lambda api: api.state(fresh))
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self.server.panel.touch()
        if not self._allowed(self.headers.get("X-Token")):
            self._json(403, {"error": "This panel has expired: open it again from Flow (sc settings)"})
            return
        method = _ROUTES.get(urlsplit(self.path).path)
        if method is None:
            self._json(404, {"error": "Not found"})
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "Expected JSON"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._json(413, {"error": "Request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "Invalid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "Invalid request"})
            return
        self._call(lambda api: getattr(api, method)(body))

    def _call(self, make) -> None:  # noqa: ANN001 - (PanelApi) -> coroutine
        panel = self.server.panel
        assert panel.loop is not None
        future = asyncio.run_coroutine_threadsafe(make(panel.api), panel.loop)
        try:
            self._json(200, future.result(CALL_TIMEOUT))
        except PanelError as e:
            self._json(400, {"error": str(e)})
        except Win32Error as e:
            self._json(400, {"error": e.message})
        except TimeoutError:
            future.cancel()
            self._json(504, {"error": "The screens took too long to answer"})
        except Exception as e:
            log.exception("Panel request failed: %s", self.path)
            self._json(500, {"error": f"Something went wrong: {e}"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    panel: PanelServer


class PanelServer:
    def __init__(self, plugin: ScreenControlPlugin) -> None:
        self.api = PanelApi(plugin)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.token = ""
        self.host = ""
        self._server: _Server | None = None
        self._last_request = 0.0
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{self.host}/?t={self.token}"

    def touch(self) -> None:
        self._last_request = time.monotonic()

    def start(self, loop: asyncio.AbstractEventLoop) -> str:
        """Start serving (if needed) and return the panel's address."""
        with self._lock:
            if self._server is None:
                self.loop = loop
                self.token = secrets.token_urlsafe(24)
                server = _Server(("127.0.0.1", 0), _Handler)
                server.panel = self
                self.host = f"127.0.0.1:{server.server_address[1]}"
                self._server = server
                self.touch()
                threading.Thread(target=server.serve_forever, name="settings-panel", daemon=True).start()
                threading.Thread(target=self._stop_when_idle, args=(server,), name="panel-idle", daemon=True).start()
                log.info("Settings panel serving on %s", self.host)
            self.touch()
            return self.url

    def open(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start serving and show the panel window."""
        open_window(self.start(loop))

    def _stop_when_idle(self, server: _Server) -> None:
        while self._server is server:
            time.sleep(5)
            if time.monotonic() - self._last_request > IDLE_STOP:
                log.info("Settings panel closed, stopping its server")
                self.stop()

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


# Browsers with an "app mode" (a window without tabs or address bar), by preference.
# Edge ships with Windows but can be removed, so any of these will do.
APP_MODE_BROWSERS = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe")
_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def app_mode_browser() -> str | None:
    """Path of the first installed app-mode browser, from Windows' App Paths registry."""
    for exe in APP_MODE_BROWSERS:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"{_APP_PATHS}\{exe}") as key:
                    path = str(winreg.QueryValue(key, None)).strip('"')
            except OSError:
                continue
            if path and os.path.isfile(path):
                return path
    return None


def open_window(url: str) -> None:
    """A chromeless browser window ("app mode"), else a tab in the default browser."""
    browser = app_mode_browser()
    if browser is not None:
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--window-size={WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return
        except OSError:
            log.exception("Could not start %s", browser)
    log.info("No app-mode browser found, opening the panel in the default browser")
    if not webbrowser.open(url):
        log.warning("Could not open a browser for the settings panel")

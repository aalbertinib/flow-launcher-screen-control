"""The settings panel: its API, input checks and the local server's protections."""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from screen_control.panel import PAGE, PanelApi, PanelError, PanelServer, clean_settings
from screen_control.query_parser import Action, parse_query
from screen_control.store import DEFAULT_PROFILES, Store

from .test_logic import SCREENS
from .test_reactive import _FakeService


class _Service(_FakeService):
    def reset(self) -> None:
        pass

    def warm_up(self) -> None:
        pass


def _plugin(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(service=_Service(), store=Store(Path(tmp)), last_query=None)


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


class ParseTests(unittest.TestCase):
    def test_settings_words(self) -> None:
        for word in ("settings", "config", "panel", "options", "réglages"):
            self.assertIs(parse_query(word).action, Action.SETTINGS, word)
        self.assertIsNotNone(__import__("screen_control.query_parser", fromlist=["x"]).profile_name_problem("settings"))


class CleanSettingsTests(unittest.TestCase):
    def test_keeps_values_and_drops_empty_entries(self) -> None:
        raw = {"*": {"brightness": 150, "hdr": False}, "id1": {"contrast": 49.6}, "id2": {}}
        self.assertEqual(clean_settings(raw), {"*": {"brightness": 100, "hdr": False}, "id1": {"contrast": 50}})

    def test_rejects_bad_input(self) -> None:
        for raw in ([], {"*": 5}, {"*": {"brightness": "50"}}, {"*": {"hdr": 1}}, {"*": {"brightness": True}}):
            with self.assertRaises(PanelError, msg=raw):
                clean_settings(raw)


class ApiTests(unittest.TestCase):
    def test_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = run(PanelApi(_plugin(tmp)).state())
            self.assertEqual([s["number"] for s in state["screens"]], [1, 2])
            self.assertEqual(state["screens"][1]["hdr"], {"enabled": True})
            self.assertEqual(state["screens"][0]["current"], {"brightness": 40, "contrast": 70, "hdr": False})
            self.assertEqual([p["name"] for p in state["profiles"]], list(DEFAULT_PROFILES))

    def test_save_rename_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = _plugin(tmp)
            api = PanelApi(plugin)
            run(api.save_profile({"name": "Work", "settings": {"*": {"brightness": 60}}}))
            self.assertEqual(plugin.store.profiles()["Work"], {"*": {"brightness": 60}})
            with self.assertRaises(PanelError):  # creating over an existing one needs confirmation
                run(api.save_profile({"name": "Work", "settings": {"*": {"brightness": 1}}}))
            run(api.save_profile({"name": "focus", "original": "Work", "settings": {"*": {"brightness": 55}}}))
            self.assertNotIn("Work", plugin.store.profiles())
            self.assertEqual(plugin.store.profiles()["focus"], {"*": {"brightness": 55}})
            run(api.delete_profile({"name": "focus"}))
            self.assertNotIn("focus", plugin.store.profiles())

    def test_save_refuses_bad_names_and_empty_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = PanelApi(_plugin(tmp))
            for body in ({"name": "hdr", "settings": {"*": {"brightness": 5}}},
                         {"name": "", "settings": {"*": {"brightness": 5}}},
                         {"name": "x", "settings": {"*": {}}},
                         {"name": "x", "original": "night", "settings": {"*": {"brightness": 5}}}):
                with self.assertRaises(PanelError, msg=body):
                    run(api.save_profile(body))

    def test_default_profile_can_be_edited_then_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = _plugin(tmp)
            api = PanelApi(plugin)
            run(api.save_profile({"name": "night", "original": "night", "settings": {"*": {"brightness": 3}}}))
            self.assertTrue(plugin.store.is_customized("night"))
            run(api.delete_profile({"name": "night"}))
            self.assertEqual(plugin.store.profiles()["night"], DEFAULT_PROFILES["night"])

    def test_apply_profile_settings_and_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = _plugin(tmp)
            api = PanelApi(plugin)
            run(api.apply({"settings": {"*": {"brightness": 10}, SCREENS[1].stable_id: {"contrast": 20}}}))
            self.assertEqual(plugin.service.applied[-1], {"path1": {"brightness": 10}, "path2": {"contrast": 20}})
            run(api.apply({"plan": {"path1": {"brightness": 40}, "unknown": {"brightness": 1}}}))
            self.assertEqual(plugin.service.applied[-1], {"path1": {"brightness": 40}})

    def test_level_and_nickname(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = _plugin(tmp)
            api = PanelApi(plugin)
            self.assertEqual(run(api.set_level({"key": "path1", "feature": "contrast", "value": 33})), {"value": 33})
            with self.assertRaises(PanelError):
                run(api.set_level({"key": "path1", "feature": "volume", "value": 3}))
            run(api.nickname({"id": SCREENS[0].stable_id, "name": "Desk"}))
            self.assertEqual(plugin.store.nickname(SCREENS[0].stable_id), "Desk")
            with self.assertRaises(PanelError):
                run(api.nickname({"id": "not-connected", "name": "X"}))


class ServerTests(unittest.TestCase):
    """A real server on 127.0.0.1, driven like the page (and like a hostile page) would."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.plugin = _plugin(self.tmp.name)
        self.panel = PanelServer(self.plugin)
        self.panel.start(self.loop)
        self.port = int(self.panel.host.rsplit(":", 1)[1])

    def tearDown(self) -> None:
        self.panel.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.tmp.cleanup()

    def request(self, method: str, path: str, body=None, headers=None):  # noqa: ANN001, ANN201
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, data

    def auth(self, **extra: str) -> dict:
        return {"X-Token": self.panel.token, "Content-Type": "application/json", **extra}

    def test_page_needs_the_token(self) -> None:
        status, body = self.request("GET", f"/?t={self.panel.token}")
        self.assertEqual(status, 200)
        self.assertIn(b"ScreenControl settings", body)
        self.assertEqual(self.request("GET", "/")[0], 403)
        self.assertEqual(self.request("GET", "/?t=wrong")[0], 403)

    def test_page_is_self_contained(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        icon = base64.b64encode((PAGE.parent.parent / "assets" / "app.png").read_bytes()).decode()
        self.assertIn(f"data:image/png;base64,{icon}", page, "panel.html's icon differs from assets/app.png")
        self.assertNotRegex(page, r"""(?:src|href)=["'](?!data:)""")
        self.assertEqual(self.request("GET", "/app.png")[0], 403)  # only the page and its API exist

    def test_api_needs_token_and_json(self) -> None:
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        status, body = self.request("GET", "/api/state", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["screens"]), 2)
        form = self.auth(**{"Content-Type": "text/plain"})
        self.assertEqual(self.request("POST", "/api/profile/save", {"name": "x"}, form)[0], 415)

    def test_other_host_names_are_refused(self) -> None:
        # A web page reaching this port through its own domain (DNS rebinding).
        status, _ = self.request("GET", "/api/state", headers=self.auth(Host=f"evil.example:{self.port}"))
        self.assertEqual(status, 403)

    def test_errors_come_back_as_messages(self) -> None:
        status, body = self.request("POST", "/api/profile/save", {"name": "hdr", "settings": {"*": {"brightness": 1}}}, self.auth())
        self.assertEqual(status, 400)
        self.assertIn("already a ScreenControl command", json.loads(body)["error"])

    def test_save_through_http(self) -> None:
        status, body = self.request("POST", "/api/profile/save", {"name": "work", "settings": {"*": {"brightness": 61}}}, self.auth())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.plugin.store.profiles()["work"], {"*": {"brightness": 61}})

    def test_a_new_start_changes_the_token(self) -> None:
        old = self.panel.token
        self.panel.stop()
        self.panel.start(self.loop)
        self.assertNotEqual(old, self.panel.token)
        self.port = int(self.panel.host.rsplit(":", 1)[1])
        self.assertEqual(self.request("GET", "/api/ping", headers={"X-Token": old})[0], 403)


if __name__ == "__main__":
    unittest.main()

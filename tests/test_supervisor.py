"""Fault injection for the supervisor: the plugin crashes, hangs, prints garbage
or cannot start, and Flow must always get an answer."""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from screen_control import supervisor as sup

FAKE_CHILD = textwrap.dedent(
    r'''
    import json, os, sys
    mode = sys.argv[1]
    if mode == "broken":
        sys.stderr.write("ImportError: lib is damaged\n"); sys.stderr.flush()
        sys.exit(1)
    for line in sys.stdin:
        msg = json.loads(line)
        if "method" not in msg or "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result = {"hide": False}
        elif msg["method"] == "query":
            text = msg["params"][0]["search"]
            if text == "crash":
                os._exit(3)
            if text == "hang":
                continue
            if text == "garbage":
                sys.stdout.write("this is not json\n"); sys.stdout.flush()
            result = {"result": [{"title": f"pid {os.getpid()}"}], "settingsChange": {}, "debugMessage": ""}
        else:
            result = {"hide": False}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\r\n")
        sys.stdout.flush()
    '''
)


class Flow:
    """Plays Flow's side of the pipes."""

    def __init__(self, mode: str = "ok") -> None:
        self.tmp = tempfile.TemporaryDirectory()
        script = Path(self.tmp.name) / "fake_child.py"
        script.write_text(FAKE_CHILD, encoding="utf-8")
        in_r, self._in_w = os.pipe()
        self._out_r, out_w = os.pipe()
        self.to_supervisor = os.fdopen(self._in_w, "wb", buffering=0)
        from_flow = os.fdopen(in_r, "rb")
        to_flow = os.fdopen(out_w, "wb", buffering=0)
        self.answers: queue.Queue[dict] = queue.Queue()
        self.supervisor = sup.Supervisor([sys.executable, "-I", str(script), mode], from_flow, to_flow)
        self.thread = threading.Thread(target=self.supervisor.run, daemon=True)
        self.thread.start()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 1

    def _read(self) -> None:
        with os.fdopen(self._out_r, "rb") as f:
            for line in f:
                self.answers.put(json.loads(line))

    def request(self, method: str, params: list) -> int:
        rid = self.next_id
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.to_supervisor.write((json.dumps(msg) + "\r\n").encode())
        return rid

    def query(self, text: str) -> int:
        return self.request("query", [{"search": text, "rawQuery": f"sc {text}", "isReQuery": False, "actionKeyword": "sc"}, {}])

    def answer(self, timeout: float = 15.0) -> dict:
        return self.answers.get(timeout=timeout)

    def titles(self, answer: dict) -> list[str]:
        return [r["title"] for r in answer["result"]["result"]]

    def close(self) -> None:
        self.to_supervisor.close()  # Flow exits: the supervisor stops and kills the child
        self.thread.join(10)
        self.tmp.cleanup()


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            mock.patch.dict(sup.REQUEST_TIMEOUTS, {"query": 2.0, "initialize": 5.0}),
            mock.patch.object(sup, "MAX_RESTARTS", 2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_normal_relay(self) -> None:
        flow = Flow()
        try:
            flow.request("initialize", [{}])
            self.assertEqual(flow.answer()["result"], {"hide": False})
            flow.query("hello")
            self.assertTrue(flow.titles(flow.answer())[0].startswith("pid "))
        finally:
            flow.close()

    def test_crash_is_answered_and_plugin_restarts(self) -> None:
        flow = Flow()
        try:
            flow.request("initialize", [{}])
            flow.answer()
            flow.query("hello")
            first_pid = flow.titles(flow.answer())[0]
            rid = flow.query("crash")
            answer = flow.answer()
            self.assertEqual(answer["id"], rid)
            self.assertIn("restarted after an error", flow.titles(answer)[0])
            time.sleep(1.5)  # backoff + restart + replayed initialize (whose answer Flow must not see)
            flow.query("hello")
            answer = flow.answer()
            self.assertTrue(flow.titles(answer)[0].startswith("pid "))
            self.assertNotEqual(flow.titles(answer)[0], first_pid)
            self.assertTrue(flow.answers.empty())  # no stray duplicate "initialize" answer
        finally:
            flow.close()

    def test_hang_is_answered_and_plugin_restarts(self) -> None:
        flow = Flow()
        try:
            flow.request("initialize", [{}])
            flow.answer()
            flow.query("hang")
            answer = flow.answer(timeout=10)
            self.assertIn("restarted", flow.titles(answer)[0])
            time.sleep(1.5)
            flow.query("hello")
            self.assertTrue(flow.titles(flow.answer())[0].startswith("pid "))
        finally:
            flow.close()

    def test_garbage_output_never_reaches_flow(self) -> None:
        flow = Flow()
        try:
            flow.query("garbage")
            self.assertTrue(flow.titles(flow.answer())[0].startswith("pid "))
        finally:
            flow.close()

    def test_repeated_crashes_stop_then_enter_restarts(self) -> None:
        flow = Flow()
        try:
            for _ in range(3):
                flow.query("crash")
                flow.answer()
                time.sleep(1.2)
            flow.query("hello")
            answer = flow.answer()
            self.assertIn("stopped after repeated errors", flow.titles(answer)[0])
            action = answer["result"]["result"][0]["jsonRPCAction"]["method"]
            self.assertEqual(action, sup.RESTART_METHOD)
            flow.request(action, [])  # the user presses Enter
            self.assertEqual(flow.answer()["result"], {"hide": False})
            time.sleep(0.5)
            flow.query("hello")
            self.assertTrue(flow.titles(flow.answer())[0].startswith("pid "))
        finally:
            flow.close()

    def test_plugin_that_cannot_start_never_blocks_flow(self) -> None:
        flow = Flow(mode="broken")
        try:
            time.sleep(2.5)  # it keeps failing until the supervisor gives up
            flow.request("initialize", [{}])
            self.assertEqual(flow.answer()["result"], {"hide": False})
            flow.query("hello")
            self.assertIn("ScreenControl", flow.titles(flow.answer())[0])
        finally:
            flow.close()

    def test_child_is_killed_when_flow_goes_away(self) -> None:
        flow = Flow()
        flow.query("hello")
        flow.answer()
        child = flow.supervisor._child
        assert child is not None
        flow.close()
        child.wait(timeout=5)
        self.assertIsNotNone(child.returncode)


class RouteQueryTests(unittest.TestCase):
    @staticmethod
    def _query(search: str, keyword: str = "") -> dict:
        raw = f"{keyword} {search}".strip()
        return {"id": 1, "method": "query", "params": [{"search": search, "rawQuery": raw, "actionKeyword": keyword}, {}]}

    def test_any_case_becomes_the_real_keyword(self) -> None:
        for typed in ("SC", "sC", "Sc"):
            for rest, raw in (("", "sc"), ("hdr  off", "sc hdr  off"), ("50", "sc 50")):
                query = sup.route_query(self._query(f"{typed} {rest}".strip()), ["sc"])["params"][0]
                self.assertEqual((query["actionKeyword"], query["search"], query["rawQuery"]), ("sc", rest, raw))

    def test_profile_names_keep_their_case(self) -> None:
        self.assertEqual(sup.route_query(self._query("SC Gaming"), ["sc"])["params"][0]["search"], "Gaming")

    def test_other_searches_are_not_for_the_plugin(self) -> None:
        for text in ("", "chrome", "scr", "SCREEN 50", "s c"):
            self.assertIsNone(sup.route_query(self._query(text), ["sc"]), text)

    def test_keyword_queries_and_odd_messages_pass_through(self) -> None:
        msg = self._query("50", "sc")
        self.assertIs(sup.route_query(msg, ["sc"]), msg)
        for odd in ({"id": 1}, {"id": 1, "params": []}, {"id": 1, "params": ["x"]}):
            self.assertIs(sup.route_query(odd, ["sc"]), odd)
        global_only = self._query("chrome")
        self.assertIs(sup.route_query(global_only, []), global_only)

    def _supervisor(self) -> tuple[sup.Supervisor, io.BytesIO]:
        out = io.BytesIO()
        s = sup.Supervisor(["unused"], io.BytesIO(), out)
        s._keywords = ["sc"]
        return s, out

    def test_unrelated_searches_are_answered_empty_even_while_restarting(self) -> None:
        s, out = self._supervisor()
        s._gave_up = True
        s._from_flow(json.dumps(self._query("chrome")).encode())
        self.assertEqual(json.loads(out.getvalue())["result"]["result"], [])

    def test_the_plugin_gets_the_rewritten_query(self) -> None:
        s, out = self._supervisor()
        s._child = object()
        with mock.patch.object(sup.Supervisor, "_write_child", return_value=True) as write:
            s._from_flow(json.dumps(self._query("SC 50")).encode())
        sent = json.loads(write.call_args.args[1])["params"][0]
        self.assertEqual((sent["actionKeyword"], sent["search"], sent["rawQuery"]), ("sc", "50", "sc 50"))
        self.assertEqual(out.getvalue(), b"")

    def test_initialize_gives_the_keywords(self) -> None:
        s, _ = self._supervisor()
        s._start_child = lambda: True
        params = [{"currentPluginMetadata": {"actionKeywords": ["sc", "*"]}}]
        s._from_flow(json.dumps({"id": 1, "method": "initialize", "params": params}).encode())
        self.assertEqual(s._keywords, ["sc"])


if __name__ == "__main__":
    unittest.main()

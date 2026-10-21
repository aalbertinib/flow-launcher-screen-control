"""Drive the plugin over JSON-RPC exactly like Flow Launcher does.

    python tools/rpc_harness.py [--python PATH] [--plugin-dir DIR] QUERY... [--run "QUERY" INDEX]

Each QUERY is sent as typed after the keyword and its results are printed.
``--run "QUERY" N`` then executes result N of that query (like pressing Enter),
answering any request the plugin makes back to Flow (ChangeQuery, ShowMsg...).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOW_PY = Path(os.path.expandvars(r"%APPDATA%\FlowLauncher\Environments\Python\PythonEmbeddable-v3.11.4\python.exe"))


class Harness:
    def __init__(self, python: Path, plugin_dir: Path) -> None:
        # Same bootstrap Flow 2.x uses for python_v2 plugins.
        code = (
            "import sys\n"
            f"sys.path.append(r'{plugin_dir / 'lib'}')\n"
            f"sys.path.append(r'{plugin_dir / 'lib' / 'win32'}')\n\n"
            "import runpy\n"
            f"runpy.run_path(r'{plugin_dir / 'main.py'}', None, '__main__')"
        )
        self.proc = subprocess.Popen(
            [str(python), "-B", "-c", code],
            cwd=plugin_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.next_id = 100
        self.answer_plugin = True  # False: simulate a Flow that never answers
        self.responses: dict[int, dict] = {}
        self.plugin_requests: list[dict] = []
        self.cond = threading.Condition()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            print("  [stderr]", line.decode(errors="replace").rstrip())

    def _reader(self) -> None:
        assert self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            msg = json.loads(line)
            if "method" in msg:  # plugin -> Flow request
                self.plugin_requests.append(msg)
                print(f"  [plugin -> flow] {msg['method']} {msg.get('params')}")
                if "id" in msg and self.answer_plugin:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
                continue
            with self.cond:
                self.responses[msg["id"]] = msg
                self.cond.notify_all()

    def _send(self, msg: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\r\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: list, timeout: float = 20) -> dict:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.wait(rid, timeout)

    def send_nowait(self, method: str, params: list) -> int:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def wait(self, rid: int, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        with self.cond:
            while rid not in self.responses:
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    raise TimeoutError(f"No response to request {rid} (plugin exit code {self.proc.poll()})")
                self.cond.wait(left)
            return self.responses.pop(rid)

    def initialize(self) -> None:
        meta = {
            "id": "35240a7f-b9c1-4aa1-9e60-251199c37ca6",
            "name": "ScreenControl",
            "author": "test",
            "version": "dev",
            "language": "python_v2",
            "description": "",
            "website": "",
            "disabled": False,
            "executeFilePath": str(ROOT / "main.py"),
            "executeFileName": "main.py",
            "pluginDirectory": str(ROOT),
            "actionKeyword": "sc",
            "actionKeywords": ["sc"],
            "icoPath": str(ROOT / "assets" / "app.png"),
        }
        print("initialize ->", self.request("initialize", [{"currentPluginMetadata": meta}]).get("result"))

    @staticmethod
    def query_params(text: str) -> list:
        raw = f"sc {text}".rstrip() if text else "sc "
        return [{"search": text, "rawQuery": raw, "isReQuery": False, "actionKeyword": "sc"}, {}]

    def query(self, text: str, show: bool = True) -> list[dict]:
        t = time.perf_counter()
        resp = self.request("query", self.query_params(text))
        ms = (time.perf_counter() - t) * 1000
        if "error" in resp:
            print(f"\nquery {text!r}: ERROR {resp['error']}")
            return []
        results = resp["result"]["result"]
        if show:
            print(f"\nquery {text!r} ({ms:.0f} ms, {len(results)} results)")
            for i, r in enumerate(results):
                bar = f" [bar {r['ProgressBar']}%]" if "ProgressBar" in r else ""
                print(f"  {i}. {r.get('title')!s}{bar}\n       {r.get('subTitle', '')}   ({r.get('icoPath')}, score {r.get('score')})")
        return results

    def run(self, text: str, index: int) -> dict:
        results = self.query(text, show=False)
        target = results[index]
        print(f"\nrun {text!r} #{index}: {target.get('title')}")
        resp = self.request(target["jsonRPCAction"]["method"], [])
        print("  ->", resp.get("result", resp.get("error")))
        return resp

    def menu(self, text: str, index: int) -> list[dict]:
        """Open the Shift+Enter submenu of result N, like Flow does."""
        results = self.query(text, show=False)
        target = results[index]
        resp = self.request("context_menu", [target["contextData"]])
        rows = resp.get("result", {}).get("result") or []
        print(f"\nmenu of {text!r} #{index} ({target.get('title')}): {len(rows)} rows")
        for i, r in enumerate(rows):
            bar = f" [bar {r['ProgressBar']}%]" if "ProgressBar" in r else ""
            print(f"  {i}. {r.get('title')}{bar}\n       {r.get('subTitle', '')}")
        if "error" in resp:
            print("  ERROR", resp["error"])
        return rows

    def close(self) -> None:
        self.proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", type=Path, default=FLOW_PY)
    ap.add_argument("--plugin-dir", type=Path, default=ROOT)
    ap.add_argument("--run", nargs=2, action="append", metavar=("QUERY", "INDEX"), default=[])
    ap.add_argument("--burst", action="store_true", help="simulate fast typing (overlapping queries)")
    ap.add_argument("--menu", nargs=2, action="append", metavar=("QUERY", "INDEX"), default=[],
                    help="open the Shift+Enter submenu of a result")
    ap.add_argument("--cancel", action="store_true", help="simulate typing with Flow's $/cancelRequest messages")
    ap.add_argument("queries", nargs="*")
    args = ap.parse_args()

    h = Harness(args.python, args.plugin_dir)
    try:
        h.initialize()
        for q in args.queries:
            h.query(q)
        if args.burst:
            text = "#2 contrast 55"
            t = time.perf_counter()
            ids = [h.send_nowait("query", h.query_params(text[:i])) for i in range(1, len(text) + 1)]
            answers = [h.wait(i) for i in ids]
            errors = [a for a in answers if "error" in a]
            print(f"\nburst: {len(ids)} overlapping queries answered in {(time.perf_counter() - t) * 1000:.0f} ms, {len(errors)} errors")
        if args.cancel:
            # What Flow really does while typing: each new query cancels the previous one.
            text = "#1 brightness 40"
            ids: list[int] = []
            for i in range(1, len(text) + 1):
                ids.append(h.send_nowait("query", h.query_params(text[:i])))
                if len(ids) > 1:
                    h._send({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": ids[-2]}})
            last = h.wait(ids[-1])
            results = last.get("result", {}).get("result") or []
            time.sleep(0.5)
            log = args.plugin_dir / "screen_control.log"
            crashes = log.read_text(encoding="utf-8", errors="replace").count("Traceback") if log.is_file() else 0
            print(f"\ncancel: last query -> {len(results)} results ({results[0]['title'] if results else 'none'}); "
                  f"tracebacks in screen_control.log: {crashes}")
        for text, index in args.menu:
            h.menu(text, int(index))
        for text, index in args.run:
            h.run(text, int(index))
        time.sleep(0.5)
        print("\nplugin still running:", h.proc.poll() is None)
    finally:
        h.close()


if __name__ == "__main__":
    main()

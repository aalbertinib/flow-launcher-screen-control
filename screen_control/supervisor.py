"""Keep the plugin alive: run it as a child process and restart it if it dies or hangs.

Python cannot catch a hard crash inside a Windows API called through ctypes
(an access violation kills the process), and Flow Launcher does not restart a
dead plugin. This supervisor sits between Flow and the real plugin:

- every line is relayed unchanged between Flow and the child;
- if the child exits, or a request goes unanswered for too long, the child is
  (re)started and Flow's pending requests are answered so Flow never waits;
- after repeated failures it stops retrying and shows a "press Enter to
  restart" result instead of crashing in a loop;
- the child is tied to this process with a Windows job object, so it can never
  outlive Flow.

Standard library only: this must work even when the plugin's code or its
dependencies are broken.
"""

from __future__ import annotations

import collections
import ctypes
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from ctypes import wintypes as wt
from typing import IO, Any

__all__ = ("RESTART_METHOD", "Supervisor")

log = logging.getLogger("screen_control.supervisor")

RESTART_METHOD = "screen_control.restart"
REQUEST_TIMEOUTS = {"initialize": 30.0, "query": 20.0, "context_menu": 20.0}
DEFAULT_TIMEOUT = 60.0  # actions (HDR switching, profiles) can legitimately take a while
MAX_RESTARTS = 5
RESTART_WINDOW = 300.0
_CREATE_NO_WINDOW = 0x08000000


def _encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg) + "\r\n").encode("utf-8")


class Supervisor:
    def __init__(
        self,
        child_cmd: list[str],
        flow_in: IO[bytes],
        flow_out: IO[bytes],
        cwd: str | None = None,
        icon: str = "assets/error.png",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.child_cmd = child_cmd
        self.flow_in = flow_in
        self.flow_out = flow_out
        self.cwd = cwd
        self.icon = icon
        self.clock = clock

        self._out_lock = threading.Lock()
        self._state = threading.RLock()
        self._child: subprocess.Popen[bytes] | None = None
        self._generation = 0
        self._pending: dict[Any, tuple[str, float]] = {}
        self._swallow: set[Any] = set()  # replayed "initialize" answers Flow must not see twice
        self._init_line: bytes | None = None
        self._restarts: collections.deque[float] = collections.deque()
        self._gave_up = False
        self._stopping = False
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        self._job = _KillOnCloseJob()

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        self._start_child()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        try:
            for line in self.flow_in:
                if line.strip():
                    self._from_flow(line)
        finally:
            log.info("Flow closed the connection, stopping")
            self._stopping = True
            self._kill_child()

    # ------------------------------------------------------------ child

    def _start_child(self) -> bool:
        with self._state:
            self._generation += 1
            generation = self._generation
            try:
                child = subprocess.Popen(
                    self.child_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.cwd,
                    creationflags=_CREATE_NO_WINDOW,
                )
            except OSError:
                log.exception("Could not start the plugin process")
                self._child = None
                return False
            self._child = child
            self._job.assign(child)
            log.info("Plugin process started (pid %s, generation %s)", child.pid, generation)
            stderr_reader = threading.Thread(target=self._child_stderr, args=(child,), daemon=True)
            stderr_reader.start()
            threading.Thread(target=self._from_child, args=(child, generation, stderr_reader), daemon=True).start()
            if self._init_line is not None:
                # A restarted child must be initialised like the first one was.
                try:
                    rid = json.loads(self._init_line).get("id")
                    self._swallow.add(rid)
                    self._pending[rid] = ("initialize", self.clock() + REQUEST_TIMEOUTS["initialize"])
                    self._write_child(child, self._init_line)
                except (ValueError, OSError):
                    log.exception("Could not replay initialize")
            return True

    def _kill_child(self) -> None:
        child = self._child
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass

    def _child_stderr(self, child: subprocess.Popen[bytes]) -> None:
        assert child.stderr is not None
        for raw in child.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                log.warning("plugin stderr: %s", text)
        try:
            child.stderr.close()
        except OSError:
            pass

    def _from_child(self, child: subprocess.Popen[bytes], generation: int, stderr_reader: threading.Thread) -> None:
        assert child.stdout is not None
        for line in child.stdout:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # Anything that is not JSON-RPC would corrupt Flow's stream: drop it.
                log.warning("Dropped non JSON-RPC output from the plugin: %.200r", line)
                continue
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                with self._state:
                    self._pending.pop(msg["id"], None)
                    if msg["id"] in self._swallow:
                        self._swallow.discard(msg["id"])
                        continue
            self._write_flow(line if line.endswith(b"\n") else line + b"\r\n")
        child.wait()
        stderr_reader.join(2.0)  # so the crash's traceback is in the log below
        for stream in (child.stdin, child.stdout):  # don't leak handles across restarts
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._on_child_exit(child, generation)

    def _on_child_exit(self, child: subprocess.Popen[bytes], generation: int) -> None:
        with self._state:
            if generation != self._generation or self._stopping:
                return
            log.error(
                "Plugin process exited (code %s). Last errors:\n%s",
                child.returncode,
                "\n".join(self._stderr_tail) or "(nothing on stderr: see screen_control.log)",
            )
            self._child = None
            self._fail_pending("ScreenControl restarted after an error: try again")

            now = self.clock()
            while self._restarts and now - self._restarts[0] > RESTART_WINDOW:
                self._restarts.popleft()
            if len(self._restarts) >= MAX_RESTARTS:
                self._gave_up = True
                log.error("Too many restarts, waiting for the user to restart ScreenControl")
                return
            delay = min(0.25 * 2 ** len(self._restarts), 4.0)
            self._restarts.append(now)
        time.sleep(delay)
        with self._state:
            if generation == self._generation and not self._stopping and self._child is None:
                self._start_child()

    def _watchdog(self) -> None:
        while not self._stopping:
            time.sleep(1.0)
            with self._state:
                now = self.clock()
                late = [(rid, m) for rid, (m, deadline) in self._pending.items() if now > deadline]
                if late and self._child is not None:
                    log.error("Plugin stopped answering (%s), restarting it", ", ".join(f"{m} #{rid}" for rid, m in late))
                    self._kill_child()  # _from_child sees EOF and restarts it

    # ------------------------------------------------------------- Flow side

    def _from_flow(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("Dropped invalid message from Flow: %.200r", line)
            return
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        rid = msg.get("id")

        if method is not None and rid is not None:  # request from Flow
            if method == RESTART_METHOD:
                self._manual_restart(rid)
                return
            if method == "initialize":
                # Answer at once: Flow's startup then waits for this small process
                # only, while the plugin keeps starting in the background. The
                # plugin still gets the message; its own answer is swallowed.
                self._init_line = line
                self._write_flow(_encode({"jsonrpc": "2.0", "id": rid, "result": {"hide": False}}))
                with self._state:
                    self._swallow.add(rid)
            with self._state:
                child = self._child
                if child is None or self._gave_up:
                    if method != "initialize":
                        self._answer_unavailable(rid, method)
                    else:
                        self._swallow.discard(rid)
                    return
                timeout = REQUEST_TIMEOUTS.get(method, DEFAULT_TIMEOUT)
                self._pending[rid] = (method, self.clock() + timeout)
            if not self._write_child(child, line):
                with self._state:
                    self._pending.pop(rid, None)
                    self._swallow.discard(rid)
                if method != "initialize":  # already answered
                    self._answer_unavailable(rid, method)
            return

        # Notifications ($/cancelRequest) and answers to the plugin's own requests.
        with self._state:
            child = self._child
        if child is not None:
            self._write_child(child, line)

    def _manual_restart(self, rid: Any) -> None:
        log.info("Restart requested by the user")
        with self._state:
            self._gave_up = False
            self._restarts.clear()
            self._kill_child()
            self._generation += 1  # the old child's exit must not trigger another restart
            self._child = None
            self._fail_pending("ScreenControl was restarted")
            self._start_child()
        self._write_flow(_encode({"jsonrpc": "2.0", "id": rid, "result": {"hide": False}}))

    # ------------------------------------------------------------- answers

    def _fail_pending(self, message: str) -> None:
        pending, self._pending = self._pending, {}
        for rid, (method, _deadline) in pending.items():
            if rid in self._swallow:
                self._swallow.discard(rid)
                continue
            self._answer(rid, method, message, "Press Enter if it keeps happening")

    def _answer_unavailable(self, rid: Any, method: str) -> None:
        if self._gave_up:
            self._answer(rid, method, "ScreenControl stopped after repeated errors",
                         "Press Enter to restart it · details in screen_control.log")
        else:
            self._answer(rid, method, "ScreenControl is restarting…", "Press Enter to restart it now")

    def _answer(self, rid: Any, method: str, title: str, sub: str) -> None:
        if method in ("query", "context_menu"):
            result: dict[str, Any] = {
                "result": [
                    {
                        "title": title,
                        "subTitle": sub,
                        "icoPath": self.icon,
                        "score": 100,
                        "jsonRPCAction": {"method": RESTART_METHOD},
                        "contextData": [],
                    }
                ],
                "settingsChange": {},
                "debugMessage": "",
            }
        else:
            result = {"hide": False}
        self._write_flow(_encode({"jsonrpc": "2.0", "id": rid, "result": result}))

    # ---------------------------------------------------------------- I/O

    def _write_flow(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self.flow_out.write(data)
                self.flow_out.flush()
            except (OSError, ValueError):
                log.warning("Could not write to Flow")

    @staticmethod
    def _write_child(child: subprocess.Popen[bytes], data: bytes) -> bool:
        try:
            assert child.stdin is not None
            child.stdin.write(data if data.endswith(b"\n") else data + b"\r\n")
            child.stdin.flush()
            return True
        except (OSError, ValueError):
            return False


class _KillOnCloseJob:
    """Windows job object: children are killed when this process goes away."""

    def __init__(self) -> None:
        self.handle = None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wt.HANDLE
            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wt.LPCWSTR)
            kernel32.SetInformationJobObject.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
            kernel32.AssignProcessToJobObject.argtypes = (wt.HANDLE, wt.HANDLE)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            # 9 = JobObjectExtendedLimitInformation
            if kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = handle
                self._kernel32 = kernel32
        except (AttributeError, OSError):
            log.debug("Job objects unavailable", exc_info=True)

    def assign(self, child: subprocess.Popen[bytes]) -> None:
        if self.handle is None:
            return
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, wt.HANDLE(int(child._handle))):  # type: ignore[attr-defined]
                log.debug("AssignProcessToJobObject failed: %s", ctypes.get_last_error())
        except (AttributeError, OSError):
            log.debug("Could not assign the plugin to the job", exc_info=True)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )

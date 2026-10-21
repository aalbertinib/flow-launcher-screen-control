"""Flow Launcher entry point.

Startup must never touch the network or install anything (Flow often starts
before the network is up). All dependencies are bundled in ``lib`` at build time.

Flow starts this file without arguments: it becomes a small supervisor that runs
the real plugin (``main.py --child``) as a child process and restarts it if it
crashes or hangs (see screen_control/supervisor.py).
"""

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _path in (ROOT / "lib", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _setup_logging(log_name: str = "screen_control.log") -> None:
    debug = (ROOT / "debug.flag").exists()
    handler = logging.handlers.RotatingFileHandler(
        ROOT / log_name,
        maxBytes=256_000,
        backupCount=1,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    # flogin logs every JSON-RPC message at DEBUG: keep it quiet unless debugging.
    logging.getLogger("flogin").setLevel(logging.DEBUG if debug else logging.WARNING)

    def _log_unhandled(exc_type, exc, tb):  # noqa: ANN001
        logging.getLogger("screen_control").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_unhandled
    threading.excepthook = lambda args: _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback)


def run_plugin() -> None:
    _setup_logging()
    log = logging.getLogger("screen_control")

    if sys.version_info < (3, 11):
        log.critical("Python 3.11+ is required, Flow is using %s", sys.version)
        sys.exit(1)

    try:
        from screen_control.plugin import ScreenControlPlugin
        from screen_control.win32 import gdi
    except ImportError:
        log.critical("Could not import the plugin. Is the 'lib' folder missing?", exc_info=True)
        raise

    gdi.enable_dpi_awareness()
    log.info("Starting ScreenControl (Python %s)", sys.version.split()[0])
    ScreenControlPlugin().run(setup_default_log_handler=False)
    # Flow is gone: exit now, even if an abandoned hardware call is still stuck in a thread.
    logging.shutdown()
    os._exit(0)


def run_supervisor() -> None:
    _setup_logging("screen_control.supervisor.log")
    try:
        from screen_control.supervisor import Supervisor
    except Exception:
        # The safety net itself is broken: run the plugin unprotected rather than not at all.
        logging.getLogger("screen_control").exception("Supervisor unavailable, running the plugin directly")
        run_plugin()
        return
    child = [sys.executable, "-B", str(ROOT / "main.py"), "--child"]
    Supervisor(child, sys.stdin.buffer, sys.stdout.buffer, cwd=str(ROOT)).run()


def main() -> None:
    if "--child" in sys.argv[1:]:
        run_plugin()
    else:
        run_supervisor()


if __name__ == "__main__":
    main()

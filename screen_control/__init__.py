"""Flow Launcher plugin: brightness, contrast and HDR for every screen."""

import sys
import types

# flogin's package __init__ does ``from .pip import *``, and flogin.pip imports
# ``requests`` (~110 ms of startup, plus ~2 MB bundled) only to offer a runtime
# pip installer this plugin never uses. Register an empty stand-in before
# flogin is imported anywhere.
if "flogin" not in sys.modules and "flogin.pip" not in sys.modules:
    _pip_stub = types.ModuleType("flogin.pip", "Unused by ScreenControl (see screen_control/__init__.py)")
    _pip_stub.__all__ = ()  # type: ignore[attr-defined]
    sys.modules["flogin.pip"] = _pip_stub

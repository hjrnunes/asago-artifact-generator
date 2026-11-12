"""Garak adapter for authored artifact packages.

The adapter turns a package into a Garak bundle and Garak's report into an
``execution-receipt-v1``. It imports no Garak code: Garak runs in its own
interpreter from the bundle's entrypoint. ``compile``, ``instantiate`` and
``parse`` are its library entry points (``entry_points``); the command line
calls them.
"""

from .compiler import CompileError
from .entry_points import Instantiated, RunSettings, compile, instantiate, parse
from .instantiate import InstantiateError
from .report_parse import ParseError

__all__ = [
    "CompileError",
    "InstantiateError",
    "Instantiated",
    "ParseError",
    "RunSettings",
    "compile",
    "instantiate",
    "parse",
]

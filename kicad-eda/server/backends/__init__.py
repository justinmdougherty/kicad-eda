"""Schematic backends for the MCP server.

``backends.sch_api`` drives KiCad 11's IPC API (headless ``kicad-cli
api-server`` or a running KiCad 11 GUI) through the vendored kicad-python in
``server/vendor/kipy``; the hand-written S-expression path in
``server/tools/sch_build.py`` stays the fallback when no KiCad 11 is installed.

Importing this package is what wires the search path: ``server/`` (so
``kicad_env`` and the tools resolve) and, at the very FRONT, ``server/vendor``
so the vendored kipy always wins over a pip-installed kicad-python. Nothing
here imports kipy itself; ``backends.kipy_bridge`` does that, guarded, so
``SchematicApi.available()`` can answer "no" without raising.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import kicad_env  # noqa: E402  (server/ is on the path now)

VENDOR_DIR = kicad_env.use_vendored_kipy()

from backends.errors import SchematicApiError  # noqa: E402

__all__ = ["SERVER_DIR", "VENDOR_DIR", "SchematicApiError"]

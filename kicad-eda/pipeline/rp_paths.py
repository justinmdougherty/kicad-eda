"""Import-path bootstrap for the pipeline harness.

The pipeline/ scripts run as plain files (``<venv-python> run_pipeline.py``),
so the server modules are importable only once server/ and server/tools/ are
on sys.path. Every rp_* module imports this module FIRST, before any server
import. The bootstrap is idempotent: a second import, or pytest's pythonpath
setting, adds nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"


def ensure_on_path() -> None:
    """Put server/tools then server at the front of sys.path, each only when
    it is not there yet (the order run_pipeline has always used)."""
    for entry in (str(SERVER), str(SERVER / "tools")):
        if entry not in sys.path:
            sys.path.insert(0, entry)


ensure_on_path()

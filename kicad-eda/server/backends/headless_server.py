"""A ``kicad-cli api-server`` child that cannot outlive this process.

kipy's ``KiCadServer`` starts the server with a plain ``subprocess.Popen``:
the child inherits the MCP host's stdio pipe and, when the host is killed
rather than closed, keeps running (``KiCad.close()`` never runs and there is
no atexit to save it). ``GuardedKiCadServer`` starts the same command with
stdin from DEVNULL and, on Windows, inside a Job Object the kernel tears
down - killing the child - together with this process
(``backends.child_guard``). ``install()`` makes ``kipy.KiCad(headless=True)``
use it; ``kipy_bridge`` calls it once at import, so every headless session
(schematic and board) is covered.

The private attributes touched here (``_process``, ``_socket_path``, ...) are
the vendored ``KiCadServer``'s own; ``start`` mirrors its body line for line
apart from ``stdin`` and the guard.
"""
from __future__ import annotations

import subprocess
import threading
from typing import IO

import kipy.kicad
from kipy.server import KiCadServer

from backends import child_guard

__all__ = ["GuardedKiCadServer", "install"]


class GuardedKiCadServer(KiCadServer):
    """kipy's KiCadServer with stdin detached and a kill-on-close guard."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.guard: child_guard.ChildGuard | None = None
        self.guard_error: str | None = None

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:  # noqa: SLF001
            return
        cmd = [self._kicad_cli_path, "api-server", "--socket", self._socket_path]  # noqa: SLF001
        if self._file_path:  # noqa: SLF001
            cmd.append(self._file_path)  # noqa: SLF001
        self._process = subprocess.Popen(  # noqa: SLF001
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._stdout_thread = self._drain(self._process.stdout, self._stdout_lines)  # noqa: SLF001
        self._stderr_thread = self._drain(self._process.stderr, self._stderr_lines)  # noqa: SLF001
        self._bind(self._process.pid)  # noqa: SLF001

    def _drain(self, stream: IO[str] | None, lines) -> threading.Thread:
        thread = threading.Thread(target=self._drain_stream, args=(stream, lines), daemon=True)
        thread.start()
        return thread

    def _bind(self, pid: int) -> None:
        """Guard the child; a failure is kept in ``guard_error``, never raised
        (the server works without it, it just needs a clean close)."""
        if not child_guard.supported():
            return
        try:
            self.guard = child_guard.ChildGuard.bind(pid)
        except OSError as exc:
            self.guard_error = str(exc)

    def stop(self) -> None:
        super().stop()
        if self.guard is not None:
            self.guard.close()
            self.guard = None


def install() -> None:
    """Route ``kipy.KiCad(headless=True)`` through GuardedKiCadServer."""
    kipy.kicad.KiCadServer = GuardedKiCadServer

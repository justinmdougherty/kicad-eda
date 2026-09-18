"""backends.child_guard + backends.headless_server: a headless api-server
child cannot outlive this process.

The guard is exercised on a real sleeping child python (Windows only: Job
Objects); the guarded KiCadServer is started with a spied Popen so the test
sees the exact arguments kipy's server would get.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from backends import child_guard
from backends import kipy_bridge as kb

requires_windows = pytest.mark.skipif(not child_guard.supported(),
                                      reason="Job Objects exist on Windows only")
requires_kipy = pytest.mark.skipif(bool(kb.IMPORT_ERROR), reason=kb.IMPORT_ERROR or "")
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(SLEEPER, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


@requires_windows
def test_closing_the_guard_kills_the_child():
    child = _sleeper()
    try:
        guard = child_guard.ChildGuard.bind(child.pid)
        assert guard.active and child.poll() is None and "active=True" in repr(guard)
        guard.close()
        assert not guard.active
        child.wait(timeout=10)                               # killed by the job, not by us
        guard.close()                                        # idempotent
    finally:
        if child.poll() is None:
            child.kill()


@requires_windows
def test_binding_a_missing_process_reports_the_step():
    with pytest.raises(OSError, match=r"OpenProcess\(\d+\) failed \(Windows error \d+"):
        child_guard.ChildGuard.bind(0x7FFFFFFC)


def test_unsupported_platform_raises_instead_of_pretending(monkeypatch):
    monkeypatch.setattr(child_guard, "PLATFORM", "linux")
    assert child_guard.supported() is False
    with pytest.raises(OSError, match="Windows only"):
        child_guard.ChildGuard.bind(1)


@requires_kipy
def test_guarded_server_detaches_stdin_and_guards_the_child(monkeypatch, tmp_path):
    import kipy.kicad
    from backends import headless_server
    assert kipy.kicad.KiCadServer is headless_server.GuardedKiCadServer   # installed by kipy_bridge
    seen: dict = {}
    real_popen = subprocess.Popen

    def spy(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = list(cmd), dict(kwargs)
        return real_popen(SLEEPER, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    server = headless_server.GuardedKiCadServer("kicad-cli-fake",
                                                file_path=str(tmp_path / "b.kicad_pcb"))
    server.start()
    try:
        assert seen["cmd"][:2] == ["kicad-cli-fake", "api-server"]
        assert seen["cmd"][2] == "--socket" and seen["cmd"][-1].endswith("b.kicad_pcb")
        assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
        if child_guard.supported():
            assert server.guard is not None and server.guard.active
            assert server.guard_error is None
        else:
            assert server.guard is None
    finally:
        server.stop()
    assert server.guard is None

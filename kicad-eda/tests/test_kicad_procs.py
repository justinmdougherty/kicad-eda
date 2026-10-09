"""tests/fixtures/kicad_procs.py: which processes a leak check may count.

Other agents and tools run kicad-cli on the same machine at the same time, so a
machine-wide PID diff is no leak check. These tests stand the base Python
interpreter in for kicad-cli (any image name works), so they need no KiCad."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fixtures.kicad_procs import owned_pids, process_table

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process table")

# the venv python.exe is a launcher that re-spawns the base interpreter as a
# child; the base one keeps every PID in these trees exactly one level deep
PY = getattr(sys, "_base_executable", sys.executable)
IMAGE = Path(PY).name
SLEEP = "import time; time.sleep(60)"


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _wait_for(pred, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=30)


def test_process_table_lists_this_process():
    table = process_table()
    assert os.getpid() in table
    ppid, image = table[os.getpid()]
    assert image.endswith(".exe") and ppid > 0


def test_own_child_counts_until_it_exits():
    child = _spawn([PY, "-c", SLEEP])
    try:
        assert _wait_for(lambda: child.pid in owned_pids(IMAGE))
        assert child.pid not in owned_pids(IMAGE, root_pid=4)      # not under System
    finally:
        child.kill()
        child.wait(timeout=30)
    assert _wait_for(lambda: child.pid not in owned_pids(IMAGE))


def test_foreign_processes_with_live_parents_do_not_count():
    table = process_table()
    mine = owned_pids(IMAGE)
    foreign = {pid for pid, (ppid, image) in table.items()
               if image == IMAGE.lower() and ppid in table and pid not in mine}
    assert not (foreign & mine)
    assert os.getpid() not in owned_pids(IMAGE)                    # the root itself never


def test_grandchild_counts_and_still_counts_once_orphaned(tmp_path: Path):
    pid_file = tmp_path / "gc.pid"
    launcher = ("import subprocess, sys\n"
                f"p = subprocess.Popen([sys.executable, '-c', {SLEEP!r}],"
                " stdin=subprocess.DEVNULL)\n"
                "open(sys.argv[1], 'w').write(str(p.pid))\n"
                "p.wait()\n")
    mid = _spawn([PY, "-c", launcher, str(pid_file)])
    gc_pid = None
    try:
        assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip() != "")
        gc_pid = int(pid_file.read_text())
        assert _wait_for(lambda: gc_pid in owned_pids(IMAGE))       # through the chain
        mid.kill()                                                  # orphan the grandchild
        mid.wait(timeout=30)
        assert gc_pid in owned_pids(IMAGE)        # parent gone: possibly our leak, counted
    finally:
        if gc_pid is not None:
            _kill(gc_pid)
        if mid.poll() is None:
            mid.kill()

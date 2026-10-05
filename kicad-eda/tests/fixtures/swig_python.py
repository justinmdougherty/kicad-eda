"""Shared live-tier helper: run a child script under KiCad's bundled Python.

Live tests that need the real pcbnew write a small child script and run it with
``run_child``; the child prints one JSON object as its last stdout line. Every
child starts with ``PREAMBLE`` (or the same lines by hand): server/tools on
sys.path, then ``import kicad_quiet`` BEFORE ``import pcbnew`` so a wx assert or
wxLogError can never open a modal dialog (ROADMAP_DECISIONS D0). Children run
with stdin closed, UTF-8 output and a timeout. Without a KiCad Python that
imports pcbnew, ``require_kicad_python`` skips the test cleanly.

``T10_BOARD`` is the T10 outline-spike board block (60 x 40 mm, r=4 corners, a
stadium slot and a round hole, datum (10, 30)); later F3 live tests build on it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import pytest

import outline as ol
import toolchain

TOOLS = Path(__file__).resolve().parents[2] / "server" / "tools"

PREAMBLE = (
    "import json\n"
    "import sys\n"
    f"sys.path.insert(0, {str(TOOLS)!r})\n"
    "import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)\n"
    "import pcbnew  # noqa: E402\n")

T10_BOARD = {"outline": {"loop": [[4, 0], [56, 0, 0.41421356237309503], [60, 4],
                                  [60, 36, 0.41421356237309503], [56, 40],
                                  [4, 40, 0.41421356237309503], [0, 36],
                                  [0, 4, 0.41421356237309503]]},
             "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                         {"circle": [30, 20, 2.5]}],
             "datum": [10, 30]}

_IMPORT = r"^\s*(?:import|from)\s+{}\b"


def _close(a: tuple, b: tuple, tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _same_ring(ra: ol.Loop | ol.Circle, rb: ol.Loop | ol.Circle, tol: float) -> bool:
    """Same ring within `tol`, from any start vertex (KiCad may save a ring's
    edges in another order; Outline already fixes the direction)."""
    if type(ra) is not type(rb):
        return False
    if isinstance(ra, ol.Circle):
        return _close((ra.cx, ra.cy, ra.r), (rb.cx, rb.cy, rb.r), tol)
    va, vb = ra.vertices, rb.vertices
    if len(va) != len(vb):
        return False
    return any(all(_close(p, q, tol) for p, q in zip(va, vb[k:] + vb[:k], strict=True))
               for k in range(len(vb)) if math.dist(va[0][:2], vb[k][:2]) <= tol)


def assert_same_outline(a: ol.Outline, b: ol.Outline, tol: float) -> None:
    """Assert two outlines match within `tol`: the outer ring, the cutouts in
    any order (KiCad's save order is not stable), each from any start vertex
    (bulges included), and the datum."""
    assert _same_ring(a.outer, b.outer, tol), (a.outer, b.outer)
    assert len(a.cutouts) == len(b.cutouts), (a.cutouts, b.cutouts)
    unmatched = list(b.cutouts)
    for ring in a.cutouts:
        hit = next((k for k, other in enumerate(unmatched) if _same_ring(ring, other, tol)), None)
        assert hit is not None, (ring, unmatched)
        unmatched.pop(hit)
    assert (a.datum is None) == (b.datum is None), (a.datum, b.datum)
    if a.datum is not None:
        assert _close(a.datum, b.datum, tol), (a.datum, b.datum)


def require_kicad_python() -> str:
    """KiCad's bundled Python verified to import pcbnew, else skip the test."""
    kpy = toolchain.kicad_python()
    if not kpy:
        pytest.skip(f"no KiCad pcbnew Python: {toolchain.kicad_python_error()}")
    return kpy


def kicad_footprints(kpy: str, lib: str) -> Path:
    """The ``<lib>.pretty`` directory of the SAME install as `kpy` (a newer
    install's footprints may be in a format this pcbnew cannot read)."""
    root = Path(kpy).resolve().parent.parent
    for share in (root / "share" / "kicad" / "footprints", Path("/usr/share/kicad/footprints")):
        if (share / lib).is_dir():
            return share / lib
    pytest.skip(f"no {lib} beside {kpy}")


def _check_quiet_first(script_text: str) -> None:
    """A child that imports pcbnew must import kicad_quiet before it."""
    pcb = re.search(_IMPORT.format("pcbnew"), script_text, re.M)
    if pcb is None:
        return
    quiet = re.search(_IMPORT.format("kicad_quiet"), script_text, re.M)
    if quiet is None or quiet.start() > pcb.start():
        raise ValueError("child script imports pcbnew before kicad_quiet; start it with "
                         "swig_python.PREAMBLE")


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise AssertionError(f"child printed no JSON object line:\n{stdout}")


def run_child(script_text: str, args: list[str], tmp_path: Path, timeout: int = 300) -> dict:
    """Write `script_text` under `tmp_path`, run it with KiCad's Python and
    `args` (stdin closed, UTF-8, `timeout` s), assert rc 0 (stdout + stderr in
    the failure message) and return the last JSON object line of stdout."""
    _check_quiet_first(script_text)
    kpy = require_kicad_python()
    digest = hashlib.sha1(script_text.encode("utf-8")).hexdigest()[:10]
    script = Path(tmp_path) / f"child_{digest}.py"
    script.write_text(script_text, encoding="utf-8")
    proc = subprocess.run([kpy, str(script), *map(str, args)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          timeout=timeout)
    output = f"--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    assert proc.returncode == 0, f"child exited {proc.returncode}\n{output}"
    return _last_json(proc.stdout)

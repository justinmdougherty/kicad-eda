#!/usr/bin/env python3
"""Connector probe: the geometry the napkin face_connector_out policy needs.

Usage (KiCad's bundled Python, stdin closed, a timeout)::

    python swig_connector_probe.py <pcb> [netlist.json]

prints ONE JSON line and exits 0::

    {"ok": true, "kicad": "<pcbnew version>",
     "board_bbox": [x0, y0, x1, y1], "sheet_origin": [ox, oy],
     "connectors": [{"ref", "footprint", "mouth": [mx, my] | null,
                     "courtyard": [x0, y0, x1, y1], "courtyard_source": "courtyard" | "bbox",
                     "centre": [x, y], "origin": [x, y], "rot", "side": "F" | "B",
                     "interior": bool,
                     "pads": {"count", "tht", "rows", "pitch_mm"}}, ...]}

or ``{"ok": false, "reason": ...}`` with exit 1 (a missing / unreadable board,
an unreadable netlist, an open outline) and exit 2 for a usage error. One
entry per J* footprint (swig_gate_checks.is_edge_connector), sorted by ref;
``interior`` is true for a J* part the netlist flags ``"interior": true``
(swig_gate_checks.interior_refs): the G3 edge rule skips it.

Frame: mm, Y down, in the D2 board frame (origin = the Edge.Cuts outline bbox
top-left, board_outline.outline_from_board), so ``board_bbox`` is [0, 0, w, h]
and ``centre`` is directly comparable with a netlist ``x``/``y`` (D3: the
courtyard-bbox centre); ``origin`` + ``rot`` are the D3 ``at`` (the footprint
origin and the rotation KiCad displays, normalised to [0, 360); on B the
post-flip value, swig_anchor.read_back). ``sheet_origin`` is where the board
frame's origin sits in pcbnew's sheet coordinates (sheet = board + origin).

``mouth`` is swig_gate_checks._world_mouth's mating direction, reused
read-only: (courtyard centre - pad centroid) snapped to the dominant axis, a
unit axis vector, null when indeterminate (a vertical header: no mating
direction). _world_mouth reads F.CrtYd only, so a B-side part is handed to it
through an adapter that serves its B.CrtYd. ``courtyard`` is the side's
courtyard bbox, else the footprint bbox (courtyard_source "bbox").

ROTATION SIGN CONVENTION (measured by tests/test_connector_probe_live.py on
KiCad 9.0.4 and 10.0.5, F and B side): when ``rot`` increases by 90 the world
mouth vector turns (mx, my) -> (my, -mx), i.e. right (+x) -> up (-y) -> left
-> down -> right: counter-clockwise as seen on screen (Y down). rot + 180 is
(-mx, -my); rot + 270 is (-my, mx). rotate_mouth() implements it and
napkin_policy.face_out mirrors it.

``pads`` summarises the footprint's pads in FOOTPRINT coordinates (so it is
rotation independent), NPTH holes excluded: ``count``; ``tht`` = every counted
pad is plated through-hole; ``rows`` = the distinct pad-centre lines along the
dominant axis (the longer span of the pad centres; centres within 0.05 mm of
each other share a line); ``pitch_mm`` = the smallest centre spacing along a
row (rounded to 0.001 mm), None without two pads in a row. F5-08 recognises
board-internal headers from it on imported libraries (a stock
PinHeader_2x05_P2.54mm_Vertical: count 10, tht true, rows 2, pitch 2.54).

KiCad facts handled: on 9.0.4 pcbnew.LoadBoard leaves every footprint's
courtyard cache EMPTY (10.0.5 builds it lazily), which hides the mouth, so
BuildCourtyardCaches() runs on every footprint right after the load. LoadBoard
returns None for a missing / unreadable file and passing that None on
segfaults the child: board_outline.load_board checks the path first and the
result after.

Runs under KiCad's bundled Python (3.11 on 9.0.4): stdlib + pcbnew only.
pcbnew and the SWIG-side modules are imported inside _kicad(), after the
module-level kicad_quiet import, so the pure helpers and the argument checks
are unit-tested in the dev venv.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import kicad_quiet  # noqa: E402,F401  (before pcbnew: no modal wx dialogs)

ROW_TOL_MM = 0.05            # pad centres closer than this share a row line
PITCH_DECIMALS = 3
MM_DECIMALS = 4
USAGE = "usage: swig_connector_probe.py <pcb> [netlist.json]"
_EMPTY_PADS = {"count": 0, "tht": False, "rows": 0, "pitch_mm": None}

PadPoint = tuple[float, float, str]      # (x, y) in footprint mm, kind pth|smd|conn|npth


class ProbeError(RuntimeError):
    """The probe cannot answer: reported as {ok: false, reason}."""


class _Kicad(NamedTuple):
    pcbnew: Any
    board_outline: Any
    outline: Any
    swig_anchor: Any
    gate_checks: Any


# ----------------------------------------------------------------------------
# Pure helpers (no pcbnew)
# ----------------------------------------------------------------------------
def rotate_mouth(mouth: Sequence[float], delta_deg: float) -> tuple[int, int]:
    """The axis mouth vector after the footprint's displayed rotation grows
    by `delta_deg` (a multiple of 90): each +90 maps (mx, my) -> (my, -mx)."""
    quarter = float(delta_deg) / 90.0
    turns = round(quarter)
    if abs(quarter - turns) > 1e-9:
        raise ValueError(f"rotation delta {delta_deg} is not a multiple of 90 degrees")
    mx, my = (int(round(float(c))) for c in mouth)
    for _ in range(turns % 4):
        mx, my = my, -mx
    return mx, my


def _rows(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    """(along, across) points grouped into lines by `across` (chained within
    ROW_TOL_MM); each line lists its `along` coordinates."""
    rows: list[list[float]] = []
    prev: float | None = None
    for along, across in sorted(points, key=lambda p: p[1]):
        if prev is None or across - prev > ROW_TOL_MM:
            rows.append([])
        rows[-1].append(along)
        prev = across
    return rows


def _pitch(rows: Sequence[Sequence[float]]) -> float | None:
    """The smallest centre spacing along any row (coincident pads skipped)."""
    gaps = [b - a for row in rows for a, b in pairwise(sorted(row)) if b - a > ROW_TOL_MM]
    return round(min(gaps), PITCH_DECIMALS) if gaps else None


def pads_summary(pads: Iterable[PadPoint]) -> dict:
    """{count, tht, rows, pitch_mm} of a footprint's pads (footprint mm);
    NPTH holes are not pads here (see the module doc)."""
    kept = [(float(x), float(y), kind) for x, y, kind in pads if kind != "npth"]
    if not kept:
        return dict(_EMPTY_PADS)
    xs, ys = [p[0] for p in kept], [p[1] for p in kept]
    along_x = max(xs) - min(xs) >= max(ys) - min(ys)
    rows = _rows((x, y) if along_x else (y, x) for x, y, _ in kept)
    return {"count": len(kept), "tht": all(kind == "pth" for _, _, kind in kept),
            "rows": len(rows), "pitch_mm": _pitch(rows)}


def _mm(v: float) -> float:
    return round(float(v), MM_DECIMALS) + 0.0           # + 0.0: no -0.0 in the JSON


def _ref_key(entry: Mapping) -> tuple:
    """Natural order: J2 before J10."""
    ref = str(entry["ref"])
    head = ref.rstrip("0123456789")
    tail = ref[len(head):]
    return head, int(tail) if tail else -1, ref


def read_design(path: str | None) -> dict:
    """The netlist as a dict ({} without one); ProbeError when unreadable."""
    if not path:
        return {}
    try:
        design = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProbeError(f"netlist not found: {path}") from e
    except (OSError, ValueError) as e:
        raise ProbeError(f"netlist {path} is not readable JSON: {e}") from e
    if not isinstance(design, dict):
        raise ProbeError(f"netlist {path} is not a JSON object")
    return design


# ----------------------------------------------------------------------------
# pcbnew side
# ----------------------------------------------------------------------------
def _kicad() -> _Kicad:
    """pcbnew and the SWIG-side modules (kicad_quiet is imported above)."""
    import pcbnew

    import board_outline
    import outline
    import swig_anchor
    import swig_gate_checks
    return _Kicad(pcbnew, board_outline, outline, swig_anchor, swig_gate_checks)


class _BackCourtyard:
    """A B-side footprint as swig_gate_checks._world_mouth reads it: that
    helper asks for F.CrtYd only, a flipped part's courtyard is on B.CrtYd."""

    def __init__(self, fp: Any, layer: int) -> None:
        self._fp = fp
        self._layer = layer

    def Pads(self) -> Any:  # noqa: N802 (pcbnew API)
        return self._fp.Pads()

    def GetCourtyard(self, _layer: int) -> Any:  # noqa: N802 (pcbnew API)
        return self._fp.GetCourtyard(self._layer)


def _pad_points(k: _Kicad, fp: Any) -> list[PadPoint]:
    p = k.pcbnew
    kinds = {p.PAD_ATTRIB_PTH: "pth", p.PAD_ATTRIB_SMD: "smd",
             p.PAD_ATTRIB_CONN: "conn", p.PAD_ATTRIB_NPTH: "npth"}
    out = []
    for pad in fp.Pads():
        rel = pad.GetFPRelativePosition()
        out.append((p.ToMM(rel.x), p.ToMM(rel.y), kinds.get(pad.GetAttribute(), "smd")))
    return out


def _fp_bbox(fp: Any) -> Any:
    try:
        return fp.GetBoundingBox(False)                 # without the texts
    except TypeError:
        return fp.GetBoundingBox()


def _courtyard(k: _Kicad, fp: Any, side: str) -> tuple[Any, str]:
    layer = k.pcbnew.B_CrtYd if side == "B" else k.pcbnew.F_CrtYd
    poly = fp.GetCourtyard(layer)
    if poly is not None and poly.OutlineCount() > 0:
        return poly.BBox(), "courtyard"
    return _fp_bbox(fp), "bbox"


def _box(k: _Kicad, bb: Any, ox: float, oy: float) -> list[float]:
    to = k.pcbnew.ToMM
    return [_mm(to(bb.GetLeft()) - ox), _mm(to(bb.GetTop()) - oy),
            _mm(to(bb.GetRight()) - ox), _mm(to(bb.GetBottom()) - oy)]


def _mouth(k: _Kicad, fp: Any, side: str) -> list[int] | None:
    view = fp if side == "F" else _BackCourtyard(fp, k.pcbnew.B_CrtYd)
    found = k.gate_checks._world_mouth(view)
    if found is None:
        return None
    (mx, my), _bb = found
    return [int(mx), int(my)]


def connector_entry(k: _Kicad, fp: Any, interior: set, origin: tuple[float, float]) -> dict:
    """The probe entry of one placed J* footprint (see the module doc)."""
    ox, oy = origin
    ref = fp.GetReference()
    x, y, rot, side = k.swig_anchor.read_back(fp, ox, oy)
    bb, source = _courtyard(k, fp, side)
    court = _box(k, bb, ox, oy)
    return {"ref": ref, "footprint": fp.GetFPIDAsString(), "mouth": _mouth(k, fp, side),
            "courtyard": court, "courtyard_source": source,
            "centre": [_mm((court[0] + court[2]) / 2), _mm((court[1] + court[3]) / 2)],
            "origin": [_mm(x), _mm(y)], "rot": rot, "side": side,
            "interior": not k.gate_checks.is_edge_connector(ref, interior),
            "pads": pads_summary(_pad_points(k, fp))}


def _interior(k: _Kicad, design: dict) -> set:
    try:
        return set(k.gate_checks.interior_refs(design))
    except (AttributeError, KeyError, TypeError) as e:
        raise ProbeError(f"netlist components are malformed: {type(e).__name__}: {e}") from e


def _load(k: _Kicad, pcb_path: str) -> Any:
    try:
        board = k.board_outline.load_board(pcb_path)
    except (FileNotFoundError, RuntimeError) as e:
        raise ProbeError(str(e)) from e
    for fp in board.GetFootprints():                    # empty after LoadBoard on 9.0.4
        if hasattr(fp, "BuildCourtyardCaches"):
            fp.BuildCourtyardCaches()
    return board


def _frame(k: _Kicad, board: Any) -> tuple[list[float], tuple[float, float]]:
    try:
        outline, (ox, oy) = k.board_outline.outline_from_board(board)
    except k.board_outline.OutlineBuildError as e:
        raise ProbeError(f"no usable board outline: {e}") from e
    return [_mm(v) for v in k.outline.bbox(outline)], (ox, oy)


def probe(pcb_path: str, netlist_path: str | None = None) -> dict:
    """The probe result for a saved board (ProbeError when it cannot answer)."""
    if not os.path.isfile(pcb_path):
        raise ProbeError(f"board file not found: {pcb_path}")
    design = read_design(netlist_path)
    k = _kicad()
    interior = _interior(k, design)
    board = _load(k, pcb_path)
    bbox, origin = _frame(k, board)
    entries = [connector_entry(k, fp, interior, origin) for fp in board.GetFootprints()
               if k.gate_checks.is_edge_connector(fp.GetReference(), set())]
    return {"ok": True, "kicad": str(k.pcbnew.Version()), "board_bbox": bbox,
            "sheet_origin": [_mm(origin[0]), _mm(origin[1])],
            "connectors": sorted(entries, key=_ref_key)}


def _emit(result: Mapping) -> None:
    print(json.dumps(result), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        _emit({"ok": False, "reason": USAGE})
        return 2
    try:
        result = probe(args[0], args[1] if len(args) == 2 else None)
    except ProbeError as e:
        _emit({"ok": False, "reason": str(e)})
        return 1
    except Exception as e:                              # report, never crash silently
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "reason": f"probe failed on {args[0]}: {type(e).__name__}: {e}"})
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

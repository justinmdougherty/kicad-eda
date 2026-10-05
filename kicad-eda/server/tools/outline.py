#!/usr/bin/env python3
"""Board outline geometry for the netlist pipeline (ROADMAP D2). Stdlib only.

One import for every consumer: lint (venv), the board API (server) and the
SWIG scripts under KiCad's bundled Python, which has no shapely / ezdxf /
sexpdata (server/tools is their sys.path[0]). The implementation is split:
  outline_geom   exact primitives (lines, arcs, DXF bulges), distances, rays
  outline_model  Outline / Loop / Circle, containment, edges, validity
  outline_inset  inset (exact erosion by d mm)
  outline_ops    largest inscribed rectangle, fab validation
and this module adds the I/O: the netlist ``board`` block, KiCad Edge.Cuts
primitives, and external (DXF / Fusion, Y-up) outlines.

The netlist block (docs/ROADMAP_DECISIONS.md D2)::

    "board": {"w": 60, "h": 40, "corner_radius": 3.0,
              "outline": {"loop": [[x, y], [x, y, bulge], ...]},
              "cutouts": [{"loop": [...]}, {"circle": [cx, cy, r]}],
              "datum": [x, y]}

Frames and conventions
----------------------
* The board frame is mm, Y DOWN, origin = the outline's bbox top-left; a loop
  outside that frame is refused (``normalize_external`` / ``translate`` move
  geometry into it). ``datum`` is the external source origin (e.g. the Fusion
  origin) in the board frame; the builder makes it the KiCad aux origin.
* All math is plain (x, y): "CCW in math terms" means positive shoelace area.
  An Outline stores its outer ring math-CCW and cutouts math-CW (material on
  the left of travel); in the Y-down board frame the outer ring therefore
  runs clockwise ON SCREEN. A bulge b = tan(theta/4) > 0 sweeps math-CCW (the
  arc bows to the right of the chord direction). Flipping Y negates bulges.
* Exact arcs go to KiCad (``to_kicad_primitives``); flattened polylines
  (``flatten``, 0.05 mm chord error) are only ever used for math.
* DXF is never parsed here: ``from_board_block`` refuses a ``{"dxf": ...}``
  outline and asks the caller to load it with dxf_io (venv, ezdxf) first.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline_geom import (BULGE_EPS, EPS, Arc, Line, Point, arc_from_bulge,  # noqa: E402,F401
                          arc_from_three_points, dist, prim_from_bulge)
from outline_model import (CHORD_ERROR_MM, TOUCH_TOL_MM, Circle, Edge,  # noqa: E402,F401
                           EdgeHit, Loop, Outline, OutlineError, area, bbox, check,
                           contains, contains_polygon, contains_rect, distance_to_edge,
                           edges, flatten, geometry_problems, nearest_edge, ray_to_edge,
                           ring_signed_area, translate)
from outline_inset import inset  # noqa: E402,F401
from outline_ops import FAB_LIMITS, FabLimits, largest_inscribed_rect, validate_fab  # noqa: E402,F401

FRAME_TOL_MM = 1e-3        # board frame: bbox top-left within this of (0, 0)
CHAIN_TOL_MM = 1e-3        # KiCad primitives: end points this close are joined
_ROUND = 6                 # decimals written to a board block (1 nm)

__all__ = [
    "CHAIN_TOL_MM", "CHORD_ERROR_MM", "FAB_LIMITS", "FRAME_TOL_MM", "TOUCH_TOL_MM",
    "Arc", "Circle", "Edge", "EdgeHit", "FabLimits", "Line", "Loop", "Outline",
    "OutlineError", "arc_from_bulge", "arc_from_three_points", "area", "bbox",
    "board_to_external", "check", "contains", "contains_polygon", "contains_rect",
    "distance_to_edge", "edges", "external_to_board", "flatten", "from_board_block",
    "from_kicad_primitives", "geometry_problems", "inset", "largest_inscribed_rect",
    "nearest_edge", "normalize_external", "ray_to_edge", "rect_loop", "ring_signed_area",
    "to_board_block", "to_kicad_primitives", "translate", "validate_fab",
]


# --- netlist board block -------------------------------------------------------------
def rect_loop(w: float, h: float, corner_radius: float = 0.0,
              x0: float = 0.0, y0: float = 0.0) -> Loop:
    """A w x h rectangle at (x0, y0), corners rounded with exact 90 degree arcs."""
    r = corner_radius
    x1, y1 = x0 + w, y0 + h
    b = math.tan(math.pi / 8)
    edges_ = [((x0 + r, y0), (x1 - r, y0), 0.0), ((x1 - r, y0), (x1, y0 + r), b),
              ((x1, y0 + r), (x1, y1 - r), 0.0), ((x1, y1 - r), (x1 - r, y1), b),
              ((x1 - r, y1), (x0 + r, y1), 0.0), ((x0 + r, y1), (x0, y1 - r), b),
              ((x0, y1 - r), (x0, y0 + r), 0.0), ((x0, y0 + r), (x0 + r, y0), b)]
    return Loop(tuple((s[0], s[1], bulge) for s, e, bulge in edges_ if dist(s, e) > EPS))


def _number(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise OutlineError(f"{what} must be a finite number, got {v!r}")
    return float(v)


def _loop_at(raw: object, where: str) -> Loop:
    if not isinstance(raw, (list, tuple)):
        raise OutlineError(f"{where} must be a list of [x, y] / [x, y, bulge], got {raw!r}")
    try:
        return Loop(tuple(raw))
    except OutlineError as e:
        raise OutlineError(f"{where}: {e}") from e


def _cutout(raw: object, i: int) -> Loop | Circle:
    where = f"board.cutouts[{i}]"
    if not isinstance(raw, Mapping) or len(set(raw) & {"loop", "circle"}) != 1 \
            or set(raw) - {"loop", "circle"}:
        raise OutlineError(f"{where} needs exactly one of 'loop' or 'circle', got {raw!r}")
    if "loop" in raw:
        return _loop_at(raw["loop"], f"{where}.loop")
    c = raw["circle"]
    if not isinstance(c, (list, tuple)) or len(c) != 3:
        raise OutlineError(f"{where}.circle must be [cx, cy, r], got {c!r}")
    try:
        return Circle(*(_number(v, f"{where}.circle") for v in c))
    except OutlineError as e:
        raise OutlineError(f"{where}: {e}") from e


def _outer_from_block(board: Mapping) -> tuple[Loop, bool]:
    spec = board.get("outline")
    r = board.get("corner_radius") or 0.0
    r = _number(r, "board.corner_radius")
    if spec is not None:
        if not isinstance(spec, Mapping):
            raise OutlineError(f"board.outline must be an object, got {spec!r}")
        if "dxf" in spec:
            raise OutlineError(
                f"board.outline references the DXF file {spec['dxf']!r}; outline.py is "
                "stdlib-only and never parses DXF. Load it with dxf_io first (it returns the "
                "normalised loops and datum) and pass a board block whose outline is "
                "{\"loop\": [...]}")
        if "loop" not in spec:
            raise OutlineError("board.outline needs a 'loop' (or a 'dxf' loaded via dxf_io)")
        if r:
            raise OutlineError("board.corner_radius only rounds the w x h rectangle; with "
                               "board.outline put the fillets in the loop as bulge vertices")
        return _loop_at(spec["loop"], "board.outline.loop"), True
    if board.get("w") is None or board.get("h") is None:
        raise OutlineError("the board block has no outline: give w and h (mm) or "
                           "outline.loop")
    w, h = _number(board["w"], "board.w"), _number(board["h"], "board.h")
    if w <= 0 or h <= 0:
        raise OutlineError(f"board.w and board.h must be positive, got {w:g} x {h:g}")
    if not 0 <= r <= min(w, h) / 2.0:
        raise OutlineError(f"board.corner_radius {r:g} must be in [0, {min(w, h) / 2:g}] "
                           f"for a {w:g} x {h:g} board")
    return rect_loop(w, h, r), False


def from_board_block(board: Mapping) -> Outline:
    """The validated Outline of a netlist ``board`` block (see module doc).
    Raises OutlineError naming the offending key: a DXF reference (load it via
    dxf_io first), a loop outside the board frame, w/h contradicting the loop,
    a self-crossing ring, a cutout off the board or overlapping another."""
    if not isinstance(board, Mapping):
        raise OutlineError(f"the board block must be an object, got {board!r}")
    outer, from_loop = _outer_from_block(board)
    raw_cutouts = board.get("cutouts") or []
    if not isinstance(raw_cutouts, (list, tuple)):
        raise OutlineError(f"board.cutouts must be a list, got {raw_cutouts!r}")
    cutouts = tuple(_cutout(c, i) for i, c in enumerate(raw_cutouts))
    datum = board.get("datum")
    if datum is not None and (not isinstance(datum, (list, tuple)) or len(datum) != 2):
        raise OutlineError(f"board.datum must be [x, y], got {datum!r}")
    o = Outline(outer, cutouts, None if datum is None else
                (_number(datum[0], "board.datum x"), _number(datum[1], "board.datum y")))
    x0, y0, x1, y1 = bbox(o)
    if from_loop:
        if abs(x0) > FRAME_TOL_MM or abs(y0) > FRAME_TOL_MM:
            raise OutlineError(
                f"board.outline.loop's bbox starts at ({x0:.4f}, {y0:.4f}); the board frame "
                f"origin is the outline bbox top-left, so translate the loop (and every "
                f"coordinate in the netlist) by ({-x0:.4f}, {-y0:.4f}); normalize_external "
                "does this for DXF / Fusion outlines")
        for key, have in (("w", x1 - x0), ("h", y1 - y0)):
            if board.get(key) is not None and \
                    abs(_number(board[key], f"board.{key}") - have) > FRAME_TOL_MM:
                raise OutlineError(f"board.{key} = {board[key]!r} contradicts the outline "
                                   f"bbox ({have:.4f} mm); drop it or fix the loop")
    return check(o)


def _r(v: float) -> float:
    out = round(v, _ROUND)
    return 0.0 if out == 0 else out


def _ring_json(ring: Loop | Circle) -> list[list[float]]:
    if isinstance(ring, Circle):
        return [[_r(ring.cx - ring.r), _r(ring.cy), 1.0], [_r(ring.cx + ring.r), _r(ring.cy), 1.0]]
    return [[_r(x), _r(y)] if abs(b) < BULGE_EPS else [_r(x), _r(y), round(b, 12)]
            for x, y, b in ring.vertices]


def to_board_block(o: Outline) -> dict:
    """A JSON-ready netlist ``board`` block for `o` (w/h = its bbox, which
    from_board_block re-checks). Coordinates rounded to 1 nm. `o` must already
    be in the board frame (bbox top-left at the origin)."""
    x0, y0, x1, y1 = bbox(o)
    if abs(x0) > FRAME_TOL_MM or abs(y0) > FRAME_TOL_MM:
        raise OutlineError(f"the outline's bbox starts at ({x0:.4f}, {y0:.4f}), not at the "
                           f"board-frame origin; translate it by ({-x0:.4f}, {-y0:.4f}) "
                           "first (keeping the datum)")
    block: dict = {"w": _r(x1 - x0), "h": _r(y1 - y0), "outline": {"loop": _ring_json(o.outer)}}
    if o.cutouts:
        block["cutouts"] = [{"circle": [_r(c.cx), _r(c.cy), _r(c.r)]} if isinstance(c, Circle)
                            else {"loop": _ring_json(c)} for c in o.cutouts]
    if o.datum is not None:
        block["datum"] = [_r(o.datum[0]), _r(o.datum[1])]
    return block


# --- external (DXF / Fusion) outlines --------------------------------------------------
def _external_ring(item: object, i: int) -> Loop | Circle:
    if isinstance(item, (Loop, Circle)):
        return item
    if isinstance(item, Mapping):
        return _cutout(item, i)
    return _loop_at(item, f"loops[{i}]")


def _flip(ring: Loop | Circle) -> Loop | Circle:
    if isinstance(ring, Circle):
        return Circle(ring.cx, -ring.cy, ring.r)
    return Loop(tuple((x, -y, -b) for x, y, b in ring.vertices))


def normalize_external(loops: Iterable, y_up: bool = True) -> tuple[Outline, Point]:
    """Bring external closed loops (e.g. from a Fusion DXF) into the board frame.

    `loops`: loops ([[x, y(, bulge)], ...]), {"loop": ...} / {"circle": [cx, cy,
    r]} mappings, or Loop / Circle objects. The largest one is the outline, the
    rest are cutouts. With `y_up` (DXF, Fusion) Y is flipped (bulges negate).
    The bbox is then translated to (0, 0). Returns (outline, datum), datum =
    where the external origin lands in the board frame (also outline.datum)."""
    rings = [_external_ring(item, i) for i, item in enumerate(loops)]
    if not rings:
        raise OutlineError("normalize_external needs at least one closed loop")
    if y_up:
        rings = [_flip(r) for r in rings]
    k = max(range(len(rings)), key=lambda i: abs(ring_signed_area(rings[i])))
    outer, cutouts = rings[k], tuple(r for i, r in enumerate(rings) if i != k)
    x0, y0, _x1, _y1 = bbox(Outline(outer, cutouts))
    o = translate(Outline(outer, cutouts, (0.0, 0.0)), -x0, -y0)
    return check(o), o.datum


def external_to_board(p: Sequence[float], datum: Sequence[float], y_up: bool = True) -> Point:
    """An external (DXF / Fusion) point in the board frame."""
    x, y = float(p[0]), float(p[1])
    return (x + datum[0], (-y if y_up else y) + datum[1])


def board_to_external(p: Sequence[float], datum: Sequence[float], y_up: bool = True) -> Point:
    """A board-frame point back in the external frame (inverse of external_to_board)."""
    x, y = float(p[0]) - datum[0], float(p[1]) - datum[1]
    return (x, -y if y_up else y)


# --- KiCad Edge.Cuts primitives --------------------------------------------------------
def _kicad_prim(kind: str, ring: int, start=None, mid=None, end=None, center=None,
                radius=None) -> dict:
    return {"kind": kind, "ring": ring, "start": start, "mid": mid, "end": end,
            "center": center, "radius": radius}


def to_kicad_primitives(o: Outline) -> list[dict]:
    """Exact Edge.Cuts primitives, outer ring first then each cutout:
    {kind: 'line'|'arc'|'circle', ring, start, mid, end, center, radius}
    (fields that do not apply are None; points are (x, y) in mm). Arcs carry
    their angular midpoint, which is what KiCad's PCB_SHAPE arc wants; the end
    of each primitive is bit-identical to the start of the next."""
    out = []
    for k, ring in enumerate((o.outer, *o.cutouts)):
        if isinstance(ring, Circle):
            out.append(_kicad_prim("circle", k, center=(ring.cx, ring.cy), radius=ring.r))
            continue
        v, n = ring.vertices, len(ring.vertices)
        for i in range(n):
            s, e = (v[i][0], v[i][1]), (v[(i + 1) % n][0], v[(i + 1) % n][1])
            a = prim_from_bulge(s, e, v[i][2])
            if isinstance(a, Line):
                out.append(_kicad_prim("line", k, start=s, end=e))
            else:
                out.append(_kicad_prim("arc", k, start=s, mid=a.mid, end=e, center=a.c,
                                       radius=a.r))
    return out


def _pt(raw: object, what: str) -> Point:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise OutlineError(f"{what} must be [x, y], got {raw!r}")
    return (_number(raw[0], what), _number(raw[1], what))


class _Index:
    """End points bucketed by a grid of the chaining tolerance."""

    def __init__(self, tol: float) -> None:
        self.cell = max(tol, 1e-9) * 4.0
        self.buckets: dict[tuple[int, int], list[int]] = {}

    def key(self, p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / self.cell), math.floor(p[1] / self.cell))

    def add(self, p: Point, i: int) -> None:
        self.buckets.setdefault(self.key(p), []).append(i)

    def near(self, p: Point) -> list[int]:
        kx, ky = self.key(p)
        return [i for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for i in self.buckets.get((kx + dx, ky + dy), ())]


Seg = tuple[Point, Point, float, int]          # start, end, bulge, input position


def _open_edges(prims: Iterable[Mapping]) -> tuple[list[Seg], list[tuple[int, Circle]]]:
    segs: list[Seg] = []
    circles: list[tuple[int, Circle]] = []
    for n, p in enumerate(prims):
        kind = p.get("kind") if isinstance(p, Mapping) else None
        if kind == "circle":
            c = _pt(p.get("center"), f"primitive {n} center")
            r = _number(p.get("radius"), f"primitive {n} radius")
            circles.append((n, Circle(c[0], c[1], r)))
        elif kind == "line":
            segs.append((_pt(p.get("start"), f"primitive {n} start"),
                         _pt(p.get("end"), f"primitive {n} end"), 0.0, n))
        elif kind == "arc":
            s, m, e = (_pt(p.get(f), f"primitive {n} {f}") for f in ("start", "mid", "end"))
            try:
                segs.append((s, e, arc_from_three_points(s, m, e).bulge, n))
            except ValueError as err:
                raise OutlineError(f"primitive {n}: {err}") from err
        else:
            raise OutlineError(f"primitive {n} has kind {kind!r}; expected 'line', 'arc' or "
                               "'circle'")
    return segs, circles


def _chain(segs: list[Seg], tol: float) -> list[tuple[int, Loop]]:
    """Closed loops from open edges, each tagged with the input position of its
    first edge so the caller can keep the input's ring order."""
    index = _Index(tol)
    for i, (s, e, _b, _n) in enumerate(segs):
        index.add(s, i)
        index.add(e, i)
    used = [False] * len(segs)
    loops = []
    for first in range(len(segs)):
        if used[first]:
            continue
        used[first] = True
        s0, cur, b, pos = segs[first]
        verts = [(s0[0], s0[1], b)]
        while dist(cur, s0) > tol:
            cands = [i for i in set(index.near(cur)) if not used[i]
                     and min(dist(segs[i][0], cur), dist(segs[i][1], cur)) <= tol]
            if not cands:
                raise OutlineError(f"the Edge.Cuts chain is open at ({cur[0]:.4f}, {cur[1]:.4f})")
            if len(cands) > 1:
                raise OutlineError(f"{len(cands) + 1} Edge.Cuts edges meet at "
                                   f"({cur[0]:.4f}, {cur[1]:.4f}); the outline must be simple")
            i = cands[0]
            used[i] = True
            s, e, b, n = segs[i]
            pos = min(pos, n)
            if dist(s, cur) > tol:                     # stored the other way round
                s, e, b = e, s, -b
            verts.append((cur[0], cur[1], b))
            cur = e
        loops.append((pos, Loop(tuple(verts))))
    return loops


def from_kicad_primitives(prims: Iterable[Mapping], tol: float = CHAIN_TOL_MM,
                          datum: Sequence[float] | None = None) -> Outline:
    """Chain Edge.Cuts primitives (to_kicad_primitives' shape; ``ring`` is
    ignored, order and direction are free) into a validated Outline: end points
    within `tol` mm join, the largest closed ring is the outline and the others
    are cutouts, in the order they first appear. The frame is kept as given
    (e.g. KiCad sheet coordinates)."""
    segs, circles = _open_edges(prims)
    rings: list[Loop | Circle] = [r for _pos, r in sorted([*_chain(segs, tol), *circles],
                                                          key=lambda pr: pr[0])]
    if not rings:
        raise OutlineError("no Edge.Cuts primitives to build an outline from")
    k = max(range(len(rings)), key=lambda i: abs(ring_signed_area(rings[i])))
    return check(Outline(rings[k], tuple(r for i, r in enumerate(rings) if i != k),
                         None if datum is None else tuple(datum)))

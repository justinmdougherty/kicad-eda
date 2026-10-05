"""route_outline - the board outline on the agent-routing path. Pure stdlib, no kipy.

``backends.pcb_items.outline_geometry`` reads a board's Edge.Cuts shapes (the
board's own and those inside footprints) into KiCad primitive dicts in KiCad
sheet millimetres::

    {"prims": [{"kind": "line" | "arc" | "circle", "start", "mid", "end",
                "center", "radius"}, ...],
     "origin": [x, y] | None,     # the prims' bbox top-left (the board frame origin)
     "datum": [x, y] | None}      # the aux (drill / place) origin, None when unset

This module turns that into an ``outline.Outline`` in the board frame (ROADMAP
D2: mm, Y down, origin = outline bbox top-left) and answers the two questions
agent routing asks of it, always taking and reporting KiCad-frame points (the
frame the agent and the board API work in):

* ``edge_obstacles`` - the board edge and every cutout, grown by the copper to
  edge clearance, as obstacle records next to route_planner's pads / tracks /
  vias ({"kind": "edge", "id": "outline" | "cutout k", "net": "", "distance",
  "t"}) along one straight segment, per copper layer;
* ``path_outline_errors`` - copper outside the board, inside a cutout or
  closer than the edge clearance to an edge.

Distances are exact: segments are measured against the outline's own lines
and arcs (outline_geom), so no 0.05 mm flattening error enters a verdict.
The builders at the top (``kicad_prim`` .. ``prims_origin``) are the pure half
of outline_geometry: rings, rounded rectangles and beziers as KiCad
primitives, plus the origin of a primitive list.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
if str(_TOOLS_DIR) not in sys.path:       # server/tools holds the stdlib-only outline engine
    sys.path.insert(0, str(_TOOLS_DIR))

import outline as ol  # noqa: E402
import outline_geom as og  # noqa: E402

Point = tuple[float, float]
Approach = tuple[float, float, int]       # (distance, t on the segment, ring index)
Box = tuple[float, float, float, float]   # x0, y0, x1, y1
Boxed = tuple[tuple[tuple[og.Prim, Box], ...], ...]   # each ring's primitives with their bboxes

ROUND = 4                  # mm decimals in records and messages (as route_planner)
EDGE_TOL_MM = 1e-4         # a gap this much under the edge clearance still passes
_PRIM_KEYS = ("start", "mid", "end", "center")


def _r(v: float) -> float:
    out = round(float(v), ROUND)
    return 0.0 if out == 0 else out


# ----------------------------------------------------------------------------
# KiCad Edge.Cuts primitive builders (the pure half of pcb_items.outline_geometry)
# ----------------------------------------------------------------------------
def kicad_prim(kind: str, **fields: Any) -> dict[str, Any]:
    """An outline.from_kicad_primitives input dict; unused fields are None."""
    return {"kind": kind, "start": None, "mid": None, "end": None, "center": None,
            "radius": None, **fields}


def arc_prim(start: Sequence[float], mid: Sequence[float], end: Sequence[float]) -> dict:
    """The arc through start, mid and end with its centre and radius; three
    collinear points (a zero-sagitta arc) are the straight edge start -> end."""
    s, m, e = list(start), list(mid), list(end)
    try:
        arc = og.arc_from_three_points(tuple(s), tuple(m), tuple(e))
    except ValueError:
        return kicad_prim("line", start=s, end=e)
    return kicad_prim("arc", start=s, mid=m, end=e, center=[_r(arc.c[0]), _r(arc.c[1])],
                      radius=_r(arc.r))


def closed_chain(pieces: Sequence[Sequence[Any]]) -> list[dict]:
    """A closed ring's primitives from its pieces in order: ("point", xy)
    vertices and ("arc", start, mid, end) arcs. A straight edge joins each
    piece to the next where they do not already meet (KiCad polygon nodes, a
    rounded rectangle's corners); the last piece is joined to the first."""
    ends = [(piece[1], piece[-1]) for piece in pieces]
    out: list[dict] = []
    for i, piece in enumerate(pieces):
        if piece[0] == "arc":
            out.append(arc_prim(*piece[1:4]))
        here, nxt = ends[i][1], ends[(i + 1) % len(pieces)][0]
        if og.dist(here, nxt) > og.EPS:
            out.append(kicad_prim("line", start=list(here), end=list(nxt)))
    return out


def _polar(c: Point, r: float, deg: float) -> list[float]:
    ang = math.radians(deg)
    return [_r(c[0] + r * math.cos(ang)), _r(c[1] + r * math.sin(ang))]


def rect_prims(corner: Sequence[float], opposite: Sequence[float],
               radius: float = 0.0) -> list[dict]:
    """A rectangle's primitives from two opposite corners; a corner radius
    (KiCad 10+ rounded rectangle, clamped to half the short side) gives four
    90 degree arcs joined by straight edges."""
    x0, x1 = sorted((float(corner[0]), float(opposite[0])))
    y0, y1 = sorted((float(corner[1]), float(opposite[1])))
    r = max(0.0, min(float(radius), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r <= og.EPS:
        return closed_chain([("point", c) for c in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
    corners = (((x1 - r, y0 + r), -90.0), ((x1 - r, y1 - r), 0.0),
               ((x0 + r, y1 - r), 90.0), ((x0 + r, y0 + r), 180.0))
    return closed_chain([("arc", *(_polar(c, r, a0 + step) for step in (0.0, 45.0, 90.0)))
                         for c, a0 in corners])


def _bezier_at(pts: Sequence[Point], t: float) -> list[float]:
    u = 1.0 - t
    w = (u * u * u, 3.0 * u * u * t, 3.0 * u * t * t, t * t * t)
    return [_r(sum(wi * p[axis] for wi, p in zip(w, pts, strict=True))) for axis in (0, 1)]


def bezier_prims(p0: Sequence[float], p1: Sequence[float], p2: Sequence[float],
                 p3: Sequence[float], max_error: float = ol.CHORD_ERROR_MM) -> list[dict]:
    """A cubic bezier as straight edges from p0 to p3 (exact end points), the
    chord count from Wang's bound so no chord strays more than max_error mm
    from the curve."""
    pts = [(float(p[0]), float(p[1])) for p in (p0, p1, p2, p3)]
    bend = max(math.hypot(pts[i][0] - 2 * pts[i + 1][0] + pts[i + 2][0],
                          pts[i][1] - 2 * pts[i + 1][1] + pts[i + 2][1]) for i in (0, 1))
    n = max(1, math.ceil(math.sqrt(0.75 * bend / max_error)))
    chain = [list(pts[0]), *(_bezier_at(pts, i / n) for i in range(1, n)), list(pts[3])]
    return [kicad_prim("line", start=s, end=e) for s, e in zip(chain, chain[1:], strict=False)
            if og.dist(s, e) > og.EPS]


def _top_left(prim: Mapping[str, Any]) -> Point:
    if prim["kind"] == "circle":
        (cx, cy), r = prim["center"], float(prim["radius"])
        return cx - r, cy - r
    if prim["kind"] == "arc":
        arc = og.arc_from_three_points(*(tuple(prim[key]) for key in ("start", "mid", "end")))
        return og.prim_bbox(arc)[:2]
    return min(prim["start"][0], prim["end"][0]), min(prim["start"][1], prim["end"][1])


def prims_origin(prims: Sequence[Mapping[str, Any]]) -> list[float] | None:
    """[x, y] top-left of the primitives' bbox (arcs exact), None without any."""
    corners = [_top_left(p) for p in prims]
    if not corners:
        return None
    return [_r(min(c[0] for c in corners)), _r(min(c[1] for c in corners))]


# ----------------------------------------------------------------------------
# outline_from_geometry
# ----------------------------------------------------------------------------
def _xy(value: Any, what: str, error: type[ValueError] = ValueError) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in value):
        raise error(f"{what} must be [x, y] in finite mm, got {value!r}")
    return float(value[0]), float(value[1])


def _shifted(prim: Any, i: int, dx: float, dy: float) -> dict:
    if not isinstance(prim, Mapping):
        raise ol.OutlineError(f"geometry prims[{i}] must be a primitive dict, got {prim!r}")
    moved = {}
    for key in _PRIM_KEYS:
        if prim.get(key) is not None:
            x, y = _xy(prim[key], f"geometry prims[{i}].{key}", ol.OutlineError)
            moved[key] = (x + dx, y + dy)
    return {**prim, **moved}


def outline_from_geometry(geom: Mapping[str, Any]) -> tuple[ol.Outline, Point]:
    """(Outline in the board frame, origin): pcb_items.outline_geometry's
    prims moved by -origin and chained by outline.from_kicad_primitives; the
    datum (aux origin) moves with them. Raises OutlineError naming the key, or
    the chaining problem (an open chain, edges meeting three at a point) in
    board-frame coordinates, adding how many shapes outline_geometry could not
    convert when that is why the chain is open."""
    if not isinstance(geom, Mapping):
        raise ol.OutlineError(f"the outline geometry must be an object from "
                              f"pcb_items.outline_geometry, got {geom!r}")
    prims = geom.get("prims")
    if not isinstance(prims, (list, tuple)):
        raise ol.OutlineError(f"geometry 'prims' must be a list of Edge.Cuts primitives, "
                              f"got {prims!r}")
    if not prims:
        raise ol.OutlineError("the board has no Edge.Cuts outline (outline_geometry found no "
                              "primitives); draw the board outline first")
    ox, oy = _xy(geom.get("origin"), "geometry 'origin'", ol.OutlineError)
    datum = geom.get("datum")
    if datum is not None:
        dx, dy = _xy(datum, "geometry 'datum'", ol.OutlineError)
        datum = (dx - ox, dy - oy)
    moved = [_shifted(p, i, -ox, -oy) for i, p in enumerate(prims)]
    try:
        return ol.from_kicad_primitives(moved, datum=datum), (ox, oy)
    except ol.OutlineError as exc:
        skipped = int(geom.get("skipped") or 0)
        if not skipped:
            raise
        raise ol.OutlineError(f"{exc}; {skipped} Edge.Cuts shape(s) outline_geometry cannot "
                              f"convert (an ellipse) were left out") from exc


# ----------------------------------------------------------------------------
# exact closest approach of a segment to the outline's rings
# ----------------------------------------------------------------------------
def _ring_name(k: int) -> str:
    return "outline" if k == 0 else f"cutout {k}"


def _approach(a: Point, b: Point, prim: og.Prim) -> tuple[float, float]:
    """(exact distance, t on a-b of the closest approach) between the segment
    a-b (a == b: a point) and one outline primitive; 0 where they meet."""
    if og.dist(a, b) <= og.EPS:
        return og.closest_point(prim, a)[1], 0.0
    seg = og.Line(a, b)
    hits = og.intersections(seg, prim)
    if hits:
        return 0.0, min(og.closest_point(seg, x)[2] for x in hits)
    cands = [(og.closest_point(prim, a)[1], 0.0), (og.closest_point(prim, b)[1], 1.0)]
    cands += [og.closest_point(seg, end)[1:] for end in (prim.start, prim.end)]
    if isinstance(prim, og.Arc):                  # interior: the centre's foot on a-b
        _foot, _d, t = og.closest_point(seg, prim.c)
        v = og.sub(seg.point_at(t), prim.c)
        if 0.0 < t < 1.0 and og.norm(v) > prim.r and prim.param(og.angle_of(v)) is not None:
            cands.append((og.norm(v) - prim.r, t))
    return min(cands)


def _boxes_meet(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _boxed(o: ol.Outline) -> Boxed:
    """Each ring's primitives (o.rings order) paired with their exact bboxes,
    computed once per public call and shared by all its segment queries."""
    return tuple(tuple((p, og.prim_bbox(p)) for p in prims) for prims in o.rings)


def _ring_approaches(rings: Boxed, a: Point, b: Point, reach: float) -> list[Approach]:
    """(distance, t, ring) of the segment's closest approach to each ring that
    it meets or comes nearer than `reach` mm to, nearest first. Only the
    primitives whose bbox lies within reach are measured."""
    pad = max(reach, 0.0) + 1e-6
    box = (min(a[0], b[0]) - pad, min(a[1], b[1]) - pad,
           max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)
    found: list[Approach] = []
    for k, ring in enumerate(rings):
        near = [_approach(a, b, p) for p, pb in ring if _boxes_meet(box, pb)]
        best = min(near, default=None)
        if best is not None and (best[0] <= 0.0 or best[0] < reach):
            found.append((best[0], best[1], k))
    return sorted(found)


def _off_board(o: ol.Outline, p: Point) -> int | None:
    """None when p is on the board; else the ring it is beyond: 0 = outside
    the outline, k = inside cutout k (the nearest edge is that ring's)."""
    return None if ol.contains(o, p) else ol.nearest_edge(o, p).ring


def _checked(outline: Any, origin: Any, edge_clearance: Any, width: Any) -> tuple:
    if not isinstance(outline, ol.Outline):
        raise ValueError(f"outline must be the Outline outline_from_geometry returns, got "
                         f"{type(outline).__name__}")
    for what, value in (("edge_clearance", edge_clearance), ("width", width)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value < 0:
            raise ValueError(f"{what} must be a finite number >= 0 mm, got {value!r}")
    return _xy(origin, "origin"), float(edge_clearance), float(width) / 2.0


# ----------------------------------------------------------------------------
# edge_obstacles / path_outline_errors
# ----------------------------------------------------------------------------
def _edge_records(o: ol.Outline, a: Point, b: Point, clear: float,
                  half: float) -> list[dict[str, Any]]:
    hits = _ring_approaches(_boxed(o), a, b, clear + half - EDGE_TOL_MM)
    gaps = {k: (d - clear, t) for d, t, k in hits}
    side = _off_board(o, a)
    if side is not None and not any(d <= 0.0 for d, _t, _k in hits):   # wholly off the board
        gaps[side] = (-(clear + ol.distance_to_edge(o, a)), 0.0)
    records = [{"kind": "edge", "id": _ring_name(k), "net": "", "distance": _r(gap), "t": _r(t)}
               for k, (gap, t) in gaps.items()]
    return sorted(records, key=lambda rec: (rec["t"], rec["id"]))


def edge_obstacles(outline: ol.Outline, origin: Sequence[float], edge_clearance: float,
                   layers: Sequence[str], a: Sequence[float], b: Sequence[float],
                   width: float = 0.0) -> dict[str, list[dict[str, Any]]]:
    """{layer: [edge records]} for the straight segment a-b (KiCad-frame mm)
    of a track `width` mm wide: the outline and each cutout, grown by
    `edge_clearance`, are keep-outs on every copper layer. A record is
    {"kind": "edge", "id": "outline" | "cutout k", "net": "", "distance", "t"}
    like route_planner.obstacles': "distance" runs from the centreline to the
    keep-out (the edge grown by edge_clearance; negative when the line crosses
    the edge or lies off the board) and the ring is listed when it is under
    width/2; "t" is where along a-b (0..1). Sorted by t."""
    (ox, oy), clear, half = _checked(outline, origin, edge_clearance, width)
    p, q = _xy(a, "a"), _xy(b, "b")
    records = _edge_records(outline, (p[0] - ox, p[1] - oy), (q[0] - ox, q[1] - oy), clear, half)
    return {layer: [dict(rec) for rec in records] for layer in layers}


def _fmt(p: Point, origin: Point) -> str:
    return f"[{_r(p[0] + origin[0])}, {_r(p[1] + origin[1])}]"


def _at(a: Point, b: Point, t: float) -> Point:
    return og.Line(a, b).point_at(t) if a != b else a


def _piece_error(o: ol.Outline, rings: Boxed, origin: Point, a: Point, b: Point,
                 half: float, clear: float) -> str | None:
    """Why the copper of a-b (a point when a == b) is not on the board with
    `clear` mm to every edge, None when it is. `rings` is _boxed(o)."""
    what = (f"copper at {_fmt(a, origin)}" if a == b
            else f"segment {_fmt(a, origin)} -> {_fmt(b, origin)}")
    hits = _ring_approaches(rings, a, b, clear + half - EDGE_TOL_MM)
    crossing = next(((t, k) for d, t, k in hits if d <= 0.0), None)
    if crossing is not None and a != b:
        return f"{what} crosses {_ring_name(crossing[1])} at {_fmt(_at(a, b, crossing[0]), origin)}"
    side = _off_board(o, a)
    if side is not None:
        where = "outside the board" if side == 0 else f"inside {_ring_name(side)}"
        return f"{what} lies {where}"
    if not hits:
        return None
    d, t, k = hits[0]
    return (f"{what}: its copper comes {_r(d - half)} mm from the {_ring_name(k)} edge near "
            f"{_fmt(_at(a, b, t), origin)}; keep >= {_r(clear)} mm (edge clearance)")


def path_outline_errors(outline: ol.Outline, origin: Sequence[float],
                        points: Sequence[Sequence[float]], width: float,
                        edge_clearance: float) -> list[str]:
    """Why the copper of a polyline `width` mm wide through `points`
    (KiCad-frame mm) is not on the board, one message per offending segment:
    it crosses the outline or a cutout, lies outside the board or inside a
    cutout, or its copper edge comes closer than `edge_clearance` mm to an
    edge. A single point is judged as a round pad `width` mm across (a via).
    Messages carry KiCad-frame coordinates; [] when the path is clean."""
    (ox, oy), clear, half = _checked(outline, origin, edge_clearance, width)
    if not isinstance(points, (list, tuple)) or not points:
        raise ValueError(f"points must hold at least one [x, y] in mm, got {points!r}")
    pts = [_xy(p, f"points[{i}]") for i, p in enumerate(points)]
    pts = [(x - ox, y - oy) for x, y in pts]
    pieces = [(p, q) for p, q in zip(pts, pts[1:], strict=False) if og.dist(p, q) > og.EPS]
    rings = _boxed(outline)
    errors = [_piece_error(outline, rings, (ox, oy), p, q, half, clear)
              for p, q in pieces or [(pts[0], pts[0])]]
    return [e for e in errors if e]

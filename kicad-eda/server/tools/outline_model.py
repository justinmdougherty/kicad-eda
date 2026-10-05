#!/usr/bin/env python3
"""Board outline model: an outer ring, cutouts and a datum, with exact queries.

Stdlib only, see outline_geom.py for the conventions (mm; plain math angles;
the board frame is Y down so a math-CCW turn looks clockwise on screen).
``outline.py`` re-exports everything public here; import that.

Invariants an ``Outline`` keeps (``__post_init__`` enforces the orientation):
  * the outer ring runs math-CCW (positive signed area) and every cutout runs
    math-CW, so the board material is always on the LEFT of the direction of
    travel. "Outward" (away from the material) is therefore the right-hand
    normal everywhere: off the board on the outer ring, into the hole on a
    cutout.
  * a ring is a ``Loop`` (vertices [x, y, bulge], implicitly closed) or a
    ``Circle``. Rings are stored as given (after orientation); validity
    (simple rings, cutouts inside and disjoint) is checked by ``check()``,
    which every public constructor in outline.py calls.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline_geom import (BULGE_EPS, EPS, TAU, Arc, Line, Point, Prim, add,  # noqa: E402
                          closest_point, cross, crossings_right, dist, flatten_prim,
                          intersections, mul, prim_bbox, prim_distance, prim_from_bulge,
                          prim_signed_area, ray_hits, sub, unit)

CHORD_ERROR_MM = 0.05      # max chord error when an arc is flattened for math
TOUCH_TOL_MM = 1e-6        # a point this close to an edge is ON the edge

Vertex = tuple[float, float, float]


class OutlineError(ValueError):
    """An outline that cannot be built or used; the message says what and where."""


def _num(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise OutlineError(f"{what} must be a finite number, got {v!r}")
    return float(v)


def _fmt(p: Point) -> str:
    return f"({p[0]:.4f}, {p[1]:.4f})"


def _clean_vertices(raw: Iterable) -> tuple[Vertex, ...]:
    verts: list[Vertex] = []
    for i, v in enumerate(raw):
        if not isinstance(v, (list, tuple)) or len(v) not in (2, 3):
            raise OutlineError(f"vertex {i} {v!r} is not [x, y] or [x, y, bulge]")
        b = _num(v[2], f"vertex {i} bulge") if len(v) == 3 else 0.0
        verts.append((_num(v[0], f"vertex {i} x"), _num(v[1], f"vertex {i} y"), b))
    if len(verts) >= 2 and dist(verts[0][:2], verts[-1][:2]) <= EPS:
        if verts[-1][2] != 0:                          # explicitly closed loop
            raise OutlineError("the last vertex repeats the first but carries a bulge")
        verts.pop()
    if len(verts) < 2:
        raise OutlineError(f"a loop needs at least 2 distinct vertices, got {len(verts)}")
    for i, v in enumerate(verts):
        w = verts[(i + 1) % len(verts)]
        if dist(v[:2], w[:2]) <= EPS:
            raise OutlineError(f"vertices {i} and {(i + 1) % len(verts)} coincide at "
                               f"{_fmt(v[:2])} (zero-length edge)")
    if len(verts) == 2 and abs(verts[0][2]) < BULGE_EPS and abs(verts[1][2]) < BULGE_EPS:
        raise OutlineError("a 2-vertex loop without bulges encloses no area")
    return tuple(verts)


@dataclass(frozen=True)
class Loop:
    """Closed loop of vertices (x, y, bulge); bulge describes the edge to the
    next vertex (0 = straight). A trailing copy of the first vertex is dropped."""
    vertices: tuple[Vertex, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "vertices", _clean_vertices(self.vertices))

    def reversed(self) -> Loop:
        """The same loop traversed the other way (bulges move and flip sign)."""
        v, n = self.vertices, len(self.vertices)
        return Loop(tuple((v[n - 1 - k][0], v[n - 1 - k][1], 0.0 - v[(n - 2 - k) % n][2])
                          for k in range(n)))         # 0.0 - b: no -0.0 bulges

    def translated(self, dx: float, dy: float) -> Loop:
        return Loop(tuple((x + dx, y + dy, b) for x, y, b in self.vertices))


@dataclass(frozen=True)
class Circle:
    """A circular ring (centre cx, cy; radius r)."""
    cx: float
    cy: float
    r: float

    def __post_init__(self) -> None:
        for name in ("cx", "cy", "r"):
            object.__setattr__(self, name, _num(getattr(self, name), f"circle {name}"))
        if self.r <= EPS:
            raise OutlineError(f"circle radius must be > 0, got {self.r!r}")

    def translated(self, dx: float, dy: float) -> Circle:
        return Circle(self.cx + dx, self.cy + dy, self.r)


Ring = Loop | Circle


def ring_prims(ring: Ring, ccw: bool = True) -> tuple[Prim, ...]:
    """Exact primitives of a ring in its stored order. `ccw` only orients a
    Circle (a Loop's orientation is its vertex order)."""
    if isinstance(ring, Circle):
        return (Arc((ring.cx, ring.cy), ring.r, 0.0, TAU if ccw else -TAU),)
    v, n = ring.vertices, len(ring.vertices)
    return tuple(prim_from_bulge(v[i][:2], v[(i + 1) % n][:2], v[i][2]) for i in range(n))


def ring_signed_area(ring: Ring) -> float:
    """Signed area (math-CCW positive); a Circle counts as CCW."""
    return sum(prim_signed_area(p) for p in ring_prims(ring))


def _oriented(ring: Ring, ccw: bool, what: str) -> Ring:
    if not isinstance(ring, (Loop, Circle)):
        raise OutlineError(f"{what} must be a Loop or Circle, got {type(ring).__name__}")
    if isinstance(ring, Circle):
        return ring
    a = ring_signed_area(ring)
    if abs(a) <= 1e-12:
        crossings = _self_crossings(ring_prims(ring))
        why = "".join(f"; edges {i} and {j} intersect at {_fmt(x)}" for i, j, x in crossings[:3])
        raise OutlineError(f"{what} encloses no area{why}")
    return ring if (a > 0) == ccw else ring.reversed()


@dataclass(frozen=True)
class Outline:
    """Board outline: outer ring, cutouts and the external datum (the source
    frame's origin expressed in this frame, or None)."""
    outer: Ring
    cutouts: tuple[Ring, ...] = ()
    datum: Point | None = None
    rings: tuple[tuple[Prim, ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        outer = _oriented(self.outer, True, "the outer ring")
        cutouts = tuple(_oriented(c, False, f"cutout {i + 1}")
                        for i, c in enumerate(self.cutouts or ()))
        datum = self.datum
        if datum is not None:
            if not isinstance(datum, (list, tuple)) or len(datum) != 2:
                raise OutlineError(f"datum must be [x, y], got {datum!r}")
            datum = (_num(datum[0], "datum x"), _num(datum[1], "datum y"))
        object.__setattr__(self, "outer", outer)
        object.__setattr__(self, "cutouts", cutouts)
        object.__setattr__(self, "datum", datum)
        object.__setattr__(self, "rings", (ring_prims(outer, True),)
                           + tuple(ring_prims(c, False) for c in cutouts))


def ring_name(k: int) -> str:
    return "outline" if k == 0 else f"cutout {k}"


def translate(o: Outline, dx: float, dy: float) -> Outline:
    """The outline moved by (dx, dy), datum included."""
    datum = None if o.datum is None else (o.datum[0] + dx, o.datum[1] + dy)
    return Outline(o.outer.translated(dx, dy), tuple(c.translated(dx, dy) for c in o.cutouts),
                   datum)


# --- measures ------------------------------------------------------------------------
def all_prims(o: Outline) -> Iterator[tuple[int, int, Prim]]:
    for k, prims in enumerate(o.rings):
        for i, p in enumerate(prims):
            yield k, i, p


def bbox(o: Outline) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of the outer ring, arcs exact."""
    boxes = [prim_bbox(p) for p in o.rings[0]]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def area(o: Outline) -> float:
    """Board area: outer minus cutouts (mm^2)."""
    return sum(prim_signed_area(p) for _k, _i, p in all_prims(o))


class PrimIndex:
    """Uniform-grid bucket index of primitives (by bbox), so a query near a
    point or along a short ray looks at a few primitives instead of all of
    them (DXF outlines can carry thousands of segments). `source` is an
    Outline (items (ring, index, prim)) or any iterable of (key, key, prim)."""

    def __init__(self, source: Outline | Iterable[tuple[int, int, Prim]]) -> None:
        items = list(all_prims(source) if isinstance(source, Outline) else source)
        boxes = [prim_bbox(p) for _k, _i, p in items]
        x0 = min((b[0] for b in boxes), default=0.0)
        y0 = min((b[1] for b in boxes), default=0.0)
        x1 = max((b[2] for b in boxes), default=0.0)
        y1 = max((b[3] for b in boxes), default=0.0)
        self.per_axis = max(1, 2 * int(math.sqrt(len(items))))
        self.cell = max(x1 - x0, y1 - y0, 1e-6) / self.per_axis
        self.x0, self.y0 = x0, y0
        self.buckets: dict[tuple[int, int], list[tuple[int, int, Prim]]] = {}
        for item, box in zip(items, boxes):
            for key in self._cells(box):
                self.buckets.setdefault(key, []).append(item)

    def _span(self, lo: float, hi: float, origin: float) -> range:
        """Cell indices covering [lo, hi], clamped to the grid (+1 for edge rounding)."""
        first = max(math.floor((lo - origin) / self.cell), -1)
        last = min(math.floor((hi - origin) / self.cell), self.per_axis + 1)
        return range(first, last + 1)

    def _cells(self, box: Sequence[float]) -> Iterator[tuple[int, int]]:
        for cx in self._span(box[0], box[2], self.x0):
            for cy in self._span(box[1], box[3], self.y0):
                yield cx, cy

    def near(self, box: Sequence[float]) -> list[tuple[int, int, Prim]]:
        """Every primitive whose bbox may overlap `box` (x0, y0, x1, y1)."""
        seen: set[tuple[int, int]] = set()
        out = []
        for key in self._cells(box):
            for item in self.buckets.get(key, ()):
                if (item[0], item[1]) not in seen:
                    seen.add((item[0], item[1]))
                    out.append(item)
        return out


def flatten(o: Outline, max_error: float = CHORD_ERROR_MM) -> tuple[tuple[Point, ...], ...]:
    """Each ring as a closed polyline (outer first), every chord within
    `max_error` of its arc and every vertex exactly on the outline. For math
    only: KiCad gets exact arcs from outline.to_kicad_primitives."""
    if isinstance(max_error, bool) or not isinstance(max_error, (int, float)) \
            or not max_error > 0:
        raise ValueError(f"max chord error must be > 0 mm, got {max_error!r}")
    return tuple(tuple(q for p in prims for q in flatten_prim(p, max_error))
                 for prims in o.rings)


# --- point queries -----------------------------------------------------------------
def _ring_contains(prims: Sequence[Prim], q: Point) -> bool:
    """Exact even-odd test: the parity of the ray's crossings with the ring's
    own lines and arcs (outline_geom.crossings_right)."""
    return sum(crossings_right(p, q) for p in prims) % 2 == 1


def _inside_material(o: Outline, q: Point) -> bool:
    return _ring_contains(o.rings[0], q) and not any(_ring_contains(r, q) for r in o.rings[1:])


def distance_to_edge(o: Outline, p: Point) -> float:
    """Exact distance from p to the nearest outline or cutout edge."""
    return min(closest_point(pr, p)[1] for _k, _i, pr in all_prims(o))


def contains(o: Outline, p: Point, clearance: float = 0.0) -> bool:
    """p is on the board (inside the outer ring, outside every cutout) and at
    least `clearance` mm from every edge. A point ON an edge counts as inside
    only when no clearance is asked for."""
    d = distance_to_edge(o, p)
    if d <= TOUCH_TOL_MM:
        return clearance <= TOUCH_TOL_MM
    if d < clearance - TOUCH_TOL_MM:
        return False
    return _inside_material(o, p)


def _point_in_polygon(pts: Sequence[Point], q: Point) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        if (a[1] > q[1]) != (b[1] > q[1]):
            if q[0] < a[0] + (q[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                inside = not inside
    return inside


def _edge_stays_inside(o: Outline, seg: Line) -> bool:
    """With zero clearance: split the segment where it meets the outline and
    require every piece's midpoint on the board (touching is allowed)."""
    ts = {0.0, 1.0}
    for _k, _i, p in all_prims(o):
        for x in intersections(seg, p):
            ts.add(closest_point(seg, x)[2])
    ts_sorted = sorted(ts)
    return all(contains(o, seg.point_at((t0 + t1) / 2.0))
               for t0, t1 in zip(ts_sorted, ts_sorted[1:]) if t1 - t0 > 1e-12)


def contains_polygon(o: Outline, points: Sequence[Sequence[float]],
                     clearance: float = 0.0) -> bool:
    """The simple polygon `points` (e.g. a courtyard) lies on the board: inside
    the outer ring, outside every cutout, at least `clearance` mm from every
    edge, and enclosing no cutout."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 3:
        raise ValueError(f"a polygon needs at least 3 points, got {len(pts)}")
    if not all(contains(o, p, clearance) for p in pts):
        return False
    segs = [Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))
            if dist(pts[i], pts[(i + 1) % len(pts)]) > EPS]
    if clearance > TOUCH_TOL_MM:
        lim = clearance - TOUCH_TOL_MM
        for seg in segs:
            for _k, _i, p in all_prims(o):
                if prim_distance(seg, p) < lim:
                    return False
    elif not all(_edge_stays_inside(o, seg) for seg in segs):
        return False
    return not any(_point_in_polygon(pts, prims[0].start) for prims in o.rings[1:])


def contains_rect(o: Outline, rect: Sequence[float], clearance: float = 0.0) -> bool:
    """The axis-aligned rectangle (x0, y0, x1, y1) lies on the board with
    `clearance` (see contains_polygon)."""
    x0, y0, x1, y1 = (float(v) for v in rect)
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"rect must be (x0, y0, x1, y1) with x0 < x1 and y0 < y1, got {rect!r}")
    return contains_polygon(o, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], clearance)


# --- edges --------------------------------------------------------------------------
@dataclass(frozen=True)
class Edge:
    """One primitive of a ring: ring 0 = outer, k = cutouts[k - 1]."""
    ring: int
    index: int
    prim: Prim

    @property
    def kind(self) -> str:
        if isinstance(self.prim, Line):
            return "line"
        return "circle" if self.prim.is_circle else "arc"

    def normal_at(self, q: Point) -> Point:
        """Outward unit normal (away from the board material) at the point q of
        this edge: the right-hand normal of the direction of travel."""
        p = self.prim
        if isinstance(p, Line):
            t = p.tangent()
            return (t[1], -t[0])
        u = unit(sub(q, p.c))
        return u if p.sweep > 0 else (-u[0], -u[1])


@dataclass(frozen=True)
class EdgeHit:
    """A point on an edge: its ring/index, distance from the query point and
    the outward normal there (a vertex gets the bisector of its two edges)."""
    ring: int
    index: int
    point: Point
    distance: float
    normal: Point


def edges(o: Outline) -> tuple[Edge, ...]:
    return tuple(Edge(k, i, p) for k, i, p in all_prims(o))


def _vertex_normal(o: Outline, k: int, i: int, t: float, q: Point) -> Point:
    prims = o.rings[k]
    here = Edge(k, i, prims[i]).normal_at(q)
    if len(prims) == 1 or 0.0 < t < 1.0:
        return here
    j = (i - 1) % len(prims) if t == 0.0 else (i + 1) % len(prims)
    other = Edge(k, j, prims[j]).normal_at(q)
    s = add(here, other)
    return unit(s) if math.hypot(*s) > 1e-9 else here


def nearest_edge(o: Outline, p: Point) -> EdgeHit:
    """The nearest edge point to p, with the outward normal there."""
    best: tuple[float, int, int, Point, float] | None = None
    for k, i, pr in all_prims(o):
        q, d, t = closest_point(pr, p)
        if best is None or d < best[0] - 1e-12:
            best = (d, k, i, q, t)
    d, k, i, q, t = best
    return EdgeHit(k, i, q, d, _vertex_normal(o, k, i, t, q))


def ray_to_edge(o: Outline, p: Point, direction: Point, skip: float = 0.0) -> EdgeHit | None:
    """First edge crossed by the ray p + t * direction (t > skip), or None."""
    d = unit(direction)
    best: tuple[float, int, int] | None = None
    for k, i, pr in all_prims(o):
        for t in ray_hits(pr, p, d):
            if t > skip and (best is None or t < best[0]):
                best = (t, k, i)
    if best is None:
        return None
    t, k, i = best
    q = add(p, mul(d, t))
    return EdgeHit(k, i, q, t, Edge(k, i, o.rings[k][i]).normal_at(q))


# --- validity ----------------------------------------------------------------------
def _boxes_touch(a: tuple, b: tuple, tol: float = 1e-7) -> bool:
    return a[0] <= b[2] + tol and b[0] <= a[2] + tol and a[1] <= b[3] + tol and b[1] <= a[3] + tol


def _self_crossings(prims: Sequence[Prim]) -> list[tuple[int, int, Point]]:
    n = len(prims)
    boxes = [prim_bbox(p) for p in prims]
    order = sorted(range(n), key=lambda i: boxes[i][0])
    found = []
    for a_pos, i in enumerate(order):
        for j in order[a_pos + 1:]:
            if boxes[j][0] > boxes[i][2] + 1e-7:
                break
            if not _boxes_touch(boxes[i], boxes[j]):
                continue
            lo, hi = min(i, j), max(i, j)
            shared = []
            if hi == lo + 1:
                shared.append(prims[lo].end)
            if lo == 0 and hi == n - 1:
                shared.append(prims[hi].end)
            for x in intersections(prims[lo], prims[hi]):
                if all(dist(x, s) > 1e-6 for s in shared):
                    found.append((lo, hi, x))
                    break
    return found


def _rings_cross(a: Sequence[Prim], b: Sequence[Prim]) -> Point | None:
    for p in a:
        bp = prim_bbox(p)
        for q in b:
            if _boxes_touch(bp, prim_bbox(q)):
                xs = intersections(p, q)
                if xs:
                    return xs[0]
    return None


def geometry_problems(o: Outline) -> list[str]:
    """Why the outline is not a valid board (empty when it is): a ring crosses
    itself, a cutout crosses or lies outside the outline, cutouts overlap."""
    problems = []
    for k, prims in enumerate(o.rings):
        for i, j, x in _self_crossings(prims):
            problems.append(f"{ring_name(k)}: edges {i} and {j} intersect at {_fmt(x)}")
    for k in range(1, len(o.rings)):
        ring = o.rings[k]
        x = _rings_cross(o.rings[0], ring)
        if x is not None:
            problems.append(f"{ring_name(k)} crosses the outline at {_fmt(x)}")
        elif not _ring_contains(o.rings[0], ring[0].start):
            problems.append(f"{ring_name(k)} lies outside the outline (at {_fmt(ring[0].start)})")
        for m in range(k + 1, len(o.rings)):
            x = _rings_cross(ring, o.rings[m])
            nested = (_ring_contains(ring, o.rings[m][0].start)
                      or _ring_contains(o.rings[m], ring[0].start))
            if x is not None or nested:
                where = _fmt(x) if x is not None else "one inside the other"
                problems.append(f"{ring_name(k)} and {ring_name(m)} overlap ({where})")
    return problems


def check(o: Outline) -> Outline:
    """Return `o` when it is a valid board outline, else raise OutlineError."""
    problems = geometry_problems(o)
    if problems:
        raise OutlineError("invalid outline: " + "; ".join(problems))
    return o


def cross_turn(p: Prim, q: Prim) -> float:
    """sin of the turn from p's end tangent to q's start tangent (+ = left)."""
    return cross(p.tangent(1.0), q.tangent(0.0))

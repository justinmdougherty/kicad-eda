#!/usr/bin/env python3
"""Exact 2-D primitive math for board outlines: lines, circular arcs, DXF bulges.

Stdlib only (math, dataclasses): KiCad's bundled Python imports this from the
SWIG scripts (server/tools is their sys.path[0]) and the server venv imports
it for lint and the board API. ``outline.py`` is the public entry point; this
module holds the per-primitive math it is built on.

Conventions
-----------
* Units are mm. Formulas are plain (x, y) math: an angle runs from +X towards
  +Y and a POSITIVE sweep turns from +X towards +Y ("counter-clockwise in math
  terms"). The board frame is Y DOWN, so on screen (KiCad, Gerber viewers) a
  math-CCW turn looks clockwise. Nothing here flips anything; the math is
  consistent in either frame and only ``outline.normalize_external`` flips Y
  (negating every bulge) when it brings a Y-up DXF into the board frame.
* A DXF bulge b on the edge p0 -> p1 is tan(theta / 4), theta the signed sweep
  of the arc from p0 to p1. b > 0 sweeps math-CCW, so the arc bows to the RIGHT
  of the chord direction; b = +-1 is a half circle; |b| > 1 is more than half.
* Arcs are exact (centre, radius, start angle, signed sweep) and keep their
  exact end points; flattening is only for callers that need polylines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]

EPS = 1e-9                 # mm: points closer than this coincide
BULGE_EPS = 1e-9           # |bulge| below this is a straight edge
TAU = 2.0 * math.pi
_PARALLEL = 1e-12          # |sin| below which two directions are parallel


# --- vectors -------------------------------------------------------------------
def sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def mul(a: Point, k: float) -> Point:
    return (a[0] * k, a[1] * k)


def dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def norm(a: Point) -> float:
    return math.hypot(a[0], a[1])


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(a: Point) -> Point:
    n = norm(a)
    if n < EPS:
        raise ValueError(f"cannot normalise the near-zero vector {a!r}")
    return (a[0] / n, a[1] / n)


def left(a: Point) -> Point:
    """`a` rotated +90 degrees (math terms): the left normal of a direction."""
    return (-a[1], a[0])


def angle_of(a: Point) -> float:
    return math.atan2(a[1], a[0])


def wrap_tau(a: float) -> float:
    """`a` wrapped into [0, 2*pi)."""
    w = math.fmod(a, TAU)
    return w + TAU if w < 0 else w


def wrap_pi(a: float) -> float:
    """`a` wrapped into [-pi, pi)."""
    return wrap_tau(a + math.pi) - math.pi


# --- primitives ------------------------------------------------------------------
@dataclass(frozen=True)
class Line:
    """The segment a -> b."""
    a: Point
    b: Point

    @property
    def start(self) -> Point:
        return self.a

    @property
    def end(self) -> Point:
        return self.b

    @property
    def mid(self) -> Point:
        return self.point_at(0.5)

    @property
    def length(self) -> float:
        return dist(self.a, self.b)

    def point_at(self, t: float) -> Point:
        return (self.a[0] + t * (self.b[0] - self.a[0]), self.a[1] + t * (self.b[1] - self.a[1]))

    def tangent(self, t: float = 0.0) -> Point:
        return unit(sub(self.b, self.a))


@dataclass(frozen=True)
class Arc:
    """Circular arc: centre `c`, radius `r`, start angle `a0` (rad) and signed
    `sweep` (rad, 0 < |sweep| <= 2*pi, positive = math-CCW). `p0`/`p1` are the
    exact end points (computed from the angles when not given), so a chain of
    primitives built from loop vertices closes without float drift. A full
    circle ends exactly at its start: sin(a0 + 2*pi) != sin(a0) in floats, and
    the resulting 1e-16 mm chord would count as a crossing in parity tests."""
    c: Point
    r: float
    a0: float
    sweep: float
    p0: Point | None = field(default=None, compare=False)
    p1: Point | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not (self.r > 0 and math.isfinite(self.r)):
            raise ValueError(f"arc radius must be positive, got {self.r!r}")
        if not 0 < abs(self.sweep) <= TAU + 1e-12:
            raise ValueError(f"arc sweep must be in (0, 2*pi], got {self.sweep!r}")
        if self.p0 is None:
            object.__setattr__(self, "p0", self._at_angle(self.a0))
        if self.p1 is None:
            object.__setattr__(self, "p1", self.p0 if self.is_circle
                               else self._at_angle(self.a0 + self.sweep))

    def _at_angle(self, ang: float) -> Point:
        return (self.c[0] + self.r * math.cos(ang), self.c[1] + self.r * math.sin(ang))

    @property
    def start(self) -> Point:
        return self.p0

    @property
    def end(self) -> Point:
        return self.p1

    @property
    def mid(self) -> Point:
        return self.point_at(0.5)

    @property
    def length(self) -> float:
        return self.r * abs(self.sweep)

    @property
    def is_circle(self) -> bool:
        return abs(abs(self.sweep) - TAU) < 1e-12

    @property
    def bulge(self) -> float:
        """DXF bulge tan(sweep / 4) (infinite for a full circle)."""
        return math.inf if self.is_circle else math.tan(self.sweep / 4.0)

    def point_at(self, t: float) -> Point:
        if t == 0.0:
            return self.p0
        if t == 1.0:
            return self.p1
        return self._at_angle(self.a0 + t * self.sweep)

    def tangent(self, t: float = 0.0) -> Point:
        ang = self.a0 + t * self.sweep
        s = 1.0 if self.sweep > 0 else -1.0
        return (-math.sin(ang) * s, math.cos(ang) * s)

    def param(self, ang: float, tol: float = EPS) -> float | None:
        """Fraction t in [0, 1] at which the arc passes the polar angle `ang`
        (about the centre), or None when `ang` lies outside the arc. `tol` is a
        length tolerance at the end points."""
        total = abs(self.sweep)
        if total >= TAU - 1e-12:
            return wrap_tau((ang - self.a0) * (1 if self.sweep > 0 else -1)) / TAU
        u = wrap_tau((ang - self.a0) * (1 if self.sweep > 0 else -1))
        slack = tol / self.r
        if u <= total + slack:
            return min(u / total, 1.0)
        if u >= TAU - slack:
            return 0.0
        return None


Prim = Line | Arc


def arc_from_bulge(p0: Point, p1: Point, bulge: float) -> Arc:
    """The exact arc p0 -> p1 for a DXF bulge (see the module docstring)."""
    if bulge == 0 or not math.isfinite(bulge):
        raise ValueError(f"an arc needs a finite non-zero bulge, got {bulge!r}")
    chord = dist(p0, p1)
    if chord < EPS:
        raise ValueError(f"zero-length arc chord at {p0!r}")
    b = bulge
    n = left(unit(sub(p1, p0)))
    h = chord * (1.0 - b * b) / (4.0 * b)        # signed centre offset, + = left
    m = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
    c = add(m, mul(n, h))
    r = chord * (1.0 + b * b) / (4.0 * abs(b))
    return Arc(c, r, angle_of(sub(p0, c)), 4.0 * math.atan(b), p0=tuple(p0), p1=tuple(p1))


def prim_from_bulge(p0: Point, p1: Point, bulge: float) -> Prim:
    """A Line for |bulge| < BULGE_EPS (float noise from a DXF / JSON round trip,
    sagitta < 1e-7 mm on a 200 mm edge), else the exact arc."""
    if abs(bulge) < BULGE_EPS:
        return Line(tuple(p0), tuple(p1))
    return arc_from_bulge(p0, p1, bulge)


def arc_from_three_points(start: Point, mid: Point, end: Point) -> Arc:
    """The arc through start, mid and end (mid may be any interior point; KiCad
    stores the angular midpoint). start == end gives a full circle (math-CCW)."""
    bx, by = sub(mid, start)
    cx, cy = sub(end, start)
    full = math.hypot(cx, cy) < EPS
    if full:                                   # mid is diametrically opposite
        c = ((start[0] + mid[0]) / 2.0, (start[1] + mid[1]) / 2.0)
    else:
        d = 2.0 * (bx * cy - by * cx)
        scale = math.hypot(bx, by) * math.hypot(cx, cy)
        if abs(d) <= 1e-12 * max(scale, 1e-300):
            raise ValueError(f"arc points {start!r}, {mid!r}, {end!r} are collinear")
        b2, c2 = bx * bx + by * by, cx * cx + cy * cy
        c = (start[0] + (b2 * cy - c2 * by) / d, start[1] + (c2 * bx - b2 * cx) / d)
    r = dist(start, c)
    a_s = angle_of(sub(start, c))
    if full:
        return Arc(c, r, a_s, TAU, p0=tuple(start), p1=tuple(start))
    tot = wrap_tau(angle_of(sub(end, c)) - a_s)
    m = wrap_tau(angle_of(sub(mid, c)) - a_s)
    sweep = tot if m < tot else -(TAU - tot)
    return Arc(c, r, a_s, sweep, p0=tuple(start), p1=tuple(end))


def reverse_prim(p: Prim) -> Prim:
    if isinstance(p, Line):
        return Line(p.b, p.a)
    return Arc(p.c, p.r, p.a0 + p.sweep, -p.sweep, p0=p.p1, p1=p.p0)


# --- measures ----------------------------------------------------------------------
def prim_bbox(p: Prim) -> tuple[float, float, float, float]:
    pts = [p.start, p.end]
    if isinstance(p, Arc):
        for k in range(4):
            ang = k * math.pi / 2
            if p.param(ang, 0.0) is not None:
                pts.append(p._at_angle(ang))
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def prim_signed_area(p: Prim) -> float:
    """This primitive's share of a closed ring's signed (shoelace) area: the
    chord term plus, for an arc, the signed cap between chord and arc."""
    a, b = p.start, p.end
    area = (a[0] * b[1] - b[0] * a[1]) / 2.0
    if isinstance(p, Arc):
        th = abs(p.sweep)
        area += math.copysign(p.r * p.r / 2.0 * (th - math.sin(th)), p.sweep)
    return area


def arc_segments(r: float, sweep: float, max_error: float) -> int:
    """Chords needed so no chord strays more than `max_error` from the arc
    (sagitta r(1 - cos(phi/2)) <= max_error), and at most 90 degrees each."""
    if not max_error > 0:
        raise ValueError(f"max chord error must be > 0 mm, got {max_error!r}")
    step = math.pi / 2
    if max_error < r:
        step = min(step, 2.0 * math.acos(1.0 - max_error / r))
    return max(1, math.ceil(abs(sweep) / step - 1e-9))


def flatten_prim(p: Prim, max_error: float) -> list[Point]:
    """Polyline points from the primitive's start (inclusive) to its end
    (exclusive): a closed ring is the concatenation over its primitives."""
    if isinstance(p, Line):
        return [p.a]
    n = arc_segments(p.r, p.sweep, max_error)
    return [p.point_at(i / n) for i in range(n)]


def closest_point(p: Prim, q: Point) -> tuple[Point, float, float]:
    """(nearest point on p, its distance from q, its parameter t in [0, 1])."""
    if isinstance(p, Line):
        d = sub(p.b, p.a)
        dd = dot(d, d)
        t = min(1.0, max(0.0, dot(sub(q, p.a), d) / dd)) if dd > 0 else 0.0
        foot = p.point_at(t)
        return foot, dist(q, foot), t
    v = sub(q, p.c)
    n = norm(v)
    if n > EPS:
        t = p.param(angle_of(v))
        if t is not None:
            return add(p.c, mul(v, p.r / n)), abs(n - p.r), t
    ds, de = dist(q, p.start), dist(q, p.end)
    return (p.start, ds, 0.0) if ds <= de else (p.end, de, 1.0)


# --- intersections -----------------------------------------------------------------
def _line_circle_params(a: Point, b: Point, c: Point, r: float) -> list[float]:
    """Parameters t (unbounded) where the infinite line a + t(b - a) meets the
    circle (c, r); a tangency within EPS counts once. Measured from the foot
    of the centre on the line (well conditioned even far from the segment)."""
    d = sub(b, a)
    length = norm(d)
    t0 = dot(sub(c, a), d) / (length * length)
    gap = abs(cross(d, sub(c, a))) / length          # centre to line
    if gap > r + EPS:
        return []
    if abs(gap - r) <= EPS:
        return [t0]
    half = math.sqrt((r - gap) * (r + gap)) / length
    return [t0 - half, t0 + half]


def circle_circle(c1: Point, r1: float, c2: Point, r2: float) -> list[Point]:
    """Intersection points of two full circles (empty when concentric). Circles
    within EPS of touching meet once, at the point on the line of centres
    (the square root below would turn float noise into a spurious pair)."""
    d = dist(c1, c2)
    if d < EPS or d > r1 + r2 + EPS or d < abs(r1 - r2) - EPS:
        return []
    u = mul(sub(c2, c1), 1.0 / d)
    if abs(d - (r1 + r2)) <= EPS:
        return [add(c1, mul(u, r1))]
    if abs(d - abs(r1 - r2)) <= EPS:
        return [add(c1, mul(u, r1 if r1 > r2 else -r1))]
    a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
    h = math.sqrt(max(0.0, r1 * r1 - a * a))
    base = add(c1, mul(u, a))
    if h <= EPS:
        return [base]
    off = mul(left(u), h)
    return [add(base, off), sub(base, off)]


def line_line_carrier(l1: Line, l2: Line) -> Point | None:
    """Intersection of the two infinite carrier lines (None when parallel)."""
    r, s = sub(l1.b, l1.a), sub(l2.b, l2.a)
    rxs = cross(r, s)
    if abs(rxs) <= _PARALLEL * norm(r) * norm(s):
        return None
    t = cross(sub(l2.a, l1.a), s) / rxs
    return l1.point_at(t)


def _on_line(l: Line, t: float) -> bool:
    slack = EPS / max(l.length, EPS)
    return -slack <= t <= 1.0 + slack


def _line_line(l1: Line, l2: Line) -> list[Point]:
    r, s = sub(l1.b, l1.a), sub(l2.b, l2.a)
    qp = sub(l2.a, l1.a)
    rxs = cross(r, s)
    if abs(rxs) <= _PARALLEL * norm(r) * norm(s):
        if abs(cross(qp, r)) > EPS * norm(r):
            return []                                  # parallel, apart
        rr = dot(r, r)                                 # collinear: the overlap
        t0 = dot(qp, r) / rr
        t1 = t0 + dot(s, r) / rr
        lo, hi = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
        if lo > hi + EPS / math.sqrt(rr):
            return []
        pts = [l1.point_at(lo), l1.point_at(hi)]
        return pts[:1] if dist(*pts) <= EPS else pts
    t, u = cross(qp, s) / rxs, cross(qp, r) / rxs
    if _on_line(l1, t) and _on_line(l2, u):
        return [l1.point_at(min(1.0, max(0.0, t)))]
    return []


def _line_arc(l: Line, arc: Arc) -> list[Point]:
    out = []
    for t in _line_circle_params(l.a, l.b, arc.c, arc.r):
        if _on_line(l, t):
            q = l.point_at(min(1.0, max(0.0, t)))
            if arc.param(angle_of(sub(q, arc.c))) is not None:
                out.append(q)
    return out


def _arc_arc(p: Arc, q: Arc) -> list[Point]:
    if dist(p.c, q.c) < EPS and abs(p.r - q.r) < EPS:          # same circle
        pts = [e for e in (q.start, q.end) if p.param(angle_of(sub(e, p.c))) is not None]
        pts += [e for e in (p.start, p.end) if q.param(angle_of(sub(e, q.c))) is not None]
        uniq: list[Point] = []
        for e in pts:
            if all(dist(e, u) > EPS for u in uniq):
                uniq.append(e)
        return uniq
    return [x for x in circle_circle(p.c, p.r, q.c, q.r)
            if p.param(angle_of(sub(x, p.c))) is not None
            and q.param(angle_of(sub(x, q.c))) is not None]


def intersections(p: Prim, q: Prim) -> list[Point]:
    """Points where two primitives meet (end points included, overlaps give
    the overlap's end points)."""
    if isinstance(p, Line):
        return _line_line(p, q) if isinstance(q, Line) else _line_arc(p, q)
    return _line_arc(q, p) if isinstance(q, Line) else _arc_arc(p, q)


def carrier_intersections(p: Prim, q: Prim) -> list[Point]:
    """Intersections of the infinite carriers (line / full circle) of p and q."""
    if isinstance(p, Line) and isinstance(q, Line):
        x = line_line_carrier(p, q)
        return [] if x is None else [x]
    if isinstance(p, Arc) and isinstance(q, Arc):
        return circle_circle(p.c, p.r, q.c, q.r)
    ln, arc = (p, q) if isinstance(p, Line) else (q, p)
    return [ln.point_at(t) for t in _line_circle_params(ln.a, ln.b, arc.c, arc.r)]


# --- distances and rays ------------------------------------------------------------
def _line_arc_gap(ln: Line, arc: Arc) -> float:
    """Interior critical distance between a segment and an arc (inf if none)."""
    d = sub(ln.b, ln.a)
    t = dot(sub(arc.c, ln.a), d) / dot(d, d)
    if not 0.0 < t < 1.0:
        return math.inf
    v = sub(ln.point_at(t), arc.c)
    n = norm(v)
    if n > arc.r and arc.param(angle_of(v)) is not None:
        return n - arc.r
    return math.inf


def _arc_arc_gap(p: Arc, q: Arc) -> float:
    """Interior critical distance between two arcs: on the line of centres."""
    if dist(p.c, q.c) < EPS:
        overlap = (q.param(p.a0) is not None or p.param(q.a0) is not None)
        return abs(p.r - q.r) if overlap else math.inf
    u = unit(sub(q.c, p.c))
    best = math.inf
    for s1 in (1.0, -1.0):
        if p.param(angle_of(mul(u, s1))) is None:
            continue
        x1 = add(p.c, mul(u, s1 * p.r))
        for s2 in (1.0, -1.0):
            if q.param(angle_of(mul(u, s2))) is not None:
                best = min(best, dist(x1, add(q.c, mul(u, s2 * q.r))))
    return best


def prim_distance(p: Prim, q: Prim) -> float:
    """Exact minimum distance between two primitives (0 when they meet)."""
    if intersections(p, q):
        return 0.0
    best = min(closest_point(q, p.start)[1], closest_point(q, p.end)[1],
               closest_point(p, q.start)[1], closest_point(p, q.end)[1])
    if isinstance(p, Line) and isinstance(q, Arc):
        best = min(best, _line_arc_gap(p, q))
    elif isinstance(p, Arc) and isinstance(q, Line):
        best = min(best, _line_arc_gap(q, p))
    elif isinstance(p, Arc) and isinstance(q, Arc):
        best = min(best, _arc_arc_gap(p, q))
    return best


def ray_hits(p: Prim, origin: Point, direction: Point) -> list[float]:
    """Distances t >= 0 at which the ray origin + t * direction (unit) meets p."""
    if isinstance(p, Line):
        s = sub(p.b, p.a)
        den = cross(direction, s)
        if abs(den) <= _PARALLEL * norm(s):
            return []
        ao = sub(p.a, origin)
        t, u = cross(ao, s) / den, cross(ao, direction) / den
        return [t] if t >= 0.0 and _on_line(p, u) else []
    f = sub(origin, p.c)
    bb, cc = dot(f, direction), dot(f, f) - p.r * p.r
    disc = bb * bb - cc
    if disc < 0:
        return []
    sq = math.sqrt(disc)
    out = []
    for t in sorted({-bb - sq, -bb + sq}):
        if t >= 0.0:
            hit = add(origin, mul(direction, t))
            if p.param(angle_of(sub(hit, p.c))) is not None:
                out.append(t)
    return out


# --- even-odd ray crossings -----------------------------------------------------
# Containment counts the crossings of the ray from q towards +X with the
# boundary itself (never with an arc's chord, which is not boundary: a chord
# crossing and a cap test decided by two separately rounded values disagree
# for a probe within rounding of the chord line, and flip the parity of a
# point far from any edge). Every primitive is split into y-monotone pieces
# and each piece is counted by the half-open rule: it crosses when exactly one
# of its end points lies strictly above q (y > q.y). Neighbouring pieces share
# their end points bit for bit, so each crossing of a closed ring is counted
# exactly once, and an error in a crossing's x only matters for a probe that
# close to the boundary.
def _arc_monotone_pieces(arc: Arc) -> list[tuple[Point, Point, bool]]:
    """(start, end, right_half) of the arc's y-monotone pieces in order: the
    arc split where it passes the top (cx, cy + r) or bottom (cx, cy - r) of
    its circle; right_half when the piece lies on the x >= cx side."""
    cx, cy = arc.c
    s = 1.0 if arc.sweep > 0 else -1.0
    total = abs(arc.sweep)
    breaks = sorted((u, pt) for u, pt in
                    ((wrap_tau(s * (math.pi / 2 - arc.a0)), (cx, cy + arc.r)),
                     (wrap_tau(s * (3 * math.pi / 2 - arc.a0)), (cx, cy - arc.r)))
                    if 0.0 < u < total)
    marks = [(0.0, arc.p0)] + breaks + [(total, arc.p1)]
    return [(pa, pb, math.cos(arc.a0 + s * (ua + ub) / 2.0) > 0.0)
            for (ua, pa), (ub, pb) in zip(marks, marks[1:])]


def crossings_right(p: Prim, q: Point) -> int:
    """How many times p crosses the ray from q towards +X, by the half-open
    rule above. The sum over a closed ring is odd exactly when q is inside."""
    qx, qy = q
    if isinstance(p, Line):
        a, b = p.a, p.b
        if (a[1] > qy) == (b[1] > qy):
            return 0
        return 1 if qx < a[0] + (qy - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) else 0
    cx, cy = p.c
    r = p.r
    if qx >= cx + r:                               # no point of the circle is right of q
        return 0
    if max(p.p0[1], p.p1[1], cy + r) <= qy or min(p.p0[1], p.p1[1], cy - r) > qy:
        return 0                                   # every piece end on one side of q
    dy = abs(qy - cy)
    half = math.sqrt(max(0.0, (r - dy) * (r + dy)))
    n = 0
    for a, b, right in _arc_monotone_pieces(p):
        if (a[1] > qy) != (b[1] > qy) and qx < (cx + half if right else cx - half):
            n += 1
    return n

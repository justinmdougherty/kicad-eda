#!/usr/bin/env python3
"""Exact inset (erosion) of a board outline: the board shrunk by d mm.

Stdlib only; ``outline.py`` re-exports ``inset``, import that. Conventions as
in outline_geom.py / outline_model.py: mm, plain math angles, the board frame
is Y down, and every ring has the board material on its LEFT.

Method (raw offset + slicing; exact arcs, nothing is flattened)
---------------------------------------------------------------
1. Raw pieces. Every primitive moved d to its left (into the material): a
   line to the parallel line, an arc to the concentric arc of radius r - d
   (convex, sweep > 0) or r + d (concave). A convex arc with r <= d gets no
   piece: no board point has its nearest edge point there. Every reflex
   vertex (the ring turns right) adds the arc of radius d about the vertex
   from one offset end to the next. A convex vertex adds nothing: no board
   point has a convex corner as its nearest edge point.
2. Every point of the true inset's boundary lies on a raw piece, exactly d
   from the outline, and a raw-piece point belongs to that boundary iff no
   edge is closer than d. Along a piece that can only change where it meets
   another piece, so all pieces of all rings are split at their mutual
   intersections and each slice is kept or dropped by its midpoint distance.
3. The kept slices chain end to start into closed rings with the material
   still on their left: one math-CCW ring is the board, math-CW rings are
   holes. Consecutive slices of one piece are merged back into one primitive.

Short edges, protrusions thinner than 2d, cutouts merging with each other (one
hole) or with the edge (a bay of the outer ring) therefore come out exact. A
strip exactly 2d wide shrinks to zero width: its two coincident boundary
slices are dropped, which leaves a cusp or splits the board. Where two
boundaries touch in a single point (a pinch), chaining takes the sharpest left
turn, so the pinched parts come out as separate rings. A contact that still
leaves no simple outline (a grown hole tangent to the shrunk edge, two grown
holes touching) is resolved by computing the inset for d + NUDGE_MM (1 nm,
KiCad's resolution), which merges the touching parts; the result is still
verified against d.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline_geom import (TAU, Arc, Line, Point, Prim, add, angle_of,  # noqa: E402
                          closest_point, cross, dist, dot, intersections, left, mul,
                          prim_bbox, prim_signed_area)
from outline_model import (Circle, Loop, Outline, OutlineError, PrimIndex, _fmt,  # noqa: E402
                           _inside_material, _ring_contains, check, geometry_problems,
                           ring_name)

# Tolerances, strictly ordered SNAP < STITCH < NUDGE < VERIFY so that the
# nudge separates what an exact 2d contact made coincide by more than the
# snap and stitch tolerances merge again.
SNAP_MM = 1e-8             # split points this close to a piece end (or each other) coincide
STITCH_TOL_MM = 1e-7       # a kept slice's end and the next slice's start coincide within this
NUDGE_MM = 1e-6            # an exact 2d contact is resolved as if d were this much larger
VERIFY_TOL_MM = 1e-5       # self-check: every boundary sample is d +- this from the outline
VALID_TOL_MM = 1e-9        # a slice is kept when its midpoint is >= d - this from every edge
_VERIFY_SPACING_MM = 0.25  # self-check sample spacing along every result edge
_CUSP_RAD = 1e-9           # |turn| within this of pi: the ring doubles back on itself


class _Degenerate(OutlineError):
    """The inset touches itself (a contact exactly 2d wide): no simple outline."""


# =============================================================================
# raw pieces
# =============================================================================
def _turn(p: Prim, q: Prim) -> float:
    """Signed turn (rad, + = left) from p's end tangent to q's start tangent."""
    t1, t2 = p.tangent(1.0), q.tangent(0.0)
    return math.atan2(cross(t1, t2), dot(t1, t2))


def _moved(p: Prim, t: float, d: float) -> Point:
    """The point of p at its start (t=0) or end (t=1) moved d along its left
    normal. Offsets and joins both use this, so they share end points exactly."""
    return add(p.end if t == 1.0 else p.start, mul(left(p.tangent(t)), d))


def _offset(p: Prim, d: float) -> Prim | None:
    """p moved d to its left; None for a convex arc with r <= d."""
    if isinstance(p, Line):
        return Line(_moved(p, 0.0, d), _moved(p, 1.0, d))
    r = p.r - d if p.sweep > 0 else p.r + d
    if r <= SNAP_MM:
        return None
    s = _moved(p, 0.0, d)
    return Arc(p.c, r, p.a0, p.sweep, p0=s, p1=s if p.is_circle else _moved(p, 1.0, d))


def _join(p: Prim, q: Prim, d: float) -> Arc | None:
    """The arc of radius d about the vertex p.end = q.start when the ring turns
    right there (a reflex vertex), else None."""
    turn = _turn(p, q)
    if abs(turn) > math.pi - _CUSP_RAD:
        raise OutlineError(f"the outline doubles back on itself at {_fmt(p.end)} (a zero-angle "
                           "spike or slit); it cannot be inset. Give the tip a width or a radius")
    if turn >= 0.0 or d * -turn <= SNAP_MM:
        return None
    return Arc(p.end, d, angle_of(left(p.tangent(1.0))), turn,
               p0=_moved(p, 1.0, d), p1=_moved(q, 0.0, d))


def _cut(p: Prim, x: Point, keep_start: bool) -> Prim:
    """p up to (keep_start) or from the point x on it."""
    if isinstance(p, Line):
        return Line(p.a, x) if keep_start else Line(x, p.b)
    t = closest_point(p, x)[2]
    if keep_start:
        return Arc(p.c, p.r, p.a0, t * p.sweep, p0=p.p0, p1=x)
    return Arc(p.c, p.r, p.a0 + t * p.sweep, (1.0 - t) * p.sweep, p0=x, p1=p.p1)


def _corner(a: Prim, b: Prim) -> Point | None:
    """Where the offsets a (ending) and b (starting) of a convex vertex cross
    each other, nearest that vertex, when both keep more than SNAP_MM."""
    xs = [x for x in intersections(a, b)
          if closest_point(a, x)[2] * a.length > SNAP_MM
          and (1.0 - closest_point(b, x)[2]) * b.length > SNAP_MM]
    return min(xs, key=lambda x: dist(x, a.end) + dist(x, b.start), default=None)


def _raw_pieces(prims: Sequence[Prim], d: float) -> list[Prim]:
    """The ring's offsets and reflex joins in ring order. Where the offsets of
    a convex vertex cross, both are cut there: the parts beyond the crossing
    lie closer than d to the other edge, by only d * turn^2 / 4 at their
    middle for a nearly straight polyline vertex (below any float tolerance),
    so they are cut here rather than judged by distance."""
    n = len(prims)
    offs = [_offset(p, d) for p in prims]
    offs = [o if o is not None and o.length > SNAP_MM else None for o in offs]
    joins = [_join(p, prims[(i + 1) % n], d) for i, p in enumerate(prims)]
    for i in range(n if n > 1 else 0):
        j = (i + 1) % n
        a, b = offs[i], offs[j]
        if a is None or b is None or joins[i] is not None or _turn(prims[i], prims[j]) <= 0.0:
            continue
        x = _corner(a, b)
        if x is not None:
            offs[i], offs[j] = _cut(a, x, keep_start=True), _cut(b, x, keep_start=False)
    out: list[Prim] = []
    for off, join in zip(offs, joins):
        out += [q for q in (off, join) if q is not None]
    return out


# =============================================================================
# slicing
# =============================================================================
@dataclass(frozen=True)
class _Slice:
    """Part of raw piece `piece` between two split points; `seq` is its place
    along the piece and `nxt` the seq of the slice that continues it (-1: none)."""
    prim: Prim
    piece: int
    seq: int
    nxt: int


def _closed(p: Prim) -> bool:
    return isinstance(p, Arc) and p.is_circle


def _snap(p: Prim, x: Point) -> Point | None:
    for e in (p.start, p.end):
        if dist(e, x) <= SNAP_MM:
            return e
    return None


def _split_points(pieces: Sequence[Prim]) -> list[list[Point]]:
    """Per piece, the points where other pieces meet it. A meeting point near
    either piece's end is moved onto that end, so both pieces split at the
    very same point."""
    index = PrimIndex([(0, j, p) for j, p in enumerate(pieces)])
    cuts: list[list[Point]] = [[] for _ in pieces]
    for i, p in enumerate(pieces):
        x0, y0, x1, y1 = prim_bbox(p)
        box = (x0 - SNAP_MM, y0 - SNAP_MM, x1 + SNAP_MM, y1 + SNAP_MM)
        for _k, j, q in index.near(box):
            if j <= i:
                continue
            for x in intersections(p, q):
                pt = _snap(p, x) or _snap(q, x) or x
                cuts[i].append(pt)
                cuts[j].append(pt)
    return cuts


def _sub(p: Prim, ta: float, xa: Point, tb: float, xb: Point) -> Prim:
    if isinstance(p, Line):
        return Line(xa, xb)
    return Arc(p.c, p.r, p.a0 + ta * p.sweep, (tb - ta) * p.sweep, p0=xa, p1=xb)


def _slices(n: int, p: Prim, cuts: Sequence[Point]) -> list[_Slice]:
    """Piece n cut at its split points (a closed circle cut cyclically)."""
    closed, length = _closed(p), p.length
    marks: list[tuple[float, Point]] = []
    for t, x in sorted(((closest_point(p, x)[2], x) for x in cuts), key=lambda tx: tx[0]):
        if not closed and (t * length <= SNAP_MM or (1.0 - t) * length <= SNAP_MM):
            continue                                   # at an end: nothing to split
        if marks and (t - marks[-1][0]) * length <= SNAP_MM:
            continue
        marks.append((t, x))
    if closed:
        if len(marks) > 1 and (marks[0][0] + 1.0 - marks[-1][0]) * length <= SNAP_MM:
            marks.pop()
        if not marks:
            return [_Slice(p, n, 0, 0)]
        bounds = marks + [(marks[0][0] + 1.0, marks[0][1])]
    else:
        bounds = [(0.0, p.start)] + marks + [(1.0, p.end)]
    m = len(bounds) - 1
    return [_Slice(_sub(p, ta, xa, tb, xb), n, k, (k + 1) % m if closed else
                   (k + 1 if k + 1 < m else -1))
            for k, ((ta, xa), (tb, xb)) in enumerate(zip(bounds, bounds[1:]))]


def _valid(s: _Slice, index: PrimIndex, d: float) -> bool:
    """No edge of the original outline is closer than d to the slice's midpoint."""
    m = s.prim.point_at(0.5)
    reach = d + 1e-3
    near = index.near((m[0] - reach, m[1] - reach, m[0] + reach, m[1] + reach))
    return all(closest_point(p, m)[1] >= d - VALID_TOL_MM for _k, _i, p in near)


# =============================================================================
# chaining kept slices into rings
# =============================================================================
class _PointGrid:
    """Slice indices bucketed by a point (start or end) on a STITCH_TOL grid."""

    def __init__(self, points: Sequence[Point]) -> None:
        self.cell = 4.0 * STITCH_TOL_MM
        self.points = points
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for n, p in enumerate(points):
            self.buckets.setdefault(self._key(p), []).append(n)

    def _key(self, p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / self.cell), math.floor(p[1] / self.cell))

    def near(self, p: Point) -> list[int]:
        kx, ky = self._key(p)
        return [n for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for n in self.buckets.get((kx + dx, ky + dy), ())
                if dist(self.points[n], p) <= STITCH_TOL_MM]


def _same_curve_reversed(a: Prim, b: Prim) -> bool:
    return (type(a) is type(b) and dist(a.start, b.end) <= STITCH_TOL_MM
            and dist(a.end, b.start) <= STITCH_TOL_MM
            and dist(a.point_at(0.5), b.point_at(0.5)) <= STITCH_TOL_MM)


def _drop_zero_width(kept: list[_Slice]) -> list[_Slice]:
    """Two kept slices that are one curve run both ways bound a zero-width
    strip (a feature exactly 2d wide): neither is a boundary of the inset."""
    starts = _PointGrid([s.prim.start for s in kept])
    dead: set[int] = set()
    for n, s in enumerate(kept):
        if n in dead:
            continue
        for m in starts.near(s.prim.end):
            if m != n and m not in dead and _same_curve_reversed(kept[m].prim, s.prim):
                dead |= {n, m}
                break
    return [s for n, s in enumerate(kept) if n not in dead]


def _left_turn(a: Prim, b: Prim) -> float:
    """Turn from a's end tangent to b's start tangent; a reversal counts as +pi."""
    t1, t2 = a.tangent(1.0), b.tangent(0.0)
    turn = math.atan2(cross(t1, t2), dot(t1, t2))
    return math.pi if turn <= -math.pi + _CUSP_RAD else turn


def _next(cur: Prim, cands: list[int], kept: list[_Slice], d: float) -> int:
    """Where two boundaries touch at one point (a pinch exactly 2d wide) two
    slices leave it: the sharpest left turn keeps the material on the left in
    one piece, so pinched regions come out as separate rings."""
    if len(cands) == 1:
        return cands[0]
    turns = sorted(((_left_turn(cur, kept[m].prim), m) for m in cands), reverse=True)
    if not cands or turns[0][0] - turns[1][0] <= 1e-9:
        how = "stops" if not cands else "branches"
        raise _Degenerate(
            f"the inset by {d:g} mm is degenerate near {_fmt(cur.end)}: its boundary {how} "
            f"there, which happens where a feature is (almost) exactly {2 * d:g} mm wide. "
            "Use a slightly different distance")
    return turns[0][1]


def _chain(kept: list[_Slice], d: float) -> list[list[_Slice]]:
    """Closed chains of kept slices, each starting at its lowest (piece, seq).
    A chain closes when its own first slice is the continuation it picks."""
    starts = _PointGrid([s.prim.start for s in kept])
    used = [False] * len(kept)
    chains = []
    for first in sorted(range(len(kept)), key=lambda n: (kept[n].piece, kept[n].seq)):
        if used[first]:
            continue
        used[first] = True
        chain = [kept[first]]
        while True:
            cands = [m for m in starts.near(chain[-1].prim.end) if not used[m] or m == first]
            nxt = _next(chain[-1].prim, cands, kept, d)
            if nxt == first:
                break
            used[nxt] = True
            chain.append(kept[nxt])
        chains.append(chain)
    return chains


def _glue(a: Prim, b: Prim) -> Prim:
    if isinstance(a, Line):
        return Line(a.a, b.b)
    return Arc(a.c, a.r, a.a0, a.sweep + b.sweep, p0=a.p0, p1=b.p1)


def _merged(chain: list[_Slice]) -> list[Prim]:
    """The chain with consecutive slices of one piece merged back together."""
    prims: list[Prim] = []
    for k, s in enumerate(chain):
        prev = chain[k - 1] if k else None
        if prev is not None and prev.piece == s.piece and prev.nxt == s.seq:
            prims[-1] = _glue(prims[-1], s.prim)
        else:
            prims.append(s.prim)
    if len(prims) > 1 and chain[-1].piece == chain[0].piece and chain[-1].nxt == chain[0].seq:
        prims[0] = _glue(prims.pop(), prims[0])         # a closed piece split at its start
    return prims


def _ring(prims: list[Prim], d: float) -> Loop | Circle:
    p = prims[0]
    if len(prims) == 1 and isinstance(p, Arc) and abs(abs(p.sweep) - TAU) < 1e-9:
        return Circle(p.c[0], p.c[1], p.r)
    verts = tuple((q.start[0], q.start[1], 0.0 if isinstance(q, Line) else math.tan(q.sweep / 4.0))
                  for q in prims)
    try:
        return Loop(verts)
    except OutlineError as e:
        raise _Degenerate(f"the inset by {d:g} mm is degenerate near {_fmt(p.start)} ({e}); "
                           "use a slightly different distance") from e


def _box(prims: Sequence[Prim]) -> str:
    boxes = [prim_bbox(p) for p in prims]
    lo = (min(b[0] for b in boxes), min(b[1] for b in boxes))
    hi = (max(b[2] for b in boxes), max(b[3] for b in boxes))
    return f"{_fmt(lo)}-{_fmt(hi)}"


def _outline(chains: list[list[Prim]], o: Outline, d: float) -> Outline:
    """One CCW ring (the board) plus CW rings (holes), in input order."""
    areas = [sum(prim_signed_area(p) for p in prims) for prims in chains]
    outers = [k for k, a in enumerate(areas) if a > 0]
    if not outers:
        raise OutlineError(f"the inset by {d:g} mm consumes the whole board: no point is "
                           f"{d:g} mm from every edge")
    if len(outers) > 1:
        where = "; ".join(_box(chains[k]) for k in outers)
        raise OutlineError(
            f"the inset by {d:g} mm splits the board into {len(outers)} separate regions "
            f"({where}): a neck is {2 * d:g} mm wide or narrower. An Outline holds one outer "
            "ring: inset the parts separately, or use KiCad's SHAPE_POLY_SET.Deflate on the "
            "SWIG side")
    holes = tuple(_ring(chains[k], d) for k, a in enumerate(areas) if a < 0)
    return Outline(_ring(chains[outers[0]], d), holes, o.datum)


# =============================================================================
# self-check and entry point
# =============================================================================
def _verify(orig: Outline, res: Outline, d: float) -> None:
    """Raise unless `res` is a valid outline whose every boundary sample lies
    d (+- VERIFY_TOL_MM) from the original edges, with every original cutout
    off its material and every hole around an original cutout."""
    problems = geometry_problems(res)
    if problems:
        raise _Degenerate(f"the inset by {d:g} mm is not a simple outline ({problems[0]}): a "
                           f"feature there is (almost) exactly {2 * d:g} mm wide. Use a "
                           "slightly different distance")
    index = PrimIndex(orig)
    reach = d + 1e-3
    for k, prims in enumerate(res.rings):
        for p in prims:
            n = max(2, min(400, math.ceil(p.length / _VERIFY_SPACING_MM)))
            for j in range(n + 1):
                q = p.point_at(j / n)
                near = index.near((q[0] - reach, q[1] - reach, q[0] + reach, q[1] + reach))
                gap = min((closest_point(pr, q)[1] for _k, _i, pr in near), default=math.inf)
                if abs(gap - d) > VERIFY_TOL_MM:
                    raise OutlineError(f"internal error: the inset by {d:g} mm put "
                                       f"{ring_name(k)} {gap:.6f} mm from the outline at "
                                       f"{_fmt(q)}")
    for k, prims in enumerate(orig.rings[1:], 1):
        if _inside_material(res, prims[0].start):
            raise OutlineError(f"internal error: the inset by {d:g} mm covers {ring_name(k)} "
                               f"at {_fmt(prims[0].start)}")
    for k, prims in enumerate(res.rings[1:], 1):
        if not any(_ring_contains(prims, c[0].start) for c in orig.rings[1:]):
            raise OutlineError(f"internal error: the inset by {d:g} mm has a hole "
                               f"({ring_name(k)}) with no cutout in it")


def _inset_at(o: Outline, d: float, asked: float) -> Outline:
    """The inset by `d`, verified against the `asked` distance (messages too)."""
    pieces = [p for prims in o.rings for p in _raw_pieces(prims, d)]
    index = PrimIndex(o)
    kept = [s for n, (p, cuts) in enumerate(zip(pieces, _split_points(pieces)))
            for s in _slices(n, p, cuts) if _valid(s, index, d)]
    chains = [_merged(chain) for chain in _chain(_drop_zero_width(kept), asked)]
    res = _outline(chains, o, asked)
    _verify(o, res, asked)
    return res


def inset(o: Outline, d: float) -> Outline:
    """The board shrunk by `d` mm (cutouts grow by `d`): the outline for a
    plane/zone with `d` of edge clearance. Exact: lines offset to lines, arcs
    to concentric arcs, a reflex corner gets an arc of radius `d`, a convex
    fillet smaller than `d` becomes a sharp corner, short edges and
    protrusions narrower than 2d disappear, cutouts that meet merge into one
    hole and a cutout that meets the edge becomes a bay of the outer ring.
    A grown hole exactly tangent to the shrunk edge or to another grown hole
    has no simple outline; it is resolved as if d were NUDGE_MM (1 nm)
    larger, which merges the two.

    Raises OutlineError, never returns a wrong shape. These depend on the
    input and d, and the message says which:
      * "consumes the whole board": nothing is left;
      * "splits the board into N separate regions": a neck 2d wide or
        narrower (down to a single touching point) cuts the board apart, and
        an Outline holds one outer ring;
      * "doubles back on itself": a zero-angle spike or slit in the input;
      * "is degenerate" / "is not a simple outline": an exact 2d contact the
        1 nm nudge did not resolve (none is known; use another distance).
    The result is self-checked before it is returned: a valid outline whose
    boundary samples (every 0.25 mm) lie d +- 1e-5 mm from the original
    edges, every original cutout off its material and every hole around an
    original cutout. A failed self-check raises an OutlineError whose message
    starts "internal error": a defect in this module, not in the input. It
    is not expected (fuzzing found none after containment became exact ray
    crossing counting; see outline_geom.crossings_right) and is raised
    rather than returning a shape that was not verified."""
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d < 0:
        raise OutlineError(f"inset distance must be a finite number >= 0 mm, got {d!r}")
    if d == 0:
        return o
    check(o)
    d = float(d)
    try:
        return _inset_at(o, d, d)
    except _Degenerate as exact:
        try:
            return _inset_at(o, d + NUDGE_MM, d)
        except _Degenerate:
            raise exact from None

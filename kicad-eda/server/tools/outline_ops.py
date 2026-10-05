#!/usr/bin/env python3
"""Outline operations: largest inscribed rectangle and fab-rule validation.

Stdlib only; ``outline.py`` re-exports these, import that. Conventions as in
outline_geom.py / outline_model.py (mm, math angles, material on the left of
every ring). ``inset`` lives in outline_inset.py.
"""
from __future__ import annotations

import bisect
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline_geom import (EPS, Arc, Line, Point, Prim, add, angle_of,  # noqa: E402
                          closest_point, cross, dist, dot, mul, norm, ray_hits, sub)
from outline_model import (CHORD_ERROR_MM, Circle, Edge, Outline,  # noqa: E402
                           OutlineError, PrimIndex, _fmt, bbox, check, contains,
                           contains_rect, cross_turn, distance_to_edge, flatten, ring_name)


# =============================================================================
# largest inscribed axis-aligned rectangle
# =============================================================================
def _coords(lo: float, hi: float, extra: list[float], grid: int) -> list[float]:
    vals = [lo + (hi - lo) * i / grid for i in range(grid + 1)]
    vals += [v for v in extra if lo < v < hi]
    vals.sort()
    out = [vals[0]]
    for v in vals[1:]:
        if v - out[-1] > 1e-9:
            out.append(v)
    return out


def _band_free(rings: Sequence[Sequence[Point]], ya: float, yb: float, pad: float,
               xs: Sequence[float]) -> list[bool]:
    """Which columns of the band [ya, yb] lie on the board at least `pad` (in
    the max-norm) from every flattened edge."""
    lo, hi = ya - pad, yb + pad
    blocked: list[tuple[float, float]] = []
    ym = (ya + yb) / 2.0
    crossings: list[float] = []
    for ring in rings:
        m = len(ring)
        for i in range(m):
            p, q = ring[i], ring[(i + 1) % m]
            if (p[1] > ym) != (q[1] > ym):
                crossings.append(p[0] + (ym - p[1]) * (q[0] - p[0]) / (q[1] - p[1]))
            if max(p[1], q[1]) < lo or min(p[1], q[1]) > hi:
                continue
            if abs(q[1] - p[1]) < 1e-15:
                xa, xb = p[0], q[0]
            else:
                t0, t1 = sorted(((lo - p[1]) / (q[1] - p[1]), (hi - p[1]) / (q[1] - p[1])))
                t0, t1 = max(0.0, t0), min(1.0, t1)
                xa, xb = p[0] + t0 * (q[0] - p[0]), p[0] + t1 * (q[0] - p[0])
            blocked.append((min(xa, xb) - pad, max(xa, xb) + pad))
    crossings.sort()
    merged: list[list[float]] = []
    for b0, b1 in sorted(blocked):
        if merged and b0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b1)
        else:
            merged.append([b0, b1])
    starts = [m[0] for m in merged]
    free = []
    for i in range(len(xs) - 1):
        xa, xb = xs[i], xs[i + 1]
        if bisect.bisect_left(crossings, (xa + xb) / 2.0) % 2 == 0:
            free.append(False)                     # the cell is off the board
            continue
        j = bisect.bisect_left(starts, xb) - 1     # last interval starting before xb
        free.append(j < 0 or merged[j][1] <= xa)
    return free


def _best_histogram_rect(free_rows: list[list[bool]], xs: Sequence[float],
                         ys: Sequence[float]) -> tuple[float, tuple | None]:
    ncol = len(xs) - 1
    heights = [0.0] * ncol
    best: tuple[float, tuple | None] = (0.0, None)
    for j, free in enumerate(free_rows):
        dy = ys[j + 1] - ys[j]
        heights = [h + dy if f else 0.0 for h, f in zip(heights, free)]
        stack: list[tuple[int, float]] = []
        for i in range(ncol + 1):
            h = heights[i] if i < ncol else 0.0
            start = i
            while stack and stack[-1][1] >= h:
                s, hh = stack.pop()
                a = hh * (xs[i] - xs[s])
                if a > best[0]:
                    best = (a, (xs[s], ys[j + 1] - hh, xs[i], ys[j + 1]))
                start = s
            stack.append((start, h))
    return best


def _grow(o: Outline, rect: list[float], clearance: float, box: Sequence[float]) -> list[float]:
    """Push each side outward as far as the exact containment test allows."""
    for _ in range(2):
        for side, sign in ((0, -1), (1, -1), (2, 1), (3, 1)):
            limit = abs(box[side] - rect[side])
            if limit <= 1e-9:
                continue
            trial = list(rect)
            trial[side] = rect[side] + sign * limit
            if contains_rect(o, trial, clearance):
                rect = trial
                continue
            lo, hi = 0.0, limit
            while hi - lo > 1e-6:
                mid = (lo + hi) / 2.0
                trial[side] = rect[side] + sign * mid
                if contains_rect(o, trial, clearance):
                    lo = mid
                else:
                    hi = mid
            rect = list(rect)
            rect[side] += sign * lo
    return rect


def largest_inscribed_rect(o: Outline, clearance: float = 0.0,
                           grid: int = 160) -> tuple[float, float, float, float] | None:
    """Largest-area axis-aligned rectangle (x0, y0, x1, y1) on the board at
    least `clearance` mm from every edge and cutout, or None when none fits.

    A grid search over the flattened outline (cells kept clear of every edge
    by clearance + the chord error, so the grid answer is always valid) picks
    the region, then each side is pushed out with the exact containment test.
    The result is exact for rectilinear outlines and within a grid cell of the
    optimum otherwise, which is the accuracy an auto-placement field needs."""
    if isinstance(clearance, bool) or not math.isfinite(clearance) or clearance < 0:
        raise OutlineError(f"clearance must be a finite number >= 0 mm, got {clearance!r}")
    if grid < 8:
        raise ValueError(f"grid must be >= 8, got {grid!r}")
    rings = flatten(o, CHORD_ERROR_MM)
    pad = clearance + CHORD_ERROR_MM
    box = bbox(o)
    # grid lines through every vertex (+- pad) make rectilinear outlines exact;
    # a densely tessellated outline gets the uniform grid only (cost ~ grid^2)
    verts = [p for r in rings for p in r]
    if len(verts) > grid:
        verts = []
    vx = [v for p in verts for v in (p[0] - pad, p[0] + pad)]
    vy = [v for p in verts for v in (p[1] - pad, p[1] + pad)]
    xs = _coords(box[0], box[2], vx, grid)
    ys = _coords(box[1], box[3], vy, grid)
    rows = [_band_free(rings, ys[j], ys[j + 1], pad, xs) for j in range(len(ys) - 1)]
    best_area, rect = _best_histogram_rect(rows, xs, ys)
    if rect is None or best_area <= 0:
        return None
    if not contains_rect(o, rect, clearance):
        raise OutlineError(f"internal: the grid rectangle {rect} failed the exact "
                           "containment test")
    return tuple(_grow(o, list(rect), clearance, box))


# =============================================================================
# fab-rule validation
# =============================================================================
_CAPS = "https://jlcpcb.com/capabilities/pcb-capabilities (retrieved 2026-10-04)"


@dataclass(frozen=True)
class FabLimits:
    """Outline-related manufacturing limits of one fab (mm). `sources` cites
    each rule; `assumptions` lists rules whose value is not a published fab
    figure (their problems are flagged assumption=True)."""
    name: str
    min_board_dim: float
    min_slot_width: float
    min_internal_radius: float
    min_feature_width: float
    min_npth_diameter: float
    min_hole_to_edge: float
    sources: Mapping[str, str] = field(default_factory=dict)
    assumptions: frozenset[str] = frozenset()


FAB_LIMITS: dict[str, FabLimits] = {
    "JLCPCB": FabLimits(
        name="JLCPCB",
        min_board_dim=3.0,
        min_slot_width=1.0,
        min_internal_radius=0.5,
        min_feature_width=0.8,
        min_npth_diameter=0.5,
        min_hole_to_edge=0.25,
        sources={
            "board_size": f"{_CAPS}: 'Minimum Dimensions: 3 x 3 mm' (FR4, >= 0.6 mm thick)",
            "slot_width": f"{_CAPS}: 'Non-Plated Slots: 1.0mm' (min milled slot width)",
            "hole_size": f"{_CAPS}: 'Min. Non-plated holes: 0.50mm'",
            "internal_corner_radius": (
                "JLCPCB support via https://techoverflow.net/2023/04/12/jlcpcb-which-"
                "milling-tool-radius-is-used-for-internal-cutouts ('normally they use 0.5mm "
                "milling tool radius'), consistent with https://jlcpcb.com/blog/pcb-board-"
                "outline ('radii >=0.5-1.0 mm'); NOT in the capabilities table"),
            "hole_to_edge": (
                "JLCPCB support on https://easyeda.com/forum/topic/Minimum-distance-between-"
                "non-plated-through-hole-NPTH-and-PCB-edge-cafa31f426214a2b92b96ac927424499 "
                "('keep at least a distance of 0.25mm'); NOT in the capabilities table"),
            "feature_width": (
                "ASSUMPTION: JLCPCB publishes no minimum board web/neck width. 0.8 mm = "
                "2 x the +-0.2 mm routed-edge tolerance (" + _CAPS + ") + 0.4 mm of FR4 "
                "left in the worst case"),
            "hole_inside": "geometry: a drilled hole must lie on the board",
        },
        assumptions=frozenset({"feature_width"})),
}

_SAMPLE_MM = 0.2           # fab width check: ray spacing along every edge
# A reflex vertex is reported when the router bit (radius R) leaves more than
# this much material beyond it: R * (1 / cos(turn / 2) - 1). 0.05 mm is a
# quarter of JLCPCB's +-0.2 mm routed-edge tolerance; a sharp 90 degree corner
# leaves 0.207 mm with R0.5, a 45 degree turn 0.041 mm (not reported).
_CORNER_EXCESS_MM = 0.05


def _limits(profile: str | FabLimits) -> FabLimits:
    if isinstance(profile, FabLimits):
        return profile
    key = str(profile).upper()
    if key not in FAB_LIMITS:
        raise ValueError(f"unknown fab profile {profile!r}; known: {sorted(FAB_LIMITS)}")
    return FAB_LIMITS[key]


def _problem(lim: FabLimits, rule: str, severity: str, message: str, at: Point | None,
             value: float, limit: float) -> dict:
    return {"rule": rule, "severity": severity, "message": message,
            "at": None if at is None else [round(at[0], 6), round(at[1], 6)],
            "value": round(value, 6), "limit": limit,
            "source": lim.sources.get(rule, ""), "assumption": rule in lim.assumptions}


def _router_excess(p: Prim, q: Prim, r_bit: float) -> float:
    """Material a bit of radius r_bit leaves beyond the vertex p -> q (0 for a
    left turn: only reflex vertices, where the material turns right, trap it)."""
    if cross_turn(p, q) >= 0:
        return 0.0
    t1, t2 = p.tangent(1.0), q.tangent(0.0)
    turn = math.acos(max(-1.0, min(1.0, t1[0] * t2[0] + t1[1] * t2[1])))
    half_cos = math.cos(turn / 2.0)
    return math.inf if half_cos <= 1e-12 else r_bit * (1.0 / half_cos - 1.0)


def _corner_problems(o: Outline, lim: FabLimits) -> list[dict]:
    """Concave (material-reflex) corners the router bit cannot cut: arcs whose
    centre is in the void (sweep < 0, material on the left) with r < the bit
    radius, and sharp reflex vertices where the bit leaves > _CORNER_EXCESS_MM.
    Polyline-approximated fillets are judged vertex by vertex."""
    out = []
    r_min = lim.min_internal_radius
    for k, prims in enumerate(o.rings):
        n = len(prims)
        for i, p in enumerate(prims):
            if isinstance(p, Arc) and not p.is_circle and p.sweep < 0 and p.r < r_min - 1e-9:
                out.append(_problem(lim, "internal_corner_radius", "warning",
                                    f"{ring_name(k)}: internal arc R{p.r:g} at {_fmt(p.mid)} "
                                    f"is tighter than the R{r_min:g} router bit; it will be "
                                    "milled wider", p.mid, p.r, r_min))
            excess = _router_excess(p, prims[(i + 1) % n], r_min) if n > 1 else 0.0
            if excess > _CORNER_EXCESS_MM:
                out.append(_problem(lim, "internal_corner_radius", "warning",
                                    f"{ring_name(k)}: sharp internal corner at {_fmt(p.end)}; "
                                    f"the R{r_min:g} router bit leaves {excess:.3f} mm of "
                                    f"material there. Draw a fillet >= {r_min:g} mm (or a "
                                    "dog-bone) if a part must fit the corner", p.end, 0.0,
                                    r_min))
    return out


# Widths. A neck, web, finger or slot is narrowest where the distance between
# two boundary points is a local minimum; at each end of that segment either
# the segment runs along the edge normal (an edge interior), or the end is a
# vertex and the segment lies in that vertex's normal cone (_Corner.faces),
# which opens into the region only where the region is reflex (a smooth join
# has the normal alone). Minima between two edge interiors are found by rays
# along the edge normals (_Rays.width_at, sampled then refined); minima with a
# vertex at one end or both are computed exactly (_Rays.at_corners), since no
# ray from an edge interior crosses those square. A corner's own wedge has no
# minimum (both sides close in on the tip), and rays across it are skipped
# (_RingWalk.is_wedge), so corners are judged by the corner rule only.
_WEDGE_TURN = math.radians(170.0)    # see _RingWalk.is_wedge
_SMOOTH_TURN_RAD = 1e-9              # a vertex turning less than this is a smooth join
_CONE_SLACK = 1e-9                   # cosine slack of the normal-cone test
_SELF_HIT_MM = 1e-7                  # ray hits nearer than this are the ray's own edge


@dataclass(frozen=True)
class _Corner:
    """A vertex of ring `ring` (where one primitive ends and the next starts):
    the tangents arriving and leaving and the signed turn between them (+ =
    left, a convex corner of the material; - = right, a reflex one)."""
    ring: int
    point: Point
    t_in: Point
    t_out: Point
    turn: float

    def faces(self, u: Point) -> bool:
        """The unit direction u lies in this vertex's normal cone: sliding off
        the vertex along either edge brings it no closer to a point in
        direction u, so a width in direction u can be narrowest AT the vertex.
        The cone spans the two edge normals on the side the vertex is reflex
        for (the material at a right turn, the void at a left turn); at a
        smooth join it is the normal alone."""
        return dot(u, self.t_in) >= -_CONE_SLACK and dot(u, self.t_out) <= _CONE_SLACK


def _corners(o: Outline) -> dict[tuple[int, int], _Corner]:
    """Every vertex, keyed (ring, index of the primitive ending there)."""
    out = {}
    for k, prims in enumerate(o.rings):
        n = len(prims)
        for i, p in enumerate(prims if n > 1 else ()):     # a full circle has no vertex
            t1, t2 = p.tangent(1.0), prims[(i + 1) % n].tangent(0.0)
            out[(k, i)] = _Corner(k, p.end, t1, t2, math.atan2(cross(t1, t2), dot(t1, t2)))
    return out


def _foot(p: Prim, x: Point) -> Point | None:
    """Foot of the perpendicular from x on p, None when it falls off p. On an
    arc only the near foot: from the far one, sliding along the arc gets closer."""
    if isinstance(p, Line):
        d = sub(p.b, p.a)
        t = dot(sub(x, p.a), d) / dot(d, d)
        return p.point_at(t) if 0.0 <= t <= 1.0 else None
    v = sub(x, p.c)
    n = norm(v)
    if n <= EPS or p.param(angle_of(v), 0.0) is None:
        return None
    return add(p.c, mul(v, p.r / n))


@dataclass(frozen=True)
class _RingWalk:
    """Signed tangent turning along one ring from its first primitive's start
    (arc sweeps plus vertex turns): tells a corner's own wedge from a neck or
    slot by the boundary between two points, however it is split into edges."""
    sweeps: tuple[float, ...]          # signed arc sweep (0 for a line)
    start_turn: tuple[float, ...]      # turning at each primitive's start; [-1] = total

    @classmethod
    def of(cls, prims: Sequence[Prim]) -> _RingWalk:
        n = len(prims)
        sweeps = tuple(p.sweep if isinstance(p, Arc) else 0.0 for p in prims)
        start_turn = [0.0]
        for i, p in enumerate(prims):
            t1, t2 = p.tangent(1.0), prims[(i + 1) % n].tangent(0.0)
            start_turn.append(start_turn[-1] + sweeps[i] + math.atan2(cross(t1, t2), dot(t1, t2)))
        return cls(sweeps, tuple(start_turn))

    def is_wedge(self, i: int, t: float, j: int, u: float) -> bool:
        """A ray along the normal at (i, t) whose first hit is (j, u), an
        edge interior on the same ring, crossed the wedge of a single corner:
        the boundary between them, walked one way round, turns less than
        _WEDGE_TURN. With the ray it closes a simple loop, so that walk turns
        180 - b degrees for a hit at b degrees off the far wall's normal: a
        corner of interior angle a gives b = a (a 60 degree tip, however its
        sides are split into edges). A minimum between two edge interiors is
        crossed square (b = 0, a U turn), so it and the rays near it are kept;
        minima at a vertex are left to _Rays.at_corners. Slivers sharper than
        10 degrees are judged too."""
        total = self.start_turn[-1]
        turn = (self.start_turn[j] + u * self.sweeps[j]) - (self.start_turn[i] + t * self.sweeps[i])
        if (j, u) < (i, t):                            # the forward walk passes the ring start
            turn += total
        return min(abs(turn), abs(total - turn)) < _WEDGE_TURN


class _Rays:
    """Width probes, measured no further than `reach` (a width beyond every
    limit is never reported, so it need not be measured): rays along the
    normals of edge interiors (width_at) and the exact widths that end at a
    vertex (at_corners). See the comment above _WEDGE_TURN."""

    def __init__(self, o: Outline, reach: float) -> None:
        self.o = o
        self.index = PrimIndex(o)
        self.reach = reach
        self.walks = tuple(_RingWalk.of(prims) for prims in o.rings)
        self.corners = _corners(o)

    def _corner_at(self, k: int, i: int, x: Point) -> _Corner | None:
        """The vertex at x when x is an end of rings[k][i], else None."""
        p = self.o.rings[k][i]
        if dist(x, p.end) <= 1e-9:
            return self.corners.get((k, i))
        if dist(x, p.start) <= 1e-9:
            return self.corners.get((k, (i - 1) % len(self.o.rings[k])))
        return None

    def width_at(self, e: Edge, t: float, outward: bool) -> tuple[float, Point, int]:
        """Width across the void (outward) or material at parameter t of edge
        e: distance along the normal to the first edge hit (inf beyond reach).
        A first hit AT a vertex counts when the reversed ray lies in that
        vertex's normal cone (_Corner.faces); a first hit inside the wedge of
        one corner of the same ring (_RingWalk.is_wedge) is not a neck or
        slot. Such samples measure nothing: acute corners above 10 degrees are
        not judged by the width rules (sharp concave ones are, by the corner
        rule)."""
        q = e.prim.point_at(t)
        nrm = e.normal_at(q)
        d = nrm if outward else (-nrm[0], -nrm[1])
        far = (q[0] + d[0] * self.reach, q[1] + d[1] * self.reach)
        box = (min(q[0], far[0]), min(q[1], far[1]), max(q[0], far[0]), max(q[1], far[1]))
        best: tuple[float, int, int] | None = None
        for k, i, p in self.index.near(box):
            for dist_t in ray_hits(p, q, d):
                if _SELF_HIT_MM < dist_t <= self.reach and (best is None or dist_t < best[0]):
                    best = (dist_t, k, i)
        if best is None:
            return math.inf, q, -1
        w, k, i = best
        if k == e.ring:
            hit = (q[0] + d[0] * w, q[1] + d[1] * w)
            corner = self._corner_at(k, i, hit)
            if corner is not None:
                counts = corner.faces((-d[0], -d[1]))
            else:
                u = closest_point(self.o.rings[k][i], hit)[2]
                counts = not self.walks[k].is_wedge(e.index, t, i, u)
            if not counts:
                return math.inf, q, -1
        return w, (q[0] + d[0] * w / 2.0, q[1] + d[1] * w / 2.0), k

    def _clear(self, a: Point, u: Point, length: float) -> bool:
        """The segment from a along the unit direction u crosses no edge
        before its far end at `length`."""
        b = (a[0] + u[0] * length, a[1] + u[1] * length)
        box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        return not any(_SELF_HIT_MM < h < length - _SELF_HIT_MM
                       for _k, _i, p in self.index.near(box) for h in ray_hits(p, a, u))

    def _targets(self, c: _Corner) -> list[tuple[Point, int, _Corner | None]]:
        """Candidate far ends of a width from the vertex c, within reach: the
        feet of perpendiculars on the edges and the other vertices."""
        x, y = c.point
        r = self.reach
        near = self.index.near((x - r, y - r, x + r, y + r))
        out: list[tuple[Point, int, _Corner | None]] = []
        keys: set[tuple[int, int]] = set()
        for k, i, p in near:
            foot = _foot(p, c.point)
            if foot is not None:
                out.append((foot, k, None))
            keys |= {(k, i), (k, (i - 1) % len(self.o.rings[k]))}
        out += [(w.point, w.ring, w) for key in sorted(keys)
                if (w := self.corners.get(key)) is not None and w is not c]
        return out

    def at_corners(self, outward: bool) -> dict[frozenset, tuple[float, Point]]:
        """Narrowest void (outward) or material width per ring pair among the
        widths that end at a vertex, exactly: from every vertex reflex for
        that side, to each foot of a perpendicular on an edge and to each
        vertex, whose direction lies in the vertex's normal cone (and, for a
        vertex, the reverse in the far one's), with nothing crossed between.
        A web under a V-notch tip, a waist between two tips, a tooth tip near
        a wall and two hole corners close together are measured here."""
        found: dict[frozenset, tuple[float, Point]] = {}
        for c in self.corners.values():
            if (c.turn if outward else -c.turn) <= _SMOOTH_TURN_RAD:
                continue
            x, y = c.point
            for q, ring, far in self._targets(c):
                length = dist(c.point, q)
                if not _SELF_HIT_MM < length <= self.reach:
                    continue
                u = ((q[0] - x) / length, (q[1] - y) / length)
                if not c.faces(u) or (far is not None and not far.faces((-u[0], -u[1]))):
                    continue
                key = frozenset((c.ring, ring))
                if (key not in found or length < found[key][0]) and self._clear(c.point, u, length):
                    found[key] = (length, ((x + q[0]) / 2.0, (y + q[1]) / 2.0))
        return found

    def refine(self, e: Edge, t: float, step: float, outward: bool,
               ring: int) -> tuple[float, Point]:
        """Golden-section refinement of a sampled width minimum within one
        sample step of t; a refined ray that lands on another ring is not taken."""
        def width(u: float) -> float:
            w, _at, hit = self.width_at(e, u, outward)
            return w if hit == ring else math.inf

        lo, hi = max(0.0, t - step), min(1.0, t + step)
        g = (math.sqrt(5.0) - 1.0) / 2.0
        for _ in range(40):
            a, b = hi - g * (hi - lo), lo + g * (hi - lo)
            if width(a) <= width(b):
                hi = b
            else:
                lo = a
        w, at, hit = self.width_at(e, (lo + hi) / 2.0, outward)
        return (w, at) if hit == ring else (math.inf, at)

    def narrowest(self, outward: bool) -> dict[frozenset, tuple[float, Point]]:
        """Narrowest void (outward=True) or material (False) width within
        reach, per ring pair: rays at every sample of every edge, refined,
        and the exact widths at vertices."""
        found: dict[frozenset, tuple[float, Point, Edge, float, float, int]] = {}
        for k, prims in enumerate(self.o.rings):
            for i, p in enumerate(prims):
                e = Edge(k, i, p)
                n = max(3, min(500, math.ceil(p.length / _SAMPLE_MM)))
                for j in range(n):
                    t = (j + 0.5) / n
                    w, at, ring = self.width_at(e, t, outward)
                    key = frozenset((k, ring))
                    if ring >= 0 and (key not in found or w < found[key][0]):
                        found[key] = (w, at, e, t, 1.0 / n, ring)
        out = {}
        for key, (w, at, e, t, step, ring) in found.items():
            rw, rat = self.refine(e, t, step, outward, ring)
            out[key] = (rw, rat) if rw < w else (w, at)
        for key, (w, at) in self.at_corners(outward).items():
            # on a tie (a strip as wide at its root vertices as along it) the
            # ray sample keeps its place along the strip
            if key not in out or w < out[key][0] - 1e-9:
                out[key] = (w, at)
        return out


def _width_problems(o: Outline, lim: FabLimits) -> list[dict]:
    rays = _Rays(o, max(lim.min_slot_width, lim.min_feature_width) + 1e-6)
    out = []
    for key, (w, at) in sorted(rays.narrowest(True).items(), key=lambda kv: sorted(kv[0])):
        if w < lim.min_slot_width - 1e-9:
            rings = sorted(key)
            where = " / ".join(ring_name(k) for k in rings)
            msg = (f"{where}: milled gap {w:.3f} mm at {_fmt(at)} is narrower than the "
                   f"{lim.min_slot_width:g} mm minimum non-plated slot")
            if any(isinstance(([o.outer] + list(o.cutouts))[k], Circle) for k in rings):
                msg += (f"; a round hole this small should be an NPTH drill "
                        f"(>= {lim.min_npth_diameter:g} mm), not an Edge.Cuts cutout")
            out.append(_problem(lim, "slot_width", "error", msg, at, w, lim.min_slot_width))
    for key, (w, at) in sorted(rays.narrowest(False).items(), key=lambda kv: sorted(kv[0])):
        if w < lim.min_feature_width - 1e-9:
            where = " / ".join(ring_name(k) for k in sorted(key))
            out.append(_problem(lim, "feature_width", "warning",
                                f"{where}: board material only {w:.3f} mm wide at {_fmt(at)} "
                                f"(minimum {lim.min_feature_width:g} mm)", at, w,
                                lim.min_feature_width))
    return out


def _hole_problems(o: Outline, lim: FabLimits, holes: Sequence[Sequence[float]]) -> list[dict]:
    out = []
    for n, h in enumerate(holes):
        if len(h) != 3:
            raise ValueError(f"hole {n} must be (x, y, diameter), got {h!r}")
        x, y, dia = (float(v) for v in h)
        c = (x, y)
        if dia < lim.min_npth_diameter - 1e-9:
            out.append(_problem(lim, "hole_size", "error",
                                f"hole {n} dia {dia:g} mm at {_fmt(c)} is below the "
                                f"{lim.min_npth_diameter:g} mm NPTH minimum", c, dia,
                                lim.min_npth_diameter))
        if not contains(o, c):
            out.append(_problem(lim, "hole_inside", "error",
                                f"hole {n} at {_fmt(c)} is off the board or inside a cutout",
                                c, 0.0, 0.0))
            continue
        gap = distance_to_edge(o, c) - dia / 2.0
        if gap < lim.min_hole_to_edge - 1e-9:
            sev = "error" if gap < 0 else "warning"
            out.append(_problem(lim, "hole_to_edge", sev,
                                f"hole {n} at {_fmt(c)} is {gap:.3f} mm from the board edge "
                                f"(minimum {lim.min_hole_to_edge:g} mm)", c, gap,
                                lim.min_hole_to_edge))
    return out


def validate_fab(o: Outline, profile: str | FabLimits = "JLCPCB",
                 holes: Sequence[Sequence[float]] = ()) -> list[dict]:
    """Outline fab problems for `profile` (a FAB_LIMITS name or a FabLimits).

    Each problem: {rule, severity, message, at [x, y] | None, value, limit,
    source, assumption}. Rules: board_size, slot_width (void narrower than the
    router: cutouts and notches), internal_corner_radius (sharp or too-tight
    concave corners), feature_width (thin necks/webs/fingers/slivers), and
    for `holes` [(x, y, diameter)] drilled NPTHs: hole_size, hole_inside,
    hole_to_edge. A width is measured where it is narrowest: between two
    edge interiors (rays along the edge normals, crossing square there) or
    at a vertex (exactly, from each vertex the region is reflex at: a web
    under a V-notch tip, a waist between two tips, a tooth tip near a wall,
    two hole corners). A measurement across the wedge of a single corner is
    not a width (_RingWalk.is_wedge), so a 60 degree tip is clean however
    its sides are split into edges, while a sliver sharper than 10 degrees
    is judged. Severity is "error" for published hard limits and "warning"
    for rules that alter the shape or rest on a secondary source /
    assumption."""
    lim = _limits(profile)
    check(o)
    out = []
    x0, y0, x1, y1 = bbox(o)
    small = min(x1 - x0, y1 - y0)
    if small < lim.min_board_dim - 1e-9:
        out.append(_problem(lim, "board_size", "error",
                            f"board is {x1 - x0:g} x {y1 - y0:g} mm; the minimum is "
                            f"{lim.min_board_dim:g} x {lim.min_board_dim:g} mm", None,
                            small, lim.min_board_dim))
    out += _corner_problems(o, lim)
    out += _width_problems(o, lim)
    out += _hole_problems(o, lim, holes)
    return out

#!/usr/bin/env python3
"""Closed loops from DXF pieces: duplicates, chaining, gap reports, degenerate
loops and the outer / cutout split. dxf_io's geometry half; it needs no ezdxf.

Everything is in the 2D drawing frame dxf_io picked (mm, Y up; ``frame`` names
it in messages: "XY" = the DXF origin, or a Fusion sketch frame whose origin is
the sketch origin). A ``Piece`` is an open run of ``(x0, y0, x1, y1, bulge)``
edges from one entity; bulges follow outline_geom (> 0 = math-CCW).
dxf_io.read_dxf_loops runs:
  drop_duplicates  closed pieces and circles repeating an earlier one within
                   ``tol`` (a hole or a whole outline drawn twice) are dropped
                   with a warning;
  chain_pieces     pieces join end to end within ``tol``; a repeated open
                   piece is dropped with a warning; 3+ ends meeting is a branch
                   (DxfOutlineError); chains left open go to
  gap_error        which pairs every dangling end (distance, both ends, entity
                   handles), lists the first ``GAP_LIST_MAX`` and keeps them all
                   in ``DxfOutlineError.gaps``. A chain whose own ends pair up
                   across more than export noise (``export_noise_mm``) is an
                   open path (construction geometry or a missing edge), and
                   "raise tol" is only advised when every gap is within that noise;
  drop_degenerate  loops enclosing (almost) nothing (|area| < tol^2 or mean
                   width < tol, e.g. a hairline ELLIPSE) are dropped with a
                   warning, so the board model never meets a zero-area cutout.
dxf_io.read_board_outline then calls classify_loops: outer = largest |area|,
the rest must be cutouts strictly inside it.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from outline_geom import (Point, Prim, dist, flatten_prim, intersections,  # noqa: E402
                          prim_bbox, prim_from_bulge, prim_signed_area)

_CLASSIFY_CHORD_MM = 0.005         # arc flattening for the point-in-loop test
_TOUCH_MM = 1e-7
_EPS = 1e-9
_BULGE_SAME = 1e-6                 # bulges this close describe the same arc
# a gap wider than both of these is a missing edge or construction geometry, not
# export noise: widening the join tolerance to bridge it would merge real geometry
_NOISE_TOLS = 50.0                 # join tolerances
_NOISE_MM = 0.5
GAP_LIST_MAX = 20                  # gaps / loops / entities spelled out in one message
_PAIR_REACH_MM = 1.0               # first search radius when pairing dangling ends

Vertex = tuple[float, float, float]
Edge = tuple[float, float, float, float, float]   # x0, y0, x1, y1, bulge
_T = TypeVar("_T")


def fmt(p: Point) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f})"


def _names(sources: Sequence[str]) -> str:
    more = f" +{len(sources) - 3} more" if len(sources) > 3 else ""
    return ", ".join(sources[:3]) + more


def frame_note(frame: str) -> str:
    """How messages name the 2D frame their coordinates are in."""
    return f"frame: {frame}, mm, Y up"


def capped(lines: Sequence[str], limit: int = GAP_LIST_MAX) -> list[str]:
    """The first ``limit`` lines, then '+K more' for the rest."""
    head = list(lines[:limit])
    return head + [f"+{len(lines) - limit} more"] if len(lines) > limit else head


def export_noise_mm(tol: float) -> float:
    """The widest gap still taken for export noise at join tolerance ``tol``."""
    return max(_NOISE_TOLS * tol, _NOISE_MM)


@dataclass(frozen=True)
class Gap:
    """Two dangling chain ends, paired nearest-first (drawing frame, mm, Y up).
    ``open_path`` names the entities of one open chain whose own two ends
    pair up across more than export noise: a stray path, not a joint."""
    distance: float
    a: Point
    a_at: str
    b: Point
    b_at: str
    open_path: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.open_path:
            return (f"open path {_names(self.open_path)} from {fmt(self.a)} to {fmt(self.b)} "
                    f"(ends {self.distance:.3f} mm apart) is not part of a closed loop")
        return (f"gap {self.distance:.3f} mm: {self.a_at} {fmt(self.a)} <-> "
                f"{self.b_at} {fmt(self.b)}")


class DxfOutlineError(ValueError):
    """A DXF cannot be read as a board outline, or a board cannot be written.
    ``gaps`` lists the unclosed joints and open paths, ``problems`` the
    rejected loops, branches and open paths."""

    def __init__(self, message: str, *, gaps: Sequence[Gap] = (),
                 problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.gaps = tuple(gaps)
        self.problems = tuple(problems)


# --- loops ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Loop:
    """A closed loop of ``(x, y, bulge)`` vertices (mm) with the entities it
    came from. ``circle`` is set for a CIRCLE (two half-circle vertices)."""
    vertices: tuple[Vertex, ...]
    sources: tuple[str, ...] = ()
    circle: tuple[float, float, float] | None = None

    def prims(self) -> list[Prim]:
        v, n = self.vertices, len(self.vertices)
        return [prim_from_bulge(v[i][:2], v[(i + 1) % n][:2], v[i][2]) for i in range(n)]

    @property
    def area(self) -> float:
        """Exact signed area (positive = math-CCW in this frame)."""
        return sum(prim_signed_area(p) for p in self.prims())

    def bbox(self) -> tuple[float, float, float, float]:
        """(min x, min y, max x, max y), arcs exact."""
        boxes = [prim_bbox(p) for p in self.prims()]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def ring(self, chord_tol: float) -> list[Point]:
        """Closed polyline (last point not repeated), arcs within ``chord_tol``."""
        return [q for p in self.prims() for q in flatten_prim(p, chord_tol)]

    def length(self) -> float:
        """Perimeter, arcs measured along the arc."""
        return sum(p.length for p in self.prims())


def circle_loop(cx: float, cy: float, r: float, source: str) -> Loop:
    return Loop(((cx - r, cy, 1.0), (cx + r, cy, 1.0)), (source,), (cx, cy, r))


def describe(loop: Loop) -> str:
    x0, y0, x1, y1 = loop.bbox()
    return f"loop {fmt((x0, y0))}-{fmt((x1, y1))} mm [{_names(loop.sources)}]"


def drop_degenerate(loops: Sequence[Loop], tol: float, warnings: list[str]) -> list[Loop]:
    """``loops`` without those enclosing (almost) nothing, each dropped with a
    warning: |area| < tol^2, or a mean width 2 |area| / perimeter (the width
    of a thin sliver; the radius of a circle) < tol. An ELLIPSE with a 1e-10
    axis ratio is a 7e-9 mm^2 'cutout' the board model would refuse."""
    kept: list[Loop] = []
    for loop in loops:
        area = abs(loop.area)
        width = 2.0 * area / max(loop.length(), _EPS)
        if area < tol * tol or width < tol:
            warnings.append(f"ignored degenerate {describe(loop)}: area {area:.3g} mm^2, mean "
                            f"width {width:.3g} mm (below the {tol:g} mm join tolerance)")
        else:
            kept.append(loop)
    return kept


# --- value checks and cutout hints --------------------------------------------------------
MAX_COORD_MM = 1e6                 # |coordinate|, radius or arc height beyond 1 km: corrupt
DRILL_HINT = "looks like a drill hole; export with --drill-shape-opt 0"


def non_finite(value: object) -> bool:
    """A float, or a vector / tuple holding one, that is inf or nan."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        return False
    return any(isinstance(c, float) and not math.isfinite(c) for c in value)


def coord_problem(points: Sequence[Sequence[float]], bulges: Sequence[float],
                  radius: float | None, scale: float) -> str | None:
    """Why an entity's geometry (drawing units, ``scale`` mm each) is corrupt: a
    coordinate, radius or bulge that is not finite, or a coordinate, radius or
    arc height (sagitta = chord x |bulge| / 2) beyond MAX_COORD_MM."""
    nums = [c for p in points for c in p] + list(bulges) + [radius or 0.0]
    if not all(math.isfinite(v) for v in nums):
        return "a coordinate, radius or bulge is not a finite number"
    sagittas = (math.dist(tuple(p), tuple(q)) * abs(b) / 2.0
                for p, q, b in zip(points, points[1:], bulges, strict=False))
    reach = max(max((abs(c) for p in points for c in p), default=0.0), abs(radius or 0.0),
                max(sagittas, default=0.0)) * scale
    if reach > MAX_COORD_MM:
        return (f"a coordinate, radius or arc reaches {reach:.3g} mm, beyond the "
                f"{MAX_COORD_MM:g} mm limit (after unit scaling)")
    return None


def drill_warnings(cutouts: Sequence[Loop], frame: str, slot: float, fab: str) -> list[str]:
    """A warning per circle cutout no wider than ``fab``'s minimum milled
    ``slot``: kicad-cli pcb export dxf writes every drill as a CIRCLE unless
    --drill-shape-opt 0 (the 1.0 mm pin-header drill of KiCad 9's default
    export is exactly at JLCPCB's 1.0 mm, so 'no wider', not 'narrower')."""
    return [f"cutout {lp.sources[0]}: a {2 * lp.circle[2]:.3f} mm circle at "
            f"{fmt(lp.circle[:2])} ({frame_note(frame)}) is no wider than {fab}'s {slot:g} mm "
            f"minimum milled slot, so it {DRILL_HINT} (or delete it)"
            for lp in cutouts if lp.circle and 2 * lp.circle[2] <= slot + 1e-9]


# --- outer / cutout classification -----------------------------------------------------
def _boxes_touch(a: Sequence[float], b: Sequence[float]) -> bool:
    return (a[0] <= b[2] + _TOUCH_MM and b[0] <= a[2] + _TOUCH_MM
            and a[1] <= b[3] + _TOUCH_MM and b[1] <= a[3] + _TOUCH_MM)


def _inside(p: Point, ring: Sequence[Point]) -> bool:
    """Even-odd point in polygon."""
    x, y = p
    inside = False
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[i - 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


@dataclass(frozen=True)
class _Shape:
    """A loop with what the containment tests need, computed once."""
    loop: Loop
    prims: tuple[Prim, ...]
    boxes: tuple[tuple[float, float, float, float], ...]
    box: tuple[float, float, float, float]
    polygon: tuple[Point, ...]

    @classmethod
    def of(cls, loop: Loop) -> _Shape:
        prims = tuple(loop.prims())
        return cls(loop, prims, tuple(prim_bbox(p) for p in prims), loop.bbox(),
                   tuple(loop.ring(_CLASSIFY_CHORD_MM)))

    def relation(self, other: _Shape) -> str:
        """'inside' | 'outside' | 'crossing' (touching included) of self vs other."""
        if _boxes_touch(self.box, other.box) and any(
                _boxes_touch(bp, bq) and intersections(p, q)
                for p, bp in zip(self.prims, self.boxes, strict=True)
                for q, bq in zip(other.prims, other.boxes, strict=True)):
            return "crossing"
        return "inside" if _inside(self.prims[0].start, other.polygon) else "outside"


@dataclass(frozen=True)
class Classified:
    """Outer loop, accepted cutouts and rejected loops with the reason."""
    outer: Loop
    cutouts: tuple[Loop, ...]
    stray: tuple[tuple[Loop, str], ...]


_OUTER_PROBLEM = {"outside": "outside the outer outline",
                  "crossing": "crosses or touches the outer outline"}


def classify_loops(loops: Sequence[Loop]) -> Classified:
    """Outer = largest |area|. A cutout lies strictly inside the outer loop and
    neither inside nor across a (larger) accepted cutout."""
    if not loops:
        raise DxfOutlineError("no closed loop to classify")
    ordered = [lp for _a, _i, lp in sorted((-abs(lp.area), i, lp) for i, lp in enumerate(loops))]
    outer = _Shape.of(ordered[0])
    if abs(outer.loop.area) <= _EPS:
        raise DxfOutlineError(f"the largest {describe(outer.loop)} encloses zero area")
    kept: list[_Shape] = []
    stray: list[tuple[Loop, str]] = []
    for loop in ordered[1:]:
        shape = _Shape.of(loop)
        why = _OUTER_PROBLEM.get(shape.relation(outer))
        for k, other in enumerate(kept if why is None else (), 1):
            rel = shape.relation(other)
            if rel != "outside":
                why = f"{'inside' if rel == 'inside' else 'overlaps'} cutout {k} " \
                      f"({describe(other.loop)})"
                break
        if why:
            stray.append((loop, why))
        else:
            kept.append(shape)
    return Classified(outer.loop, tuple(s.loop for s in kept), tuple(stray))


# --- pieces and duplicates -------------------------------------------------------------
def _edge_length(e: Edge) -> float:
    chord, b = math.hypot(e[2] - e[0], e[3] - e[1]), abs(e[4])
    if b < 1e-12:
        return chord
    return chord * (1.0 + b * b) / (4.0 * b) * 4.0 * math.atan(b)   # radius x sweep


@dataclass(frozen=True)
class Piece:
    """An open run of edges from one entity (drawing frame, mm)."""
    edges: tuple[Edge, ...]
    source: str
    start_at: str
    end_at: str

    @property
    def start(self) -> Point:
        return self.edges[0][0], self.edges[0][1]

    @property
    def end(self) -> Point:
        return self.edges[-1][2], self.edges[-1][3]

    def reversed(self) -> Piece:
        return Piece(tuple((x1, y1, x0, y0, -b) for x0, y0, x1, y1, b in reversed(self.edges)),
                     self.source, self.end_at, self.start_at)

    def length(self) -> float:
        """Path length, arcs measured along the arc (not the chord)."""
        return sum(_edge_length(e) for e in self.edges)

    def is_ring(self, tol: float) -> bool:
        """Closed on itself: two or more edges, the ends within ``tol``."""
        return len(self.edges) > 1 and dist(self.start, self.end) <= tol


def _same_edges(a: Sequence[Edge], b: Sequence[Edge], tol: float) -> bool:
    return len(a) == len(b) and all(
        dist(e[:2], f[:2]) <= tol and dist(e[2:4], f[2:4]) <= tol
        and abs(e[4] - f[4]) <= _BULGE_SAME for e, f in zip(a, b, strict=True))


def _same_ring(a: Piece, b: Piece, tol: float) -> bool:
    """The same closed path, whatever its start vertex and direction."""
    if len(a.edges) != len(b.edges):
        return False
    return any(_same_edges(a.edges, c[k:] + c[:k], tol)
               for c in (b.edges, b.reversed().edges) for k in range(len(c)))


def _unique(items: Sequence[_T], ref: Callable[[_T], Point], same: Callable[[_T, _T], bool],
            name: Callable[[_T], str], tol: float, warnings: list[str]) -> list[_T]:
    """``items`` without those repeating an earlier one; candidates are found
    by bucketing each ``ref`` point on a ``tol`` grid."""
    grid: dict[tuple[int, int], list[_T]] = {}
    kept: list[_T] = []
    for item in items:
        x, y = ref(item)
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        twin = next((o for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     for o in grid.get((cx + dx, cy + dy), ()) if same(item, o)), None)
        if twin is None:
            grid.setdefault((cx, cy), []).append(item)
            kept.append(item)
        else:
            warnings.append(f"ignored duplicate {name(item)} (same geometry as {name(twin)})")
    return kept


def drop_duplicates(pieces: Sequence[Piece], circles: Sequence[Loop], tol: float,
                    warnings: list[str]) -> tuple[list[Piece], list[Loop]]:
    """Drop closed pieces and circles that repeat an earlier one within ``tol``,
    each with a warning. Open pieces are left for chain_pieces, which drops a
    repeated one where it meets the chain."""
    rings = _unique([p for p in pieces if p.is_ring(tol)],
                    lambda p: (min(e[0] for e in p.edges), min(e[1] for e in p.edges)),
                    lambda p, q: _same_ring(p, q, tol), lambda p: p.source, tol, warnings)
    kept = {id(p) for p in rings}
    circles = _unique(circles, lambda c: c.circle[:2],
                      lambda c, d: dist(c.circle[:2], d.circle[:2]) <= tol
                      and abs(c.circle[2] - d.circle[2]) <= tol,
                      lambda c: c.sources[0], tol, warnings)
    return [p for p in pieces if id(p) in kept or not p.is_ring(tol)], circles


# --- chaining ----------------------------------------------------------------------------
class _Chainer:
    """Joins pieces whose ends are within ``tol`` (grid-bucketed lookup)."""

    def __init__(self, pieces: Sequence[Piece], tol: float, warnings: list[str],
                 frame: str) -> None:
        self.pieces, self.tol, self.warnings, self.frame = pieces, tol, warnings, frame
        self.used = [False] * len(pieces)
        self.grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for i, p in enumerate(pieces):
            for which, pt in ((0, p.start), (1, p.end)):
                self.grid.setdefault(self._cell(pt), []).append((i, which))

    def _cell(self, p: Point) -> tuple[int, int]:
        return math.floor(p[0] / self.tol), math.floor(p[1] / self.tol)

    def _near(self, p: Point) -> list[tuple[int, int]]:
        cx, cy = self._cell(p)
        return [(i, which) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for i, which in self.grid.get((cx + dx, cy + dy), ())
                if not self.used[i]
                and dist(p, self.pieces[i].start if which == 0 else self.pieces[i].end) <= self.tol]

    def _candidates(self, chain: list[Piece], at_end: bool) -> list[tuple[int, Piece]]:
        """Unused pieces at the tip, oriented to leave (at_end) or reach it. A
        piece retracing the tip piece or another candidate is a duplicate: it is
        dropped with a warning."""
        neighbour = (chain[-1] if at_end else chain[0]).reversed()   # also touches the tip
        keep: list[tuple[int, Piece]] = []
        for i, which in self._near(chain[-1].end if at_end else chain[0].start):
            p = self.pieces[i]
            q = p if (which == 0) == at_end else p.reversed()
            twin = next((o for o in [neighbour] + [k for _j, k in keep]
                         if _same_edges(q.edges, o.edges, self.tol)), None)
            if twin is not None:
                self.used[i] = True
                self.warnings.append(f"ignored duplicate {p.source} "
                                     f"(same geometry as {twin.source})")
            else:
                keep.append((i, q))
        return keep

    def _extend(self, chain: list[Piece], at_end: bool) -> bool:
        """Grow one side; True when the chain closed."""
        while True:
            tip = chain[-1].end if at_end else chain[0].start
            closes = dist(tip, chain[0].start if at_end else chain[-1].end) <= self.tol
            cands = self._candidates(chain, at_end)
            if len(cands) + closes > 1:
                here = [chain[-1].end_at if at_end else chain[0].start_at]
                here += [q.start_at if at_end else q.end_at for _i, q in cands]
                here += [chain[0].start_at if at_end else chain[-1].end_at] if closes else []
                raise DxfOutlineError(
                    f"{len(here)} entity ends meet at {fmt(tip)} mm ({', '.join(here)}; "
                    f"{frame_note(self.frame)}): an outline must be a simple closed path. Delete "
                    "the construction or duplicate geometry (Fusion exports construction lines, "
                    "e.g. a center rectangle's diagonals, as plain LINEs) or move it to another "
                    "layer and pass layer=", problems=(f"branch at {fmt(tip)}",))
            if closes or not cands:
                return closes
            i, q = cands[0]
            self.used[i] = True
            chain.insert(len(chain) if at_end else 0, q)

    def run(self) -> tuple[list[Loop], list[list[Piece]]]:
        loops: list[Loop] = []
        open_chains: list[list[Piece]] = []
        for i, first in enumerate(self.pieces):
            if self.used[i]:
                continue
            self.used[i] = True
            chain = [first]
            if not (self._extend(chain, at_end=True) or self._extend(chain, at_end=False)):
                open_chains.append(chain)
                continue
            edges = [e for p in chain for e in p.edges]   # vertex = joint midpoint
            if len(edges) < 2:
                raise DxfOutlineError(
                    f"{first.source} closes on itself with a single arc edge ({first.start_at} "
                    f"{fmt(first.start)}, {first.end_at} {fmt(first.end)}): draw the hole as a "
                    "CIRCLE", problems=(f"single-edge loop at {fmt(first.start)}",))
            verts = tuple(((x0 + edges[k - 1][2]) / 2.0, (y0 + edges[k - 1][3]) / 2.0, b)
                          for k, (x0, y0, _x1, _y1, b) in enumerate(edges))
            loops.append(Loop(verts, tuple(dict.fromkeys(p.source for p in chain))))
        return loops, open_chains


def chain_pieces(pieces: Sequence[Piece], tol: float, warnings: list[str],
                 frame: str = "XY") -> tuple[list[Loop], list[list[Piece]]]:
    """Closed loops and the chains that stay open (for gap_error). ``frame``
    names the drawing frame in the branch error."""
    return _Chainer(pieces, tol, warnings, frame).run()


def _close_pairs(pts: Sequence[Point], live: Sequence[int],
                 reach: float) -> list[tuple[float, int, int]]:
    """(distance, i, j), i < j, for the ``live`` points within ``reach`` of
    each other (grid-bucketed), closest first."""
    grid: dict[tuple[int, int], list[int]] = {}
    for i in live:
        grid.setdefault((math.floor(pts[i][0] / reach), math.floor(pts[i][1] / reach)),
                        []).append(i)
    pairs = []
    for (cx, cy), members in grid.items():
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for j in grid.get((cx + dx, cy + dy), ())]
        for i in members:
            for j in near:
                if j > i and (d := dist(pts[i], pts[j])) <= reach:
                    pairs.append((d, i, j))
    return sorted(pairs)


def _pair_ends(open_chains: Sequence[Sequence[Piece]], noise: float) -> list[Gap]:
    """Every dangling end paired with its nearest partner, closest pairs first
    (greedy over all pairs by distance, then index); a chain whose own two ends
    pair up wider than ``noise`` is an open path. Pairs are searched within a
    growing reach, so 1500 stray lines pair in well under a second; the result
    is the same as sorting every pair (no pair within the reach of a round is
    left for a later one)."""
    ends = [(pt, at, k) for k, c in enumerate(open_chains)
            for pt, at in ((c[0].start, c[0].start_at), (c[-1].end, c[-1].end_at))]
    pts = [e[0] for e in ends]
    live, reach = list(range(len(ends))), _PAIR_REACH_MM
    gaps: list[Gap] = []
    while len(live) > 1 and math.isfinite(reach):
        taken: set[int] = set()
        for d, i, j in _close_pairs(pts, live, reach):
            if i in taken or j in taken:
                continue
            taken.update((i, j))
            (a, a_at, ka), (b, b_at, kb) = ends[i], ends[j]
            stray = ka == kb and d > noise
            gaps.append(Gap(d, a, a_at, b, b_at, tuple(dict.fromkeys(
                p.source for p in open_chains[ka])) if stray else ()))
        live = [i for i in live if i not in taken]
        reach *= 4.0
    return gaps


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def gap_error(path: str | Path, open_chains: Sequence[Sequence[Piece]], tol: float,
              closed: int, frame: str = "XY") -> DxfOutlineError:
    """The error for chains left open: the first GAP_LIST_MAX gaps and open
    paths ('+K more'; ``gaps`` keeps every one), in the drawing ``frame``, with
    advice that never suggests a join tolerance wide enough to merge real
    geometry ("pass tol >= X" only when every gap is within export noise)."""
    noise = export_noise_mm(tol)
    gaps = _pair_ends(open_chains, noise)
    joints = [g for g in gaps if not g.open_path]
    paths = [g for g in gaps if g.open_path]
    counts = [f"{_plural(len(joints), 'gap')} wider than the {tol:g} mm join tolerance"
              ] if joints else []
    if paths:
        counts.append(_plural(len(paths), "open path"))
    advice: list[str] = []
    if paths:
        advice.append("An open path is construction geometry (Fusion exports construction lines "
                      "as plain LINEs) or an outline with a missing edge: delete it or move it to "
                      "another layer and pass layer=, or draw the missing edge.")
    if any(g.distance > noise for g in joints):
        advice.append(f"Gaps wider than {noise:g} mm are not export noise: draw the missing edge, "
                      "or delete the construction geometry (or move it to another layer and pass "
                      "layer=); a larger tol would merge real geometry.")
    elif joints:
        worst = max(g.distance for g in joints)
        advice.append("Join the ends in the sketch (coincident constraint)" + (
            "" if paths else ", or, if the gaps are export noise, pass tol >= "
                             f"{math.ceil(worst * 1001) / 1000:.3f} mm") + ".")
    return DxfOutlineError(
        f"{path}: the outline does not close: {' and '.join(counts)} ({closed} closed loop(s) "
        f"found; {frame_note(frame)}):\n  " + "\n  ".join(capped([g.describe() for g in gaps]))
        + "\n" + "\n".join(advice), gaps=gaps, problems=tuple(g.describe() for g in paths))

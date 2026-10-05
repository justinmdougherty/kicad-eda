"""Outline validity checks on dense, DXF-like outlines (outline_model.geometry_problems).

The complexity fix is asserted with operation counters, never wall-clock time
(the suite runs on CI, in Docker and next to other agents here):
  (a) prim_bbox runs at most once per primitive per call;
  (b) primitive-pair intersection tests across rings stay <= 2 % of
      n_outer x m_cutout on the 4000-segment + 40 x 64-gon board;
  (c) ring pairs whose bboxes are disjoint are rejected before any pair test
      (and before any containment test between them).
Wall-clock numbers come from a scratch benchmark (see the task report).
The equivalence tests pin the reported problems (text and order) to the
plain all-pairs algorithm the indexed one replaces.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence

import pytest

import outline as ol
import outline_model as om

SEGMENTS_PER_QUADRANT = 1000
HOLE_SIDES = 64
Pt = tuple[float, float]
Dense = tuple[ol.Loop, tuple[ol.Loop, ...]]


def _rounded_rect_points(w: float, h: float, r: float, per_quadrant: int) -> list[Pt]:
    """A w x h rectangle with R`r` corners as a polyline of 4 * per_quadrant
    equal arc-length segments starting at the middle of the right side (so
    each quadrant: half a side, a corner, half a side, gets per_quadrant),
    every point exactly on the rounded rectangle."""
    def seg(a: Pt, b: Pt) -> tuple[float, Callable[[float], Pt]]:
        n = math.dist(a, b)
        return n, lambda t: (a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n)

    def arc(cx: float, cy: float, a0: float) -> tuple[float, Callable[[float], Pt]]:
        return math.pi * r / 2, lambda t: (cx + r * math.cos(a0 + t / r),
                                           cy + r * math.sin(a0 + t / r))

    legs = [seg((w, h / 2), (w, h - r)), arc(w - r, h - r, 0.0), seg((w - r, h), (r, h)),
            arc(r, h - r, math.pi / 2), seg((0, h - r), (0, r)), arc(r, r, math.pi),
            seg((r, 0), (w - r, 0)), arc(w - r, r, 1.5 * math.pi), seg((w, r), (w, h / 2))]
    total, n = sum(length for length, _f in legs), 4 * per_quadrant
    pts, leg, start = [], 0, 0.0
    for k in range(n):
        s = total * k / n
        while leg < len(legs) - 1 and s > start + legs[leg][0]:
            start += legs[leg][0]
            leg += 1
        pts.append(legs[leg][1](s - start))
    return pts


def _ngon(cx: float, cy: float, r: float, n: int) -> ol.Loop:
    return ol.Loop(tuple((cx + r * math.cos(2 * math.pi * k / n),
                          cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)))


def _dense_rings() -> Dense:
    """The review's case: a 60 x 40 R5 rounded rectangle in 4 x 1000 segments
    and 40 regular 64-gon holes (r 0.6) on an 8 x 5 grid, 6 mm pitch."""
    outer = ol.Loop(tuple(_rounded_rect_points(60.0, 40.0, 5.0, SEGMENTS_PER_QUADRANT)))
    holes = tuple(_ngon(9.0 + 6.0 * i, 8.0 + 6.0 * j, 0.6, HOLE_SIDES)
                  for j in range(5) for i in range(8))
    return outer, holes


@pytest.fixture(scope="module")
def dense() -> Dense:
    return _dense_rings()


def test_the_dense_board_is_what_the_counters_are_judged_on(dense: Dense) -> None:
    outer, holes = dense
    o = ol.check(ol.Outline(outer, holes))
    assert len(o.rings[0]) == 4 * SEGMENTS_PER_QUADRANT
    assert len(o.rings) == 41 and all(len(r) == HOLE_SIDES for r in o.rings[1:])
    assert ol.bbox(o) == pytest.approx((0.0, 0.0, 60.0, 40.0), abs=1e-9)
    hole = HOLE_SIDES / 2 * 0.6 ** 2 * math.sin(2 * math.pi / HOLE_SIDES)
    assert ol.area(o) == pytest.approx(60 * 40 - (4 - math.pi) * 25 - 40 * hole, abs=0.01)


def _count_bbox_calls(monkeypatch: pytest.MonkeyPatch) -> Counter:
    calls: Counter = Counter()
    real = om.prim_bbox

    def counting(p: om.Prim) -> om.Box:
        calls[id(p)] += 1
        return real(p)

    monkeypatch.setattr(om, "prim_bbox", counting)
    return calls


def test_check_computes_each_prim_bbox_at_most_once(
        dense: Dense, monkeypatch: pytest.MonkeyPatch) -> None:
    outer, holes = dense
    calls = _count_bbox_calls(monkeypatch)
    o = ol.check(ol.Outline(outer, holes))
    n = sum(len(r) for r in o.rings)
    assert calls and max(calls.values()) == 1 and sum(calls.values()) <= n
    calls.clear()
    ol.check(o)                                        # a second call reuses them
    ol.geometry_problems(o)
    assert sum(calls.values()) == 0


def test_validate_fab_computes_each_prim_bbox_at_most_once(
        dense: Dense, monkeypatch: pytest.MonkeyPatch) -> None:
    outer, holes = dense
    o = ol.Outline(outer, holes)                       # nothing computed yet
    calls = _count_bbox_calls(monkeypatch)
    ol.validate_fab(o)
    assert calls and max(calls.values()) == 1
    assert sum(calls.values()) <= sum(len(r) for r in o.rings)


def _record_pairs(monkeypatch: pytest.MonkeyPatch, o: ol.Outline) -> tuple[list, list]:
    """Wrap the per-primitive-pair test and the ring containment test, recording
    the rings involved (by primitive identity)."""
    ring_of = {id(p): k for k, prims in enumerate(o.rings) for p in prims}
    ring_by_prims = {id(prims): k for k, prims in enumerate(o.rings)}
    pairs: list[tuple[int, int]] = []
    contains: list[int] = []
    real_pair, real_contains = om._pair_crossing, om._Ring.contains

    def pair(p: om.Prim, bp: om.Box, q: om.Prim, bq: om.Box) -> Pt | None:
        pairs.append((ring_of[id(p)], ring_of[id(q)]))
        return real_pair(p, bp, q, bq)

    def ring_contains(ring: om._Ring, q: Pt) -> bool:
        contains.append(ring_by_prims.get(id(ring.prims), -1))
        return real_contains(ring, q)

    monkeypatch.setattr(om, "_pair_crossing", pair)
    monkeypatch.setattr(om._Ring, "contains", ring_contains)
    return pairs, contains


def _ring_box(o: ol.Outline, k: int) -> tuple:
    boxes = [om.prim_bbox(p) for p in o.rings[k]]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _disjoint(a: tuple, b: tuple) -> bool:
    return a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def test_ring_pair_tests_are_a_small_fraction_of_all_pairs(
        dense: Dense, monkeypatch: pytest.MonkeyPatch) -> None:
    outer, holes = dense
    o = ol.Outline(outer, holes)
    pairs, _contains = _record_pairs(monkeypatch, o)
    assert ol.geometry_problems(o) == []
    n_outer, m_cutout = len(o.rings[0]), sum(len(r) for r in o.rings[1:])
    assert len(pairs) <= 0.02 * n_outer * m_cutout


def test_disjoint_ring_bboxes_are_rejected_before_any_pair_test(
        dense: Dense, monkeypatch: pytest.MonkeyPatch) -> None:
    outer, holes = dense
    o = ol.Outline(outer, holes)
    pairs, contains = _record_pairs(monkeypatch, o)
    ol.geometry_problems(o)
    boxes = [_ring_box(o, k) for k in range(len(o.rings))]
    assert not [(a, b) for a, b in pairs if _disjoint(boxes[a], boxes[b])]
    # every cutout is tested once for lying inside the outline, never against
    # another cutout (all 40 are apart)
    assert sorted(contains) == [0] * len(holes)


def test_ring_pairs_whose_bboxes_touch_are_still_tested(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # positive control for the counters: hole 2 sits 0.7 mm outside the long
    # hypotenuse of triangle hole 1 (inside its bbox), hole 4 0.7 mm inside the
    # outline's diagonal edge (inside that edge's bbox); hole 3 is far from all
    o = ol.Outline(ol.Loop(((0, 0), (60, 0), (60, 20), (40, 40), (0, 40))),
                   (ol.Loop(((10, 10), (14, 10), (10, 14))),
                    ol.Loop(((12.5, 12.5), (13, 12.5), (13, 13), (12.5, 13))),
                    ol.Circle(30, 30, 1), ol.Loop(((50, 25), (52, 25), (52, 27), (50, 27)))))
    pairs, contains = _record_pairs(monkeypatch, o)
    assert ol.geometry_problems(o) == []
    tested = {tuple(sorted(p)) for p in pairs}
    assert (1, 2) in tested and (0, 4) in tested
    assert not {(1, 3), (2, 3), (1, 4), (2, 4), (3, 4)} & tested
    assert sorted(contains) == [0, 0, 0, 0, 1, 2]       # 1 and 2: the nesting test


def test_self_crossing_check_is_local_on_a_long_split_edge(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # a 40 x 30 rectangle whose vertical sides are split into 5000 segments
    # each: a sweep by bbox x0 compares every pair on one side (25 M bbox tests)
    side = 5000
    loop = ol.Loop(tuple([(0.0, 30.0 - 30.0 * k / side) for k in range(side)]
                         + [(40.0, 30.0 * k / side) for k in range(side)]))
    calls = Counter()
    real = om._boxes_touch

    def counting(a: tuple, b: tuple, tol: float = om._BOX_TOL_MM) -> bool:
        calls["touch"] += 1
        return real(a, b, tol)

    monkeypatch.setattr(om, "_boxes_touch", counting)
    assert ol.geometry_problems(ol.Outline(loop)) == []
    assert 0 < calls["touch"] <= 100 * 2 * side


# --- the indexed checks report what the all-pairs algorithm reports -----------------
def _reference_self_crossings(prims: Sequence[om.Prim]) -> list[tuple[int, int, Pt]]:
    """The pre-index self-crossing sweep (by bbox x0), verbatim."""
    n = len(prims)
    boxes = [om.prim_bbox(p) for p in prims]
    order = sorted(range(n), key=lambda i: boxes[i][0])
    found = []
    for a_pos, i in enumerate(order):
        for j in order[a_pos + 1:]:
            if boxes[j][0] > boxes[i][2] + 1e-7:
                break
            if not om._boxes_touch(boxes[i], boxes[j]):
                continue
            lo, hi = min(i, j), max(i, j)
            shared = []
            if hi == lo + 1:
                shared.append(prims[lo].end)
            if lo == 0 and hi == n - 1:
                shared.append(prims[hi].end)
            for x in om.intersections(prims[lo], prims[hi]):
                if all(om.dist(x, s) > 1e-6 for s in shared):
                    found.append((lo, hi, x))
                    break
    return found


def _reference_problems(o: ol.Outline) -> list[str]:
    """The pre-index geometry_problems: every primitive of one ring against
    every primitive of the other, containment over every primitive."""
    def cross(a: Sequence[om.Prim], b: Sequence[om.Prim]) -> Pt | None:
        for p in a:
            for q in b:
                if om._boxes_touch(om.prim_bbox(p), om.prim_bbox(q)):
                    xs = om.intersections(p, q)
                    if xs:
                        return xs[0]
        return None

    out = []
    for k, prims in enumerate(o.rings):
        out += [f"{om.ring_name(k)}: edges {i} and {j} intersect at {om._fmt(x)}"
                for i, j, x in _reference_self_crossings(prims)]
    for k in range(1, len(o.rings)):
        ring = o.rings[k]
        x = cross(o.rings[0], ring)
        if x is not None:
            out.append(f"{om.ring_name(k)} crosses the outline at {om._fmt(x)}")
        elif not om._ring_contains(o.rings[0], ring[0].start):
            out.append(f"{om.ring_name(k)} lies outside the outline (at {om._fmt(ring[0].start)})")
        for m in range(k + 1, len(o.rings)):
            x = cross(ring, o.rings[m])
            nested = (om._ring_contains(ring, o.rings[m][0].start)
                      or om._ring_contains(o.rings[m], ring[0].start))
            if x is not None or nested:
                where = om._fmt(x) if x is not None else "one inside the other"
                out.append(f"{om.ring_name(k)} and {om.ring_name(m)} overlap ({where})")
    return out


T8 = math.tan(math.pi / 8)
SLOT = ol.Loop(((10, 10), (14, 10, 1.0), (14, 11), (10, 11, 1.0)))
VALID_LAYOUT = 4


def _layout(n: int) -> ol.Outline:
    outer = ol.Loop(tuple(_rounded_rect_points(40.0, 30.0, 4.0, 60)))
    corner = ol.Loop(((0, 0), (30, 0), (30, 20, T8), (25, 25), (0, 25)))
    return [
        lambda: ol.Outline(outer, (_ngon(10, 10, 2, 32), _ngon(12.5, 10, 1, 32))),   # cross
        lambda: ol.Outline(outer, (_ngon(10, 10, 3, 32), _ngon(10.5, 10, 1, 32))),   # nested
        lambda: ol.Outline(outer, (_ngon(10, 10, 1, 32), ol.Circle(50, 10, 1))),     # outside
        lambda: ol.Outline(outer, (_ngon(1.0, 15, 2, 32), ol.Circle(20, 15, 2))),    # edge
        lambda: ol.Outline(outer, (ol.Circle(30, 20, 1.5), SLOT, ol.Circle(20, 20, 0.5))),
        lambda: ol.Outline(corner, (SLOT, ol.Circle(28, 22, 4), ol.Circle(5, 5, 1),
                                    ol.Circle(5.5, 5, 2), ol.Circle(12, 10.5, 0.4))),
        lambda: ol.Outline(ol.Loop(((0, 0), (10, 10), (10, 0), (0, 12))), ()),       # bow tie
        lambda: ol.Outline(outer, tuple(_ngon(5 + 3 * (k % 10), 5 + 3 * (k // 10), 1.6, 16)
                                        for k in range(30))),                      # chain
        lambda: ol.Outline(ol.Loop(tuple(_PENTAGRAM)),                             # 5 + 1
                           (ol.Loop(((18, 18), (22, 22), (22, 18), (18, 23))),)),
    ][n]()


_PENTAGRAM = [(20 + 15 * math.cos(math.radians(90 + 144 * k)),
               20 + 15 * math.sin(math.radians(90 + 144 * k))) for k in range(5)]


@pytest.mark.parametrize("n", range(9))
def test_geometry_problems_match_the_all_pairs_algorithm(n: int) -> None:
    o = _layout(n)
    got = ol.geometry_problems(o)
    assert got == _reference_problems(o)
    assert bool(got) == (n != VALID_LAYOUT)

"""Unit tests for the stdlib-only board-outline geometry (server/tools/outline*.py).

ROADMAP D2: outline = outer loop + cutouts (loops or circles) + datum; a loop
vertex is [x, y] or [x, y, bulge]. Every expected value here is computed
independently of the module (closed-form areas, hand-placed arc centres,
brute-force optima), never read back from it.

Frame reminder: the board frame is Y DOWN, but all math is plain (x, y) math.
"CCW in math terms" means a positive shoelace area / a sweep from +X towards
+Y; on screen in the Y-down board frame that turn looks clockwise.

inset is tested in test_outline_inset.py, validate_fab (the JLCPCB rules) in
test_outline_fab.py.
"""
from __future__ import annotations

import ast
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

import outline as ol

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
OUTLINE_MODULES = ("outline", "outline_geom", "outline_inset", "outline_model", "outline_ops")
T8 = math.tan(math.pi / 8)          # bulge of a 90 degree arc
LIVE_TIMEOUT_S = 120

# 60 x 40, R5 corners, a dia-8 hole and a sharp 10 x 10 square cutout
RR_BLOCK = {"w": 60, "h": 40, "corner_radius": 5,
            "cutouts": [{"circle": [30, 20, 4]},
                        {"loop": [[10, 15], [20, 15], [20, 25], [10, 25]]}]}
RR_AREA = 2400 - (4 - math.pi) * 25 - math.pi * 16 - 100
L_LOOP = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]


def rr() -> ol.Outline:
    return ol.from_board_block(RR_BLOCK)


def l_shape() -> ol.Outline:
    return ol.from_board_block({"outline": {"loop": L_LOOP}})


def close(a, b, tol=1e-9) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _cycle_match(va, vb, tol) -> bool:
    """Same closed vertex cycle (x, y, bulge) up to the starting vertex."""
    if len(va) != len(vb):
        return False
    n = len(va)
    return any(all(close(va[i], vb[(i + k) % n], tol) for i in range(n)) for k in range(n))


def _same_ring(ra, rb, tol) -> bool:
    if isinstance(ra, ol.Circle) and isinstance(rb, ol.Circle):
        return close((ra.cx, ra.cy, ra.r), (rb.cx, rb.cy, rb.r), tol)
    if isinstance(ra, ol.Loop) and isinstance(rb, ol.Loop):
        return _cycle_match(ra.vertices, rb.vertices, tol)
    return False


def same_geometry(a: ol.Outline, b: ol.Outline, tol=1e-6, ordered=True) -> bool:
    """Same area, bbox, outer ring and cutouts (vertex cycles incl. bulges)."""
    if not (math.isclose(ol.area(a), ol.area(b), abs_tol=tol)
            and close(ol.bbox(a), ol.bbox(b), tol) and len(a.cutouts) == len(b.cutouts)
            and _same_ring(a.outer, b.outer, tol)):
        return False
    if ordered:
        return all(_same_ring(x, y, tol) for x, y in zip(a.cutouts, b.cutouts))
    left = list(b.cutouts)
    for x in a.cutouts:
        hit = next((y for y in left if _same_ring(x, y, tol)), None)
        if hit is None:
            return False
        left.remove(hit)
    return True


# ---------------------------------------------------------------------------
# 1. exact bulge-arc math
# ---------------------------------------------------------------------------
def test_quarter_arc_positive_bulge_sweeps_ccw_in_math_terms():
    a = ol.arc_from_bulge((1, 0), (0, 1), T8)
    assert close(a.c, (0, 0)) and math.isclose(a.r, 1.0)
    assert math.isclose(a.sweep, math.pi / 2)            # positive = math-CCW
    h = math.sqrt(0.5)
    assert close(a.mid, (h, h))                          # bows right of the chord direction
    assert close(a.start, (1, 0)) and close(a.end, (0, 1))


def test_quarter_arc_negative_bulge_sweeps_cw_and_bows_left():
    a = ol.arc_from_bulge((1, 0), (0, 1), -T8)
    assert close(a.c, (1, 1)) and math.isclose(a.r, 1.0)
    assert math.isclose(a.sweep, -math.pi / 2)
    h = 1 - math.sqrt(0.5)
    assert close(a.mid, (h, h))


@pytest.mark.parametrize("bulge, centre, mid_y", [(1.0, (1, 0), -1.0), (-1.0, (1, 0), 1.0)])
def test_half_arcs_bow_right_for_positive_and_left_for_negative(bulge, centre, mid_y):
    a = ol.arc_from_bulge((0, 0), (2, 0), bulge)
    assert close(a.c, centre) and math.isclose(a.r, 1.0)
    assert math.isclose(abs(a.sweep), math.pi) and math.copysign(1, a.sweep) == bulge
    assert close(a.mid, (1, mid_y))


def test_three_quarter_arc_from_a_bulge_above_one():
    b = math.tan(3 * math.pi / 8)                        # 270 degrees
    a = ol.arc_from_bulge((0, 0), (2, 0), b)
    assert close(a.c, (1, -1)) and math.isclose(a.r, math.sqrt(2))
    assert math.isclose(a.sweep, 1.5 * math.pi)
    assert close(a.mid, (1, -1 - math.sqrt(2)))          # the far side, right of +X


@pytest.mark.parametrize("bulge", [0.1, T8, 1.0, 2.4142, 9.0, -0.3, -1.0, -3.0])
def test_bulge_round_trips_through_start_mid_end(bulge):
    a = ol.arc_from_bulge((3, -2), (7.5, 4), bulge)
    back = ol.arc_from_three_points(a.start, a.mid, a.end)
    assert close(back.c, a.c, 1e-9) and math.isclose(back.r, a.r, rel_tol=1e-12)
    assert math.isclose(back.sweep, a.sweep, rel_tol=1e-12)
    assert math.isclose(back.bulge, bulge, rel_tol=1e-9)


def test_three_collinear_points_are_not_an_arc():
    with pytest.raises(ValueError, match="collinear"):
        ol.arc_from_three_points((0, 0), (1, 1), (2, 2))


def test_primitive_distance_exact_cases():
    import outline_geom as og
    right = og.Arc((0, 0), 1, -math.pi / 4, math.pi / 2)          # faces +X
    left = og.Arc((5, 0), 1, 3 * math.pi / 4, math.pi / 2)        # faces -X
    assert math.isclose(og.prim_distance(right, left), 3.0)       # (1,0) to (4,0)
    inner = og.Arc((0, 0), 0.4, -0.2, 0.4)
    assert math.isclose(og.prim_distance(right, inner), 0.6)      # concentric
    above = og.Line((-10, 5), (10, 5))
    top = og.Arc((0, 0), 2, 0, math.pi)
    assert math.isclose(og.prim_distance(above, top), 3.0)        # interior to interior
    assert og.intersections(og.Line((0, 0), (4, 0)), og.Line((2, 0), (6, 0))) == \
        [(2.0, 0.0), (4.0, 0.0)]                                  # collinear overlap


def test_primitive_distance_matches_brute_force_sampling():
    import outline_geom as og
    rng = random.Random(42)

    def rand_prim():
        if rng.random() < 0.4:
            return og.Line((rng.uniform(-5, 5), rng.uniform(-5, 5)),
                           (rng.uniform(-5, 5), rng.uniform(-5, 5)))
        return og.Arc((rng.uniform(-4, 4), rng.uniform(-4, 4)), rng.uniform(0.3, 3),
                      rng.uniform(-math.pi, math.pi), rng.choice((1, -1)) * rng.uniform(0.2, 6))

    for _ in range(40):
        p, q = rand_prim(), rand_prim()
        exact = og.prim_distance(p, q)
        pts_p = [p.point_at(i / 150) for i in range(151)]
        pts_q = [q.point_at(i / 150) for i in range(151)]
        brute = min(math.dist(a, b) for a in pts_p for b in pts_q)
        assert exact <= brute + 1e-9 and brute - exact <= 0.15, (p, q, exact, brute)


def test_loop_area_counts_the_arc_cap():
    # 2 x 2 square whose right edge is a half circle bowing out to x = 3
    o = ol.Outline(ol.Loop([(0, 0), (2, 0, 1.0), (2, 2), (0, 2)]))
    assert math.isclose(ol.area(o), 4 + math.pi / 2)
    assert ol.contains(o, (2.9, 1.0)) and not ol.contains(o, (2.9, 0.2))
    assert close(ol.bbox(o), (0, 0, 3, 2))


def test_outline_orients_outer_ccw_and_cutouts_cw_preserving_geometry():
    cw_outer = ol.Loop([(0, 0), (0, 2), (2, 2, -1.0), (2, 0)])   # same shape, reversed
    ccw_hole = ol.Loop([(0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)])
    o = ol.Outline(cw_outer, (ccw_hole,))
    assert ol.ring_signed_area(o.outer) > 0 > ol.ring_signed_area(o.cutouts[0])
    assert math.isclose(ol.area(o), 4 + math.pi / 2 - 0.25)
    assert ol.contains(o, (2.9, 1.0)) and not ol.contains(o, (0.75, 0.75))
    # the reversed loop carries the arc on the same edge with the bulge negated
    assert any(math.isclose(v[2], 1.0) for v in o.outer.vertices)


def test_flatten_keeps_every_chord_within_the_error_and_no_finer():
    o = ol.Outline(ol.Circle(0, 0, 10))
    (ring,) = ol.flatten(o, 0.05)
    n = len(ring)
    sag = 10 * (1 - math.cos(math.pi / n))
    assert sag <= 0.05 + 1e-12
    assert 10 * (1 - math.cos(math.pi / (n - 1))) > 0.05        # n - 1 chords would not do
    assert all(math.isclose(math.hypot(*p), 10) for p in ring)   # vertices on the arc
    with pytest.raises(ValueError):
        ol.flatten(o, 0)


def test_float_noise_bulge_is_a_straight_edge():
    o = ol.from_board_block({"outline": {"loop": [[0, 0], [60, 0, 1e-13], [60, 40], [0, 40]]}})
    assert [p["kind"] for p in ol.to_kicad_primitives(o)] == ["line"] * 4
    assert math.isclose(ol.area(o), 2400) and ol.to_board_block(o)["outline"]["loop"][1] == [60, 0]


def test_loop_drops_an_explicit_closing_vertex_and_rejects_zero_length_edges():
    lp = ol.Loop([[0, 0], [5, 0], [5, 5], [0, 0]])
    assert len(lp.vertices) == 3
    with pytest.raises(ol.OutlineError, match="coincide"):
        ol.Loop([[0, 0], [5, 0], [5, 0], [5, 5]])
    with pytest.raises(ol.OutlineError, match=r"\[x, y\]"):
        ol.Loop([[0, 0], [1]])


# ---------------------------------------------------------------------------
# from_board_block / to_board_block
# ---------------------------------------------------------------------------
def test_board_block_rectangle_and_rounded_rectangle():
    assert math.isclose(ol.area(ol.from_board_block({"w": 60, "h": 40})), 2400)
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 3})
    assert math.isclose(ol.area(o), 2400 - (4 - math.pi) * 9)
    assert close(ol.bbox(o), (0, 0, 60, 40))
    stadium = ol.from_board_block({"w": 30, "h": 10, "corner_radius": 5})
    assert math.isclose(ol.area(stadium), 20 * 10 + math.pi * 25)


def test_board_block_with_cutouts_and_datum():
    o = rr()
    assert math.isclose(ol.area(o), RR_AREA)
    assert o.datum is None
    assert ol.from_board_block({**RR_BLOCK, "datum": [30, 20]}).datum == (30.0, 20.0)


def test_board_block_from_the_schema_example():
    # tests/test_netlist_schema.py _full_design(): a 90 degree arc bows the
    # bottom edge out below y = 40, a stadium slot and a dia-3.2 hole
    block = {"outline": {"loop": [[0, 0], [60, 0], [60, 40, 0.4142], [0, 40]]},
             "cutouts": [{"loop": [[20, 10], [30, 10, 1.0], [30, 20], [20, 20, 1.0]]},
                         {"circle": [45.0, 20.0, 1.6]}],
             "datum": [30.0, 20.0]}
    o = ol.from_board_block(block)
    th = 4 * math.atan(0.4142)
    r = 60 / (2 * math.sin(th / 2))
    sag = r * (1 - math.cos(th / 2))
    cap = r * r / 2 * (th - math.sin(th))
    stadium = 10 * 10 + math.pi * 25
    assert close(ol.bbox(o), (0, 0, 60, 40 + sag), 1e-9)
    assert math.isclose(ol.area(o), 2400 + cap - stadium - math.pi * 1.6 ** 2)
    assert not ol.contains(o, (33, 15)) and ol.contains(o, (36, 15))


@pytest.mark.parametrize("block, fragment", [
    ({"outline": {"dxf": "mech/outline.dxf", "layer": "OUTLINE"}}, "dxf_io"),
    ({"outline": {"loop": [[5, 5], [65, 5], [65, 45], [5, 45]]}}, "translate"),
    ({"w": 50, "h": 40, "outline": {"loop": [[0, 0], [60, 0], [60, 40], [0, 40]]}},
     "contradict"),
    ({"corner_radius": 2, "outline": {"loop": [[0, 0], [60, 0], [60, 40], [0, 40]]}},
     "corner_radius"),
    ({"w": 60, "h": 40, "corner_radius": 25}, "corner_radius"),
    ({"w": 60}, "w and h"),
    ({}, "w and h"),
    ({"w": -1, "h": 4}, "positive"),
    ({"w": 60, "h": 40, "cutouts": [{"circle": [70, 20, 2]}]}, "outside"),
    ({"w": 60, "h": 40, "cutouts": [{"circle": [20, 20, 3]}, {"circle": [23, 20, 2]}]},
     "overlap"),
    ({"outline": {"loop": [[0, 0], [10, 10], [10, 0], [0, 10]]}}, "intersect"),
    ({"w": 60, "h": 40, "cutouts": [{"slot": [1, 2]}]}, "loop' or 'circle"),
    ({"w": 60, "h": 40, "cutouts": {"circle": [5, 5, 1]}}, "must be a list"),
    ({"w": 60, "h": 40, "datum": [1, 2, 3]}, "datum"),
])
def test_board_block_errors_say_what_is_wrong(block, fragment):
    with pytest.raises(ol.OutlineError, match=fragment):
        ol.from_board_block(block)


def test_board_block_round_trip():
    o = ol.from_board_block({**RR_BLOCK, "datum": [12.5, -3]})
    block = ol.to_board_block(o)
    assert block["w"] == 60 and block["h"] == 40 and block["datum"] == [12.5, -3.0]
    json.dumps(block)                                     # JSON-ready
    back = ol.from_board_block(block)
    assert same_geometry(back, o) and back.datum == o.datum


def test_board_block_refuses_an_outline_off_the_board_frame():
    with pytest.raises(ol.OutlineError, match="translate it by"):
        ol.to_board_block(ol.translate(rr(), 5, 0))


def test_full_circle_ends_exactly_where_it_starts():
    # sin(a0 + 2*pi) != sin(a0) in floats: a computed end point would give the
    # circle a 4e-16 mm chord that the even-odd test counts as a crossing
    for sweep in (2 * math.pi, -2 * math.pi):
        c = ol.Arc((3.5, 3.5), 1.6, 0.0, sweep)
        assert c.end == c.start
    assert ol.arc_from_three_points((5.1, 3.5), (1.9, 3.5), (5.1, 3.5)).end == (5.1, 3.5)


def test_holes_on_one_line_are_disjoint_not_nested():
    # four mounting holes, two by two at the same centre y (a probe at exactly
    # a circle's centre y used to toggle the even-odd parity)
    holes = [[3.5, 3.5, 1.6], [56.5, 3.5, 1.6], [3.5, 36.5, 1.6], [56.5, 36.5, 1.6]]
    o = ol.from_board_block({"w": 60, "h": 40, "cutouts": [{"circle": h} for h in holes]})
    assert math.isclose(ol.area(o), 2400 - 4 * math.pi * 1.6 ** 2)
    assert ol.contains(o, (30, 3.5)) and not ol.contains(o, (56.5, 3.5))
    assert ol.contains(o, (58.2, 3.5)) and ol.contains(o, (1.8, 3.5))


@pytest.mark.parametrize("p, inside", [
    ((11, 0), False), ((9, 0), False), ((11, 1), False), ((11, 2.5), True), ((7, 0.5), True)])
def test_contains_probe_level_with_an_arc_chord(p, inside):
    # a half-circle bite (centre (10, 0), R2) into the y = 0 edge: (11, 0) and
    # (9, 0) lie exactly on the bite's chord; the even-odd test must judge them
    # with the same tie-break as the ray crossings (they used to come out inside)
    o = ol.Outline(ol.Loop([(0, 0), (8, 0, -1.0), (12, 0), (20, 0), (20, 10), (0, 10)]))
    assert ol.contains(o, p) is inside


def test_contains_round_holes_drawn_as_two_half_circle_bulges_at_any_angle():
    # a DXF LWPOLYLINE circle: two bulge-1 halves whose chord is a diameter at
    # an arbitrary angle. Every probe on that chord is in the hole; with the
    # chord crossing and the cap test rounded separately ~4% of them used to
    # flip the parity and land on the board
    rng = random.Random(1)
    wrong = []
    for _ in range(500):
        a = rng.uniform(0, 2 * math.pi)
        cx, cy, r = rng.uniform(10, 30), rng.uniform(8, 22), rng.uniform(0.5, 3)
        p0 = (cx + r * math.cos(a), cy + r * math.sin(a))
        p1 = (cx - r * math.cos(a), cy - r * math.sin(a))
        o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [
            {"loop": [[p0[0], p0[1], 1.0], [p1[0], p1[1], 1.0]]}]})
        probes = [(cx, cy)] + [(p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))
                               for t in (0.1, 0.3, 0.5, 0.7, 0.9)]
        wrong += [q for q in probes if ol.contains(o, q)]
    assert wrong == []


def test_contains_probe_on_a_chord_whose_ends_differ_by_one_ulp():
    # the stadium hole inset() grew from a slot at float coordinates: its end
    # arcs' chords run from x to x + 1 ulp, with bulges a hair under 1. A
    # probe exactly on such a chord (the slot's own corner) is in the hole
    x, x2 = 3.8900860080471538, 3.890086008047154
    b = -0.9999999999999999
    hole = ol.Loop([(x, 9.53353591483795, 0.0), (10.68981891285907, 9.53353591483795, b),
                    (10.68981891285907, 6.691043576534912, 0.0), (x2, 6.691043576534912, b)])
    o = ol.Outline(ol.Loop([(0, 0), (20, 0), (20, 20), (0, 20)]), (hole,))
    for y in (6.9, 7.141043576534912, 8.0, 9.08353591483795, 9.3):
        for xq in (x, x2, 10.68981891285907):
            assert not ol.contains(o, (xq, y)), (xq, y)
    assert ol.contains(o, (x - 1.5, 8.0)) and ol.contains(o, (10.68981891285907 + 1.5, 8.0))


def test_board_block_of_a_round_board():
    o = ol.translate(ol.Outline(ol.Circle(0, 0, 10)), 10, 10)
    back = ol.from_board_block(ol.to_board_block(o))
    assert math.isclose(ol.area(back), math.pi * 100)
    assert close(ol.bbox(back), (0, 0, 20, 20))


# ---------------------------------------------------------------------------
# 2. contains / distance / edges / inset
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("p, inside", [
    ((30, 10), True), ((30, 20), False), ((15, 20), False), ((0.5, 0.5), False),
    ((1.5, 1.5), True), ((61, 20), False), ((-1, 20), False), ((25, 20), True)])
def test_contains_rounded_rect_with_cutouts(p, inside):
    assert ol.contains(rr(), p) is inside


def test_contains_on_the_edge_and_with_clearance():
    o = rr()
    assert ol.contains(o, (0, 20)) and not ol.contains(o, (0, 20), clearance=0.1)
    assert ol.contains(o, (30, 15.5), clearance=0.4)
    assert not ol.contains(o, (30, 15.5), clearance=0.6)       # 0.5 mm from the hole


@pytest.mark.parametrize("p, d", [
    ((30, 10), 6.0), ((2, 2), 5 - math.sqrt(18)), ((15, 10), 5.0),
    ((22, 27), math.sqrt(8)), ((30, 20), 4.0), ((70, 20), 10.0)])
def test_distance_to_edge_rounded_rect_with_cutouts(p, d):
    assert math.isclose(ol.distance_to_edge(rr(), p), d, abs_tol=1e-12)


def test_nearest_edge_gives_the_outward_normal():
    o = rr()
    hit = ol.nearest_edge(o, (30, 10))
    assert hit.ring == 1 and close(hit.point, (30, 16)) and math.isclose(hit.distance, 6)
    assert close(hit.normal, (0, 1))                     # out of the board = into the hole
    top = ol.nearest_edge(o, (30, 2))
    assert top.ring == 0 and close(top.point, (30, 0)) and close(top.normal, (0, -1))
    corner = ol.nearest_edge(o, (2, 2))                  # on the R5 corner arc
    s = math.sqrt(0.5)
    assert close(corner.normal, (-s, -s)) and close(corner.point, (5 - 5 * s, 5 - 5 * s))
    lsh = ol.nearest_edge(l_shape(), (15, 15))           # the L's inner vertex
    assert close(lsh.point, (20, 20)) and close(lsh.normal, (s, s))


def test_edges_lists_every_primitive_with_its_ring():
    es = ol.edges(rr())
    assert [e.ring for e in es] == [0] * 8 + [1] + [2] * 4
    assert [e.kind for e in es if e.ring == 0].count("arc") == 4
    assert es[8].kind == "circle"


def test_ray_to_edge_finds_the_first_crossing():
    o = rr()
    up = ol.ray_to_edge(o, (30, 10), (0, -1))
    assert math.isclose(up.distance, 10) and close(up.point, (30, 0)) and up.ring == 0
    down = ol.ray_to_edge(o, (30, 10), (0, 3))           # direction need not be unit
    assert math.isclose(down.distance, 6) and down.ring == 1
    assert ol.ray_to_edge(o, (100, 100), (1, 0)) is None


@pytest.mark.parametrize("rect, clearance, ok", [
    ((22, 2, 28, 12), 0.0, True),
    ((22, 2, 28, 12), 1.5, True),
    ((22, 2, 28, 12), 2.5, False),          # 2 mm from the top edge
    ((22, 2, 28, 18), 0.0, False),          # runs into the dia-8 hole
    ((0.2, 0.2, 4, 4), 0.0, False),         # pokes out through the R5 corner
    ((12, 17, 18, 23), 0.0, False),         # wholly inside the square cutout
    ((5, 12, 25, 28), 0.0, False),          # wholly encloses the square cutout
    ((0, 10, 60, 12), 0.0, True),           # touches both side edges
])
def test_contains_rect(rect, clearance, ok):
    assert ol.contains_rect(rr(), rect, clearance) is ok


def test_contains_polygon_non_convex_l():
    o = l_shape()
    assert ol.contains_polygon(o, [(2, 2), (50, 2), (50, 18), (18, 18), (18, 45), (2, 45)])
    # a triangle whose vertices are all inside but whose edge cuts the notch
    assert not ol.contains_polygon(o, [(5, 45), (55, 15), (5, 15)])
    assert not ol.contains_polygon(o, [(2, 2), (50, 2), (50, 18)], clearance=2.5)
    with pytest.raises(ValueError, match="3 points"):
        ol.contains_polygon(o, [(1, 1), (2, 2)])


def test_off_origin_outline_queries_translate_exactly():
    dx, dy = 100.0, -37.5
    o = ol.translate(rr(), dx, dy)
    assert close(ol.bbox(o), (dx, dy, 60 + dx, 40 + dy))
    assert ol.contains(o, (30 + dx, 10 + dy)) and not ol.contains(o, (30 + dx, 20 + dy))
    assert math.isclose(ol.distance_to_edge(o, (30 + dx, 10 + dy)), 6)
    assert math.isclose(ol.area(ol.inset(o, 1)), ol.area(ol.inset(rr(), 1)), abs_tol=1e-9)
    assert ol.contains_rect(o, (22 + dx, 2 + dy, 28 + dx, 12 + dy), 1.5)
    lo = ol.translate(l_shape(), -250, 80)
    assert math.isclose(ol.distance_to_edge(lo, (-235, 95)), math.sqrt(50))
    assert math.isclose(ol.area(ol.inset(lo, 2)), 1380 - math.pi, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# 3. largest inscribed axis-aligned rectangle
# ---------------------------------------------------------------------------
def _rect_area(r) -> float:
    return (r[2] - r[0]) * (r[3] - r[1])


def test_largest_inscribed_rect_l_shape():
    o = l_shape()
    assert close(ol.largest_inscribed_rect(o), (0, 0, 60, 20), 1e-3)
    assert close(ol.largest_inscribed_rect(o, clearance=1.0), (1, 1, 59, 19), 1e-3)
    lo = ol.translate(o, -250, 80)
    assert close(ol.largest_inscribed_rect(lo), (-250, 80, -190, 100), 1e-3)


def test_largest_inscribed_rect_rounded_rect_near_the_analytic_optimum():
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 5})
    best = max((50 + 10 * math.cos(f)) * (30 + 10 * math.sin(f))
               for f in (i * (math.pi / 2) / 20000 for i in range(20001)))
    r = ol.largest_inscribed_rect(o)
    assert ol.contains_rect(o, r)
    assert 0.995 * best <= _rect_area(r) <= best + 1e-6


def test_largest_inscribed_rect_avoids_a_cutout():
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 5,
                             "cutouts": [{"circle": [30, 20, 4]}]})
    # beside the hole: right side at x = 26, left corners on the R5 arcs
    best = max((21 + 5 * math.cos(f)) * (30 + 10 * math.sin(f))
               for f in (i * (math.pi / 2) / 20000 for i in range(20001)))
    r = ol.largest_inscribed_rect(o)
    assert ol.contains_rect(o, r)
    assert 0.98 * best <= _rect_area(r) <= best + 1e-6


def test_largest_inscribed_rect_none_when_clearance_eats_the_board():
    assert ol.largest_inscribed_rect(ol.from_board_block({"w": 4, "h": 4}), clearance=2.5) \
        is None


# ---------------------------------------------------------------------------
# 4. KiCad primitives round trip
# ---------------------------------------------------------------------------
def test_kicad_primitives_are_exact_lines_arcs_and_circles():
    prims = ol.to_kicad_primitives(rr())
    assert [p["kind"] for p in prims] == ["line", "arc"] * 4 + ["circle"] + ["line"] * 4
    assert {tuple(p) for p in prims} == {("kind", "ring", "start", "mid", "end", "center",
                                          "radius")}
    for p in prims:
        if p["kind"] == "arc":
            c, r = p["center"], p["radius"]
            assert math.isclose(r, 5)
            for k in ("start", "mid", "end"):
                assert math.isclose(math.dist(p[k], c), r)
            assert math.isclose(math.dist(p["start"], p["mid"]), math.dist(p["mid"], p["end"]))
        elif p["kind"] == "circle":
            assert p["center"] == (30, 20) and p["radius"] == 4 and p["start"] is None
        else:
            assert p["mid"] is None and p["center"] is None and p["radius"] is None
    # consecutive primitives share their end points exactly (KiCad chains them)
    ring0 = [p for p in prims if p["ring"] == 0]
    for a, b in zip(ring0, ring0[1:] + ring0[:1]):
        assert a["end"] == b["start"]


def test_kicad_primitives_round_trip_to_the_same_geometry():
    for o in (rr(), l_shape(), ol.inset(rr(), 1), ol.inset(l_shape(), 2),
              ol.translate(rr(), 147.3, 52.1)):
        prims = json.loads(json.dumps(ol.to_kicad_primitives(o)))
        back = ol.from_kicad_primitives(prims)
        assert same_geometry(back, o), o


def test_kicad_primitives_chain_in_any_order_and_direction():
    o = ol.translate(rr(), 147.3, 52.1)
    prims = [dict(p) for p in ol.to_kicad_primitives(o)]
    random.Random(7).shuffle(prims)
    for p in prims[::3]:                                 # KiCad may store either way round
        if p["kind"] in ("line", "arc"):
            p["start"], p["end"] = p["end"], p["start"]
    back = ol.from_kicad_primitives(prims, datum=(150, 60))
    assert same_geometry(back, o, ordered=False) and back.datum == (150, 60)


def test_kicad_primitives_open_chain_is_refused():
    prims = [p for p in ol.to_kicad_primitives(l_shape())][:-1]
    with pytest.raises(ol.OutlineError, match="open"):
        ol.from_kicad_primitives(prims)


# ---------------------------------------------------------------------------
# normalize_external (Fusion / DXF, Y up)
# ---------------------------------------------------------------------------
def test_normalize_external_flips_y_and_keeps_the_datum():
    # DXF frame, Y up: a 10 x 10 square with its right edge a half circle
    # bowing out to x = 15, a dia-2 hole, origin at the square's lower-left
    loops = [[[0, 0], [10, 0, 1.0], [10, 10], [0, 10]], {"circle": [5, 5, 1]}]
    o, datum = ol.normalize_external(loops, y_up=True)
    assert close(datum, (0, 10)) and close(o.datum, (0, 10))
    assert close(ol.bbox(o), (0, 0, 15, 10))
    assert math.isclose(ol.area(o), 100 + math.pi * 25 / 2 - math.pi)
    # a DXF point maps through the datum
    p = ol.external_to_board((12, 3), datum)
    assert close(p, (12, 7)) and ol.contains(o, p)
    assert close(ol.board_to_external(p, datum), (12, 3))
    assert not ol.contains(o, ol.external_to_board((5, 5), datum))      # in the hole


def test_normalize_external_picks_the_largest_loop_as_outer_and_translates():
    loops = [{"loop": [[-40, -20], [-30, -20], [-30, -10], [-40, -10]]},
             [[-50, -30], [50, -30], [50, 30], [-50, 30]]]
    o, datum = ol.normalize_external(loops, y_up=False)
    assert close(datum, (50, 30)) and close(ol.bbox(o), (0, 0, 100, 60))
    assert len(o.cutouts) == 1 and math.isclose(ol.area(o), 6000 - 100)
    assert ol.from_board_block(ol.to_board_block(o)).datum == (50, 30)


# ---------------------------------------------------------------------------
# 5. stdlib only
# ---------------------------------------------------------------------------
def _top_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative import"
            names.add((node.module or "").split(".")[0])
    return names


def test_outline_modules_import_only_the_stdlib():
    for name in OUTLINE_MODULES:
        path = TOOLS / f"{name}.py"
        bad = {m for m in _top_level_imports(path)
               if m not in sys.stdlib_module_names and m not in OUTLINE_MODULES}
        assert not bad, f"{path.name} imports non-stdlib {sorted(bad)}"
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 800, path.name


def test_outline_runs_with_no_site_packages_at_all():
    """-I -S: no site-packages, no PYTHONPATH, no user site. Every module the
    import pulls in must be stdlib or one of ours."""
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "before = set(sys.modules)\n"
        "import outline as ol\n"
        f"o = ol.from_board_block({json.dumps(RR_BLOCK)})\n"
        "r = ol.largest_inscribed_rect(ol.inset(o, 0.5))\n"
        "probs = ol.validate_fab(o)\n"
        "ours = set(" + repr(list(OUTLINE_MODULES)) + ")\n"
        "new = [m for m in set(sys.modules) - before if m.split('.')[0] not in "
        "sys.stdlib_module_names and m.split('.')[0] not in ours]\n"
        "print(json.dumps({'area': ol.area(o), 'rect': r, 'n': len(probs), 'bad': new}))\n")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["bad"] == [] and math.isclose(out["area"], RR_AREA)
    assert out["rect"] is not None and out["n"] == 4


@pytest.fixture(scope="module")
def kicad9_python() -> Path:
    """KiCad 9's bundled Python (3.11, no site extras) — what SWIG scripts use."""
    import kicad_env as env
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return inst.python
    pytest.skip("no KiCad 9 bundled Python (the SWIG-side importer of outline.py)")


def test_live_outline_imports_under_kicad9_bundled_python(kicad9_python, tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        # the KiCad-python rule: silence wx asserts/log dialogs before anything
        "import wx; wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())\n"
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "before = set(sys.modules)\n"
        "import outline as ol\n"
        "o = ol.from_board_block(json.loads(sys.argv[2]))\n"
        "ins = ol.inset(o, 1.0)\n"
        "new = sorted(m for m in set(sys.modules) - before\n"
        "             if m.split('.')[0] not in sys.stdlib_module_names\n"
        "             and not m.startswith('outline'))\n"
        "print(json.dumps({'py': sys.version_info[:2], 'area': ol.area(ins), 'new': new,\n"
        "                  'prims': len(ol.to_kicad_primitives(ins))}))\n",
        encoding="utf-8")
    proc = subprocess.run([str(kicad9_python), str(child), str(TOOLS), json.dumps(RR_BLOCK)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=LIVE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["new"] == []
    assert math.isclose(out["area"], ol.area(ol.inset(rr(), 1.0)))
    assert out["prims"] == len(ol.to_kicad_primitives(ol.inset(rr(), 1.0)))


KICAD_DRAW_CHILD = '''\
import wx; wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())
import json, sys
import pcbnew
prims = json.loads(open(sys.argv[1], encoding="utf-8").read())
mm = lambda v: pcbnew.FromMM(float(v))
V = lambda p: pcbnew.VECTOR2I(mm(p[0]), mm(p[1]))
board = pcbnew.CreateEmptyBoard()
for p in prims:
    s = pcbnew.PCB_SHAPE(board)
    if p["kind"] == "line":
        s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetStart(V(p["start"])); s.SetEnd(V(p["end"]))
    elif p["kind"] == "arc":
        s.SetShape(pcbnew.SHAPE_T_ARC); s.SetArcGeometry(V(p["start"]), V(p["mid"]), V(p["end"]))
    else:
        s.SetShape(pcbnew.SHAPE_T_CIRCLE); s.SetCenter(V(p["center"]))
        s.SetEnd(V((p["center"][0] + p["radius"], p["center"][1])))
    s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(mm(0.1)); board.Add(s)
sp = pcbnew.SHAPE_POLY_SET()
try:
    ok = board.GetBoardPolygonOutlines(sp)
except TypeError:                      # 10.0 added aInferOutlineIfNecessary
    ok = board.GetBoardPolygonOutlines(sp, False)
pcbnew.SaveBoard(sys.argv[2], board)
to_mm = lambda v: [pcbnew.ToMM(v.x), pcbnew.ToMM(v.y)]
back = []
for d in pcbnew.LoadBoard(sys.argv[2]).GetDrawings():
    sh = d.GetShape()
    if sh == pcbnew.SHAPE_T_SEGMENT:
        back.append({"kind": "line", "start": to_mm(d.GetStart()), "end": to_mm(d.GetEnd())})
    elif sh == pcbnew.SHAPE_T_ARC:
        back.append({"kind": "arc", "start": to_mm(d.GetStart()), "mid": to_mm(d.GetArcMid()),
                     "end": to_mm(d.GetEnd())})
    elif sh == pcbnew.SHAPE_T_CIRCLE:
        back.append({"kind": "circle", "center": to_mm(d.GetCenter()),
                     "radius": pcbnew.ToMM(d.GetRadius())})
print(json.dumps({"ok": bool(ok), "area": sp.Area() / 1e12, "outlines": sp.OutlineCount(),
                  "holes": sp.HoleCount(0) if sp.OutlineCount() else -1, "back": back}))
'''


def test_live_kicad9_reads_the_primitives_as_the_same_outline(kicad9_python, tmp_path):
    """Exact arcs through pcbnew (SetArcGeometry start/mid/end): KiCad's own
    board polygon has our area, and its saved shapes chain back to our outline."""
    o = ol.translate(ol.from_board_block({**RR_BLOCK, "cutouts": RR_BLOCK["cutouts"] + [
        {"loop": [[40, 10], [50, 10, 1.0], [50, 14], [40, 14, 1.0]]}]}), 120.5, 80.25)
    src = tmp_path / "prims.json"
    src.write_text(json.dumps(ol.to_kicad_primitives(o)), encoding="utf-8")
    child = tmp_path / "draw.py"
    child.write_text(KICAD_DRAW_CHILD, encoding="utf-8")
    proc = subprocess.run([str(kicad9_python), str(child), str(src),
                           str(tmp_path / "rt.kicad_pcb")], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] and out["outlines"] == 1 and out["holes"] == 3
    assert math.isclose(out["area"], ol.area(o), abs_tol=0.05)      # KiCad tessellates arcs
    back = ol.from_kicad_primitives(out["back"])
    # KiCad keeps arc mid points to 1 nm: bulges return within ~1e-6, area ~1e-5
    assert same_geometry(back, o, tol=1e-4, ordered=False)


"""inset() (server/tools/outline_inset.py via outline.py): the board shrunk by d mm.

The contract checked on every shape: a point is inside inset(o, d) exactly
when it is on o at least d from every edge, and every point of the inset's
boundary lies exactly d from the original edges. Expected values are set by
the test geometry (hand-placed arc centres, closed-form areas), never read
back from the module.
"""
from __future__ import annotations

import math
import random

import pytest

import outline as ol

T8 = math.tan(math.pi / 8)          # bulge of a 90 degree arc
L_LOOP = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]
RR_BLOCK = {"w": 60, "h": 40, "corner_radius": 5,
            "cutouts": [{"circle": [30, 20, 4]},
                        {"loop": [[10, 15], [20, 15], [20, 25], [10, 25]]}]}


def blk(loop, cutouts=()) -> ol.Outline:
    return ol.from_board_block({"outline": {"loop": loop}, "cutouts": list(cutouts)})


def rr() -> ol.Outline:
    return ol.from_board_block(RR_BLOCK)


def l_shape() -> ol.Outline:
    return blk(L_LOOP)


def close(a, b, tol=1e-9) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def arcs(o: ol.Outline, ring: int | None = None) -> list[dict]:
    return [p for p in ol.to_kicad_primitives(o)
            if p["kind"] == "arc" and (ring is None or p["ring"] == ring)]


def centres(o: ol.Outline, ring: int | None = None) -> list[tuple[float, float]]:
    """Sorted arc centres, rounded to 1e-9 mm (KiCad arcs are rebuilt from bulges)."""
    return sorted((round(p["center"][0], 9) + 0.0, round(p["center"][1], 9) + 0.0)
                  for p in arcs(o, ring))


def assert_exact_inset(o: ol.Outline, d: float, windows=(), n: int = 1500,
                       tol: float = 1e-7) -> ol.Outline:
    """inset(o, d) is the region of o at least d from every edge (random
    points over the whole board plus `windows` (x, y, half-size) around the
    small features; points within `tol` of the inset boundary are skipped),
    and its whole boundary lies d (+- tol) from o."""
    ins = ol.inset(o, d)
    x0, y0, x1, y1 = ol.bbox(o)
    rng = random.Random(f"{x1}-{y1}-{d}-{len(windows)}")
    boxes = [(x0 - 1, y0 - 1, x1 + 1, y1 + 1)] + [(x - h, y - h, x + h, y + h)
                                                  for x, y, h in windows]
    checked = 0
    for bx0, by0, bx1, by1 in boxes:
        for _ in range(n):
            p = (rng.uniform(bx0, bx1), rng.uniform(by0, by1))
            if ol.distance_to_edge(ins, p) < tol:
                continue
            assert ol.contains(ins, p) is ol.contains(o, p, clearance=d), (d, p)
            checked += 1
    assert checked > 0.9 * n * len(boxes)
    for ring in ol.flatten(ins, 0.01):
        for p in ring:
            assert math.isclose(ol.distance_to_edge(o, p), d, abs_tol=tol), (d, p)
    return ins


# ---------------------------------------------------------------------------
# exact offsets on the basic shapes
# ---------------------------------------------------------------------------
def test_inset_rounded_rect_keeps_exact_arcs():
    ins = ol.inset(rr(), 1.0)
    outer_area = 58 * 38 - (4 - math.pi) * 16
    square = 12 * 12 - (4 - math.pi) * 1                 # grown with R1 corners
    assert math.isclose(ol.area(ins), outer_area - math.pi * 25 - square, abs_tol=1e-9)
    assert ins.cutouts[0] == ol.Circle(30, 20, 5)
    outer = arcs(ins, 0)
    assert sorted((round(p["center"][0], 9), round(p["center"][1], 9)) for p in outer) == \
        [(5, 5), (5, 35), (55, 5), (55, 35)]
    assert all(math.isclose(p["radius"], 4) for p in outer)
    grown = arcs(ins, 2)
    assert len(grown) == 4 and all(math.isclose(p["radius"], 1) for p in grown)
    for ring in ol.flatten(ins, 0.01):                   # every point exactly d from the edge
        for p in ring:
            assert math.isclose(ol.distance_to_edge(rr(), p), 1.0, abs_tol=1e-9)


def test_inset_beyond_the_corner_radius_gives_sharp_corners():
    ins = ol.inset(ol.from_board_block({"w": 20, "h": 10, "corner_radius": 1}), 2)
    assert math.isclose(ol.area(ins), 16 * 6)
    assert [p["kind"] for p in ol.to_kicad_primitives(ins)] == ["line"] * 4
    assert close(ol.bbox(ins), (2, 2, 18, 8))


def test_inset_non_convex_l_rounds_the_inner_corner():
    ins = ol.inset(l_shape(), 2)
    assert math.isclose(ol.area(ins), 56 * 16 + 16 * 30 + 4 - math.pi, abs_tol=1e-9)
    (join,) = arcs(ins)
    assert close(join["center"], (20, 20)) and math.isclose(join["radius"], 2)
    assert ol.contains(ins, (17.5, 17.5)) and not ol.contains(ins, (18.8, 18.8))


def test_inset_drops_a_short_edge_between_two_convex_corners():
    # a 0.5 mm chamfer at a corner vanishes in a 2 mm inset: the sharp result
    ins = ol.inset(blk([[0, 0], [20, 0], [20, 9.5], [19.5, 10], [0, 10]]), 2)
    assert len(ins.outer.vertices) == 4
    assert close(ol.bbox(ins), (2, 2, 18, 8), 1e-9)


def test_inset_zero_is_identity_and_negative_is_refused():
    assert ol.inset(rr(), 0) == rr()
    with pytest.raises(ol.OutlineError, match=">= 0"):
        ol.inset(rr(), -1)
    with pytest.raises(ol.OutlineError, match=">= 0"):
        ol.inset(rr(), math.nan)


def test_inset_of_a_round_board_is_a_circle():
    ins = ol.inset(ol.translate(ol.Outline(ol.Circle(0, 0, 10)), 10, 10), 2.5)
    assert ins.outer == ol.Circle(10, 10, 7.5) and ins.cutouts == ()


# ---------------------------------------------------------------------------
# the short-edge cases that used to raise (review round 1)
# ---------------------------------------------------------------------------
def test_inset_jog_shorter_than_d_keeps_the_round_join():
    # 0.2 mm jog between a reflex and a convex corner, d = 0.3: the jog's own
    # offset vanishes and the R0.3 join about (10, 10) meets the y = 9.9 offset
    o = blk([[0, 0], [20, 0], [20, 10], [10, 10], [10, 10.2], [0, 10.2]])
    ins = assert_exact_inset(o, 0.3, windows=[(10, 10, 0.6)])
    (join,) = arcs(ins)
    assert close(join["center"], (10, 10)) and math.isclose(join["radius"], 0.3)
    a = math.sqrt(0.3 ** 2 - 0.1 ** 2)                   # the join meets y = 9.9 here
    assert {tuple(round(v, 9) for v in join[k]) for k in ("start", "end")} == \
        {(10.0, 9.7), (round(10 - a, 9), 9.9)}
    cap = 0.3 * a - (a / 2 * 0.1 + 0.3 ** 2 / 2 * math.asin(a / 0.3))
    assert math.isclose(ol.area(ins), 19.4 * 9.4 + 0.2 * (10 - a - 0.3) + cap, abs_tol=1e-9)


@pytest.mark.parametrize("d", [0.3, 0.5])
def test_inset_step_as_tall_as_d_or_shorter(d):
    # at d = 0.3 the join ends exactly on the other offset (a triple point)
    o = blk([[0, 0], [20, 0], [20, 10], [10, 10], [10, 10.3], [0, 10.3]])
    ins = assert_exact_inset(o, d, windows=[(10, 10, 0.8)])
    (join,) = arcs(ins)
    assert close(join["center"], (10, 10)) and math.isclose(join["radius"], d)


def test_inset_tab_narrower_than_2d_vanishes():
    # a 0.5 x 3 mm tab at d = 0.3: the two R0.3 joins at its base meet
    # each other below it, at x = 10
    o = blk([[0, 3], [9.75, 3], [9.75, 0], [10.25, 0], [10.25, 3], [20, 3], [20, 13], [0, 13]])
    ins = assert_exact_inset(o, 0.3, windows=[(10, 2, 1.5)])
    joins = arcs(ins)
    assert sorted(tuple(p["center"]) for p in joins) == [(9.75, 3), (10.25, 3)]
    assert close(ol.bbox(ins), (0.3, 3 + math.sqrt(0.3 ** 2 - 0.25 ** 2), 19.7, 12.7), 1e-9)


def test_inset_stair_riser_shorter_than_d():
    o = blk([[0, 0], [5, 0], [5, 1], [10, 1], [10, 2], [15, 2], [15, 20], [0, 20]])
    ins = assert_exact_inset(o, 1.2, windows=[(5, 1, 1.5), (10, 2, 1.5)])
    assert centres(ins) == [(5, 1), (10, 2)]


def test_inset_shallow_wide_notch():
    # a 9 x 0.6 mm notch at d = 1: the walls' offsets vanish, the R1 joins
    # at the notch's inner corners meet the y = 1 offset of the edge
    o = blk([[0, 0], [20.5, 0], [20.5, 0.6], [29.5, 0.6], [29.5, 0], [50, 0], [50, 30], [0, 30]])
    ins = assert_exact_inset(o, 1.0, windows=[(20.5, 1, 1.5), (29.5, 1, 1.5)])
    assert centres(ins) == [(20.5, 0.6), (29.5, 0.6)]
    assert not ol.contains(ins, (25, 1.55)) and ol.contains(ins, (25, 1.65))


def test_inset_tab_exactly_2d_wide_drops_the_zero_width_sliver():
    # 0.6 mm tab at d = 0.3: the side offsets coincide (zero-width region)
    o = blk([[0, 3], [9.7, 3], [9.7, 0], [10.3, 0], [10.3, 3], [20, 3], [20, 13], [0, 13]])
    ins = assert_exact_inset(o, 0.3, windows=[(10, 3, 0.8)])
    assert centres(ins) == [(9.7, 3), (10.3, 3)]
    assert ol.bbox(ins)[1] == pytest.approx(3.0)     # the joins touch at (10, 3)


# ---------------------------------------------------------------------------
# cutouts merging, islands, contract errors
# ---------------------------------------------------------------------------
def test_inset_cutout_merging_with_the_edge_becomes_a_bay():
    o = ol.from_board_block({"w": 20, "h": 10, "cutouts": [{"circle": [3, 5, 1.5]}]})
    ins = assert_exact_inset(o, 1.0, windows=[(1, 5, 2)])
    assert ins.cutouts == ()
    (bay,) = arcs(ins)
    assert close(bay["center"], (3, 5)) and math.isclose(bay["radius"], 2.5)


def test_inset_cutouts_merging_with_each_other_become_one_hole():
    o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [{"circle": [15, 15, 3]},
                                                           {"circle": [22, 15, 3]}]})
    ins = assert_exact_inset(o, 1.0, windows=[(18.5, 15, 2)])
    (hole,) = ins.cutouts
    assert isinstance(hole, ol.Loop) and len(hole.vertices) == 2
    assert centres(ins, 1) == [(15, 15), (22, 15)]


def test_inset_concave_corners_and_a_slot_near_the_edge():
    o = ol.from_board_block({"w": 30, "h": 20, "corner_radius": 2, "cutouts": [
        {"loop": [[5, 2.5], [25, 2.5, 1.0], [25, 3.5], [5, 3.5, 1.0]]},
        {"loop": [[18, 9], [22, 9], [22, 17], [14, 17], [14, 13], [18, 13]]}]})
    for d in (0.45, 1.0):
        assert_exact_inset(o, d, windows=[(15, 2, 2), (18, 13, 2)], n=800)


def test_inset_hole_sharing_an_edge_line_with_the_shrunk_board_opens_into_a_bay():
    # the grown square's bottom edge lies exactly on the shrunk board's
    # bottom edge: the zero-width strip between them is dropped
    o = ol.from_board_block({"w": 20, "h": 10, "cutouts": [
        {"loop": [[4, 2], [8, 2], [8, 5], [4, 5]]}]})
    ins = assert_exact_inset(o, 1.0, windows=[(6, 1.5, 2)])
    assert ins.cutouts == () and centres(ins) == [(4, 2), (4, 5), (8, 2), (8, 5)]


def test_inset_hole_tangent_to_the_shrunk_edge_is_taken_as_merged():
    # the grown dia-4 hole touches the y = 1 offset at one point: no simple
    # outline exists, so the contact is resolved as if d were 1 nm larger
    o = ol.from_board_block({"w": 20, "h": 10, "cutouts": [{"circle": [5, 3, 1]}]})
    ins = assert_exact_inset(o, 1.0, windows=[(5, 1, 1)], tol=2e-6)
    assert ins.cutouts == () and centres(ins) == [(5, 3)]


def test_inset_holes_whose_growth_just_touches_merge_into_one():
    o = ol.from_board_block({"w": 30, "h": 10, "cutouts": [{"circle": [10, 5, 1]},
                                                           {"circle": [14, 5, 1]}]})
    ins = assert_exact_inset(o, 1.0, windows=[(12, 5, 1)], tol=2e-6)
    assert len(ins.cutouts) == 1 and centres(ins, 1) == [(10, 5), (14, 5)]


def test_inset_pinched_exactly_at_a_point_is_two_regions():
    # the R2 join round the V-notch tip (10, 6) touches the y = 8 offset of
    # the far edge at (10, 8): the two halves meet in a single point
    o = blk([[0, 0], [9, 0], [10, 6], [11, 0], [20, 0], [20, 10], [0, 10]])
    with pytest.raises(ol.OutlineError, match="2 separate regions"):
        ol.inset(o, 2.0)
    assert_exact_inset(o, 1.9, windows=[(10, 7.5, 1)])


def test_slices_of_one_piece_merge_back_into_one_primitive():
    # a kept piece split where another piece only touches it comes back whole
    import outline_inset as oi
    line = ol.Line((0.0, 0.0), (10.0, 0.0))
    cut = oi._slices(0, line, [(7.0, 0.0), (4.0, 0.0), (4.0 + 1e-9, 0.0)])
    assert [(s.seq, s.nxt) for s in cut] == [(0, 1), (1, 2), (2, -1)]   # 1e-9 apart: one cut
    assert oi._merged(cut) == [line]
    circle = ol.Arc((0.0, 0.0), 2.0, 0.0, 2 * math.pi)                 # cut at 90/180/270 deg
    q = oi._slices(1, circle, [(0.0, -2.0), (0.0, 2.0), (-2.0, 0.0)])
    assert [(s.seq, s.nxt) for s in q] == [(0, 1), (1, 2), (2, 0)]
    assert [round(s.prim.sweep / math.pi, 9) for s in q] == [0.5, 0.5, 1.0]
    (whole,) = oi._merged([q[1], q[2], q[0]])                           # chain starts mid-way
    assert whole.is_circle and whole.start == whole.end == (-2.0, 0.0)
    other = oi._Slice(ol.Line((0.0, 2.0), (0.0, -2.0)), 2, 0, -1)
    first, mid = oi._merged([q[0], other, q[2]])                       # last run wraps to first
    assert first.start == (0.0, -2.0) and first.end == (-2.0, 0.0) and mid is other.prim
    assert math.isclose(first.sweep, 1.5 * math.pi)


DUMBBELL = [[0, 0], [10, 0], [10, 4.5], [14, 4.5], [14, 0], [24, 0], [24, 10], [14, 10],
            [14, 5.5], [10, 5.5], [10, 10], [0, 10]]


def test_inset_keeps_a_neck_wider_than_2d():
    ins = assert_exact_inset(blk(DUMBBELL), 0.4, windows=[(12, 5, 2.5)])
    assert ol.contains(ins, (12, 5))


@pytest.mark.parametrize("d", [0.6, 0.5])          # 0.5: the neck is exactly 2d
def test_inset_that_splits_the_board_into_islands_raises(d):
    with pytest.raises(ol.OutlineError, match="2 separate regions"):
        ol.inset(blk(DUMBBELL), d)


def test_inset_that_consumes_the_board_raises():
    with pytest.raises(ol.OutlineError, match="consumes the whole board"):
        ol.inset(ol.from_board_block({"w": 20, "h": 10}), 6)
    with pytest.raises(ol.OutlineError, match="consumes the whole board"):
        ol.inset(ol.Outline(ol.Circle(0, 0, 2)), 2)


def test_inset_refuses_a_ring_that_doubles_back():
    # a straight edge followed by a tangent arc running back along it (a
    # zero-angle horn): the side the offset belongs on is undefined
    horn = ol.Outline(ol.Loop([(0, 5), (10, 5, T8), (5, 0)]))
    with pytest.raises(ol.OutlineError, match="doubles back"):
        ol.inset(horn, 0.5)


# ---------------------------------------------------------------------------
# property: inside inset(o, d) == on o with clearance d
# ---------------------------------------------------------------------------
def _bite() -> ol.Outline:
    """Concave arc biting into the bottom edge plus a concave R1 inner fillet."""
    return blk([[0, 0, -0.3], [40, 0], [40, 30], [21, 30, -T8], [20, 31], [20, 50], [0, 50]])


def _star() -> ol.Outline:
    """Ten-point star: acute convex tips and reflex notches, off the origin."""
    pts = [(20 + (18 if k % 2 == 0 else 8) * math.cos(k * math.pi / 5),
            20 + (18 if k % 2 == 0 else 8) * math.sin(k * math.pi / 5)) for k in range(10)]
    return ol.Outline(ol.Loop(tuple(pts)))


def _wavy() -> ol.Outline:
    """A DXF-like board: the top edge a 0.25 mm polyline of a 1 mm wave."""
    top = [(40 - k * 0.25, 1 - math.cos(2 * math.pi * (40 - k * 0.25) / 10)) for k in range(161)]
    return ol.Outline(ol.Loop(tuple(top + [(0, 30), (40, 30)])))


def _sine() -> ol.Outline:
    """A 0.2 mm polyline of a sine edge: near its inflections the convex turns
    are ~1e-4 rad, so the offsets overlap there by only d * turn^2 / 4."""
    edge = [(0.2 * k, 1 + math.sin(k / 7)) for k in range(151)]
    return ol.Outline(ol.Loop(tuple(edge + [(30, 20), (0, 20)])))


INSET_SHAPES = {"rr": rr, "l": l_shape, "bite": _bite, "star": _star, "wavy": _wavy,
                "sine": _sine,
                "schema": lambda: ol.from_board_block({
                    "outline": {"loop": [[0, 0], [60, 0], [60, 40, 0.4142], [0, 40]]},
                    "cutouts": [{"loop": [[20, 10], [30, 10, 1.0], [30, 20], [20, 20, 1.0]]},
                                {"circle": [45.0, 20.0, 1.6]}]})}


@pytest.mark.parametrize("d", [0.3, 2.0])
@pytest.mark.parametrize("name", sorted(INSET_SHAPES))
def test_inset_is_exactly_the_region_with_clearance(name, d):
    assert_exact_inset(INSET_SHAPES[name](), d)


# ---------------------------------------------------------------------------
# slots at float coordinates (review round 2): the grown end arcs' chords run
# from x to x + 1 ulp, which used to flip the even-odd parity on the chord
# ---------------------------------------------------------------------------
FLOAT_SLOT = [[3.8900860080471538, 7.141043576534912],
              [10.68981891285907, 7.141043576534912, 1.0],
              [10.68981891285907, 9.08353591483795],
              [3.8900860080471538, 9.08353591483795, 1.0]]


def _stadium(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"loop": [[x0, y0], [x1, y0, 1.0], [x1, y1], [x0, y1, 1.0]]}


@pytest.mark.parametrize("d", [round(0.05 * k, 2) for k in range(1, 30)])
def test_inset_slot_at_float_coordinates(d):
    o = ol.from_board_block({"w": 40, "h": 30, "corner_radius": 1,
                             "cutouts": [{"loop": FLOAT_SLOT}]})
    ins = assert_exact_inset(o, d, windows=[(7.3, 8.1, 3.5)], n=250)
    for v in FLOAT_SLOT:                     # the slot's corners lie in the grown hole
        assert not ol.contains(ins, (v[0], v[1]))


@pytest.mark.parametrize("d, slots", [
    (0.45, [(26.83062897917767, 21.747448609091123, 29.709089963382546, 23.437781570633025),
            (5.352349392100088, 14.919635222656163, 12.874863835580108, 16.88122383503293),
            (3.8900860080471538, 7.141043576534912, 10.68981891285907, 9.08353591483795)]),
    (0.6, [(2.8352419640005517, 6.589731046704808, 7.657426195999019, 8.40158766499579),
           (29.4232734128622, 19.645469604052053, 33.3420194360067, 20.207241102497544),
           (5.874146983835418, 16.891261928700523, 11.373789754552753, 18.82404596227317)])])
def test_inset_several_slots_at_float_coordinates(d, slots):
    # the reviewer's fuzz cases 248 / 362 (the round holes of 248 included)
    extra = ([{"circle": [15.29106823624258, 22.702165356007892, 0.9221962577806146]},
              {"circle": [20.43180005814642, 21.760484265838002, 1.0140030624463638]}]
             if d == 0.45 else [])
    o = ol.from_board_block({"w": 40, "h": 30, "corner_radius": 1,
                             "cutouts": [_stadium(*s) for s in slots] + extra})
    ins = assert_exact_inset(o, d, n=800)
    for x0, y0, x1, y1 in slots:
        for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            assert not ol.contains(ins, corner)

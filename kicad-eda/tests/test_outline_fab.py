"""validate_fab (server/tools/outline_ops.py via outline.py): JLCPCB outline rules.

The limit values are pinned to their citations in FAB_LIMITS["JLCPCB"].sources:
  board 3 x 3 mm, non-plated slot 1.0 mm, NPTH 0.50 mm   - jlcpcb.com capabilities page
  internal corner R0.5 (router bit radius)               - JLCPCB support / JLCPCB blog
  NPTH to board edge 0.25 mm                             - JLCPCB support (forum)
  board web / neck 0.8 mm                                - ASSUMPTION (no published value)
Every expected width / radius below is set by the test geometry itself. A ray
that crosses the wedge of one corner is not a width (judged by the boundary
walked between its ends, never by how the sides are split into edges), while
a width whose narrowest point is a vertex (a V-notch tip, a tooth tip, a hole
corner) is measured from that vertex.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

import outline as ol

T8 = math.tan(math.pi / 8)          # bulge of a 90 degree arc
L_LOOP = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 50], [0, 50]]
STADIUM_08 = {"w": 40, "h": 30, "cutouts": [               # 0.8 mm wide, R0.4 ends
    {"loop": [[10, 15], [20, 15, 1.0], [20, 15.8], [10, 15.8, 1.0]]}]}


def rules(problems: list[dict]) -> list[str]:
    return sorted(p["rule"] for p in problems)


def test_jlcpcb_limits_are_the_cited_values():
    lim = ol.FAB_LIMITS["JLCPCB"]
    assert (lim.min_board_dim, lim.min_slot_width, lim.min_npth_diameter) == (3.0, 1.0, 0.5)
    assert (lim.min_internal_radius, lim.min_hole_to_edge) == (0.5, 0.25)
    assert lim.min_feature_width == 0.8 and lim.assumptions == frozenset({"feature_width"})
    for rule in ("board_size", "slot_width", "hole_size", "internal_corner_radius",
                 "hole_to_edge", "feature_width"):
        assert rule in lim.sources and lim.sources[rule]
    for rule in ("board_size", "slot_width", "hole_size"):
        assert "jlcpcb.com/capabilities/pcb-capabilities" in lim.sources[rule]
    assert "NOT in the capabilities table" in lim.sources["internal_corner_radius"]
    assert "NOT in the capabilities table" in lim.sources["hole_to_edge"]
    assert "ASSUMPTION" in lim.sources["feature_width"]


def test_validate_fab_clean_board_has_no_problems():
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 3,
                             "cutouts": [{"circle": [30, 20, 2]}]})
    assert ol.validate_fab(o) == []
    assert ol.validate_fab(o, "jlcpcb", holes=[(4, 4, 3.2)]) == []


def test_validate_fab_schema_example_board_is_clean():
    o = ol.from_board_block({
        "outline": {"loop": [[0, 0], [60, 0], [60, 40, 0.4142], [0, 40]]},
        "cutouts": [{"loop": [[20, 10], [30, 10, 1.0], [30, 20], [20, 20, 1.0]]},
                    {"circle": [45.0, 20.0, 1.6]}]})
    assert ol.validate_fab(o) == []


def test_validate_fab_board_too_small():
    (p,) = ol.validate_fab(ol.from_board_block({"w": 2.5, "h": 10}))
    assert p["rule"] == "board_size" and p["severity"] == "error"
    assert p["value"] == 2.5 and p["limit"] == 3.0 and "jlcpcb.com" in p["source"]
    assert p["at"] is None and p["assumption"] is False


def test_validate_fab_square_cutout_corners_are_internal_corners():
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 5, "cutouts": [
        {"circle": [30, 20, 4]}, {"loop": [[10, 15], [20, 15], [20, 25], [10, 25]]}]})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["internal_corner_radius"] * 4
    assert {tuple(p["at"]) for p in probs} == {(10, 15), (20, 15), (20, 25), (10, 25)}
    assert all(p["severity"] == "warning" and p["value"] == 0 and p["limit"] == 0.5
               for p in probs)
    # R0.5 bit in a 90 degree corner: 0.5 * (sqrt(2) - 1) = 0.207 mm left behind
    assert all("0.207 mm" in p["message"] for p in probs)


def test_validate_fab_acute_convex_corners_are_not_thin_features():
    trapezoid = ol.from_board_block({"outline": {"loop": [
        [0, 0], [40, 0], [30, 10 * math.sqrt(3)], [0, 10 * math.sqrt(3)]]}})   # 60 deg tip
    assert ol.validate_fab(trapezoid) == []
    bite = ol.from_board_block({"outline": {"loop": [            # 57 deg tips, R1 fillet
        [0, 0, -0.3], [40, 0], [40, 30], [21, 30, -T8], [20, 31], [20, 50], [0, 50]]}})
    assert ol.validate_fab(bite) == []


def test_validate_fab_acute_tip_does_not_depend_on_how_the_side_is_split():
    # the same 60 degree tip, its sides split by collinear vertices (the old
    # adjacent-edge exclusion reported 0.433 mm for the second loop)
    h = 10 * math.sqrt(3)
    for loop in ([[0, 0], [40, 0], [30, h], [0, h]],
                 [[0, 0], [39.5, 0], [40, 0], [30, h], [0, h]],
                 [[0, 0], [20, 0], [39.9, 0], [40, 0], [35, h / 2], [30, h], [0, h]]):
        assert ol.validate_fab(ol.from_board_block({"outline": {"loop": loop}})) == [], loop


def test_validate_fab_polyline_side_at_an_acute_corner_is_not_a_thin_feature():
    # a DXF-like side: 0.2 mm segments of a 0.3 mm wave leaving (40, 0) at a
    # 66 degree convex corner (the old rule saw a 0.179 mm "web" there)
    u = (math.cos(math.radians(114)), math.sin(math.radians(114)))
    side = []
    for k in range(1, 101):
        s, w = 0.2 * k, 0.3 * (1 - math.cos(2 * math.pi * 0.2 * k / 5))
        side.append((40 + s * u[0] - w * u[1], s * u[1] + w * u[0]))
    o = ol.Outline(ol.Loop(tuple([(0, 0), (40, 0)] + side + [(0, side[-1][1])])))
    assert ol.validate_fab(o) == []


def test_validate_fab_thin_finger_is_a_thin_feature():
    # 0.5 x 5 mm finger: its sides are joined through a U turn, not one corner
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [20, 0], [20, 4.75], [25, 4.75], [25, 5.25], [20, 5.25], [20, 10], [0, 10]]}})
    probs = [p for p in ol.validate_fab(o) if p["rule"] == "feature_width"]
    (p,) = probs
    assert math.isclose(p["value"], 0.5, abs_tol=1e-6) and 20 < p["at"][0] < 25


@pytest.mark.parametrize("half_base, judged", [(1.0, False), (0.5, True)])
def test_validate_fab_spike_is_a_corner_unless_it_is_a_sliver(half_base, judged):
    # a 10 mm spike: 2 mm base = an 11.4 degree corner (a wedge, not a width);
    # 1 mm base = a 5.7 degree sliver, sharper than the 10 degree wedge bound
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [40, 0], [40, 10 - half_base], [50, 10], [40, 10 + half_base], [40, 20],
        [0, 20]]}})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["feature_width"] * judged + ["internal_corner_radius"] * 2
    if judged:
        thin = next(p for p in probs if p["rule"] == "feature_width")
        assert thin["at"][0] > 45 and thin["value"] < 0.8 and thin["assumption"]


# ---------------------------------------------------------------------------
# widths whose narrowest point is a VERTEX (review round 2): no ray from an
# edge interior crosses them square, so they are measured from the vertex
# ---------------------------------------------------------------------------
def only(probs: list[dict], rule: str) -> dict:
    (p,) = [p for p in probs if p["rule"] == rule]
    return p


@pytest.mark.parametrize("angle", [35, 65, 90])
def test_validate_fab_web_under_a_v_notch_tip(angle):
    # a V notch from the y = 0 edge whose tip (10, 9.5) is 0.5 mm from y = 10
    h = 9.5 * math.tan(math.radians(angle / 2))
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [10 - h, 0], [10, 9.5], [10 + h, 0], [20, 0], [20, 10], [0, 10]]}})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["feature_width", "internal_corner_radius"]
    web = only(probs, "feature_width")
    assert math.isclose(web["value"], 0.5, abs_tol=1e-9) and web["at"] == [10, 9.75]
    assert web["severity"] == "warning" and web["assumption"]


def test_validate_fab_web_whose_foot_is_a_collinear_split_vertex():
    # the 90 degree V-notch web again, the far edge split exactly under the tip
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [0.5, 0], [10, 9.5], [19.5, 0], [20, 0], [20, 10], [10, 10], [0, 10]]}})
    web = only(ol.validate_fab(o), "feature_width")
    assert math.isclose(web["value"], 0.5, abs_tol=1e-9) and web["at"] == [10, 9.75]


def test_validate_fab_web_between_a_notch_tip_and_a_round_hole():
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [5, 0], [10, 7.5], [15, 0], [20, 0], [20, 20], [0, 20]]},
        "cutouts": [{"circle": [10, 11, 3]}]})
    web = only(ol.validate_fab(o), "feature_width")
    assert math.isclose(web["value"], 0.5, abs_tol=1e-6) and "outline / cutout 1" in web["message"]


@pytest.mark.parametrize("half", [1.0, 5.0])
def test_validate_fab_hourglass_waist_between_two_tips(half):
    # two V notches whose tips (10, 4.75) and (10, 5.25) are 0.5 mm apart
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [10 - half, 0], [10, 4.75], [10 + half, 0], [20, 0], [20, 10],
        [10 + half, 10], [10, 5.25], [10 - half, 10], [0, 10]]}})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["feature_width"] + ["internal_corner_radius"] * 2
    waist = only(probs, "feature_width")
    assert math.isclose(waist["value"], 0.5, abs_tol=1e-9) and waist["at"] == [10, 5]


def test_validate_fab_tooth_tip_near_the_far_wall_of_a_cutout():
    # a square hole with a tooth of board pointing into it; its tip (19.5, 15)
    # is 0.5 mm from the hole's far wall: the router cannot pass
    o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [{"loop": [
        [10, 10], [20, 10], [20, 20], [10, 20], [10, 16], [19.5, 15], [10, 14]]}]})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["internal_corner_radius"] * 6 + ["slot_width"]
    slot = only(probs, "slot_width")
    assert slot["severity"] == "error" and math.isclose(slot["value"], 0.5, abs_tol=1e-9)
    assert slot["at"] == [19.75, 15]


def test_validate_fab_web_between_two_hole_corners():
    # two square holes whose nearest corners are 0.5 mm apart on the diagonal
    a = 15 + 0.5 / math.sqrt(2)
    o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [
        {"loop": [[10, 10], [15, 10], [15, 15], [10, 15]]},
        {"loop": [[a, a], [a + 5, a], [a + 5, a + 5], [a, a + 5]]}]})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["feature_width"] + ["internal_corner_radius"] * 8
    web = only(probs, "feature_width")
    assert math.isclose(web["value"], 0.5, abs_tol=1e-9)
    assert "cutout 1 / cutout 2" in web["message"]
    m = (15 + a) / 2
    assert web["at"] == [round(m, 6), round(m, 6)]


def test_validate_fab_polyline_fillet_is_not_a_row_of_sharp_corners():
    # the L's inner corner as a 4-chord polyline of a concave R2 fillet
    arc = [(22 + 2 * math.cos(a), 22 + 2 * math.sin(a))
           for a in (-math.pi / 2 - k * math.pi / 8 for k in range(5))]
    loop = [[0, 0], [60, 0], [60, 20], *arc, [20, 50], [0, 50]]
    assert ol.validate_fab(ol.from_board_block({"outline": {"loop": loop}})) == []


def test_validate_fab_narrow_slot_is_an_error():
    probs = ol.validate_fab(ol.from_board_block(STADIUM_08))
    # one slot problem for the ring, one radius warning per R0.4 end arc
    assert rules(probs) == ["internal_corner_radius"] * 2 + ["slot_width"]
    slot = next(p for p in probs if p["rule"] == "slot_width")
    assert slot["severity"] == "error" and math.isclose(slot["value"], 0.8, abs_tol=1e-6)
    assert slot["limit"] == 1.0 and "1 mm minimum non-plated slot" in slot["message"]
    corners = [p for p in probs if p["rule"] == "internal_corner_radius"]
    assert all(math.isclose(p["value"], 0.4) for p in corners)


def test_validate_fab_tiny_round_cutout_should_be_a_drill():
    o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [{"circle": [20, 15, 0.4]}]})
    (p,) = ol.validate_fab(o)
    assert p["rule"] == "slot_width" and math.isclose(p["value"], 0.8) and "NPTH" in p["message"]


def test_validate_fab_notch_from_the_edge():
    o = ol.from_board_block({"outline": {"loop": [
        [0, 0], [40, 0], [40, 30], [20.3, 30], [20.3, 10], [19.7, 10], [19.7, 30], [0, 30]]}})
    probs = ol.validate_fab(o)
    assert rules(probs) == ["internal_corner_radius"] * 2 + ["slot_width"]
    assert {tuple(p["at"]) for p in probs if p["rule"] != "slot_width"} == \
        {(20.3, 10), (19.7, 10)}
    slot = next(p for p in probs if p["rule"] == "slot_width")
    assert math.isclose(slot["value"], 0.6, abs_tol=1e-9)


def test_validate_fab_thin_web_is_an_assumption_based_warning():
    o = ol.from_board_block({"w": 40, "h": 30, "cutouts": [{"circle": [5, 15, 4.5]}]})
    (p,) = ol.validate_fab(o)
    assert p["rule"] == "feature_width" and p["severity"] == "warning" and p["assumption"]
    assert math.isclose(p["value"], 0.5, abs_tol=1e-3) and p["limit"] == 0.8
    assert "ASSUMPTION" in p["source"]


@pytest.mark.parametrize("fillet, warned", [(None, 0.0), (1.0, None), (0.3, 0.3)])
def test_validate_fab_l_inner_corner(fillet, warned):
    if fillet is None:
        loop = L_LOOP
    else:
        b = -T8                                          # concave: centre in the void
        loop = [[0, 0], [60, 0], [60, 20], [20 + fillet, 20, b], [20, 20 + fillet],
                [20, 50], [0, 50]]
    probs = ol.validate_fab(ol.from_board_block({"outline": {"loop": loop}}))
    if warned is None:
        assert probs == []
    else:
        (p,) = probs
        assert p["rule"] == "internal_corner_radius" and math.isclose(p["value"], warned)


def test_validate_fab_drilled_holes():
    o = ol.from_board_block({"w": 60, "h": 40, "corner_radius": 3,
                             "cutouts": [{"circle": [30, 20, 4]}]})
    probs = ol.validate_fab(o, holes=[(10, 10, 0.4), (20, 1.0, 1.6), (70, 20, 3),
                                      (30, 20, 1.0), (40, 1.0, 3.0)])
    assert rules(probs) == ["hole_inside", "hole_inside", "hole_size", "hole_to_edge",
                            "hole_to_edge"]
    by_at = {tuple(p["at"]): p for p in probs if p["rule"] == "hole_to_edge"}
    near, through = by_at[(20, 1.0)], by_at[(40, 1.0)]
    assert near["severity"] == "warning" and math.isclose(near["value"], 0.2)
    assert near["limit"] == 0.25
    assert through["severity"] == "error" and math.isclose(through["value"], -0.5)
    size = next(p for p in probs if p["rule"] == "hole_size")
    assert size["severity"] == "error" and size["value"] == 0.4
    with pytest.raises(ValueError, match="x, y, diameter"):
        ol.validate_fab(o, holes=[(1, 2)])


def test_validate_fab_profiles():
    o = ol.from_board_block(STADIUM_08)
    loose = dataclasses.replace(ol.FAB_LIMITS["JLCPCB"], name="loose",
                                min_slot_width=0.5, min_internal_radius=0.3)
    assert ol.validate_fab(o, loose) == []
    with pytest.raises(ValueError, match="JLCPCB"):
        ol.validate_fab(o, "NO_SUCH_FAB")

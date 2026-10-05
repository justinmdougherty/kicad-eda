"""Unit tests for outline_audit: the pure outline rules behind G1 lint, the G3/G4
SWIG gates, the silk placer and the mechanical tools (stdlib only, no pcbnew).

Every expected value is hand-computed from the geometry, never read back from
the module. Boards (board frame: mm, Y down, origin = outline bbox top-left):

* T10 (the wave-1 outline spike, scratch/wave1/T10-outlinespike/outline_A.json):
  60 x 40 with R4 corners; cutout 1 = a stadium x 27..33, y 4..14 (straight
  sides x = 27 and x = 33 for y 7..11, semicircle centres (30, 7) and (30, 11),
  r 3); cutout 2 = a dia-5 hole at (30, 20).
* L: the 60 x 45 bbox minus the bay x 32..60, y 0..20 (top right).
* the 40 x 30 rectangle of tests/test_gate_checks_interior.py.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest

import outline as ol
import outline_audit as oa

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
MODULE = TOOLS / "outline_audit.py"
OURS = ("outline", "outline_geom", "outline_inset", "outline_model", "outline_ops", "outline_audit")
LIVE_TIMEOUT_S = 120

# literal copy of outline_A.json (T10); bulge 0.41421356 = tan(22.5 deg), a 90 deg corner
T10_BLOCK = {
    "outline": {"loop": [[4, 0], [56, 0, 0.41421356237309503], [60, 4],
                         [60, 36, 0.41421356237309503], [56, 40],
                         [4, 40, 0.41421356237309503], [0, 36],
                         [0, 4, 0.41421356237309503]]},
    "cutouts": [{"loop": [[27, 7], [27, 11, -1], [33, 11], [33, 7, -1]]},
                {"circle": [30, 20, 2.5]}],
    "datum": [10, 30],
}
L_LOOP = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]
INTO = "{}: mating face points INTO the board interior (unusable in an enclosure)"
FAR = "{}: mating face is {:.1f} mm from the board edge it faces (must be at the edge, <=3 mm)"


@pytest.fixture(scope="module")
def t10() -> ol.Outline:
    return ol.from_board_block(T10_BLOCK)


@pytest.fixture(scope="module")
def lshape() -> ol.Outline:
    return ol.from_board_block({"outline": {"loop": L_LOOP}})


@pytest.fixture(scope="module")
def rect40() -> ol.Outline:
    return ol.from_board_block({"w": 40, "h": 30})


def box(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def part(ref: str, poly: list, overhang: float | None = None) -> dict:
    return {"ref": ref, "polygon": poly, "overhang": overhang}


def codes(issues: list) -> list[str]:
    return [i.code for i in issues]


# ---------------------------------------------------------------------------
# Issue record and the test boards
# ---------------------------------------------------------------------------
def test_issue_is_frozen_and_serialises():
    issue = oa.Issue("courtyard_outside", "error", "U1", "msg (board frame, mm)", (1.0, 2.5))
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.code = "x"  # type: ignore[misc]
    assert issue.to_dict() == {"code": "courtyard_outside", "severity": "error", "ref": "U1",
                               "message": "msg (board frame, mm)", "at": [1.0, 2.5]}
    assert oa.Issue("fab_board_size", "warning", None, "m").to_dict()["at"] is None
    with pytest.raises(ValueError, match="severity"):
        oa.Issue("x", "fatal", None, "m")


def test_l_shape_loop_is_accepted_and_normalised(lshape):
    assert math.isclose(ol.area(lshape), 60 * 45 - 28 * 20)
    flipped = ol.from_board_block({"outline": {"loop": L_LOOP[::-1]}})
    assert math.isclose(ol.area(flipped), ol.area(lshape))
    # material on the left of travel: the x = 32 bay wall's outward normal is +x
    assert ol.nearest_edge(flipped, (31.5, 10)).normal == pytest.approx((1.0, 0.0))


# ---------------------------------------------------------------------------
# courtyards
# ---------------------------------------------------------------------------
def test_courtyard_inside_the_board_is_clean(t10, lshape):
    assert oa.courtyard_issues(t10, [part("U1", box(5, 5, 10, 8))]) == []
    assert oa.courtyard_issues(lshape, [part("U1", box(2, 25, 58, 43))]) == []


@pytest.mark.parametrize("poly,ring", [
    (box(28, 8, 32, 10), 1),        # wholly inside the stadium
    (box(29, 19, 31, 21), 2),       # wholly inside the dia-5 hole
    (box(31, 18, 34, 22), 2),       # straddling the hole's rim
    (box(26, 16, 34, 24), 2),       # enclosing the whole hole
])
def test_courtyard_in_a_cutout_is_an_error(t10, poly, ring):
    issues = oa.courtyard_issues(t10, [part("R7", poly)])
    assert codes(issues) == ["courtyard_in_cutout"]
    assert issues[0].severity == "error" and issues[0].ref == "R7"
    assert f"cutout {ring}" in issues[0].message and "board frame, mm" in issues[0].message


@pytest.mark.parametrize("poly", [box(57, 10, 62, 14), box(70, 10, 75, 14)])
def test_courtyard_crossing_or_off_the_edge_is_an_error(t10, poly):
    issues = oa.courtyard_issues(t10, [part("J1", poly)])
    assert codes(issues) == ["courtyard_outside"] and issues[0].severity == "error"


def test_courtyard_across_the_l_bay_is_outside(lshape):
    # every vertex on the material; the edge (31, 15) -> (36, 25) cuts the bay corner
    poly = [[28, 15], [31, 15], [36, 25], [28, 25]]
    assert all(ol.contains(lshape, tuple(p)) for p in poly)
    issues = oa.courtyard_issues(lshape, [part("U2", poly)])
    assert codes(issues) == ["courtyard_outside"]
    assert 32 < issues[0].at[0] and issues[0].at[1] < 20


def test_courtyard_clearance_is_measured(t10):
    poly = box(10, 1, 15, 4)                      # 1.0 mm below the top edge
    assert oa.courtyard_issues(t10, [part("U1", poly)], clearance=0.5) == []
    issues = oa.courtyard_issues(t10, [part("U1", poly)], clearance=1.5)
    assert codes(issues) == ["courtyard_edge_clearance"]
    assert "1.000 mm" in issues[0].message


def test_overhang_may_cross_only_the_outer_edge(t10):
    poly = box(57, 10, 62, 14)                    # 2 mm past the x = 60 edge
    assert oa.courtyard_issues(t10, [part("J1", poly, overhang=2.5)]) == []
    assert oa.courtyard_issues(t10, [part("J1", poly, overhang=2.0)]) == []
    issues = oa.courtyard_issues(t10, [part("J1", poly, overhang=1.0)])
    assert codes(issues) == ["courtyard_overhang"]
    assert "2.000 mm" in issues[0].message and issues[0].at[0] == pytest.approx(62.0)


def test_overhang_never_excuses_a_cutout(t10):
    for poly in (box(31, 18, 34, 22), box(28, 8, 32, 10), box(26, 16, 34, 24)):
        issues = oa.courtyard_issues(t10, [part("J2", poly, overhang=5.0)])
        assert codes(issues) == ["courtyard_in_cutout"]
    # an overhanging part still keeps the clearance to a cutout (0.5 mm here)
    issues = oa.courtyard_issues(t10, [part("J2", box(33, 18, 36, 22), overhang=5.0)],
                                 clearance=1.0)
    assert codes(issues) == ["courtyard_edge_clearance"]
    assert "0.500 mm" in issues[0].message and "cutout 2" in issues[0].message


def test_overhang_part_wholly_off_the_board_is_outside(t10):
    issues = oa.courtyard_issues(t10, [part("J3", box(60.5, 10, 61.5, 14), overhang=5.0)])
    assert codes(issues) == ["courtyard_outside"]


def test_courtyard_records_are_validated(t10):
    with pytest.raises(ValueError, match="polygon"):
        oa.courtyard_issues(t10, [{"ref": "U1"}])
    with pytest.raises(ValueError, match="overhang"):
        oa.courtyard_issues(t10, [part("U1", box(5, 5, 6, 6), overhang=-1.0)])


# ---------------------------------------------------------------------------
# pads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pad", [
    {"ref": "U1", "pad": "3", "center": [33.25, 20.0], "radius": 0.5},
    {"ref": "U1", "pad": "3", "polygon": box(32.75, 19.5, 33.75, 20.5)},
])
def test_pad_0p25_from_the_hole_is_flagged_at_0p3(t10, pad):
    issues = oa.pad_issues(t10, [pad], edge_clearance=0.3)
    assert codes(issues) == ["pad_edge_clearance"] and issues[0].severity == "error"
    assert "U1 pad 3" in issues[0].message and "0.250 mm" in issues[0].message
    assert "cutout 2" in issues[0].message and "board frame, mm" in issues[0].message
    assert issues[0].at == pytest.approx((32.75, 20.0), abs=1e-6)
    assert oa.pad_issues(t10, [pad], edge_clearance=0.25) == []
    assert oa.pad_issues(t10, [pad], edge_clearance=0.2) == []


@pytest.mark.parametrize("pad", [
    {"ref": "U2", "pad": "1", "polygon": box(29, 8.5, 31, 9.5)},        # in the stadium
    {"ref": "U2", "pad": "1", "polygon": box(59.5, 20, 60.5, 21)},      # over the edge
    {"ref": "U2", "pad": "1", "polygon": box(26, 19.8, 34, 20.2)},      # ends clear, crosses hole
    {"ref": "U2", "pad": "1", "center": [30, 20], "radius": 0.4},       # in the hole
    {"ref": "U2", "pad": "1", "center": [0.2, 20], "radius": 0.4},      # straddles x = 0
])
def test_pad_off_the_material_is_an_error(t10, pad):
    issues = oa.pad_issues(t10, [pad], edge_clearance=0.0)
    assert codes(issues) == ["pad_outside"] and issues[0].severity == "error"


# ---------------------------------------------------------------------------
# copper
# ---------------------------------------------------------------------------
def test_track_crossing_the_hole_with_both_ends_outside_is_an_error(t10):
    track = {"kind": "track", "net": "SIG", "start": [25, 20], "end": [35, 20], "width": 0.25}
    assert ol.contains(t10, (25, 20)) and ol.contains(t10, (35, 20))
    issues = oa.copper_issues(t10, [track], edge_clearance=0.0)
    assert codes(issues) == ["copper_outside"] and issues[0].severity == "error"
    assert "cutout 2" in issues[0].message and "SIG" in issues[0].message
    assert 27.5 <= issues[0].at[0] <= 32.5 and issues[0].at[1] == pytest.approx(20.0)


def test_via_inside_the_stadium_is_an_error(t10):
    via = {"kind": "via", "net": "GND", "center": [30, 9], "radius": 0.3}
    issues = oa.copper_issues(t10, [via], edge_clearance=0.2)
    assert codes(issues) == ["copper_outside"] and issues[0].severity == "error"
    assert "cutout 1" in issues[0].message and issues[0].ref == "GND"


def test_copper_clearance_and_clean_copper(t10):
    near = {"kind": "track", "net": "A", "start": [25, 23], "end": [35, 23], "width": 0.2}
    # centreline 0.5 mm below the hole: copper edge 0.4 mm from it
    assert oa.copper_issues(t10, [near], edge_clearance=0.3) == []
    issues = oa.copper_issues(t10, [near], edge_clearance=0.5)
    assert codes(issues) == ["copper_edge_clearance"] and "0.400 mm" in issues[0].message
    clean = [{"kind": "track", "net": "B", "start": [10, 30], "end": [20, 30], "width": 0.3},
             {"kind": "via", "net": "B", "center": [20, 30], "radius": 0.3}]
    assert oa.copper_issues(t10, clean, edge_clearance=0.5) == []
    edge_via = {"kind": "via", "net": "C", "center": [59.9, 20], "radius": 0.3}
    assert codes(oa.copper_issues(t10, [edge_via], 0.0)) == ["copper_outside"]


def test_via_in_the_pcb_track_shape_is_accepted(t10):
    # SWIG's PCB_VIA is a PCB_TRACK: start == end == position, width = the via diameter
    via = {"kind": "via", "net": "GND", "start": [30, 9], "end": [30, 9], "width": 0.6}
    issues = oa.copper_issues(t10, [via], edge_clearance=0.0)
    assert codes(issues) == ["copper_outside"] and "cutout 1" in issues[0].message
    ok = {**via, "start": [10, 30], "end": [10, 30]}
    assert oa.copper_issues(t10, [ok], edge_clearance=0.3) == []
    near = {**via, "start": [32.6, 20], "end": [32.6, 20]}       # 0.2 mm of it over the hole
    issues = oa.copper_issues(t10, [near], edge_clearance=0.0)
    assert codes(issues) == ["copper_outside"] and issues[0].at[0] < 32.5


def test_arc_tracks_follow_the_arc_not_the_chord(t10):
    around = {"kind": "arc", "net": "R", "start": [26, 20], "mid": [30, 24], "end": [34, 20],
              "width": 0.2}                                  # r 4 about the hole: 1.4 mm gap
    assert oa.copper_issues(t10, [around], edge_clearance=0.3) == []
    # the chord y = 24 clears the hole, the arc dips to y = 21.8 inside it
    dip = {"kind": "arc", "net": "R", "start": [26, 24], "mid": [30, 21.8], "end": [34, 24],
           "width": 0.2}
    issues = oa.copper_issues(t10, [dip], edge_clearance=0.0)
    assert codes(issues) == ["copper_outside"] and "cutout 2" in issues[0].message


@pytest.mark.parametrize("call,match", [
    (lambda o: oa.pad_issues(o, [{"ref": "U1", "polygon": [[1, 1], [2, 2]]}], 0), "polygon"),
    (lambda o: oa.pad_issues(o, [{"ref": "U1", "center": [5, 5], "radius": 0}], 0), "radius"),
    (lambda o: oa.pad_issues(o, [{"ref": "U1", "center": [5], "radius": 1}], 0), "center"),
    (lambda o: oa.pad_issues(o, [{"ref": "U1", "center": [5, 5], "radius": 1}], math.nan),
     "edge_clearance"),
    (lambda o: oa.fab_issues(o, holes=[(1, 2)]), "holes"),
    (lambda o: oa.fab_issues(o, profile="NOFAB"), "profile"),
])
def test_bad_records_fail_fast(t10, call, match):
    with pytest.raises(ValueError, match=match):
        call(t10)


def test_copper_records_are_validated(t10):
    with pytest.raises(ValueError, match="kind"):
        oa.copper_issues(t10, [{"kind": "zone", "net": "X"}], 0.0)
    with pytest.raises(ValueError, match="width"):
        oa.copper_issues(t10, [{"kind": "track", "net": "X", "start": [1, 1], "end": [2, 2]}], 0)


# ---------------------------------------------------------------------------
# mounting holes
# ---------------------------------------------------------------------------
def test_mounting_hole_0p2_from_the_edge_is_a_warning(t10):
    issues = oa.mounting_hole_issues(t10, [{"ref": "H1", "center": [10, 1.8], "drill": 3.2}])
    assert codes(issues) == ["hole_edge"] and issues[0].severity == "warning"
    assert "0.200 mm" in issues[0].message and "0.25" in issues[0].message
    assert oa.mounting_hole_issues(t10, [{"ref": "H1", "center": [10, 1.85], "drill": 3.2}]) == []


@pytest.mark.parametrize("center", [[10, 1.0], [30, 20], [70, 20]])
def test_mounting_hole_drill_off_the_material_is_an_error(t10, center):
    issues = oa.mounting_hole_issues(t10, [{"ref": "H2", "center": center, "drill": 3.2}])
    assert codes(issues) == ["hole_outside"] and issues[0].severity == "error"


def test_mounting_hole_judges_the_drill_not_the_courtyard(t10):
    # MountingHole_3.2mm's courtyard (r 3.45) would cross the top edge; the drill clears it
    assert oa.mounting_hole_issues(t10, [{"ref": "H3", "center": [10, 3.0], "drill": 3.2}]) == []
    with pytest.raises(ValueError, match="profile"):
        oa.mounting_hole_issues(t10, [], profile="NOFAB")


# ---------------------------------------------------------------------------
# connector mouths
# ---------------------------------------------------------------------------
def test_connector_in_the_l_bay_facing_the_x32_wall(lshape):
    assert ol.nearest_edge(lshape, (31.5, 10)).ring == 0
    assert oa.connector_edge_issue(lshape, "J1", (31.5, 10), (1, 0)) is None
    issue = oa.connector_edge_issue(lshape, "J1", (31.5, 10), (-1, 0))
    assert issue is not None and issue.code == "connector_edge" and issue.severity == "error"
    assert issue.message.startswith(INTO.format("J1"))
    assert "(32.000, 10.000)" in issue.message and "(1.000, 0.000)" in issue.message
    assert "board frame, mm" in issue.message


@pytest.mark.parametrize("point,mouth,ring", [((30, 23.0), (0, -1), 2),     # under the hole
                                               ((26.5, 9.0), (1, 0), 1)])    # left of the stadium
def test_connector_facing_a_cutout_needs_panel_cutouts(t10, point, mouth, ring):
    assert ol.nearest_edge(t10, point).ring == ring
    issue = oa.connector_edge_issue(t10, "J2", point, mouth)
    assert issue is not None and f"cutout {ring}" in issue.message
    assert issue.message.startswith(INTO.format("J2"))
    assert oa.connector_edge_issue(t10, "J2", point, mouth, panel_cutouts=True) is None


ROTATION_SEATS = {0: (39.5, 15.0), 90: (20.0, 0.5), 180: (0.5, 15.0), 270: (20.0, 29.5)}


def _mouth(rot_deg: float) -> tuple[float, float]:
    """A KiCad rotation as displayed (CCW on screen, Y down) applied to a +x mouth."""
    a = math.radians(rot_deg)
    return (math.cos(a), -math.sin(a))


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_mouth_rotations(rect40, rot, turn):
    issue = oa.connector_edge_issue(rect40, "J1", ROTATION_SEATS[rot], _mouth(rot + turn))
    assert (issue is None) is (turn == 0)


def test_mouth_facing_cone_is_45_degrees(rect40):
    seat = ROTATION_SEATS[0]
    assert oa.connector_edge_issue(rect40, "J1", seat, _mouth(44)) is None
    assert oa.connector_edge_issue(rect40, "J1", seat, _mouth(-44)) is None
    assert oa.connector_edge_issue(rect40, "J1", seat, _mouth(46)) is not None
    with pytest.raises(ValueError, match="mouth_dir"):
        oa.connector_edge_issue(rect40, "J1", seat, (0, 0))


def test_connector_too_far_from_the_edge(rect40):
    issue = oa.connector_edge_issue(rect40, "J9", (36.0, 15.0), (1, 0))
    assert issue is not None and issue.message.startswith(FAR.format("J9", 4.0))
    assert oa.connector_edge_issue(rect40, "J9", (36.0, 15.0), (1, 0), max_dist=4.5) is None


# Records encoded by hand from tests/test_gate_checks_interior.py (40 x 30 board):
# (ref, pad centroid, courtyard box (x0, y0, x1, y1), the old bbox rule's text).
GATE_CASES = [
    ("J1", (20, 4), (17, 3, 23, 9), INTO.format("J1")),          # _inward("J1")
    ("J1", (10, 4), (7, 3, 13, 9), INTO.format("J1")),           # _inward("J1", x=10)
    ("J2", (30, 4), (27, 3, 33, 9), INTO.format("J2")),          # _inward("J2", x=30)
    ("J1", (20, 12), (17, 6, 23, 13), FAR.format("J1", 6.0)),    # _recessed("J1")
    # the same parts turned to face out of each edge: the old rule passes them
    ("J3", (20, 5), (17, 0.5, 23, 6), None),
    ("J4", (36, 15), (35, 12, 39.5, 18), None),
    ("J5", (4, 15), (0.5, 12, 5, 18), None),
    ("J6", (20, 25), (17, 24, 23, 29.5), None),
]


def _gate_mouth(pad_c: tuple, cy: tuple) -> tuple[tuple, tuple]:
    """swig_gate_checks._world_mouth (courtyard centre - pad centroid, snapped to an
    axis) and the courtyard face the mouth opens through, as the G3 adapter feeds it."""
    cx, cyy = (cy[0] + cy[2]) / 2, (cy[1] + cy[3]) / 2
    dx, dy = cx - pad_c[0], cyy - pad_c[1]
    d = (math.copysign(1.0, dx), 0.0) if abs(dx) >= abs(dy) else (0.0, math.copysign(1.0, dy))
    faces = {(1.0, 0.0): (cy[2], cyy), (-1.0, 0.0): (cy[0], cyy),
             (0.0, 1.0): (cx, cy[3]), (0.0, -1.0): (cx, cy[1])}
    return faces[d], d


@pytest.mark.parametrize("ref,pad_c,cy,old", GATE_CASES)
def test_rect_board_matches_the_old_bbox_rule(rect40, ref, pad_c, cy, old):
    point, mouth = _gate_mouth(pad_c, cy)
    issue = oa.connector_edge_issue(rect40, ref, point, mouth)
    if old is None:
        assert issue is None
    else:
        assert issue is not None and issue.message.startswith(old)


# ---------------------------------------------------------------------------
# fab rules and the summary
# ---------------------------------------------------------------------------
def test_fab_issues_map_validate_fab(t10):
    assert oa.fab_issues(t10) == []
    web = ol.from_board_block({"w": 20, "h": 10, "cutouts": [{"circle": [10, 3, 2.5]}]})
    issues = oa.fab_issues(web, holes=[{"ref": "H1", "center": [16, 5], "drill": 0.3}])
    by_code = {i.code: i for i in issues}
    assert set(by_code) == {"fab_feature_width", "fab_hole_size"}
    assert by_code["fab_feature_width"].severity == "warning"
    assert "ASSUMPTION" in by_code["fab_feature_width"].message
    assert by_code["fab_hole_size"].severity == "error" and by_code["fab_hole_size"].ref == "H1"
    assert all("board frame, mm" in i.message for i in issues)
    assert codes(oa.fab_issues(web, holes=[(16, 5, 0.3)])).count("fab_hole_size") == 1


def test_outline_summary_of_t10(t10):
    s = oa.outline_summary(t10)
    assert s["bbox"] == pytest.approx([0, 0, 60, 40])
    corners, stadium, hole = 64 - 16 * math.pi, 24 + 9 * math.pi, math.pi * 2.5 ** 2
    assert s["area"] == pytest.approx(2400 - corners - stadium - hole, abs=1e-6)
    assert [c["kind"] for c in s["cutouts"]] == ["loop", "circle"]
    assert s["cutouts"][0]["bbox"] == pytest.approx([27, 4, 33, 14])
    assert s["cutouts"][0]["area"] == pytest.approx(stadium)
    assert s["cutouts"][1]["area"] == pytest.approx(hole)
    assert s["datum"] == [10, 30]
    # narrowest web: the stadium's bottom (30, 14) to the hole's top (30, 17.5)
    assert s["min_feature"] == pytest.approx(3.5, abs=1e-3)
    assert s["min_feature_at"] == pytest.approx([30, 15.75], abs=1e-2)
    plain = oa.outline_summary(ol.from_board_block({"w": 60, "h": 40}))
    assert plain["cutouts"] == [] and plain["datum"] is None and plain["min_feature"] is None
    json.dumps(s)


def test_every_message_states_the_frame(t10):
    issues = (oa.courtyard_issues(t10, [part("A", box(57, 10, 62, 14))])
              + oa.pad_issues(t10, [{"ref": "B", "pad": "1", "center": [30, 9], "radius": 1}], 0)
              + oa.copper_issues(t10, [{"kind": "via", "net": "N", "center": [30, 20],
                                        "radius": 0.3}], 0)
              + oa.mounting_hole_issues(t10, [{"ref": "H", "center": [30, 20], "drill": 3}])
              + [oa.connector_edge_issue(t10, "J", (30, 30), (1, 0))])
    assert len(issues) == 5
    assert all("board frame, mm" in i.message for i in issues)


# ---------------------------------------------------------------------------
# performance, imports, size
# ---------------------------------------------------------------------------
def test_500_pads_on_t10_in_under_a_second(t10):
    xs = [3 + 2.25 * i for i in range(25)]
    ys = [2 + 1.9 * j for j in range(20)]
    pads = [{"ref": f"U{n}", "pad": "1", "polygon": box(x - 0.5, y - 0.3, x + 0.5, y + 0.3)}
            for n, (x, y) in enumerate((x, y) for x in xs for y in ys)]
    assert len(pads) == 500
    start = time.perf_counter()
    issues = oa.pad_issues(t10, pads, edge_clearance=0.3)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"500-pad audit took {elapsed:.3f} s"
    assert issues and {i.code for i in issues} <= {"pad_outside", "pad_edge_clearance"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


def test_module_is_stdlib_only_and_small():
    bad = {m for m in _imports(MODULE) if m not in sys.stdlib_module_names and m not in OURS}
    assert not bad, f"outline_audit imports non-stdlib {sorted(bad)}"
    assert len(MODULE.read_text(encoding="utf-8").splitlines()) < 450


def test_import_pulls_in_no_pcbnew_and_no_third_party_module():
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "before = set(sys.modules)\n"
        "import outline_audit as oa, outline as ol\n"
        f"o = ol.from_board_block({json.dumps(T10_BLOCK)})\n"
        "n = len(oa.copper_issues(o, [{'kind': 'via', 'net': 'G', 'center': [30, 9], "
        "'radius': 0.3}], 0.0))\n"
        f"ours = set({list(OURS)!r})\n"
        "new = sorted(m for m in set(sys.modules) - before if m.split('.')[0] not in "
        "sys.stdlib_module_names and m.split('.')[0] not in ours)\n"
        "print(json.dumps({'pcbnew': 'pcbnew' in sys.modules, 'new': new, 'n': n}))\n")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"pcbnew": False, "new": [], "n": 1}


@pytest.fixture(scope="module")
def kicad9_python() -> Path:
    """KiCad 9's bundled Python (3.11): the SWIG-side importer of this module."""
    import kicad_env as env
    for inst in env.discover():
        if inst.version_tuple[0] == 9 and inst.python is not None:
            return inst.python
    pytest.skip("no KiCad 9 bundled Python")


def test_live_imports_under_kicad9_bundled_python(kicad9_python, tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "import wx; wx.DisableAsserts(); wx.Log.SetActiveTarget(wx.LogStderr())\n"
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import outline as ol, outline_audit as oa\n"
        "o = ol.from_board_block(json.loads(sys.argv[2]))\n"
        "t = {'kind': 'track', 'net': 'S', 'start': [25, 20], 'end': [35, 20], 'width': 0.25}\n"
        "out = [i.to_dict() for i in oa.copper_issues(o, [t], 0.0)]\n"
        "print(json.dumps({'py': list(sys.version_info[:2]), 'pcbnew': 'pcbnew' in sys.modules,"
        " 'codes': [i['code'] for i in out]}))\n", encoding="utf-8")
    proc = subprocess.run([str(kicad9_python), str(child), str(TOOLS), json.dumps(T10_BLOCK)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=LIVE_TIMEOUT_S, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"py": [3, 11], "pcbnew": False, "codes": ["copper_outside"]}

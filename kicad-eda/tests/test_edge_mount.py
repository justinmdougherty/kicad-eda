"""edge_mount (F3-25): edge-launch connector geometry from the footprint.

A synthetic edge-mount footprint (pads on the board side, a 'PCB Edge' marker
on Dwgs.User, a courtyard reaching far beyond it) is checked in all four
quarter-turn orientations for each source (marker line, marker text, name);
then the .kicad_mod reader, straddle_issue, and - when a KiCad 9 install is
present - the stock SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount and a vertical U.FL,
plus an import check under KiCad 9's bundled Python 3.11.
"""
from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

import edge_mount as em
import kicad_env as env

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
SOURCE = TOOLS / "edge_mount.py"

# canonical footprint (rot 0): pads around x = 0, edge line x = 2, text beside it
PADS = [{"kind": "pad", "layer": "F.Cu", "x": 0.0, "y": y, "w": 1.0, "h": 1.0}
        for y in (-2.5, 0.0, 2.5)]
LINE = {"kind": "line", "layer": "Dwgs.User", "x0": 2.0, "y0": -4.0, "x1": 2.0, "y1": 4.0}
TEXT = {"kind": "text", "layer": "Dwgs.User", "x": 2.5, "y": 0.0, "rot": 90.0,
        "text": "PCB Edge"}
COURTYARD = (-2.0, -4.0, 12.0, 4.0)


# ----------------------------------------------------------------------------
# helpers: exact quarter turns in KiCad's frame (Y down, +angle = CCW on screen)
# ----------------------------------------------------------------------------
def _turn(x: float, y: float, k: int) -> tuple[float, float]:
    """(1, 0) -> (0, -1) for one quarter turn, like a KiCad +90 rotation."""
    for _ in range(k % 4):
        x, y = y, -x
    return (x + 0.0, y + 0.0)


def _turn_prim(p: dict, k: int) -> dict:
    q = dict(p)
    if p["kind"] == "line":
        q["x0"], q["y0"] = _turn(p["x0"], p["y0"], k)
        q["x1"], q["y1"] = _turn(p["x1"], p["y1"], k)
        return q
    q["x"], q["y"] = _turn(p["x"], p["y"], k)
    if p["kind"] == "text":
        q["rot"] = (p["rot"] + 90.0 * k) % 360.0
    elif k % 2:
        q["w"], q["h"] = p["h"], p["w"]
    return q


def _turn_box(box: tuple, k: int) -> tuple:
    a, b = _turn(box[0], box[1], k), _turn(box[2], box[3], k)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _expect(k: int, anchor: tuple[float, float], overhang: float, source: str) -> dict:
    out = _turn(1.0, 0.0, k)
    ax, ay = _turn(*anchor, k)
    axis = "x" if out[0] else "y"
    return {"edge_mount": True, "axis": axis, "edge_coord_mm": ax if axis == "x" else ay,
            "outward": out, "overhang_mm": overhang, "source": source,
            "anchor_mm": (ax, ay)}


def _as_dict(r: em.EdgeMount) -> dict:
    return {"edge_mount": r.edge_mount, "axis": r.axis, "edge_coord_mm": r.edge_coord_mm,
            "outward": r.outward, "overhang_mm": r.overhang_mm, "source": r.source,
            "anchor_mm": r.anchor_mm}


def _run(prims: list[dict], k: int, **kw: str) -> dict:
    return _as_dict(em.edge_from_primitives([_turn_prim(p, k) for p in prims],
                                            _turn_box(COURTYARD, k), **kw))


def _approx(d: dict) -> dict:
    return {key: pytest.approx(v, abs=1e-9) if not isinstance(v, (str, bool)) and v is not None
            else v for key, v in d.items()}


# ----------------------------------------------------------------------------
# edge_from_primitives: the three sources in all four orientations
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_marker_line_wins_in_every_orientation(k: int) -> None:
    got = _run([*PADS, LINE, TEXT], k)
    assert got == _approx(_expect(k, (2.0, 0.0), 10.0, "marker_line"))


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_marker_text_alone_gives_edge_and_axis_in_every_orientation(k: int) -> None:
    got = _run([*PADS, TEXT], k)
    assert got == _approx(_expect(k, (2.5, 0.0), 9.5, "marker_text"))


@pytest.mark.parametrize("k", [0, 1, 2, 3])
@pytest.mark.parametrize("kw", [{"name": "SMA_Foo_EdgeMount"}, {"name": "X_Edge_Mount_Y"},
                                {"tags": "sma straight edge mount"},
                                {"name": "plain", "tags": "EDGE MOUNT"}])
def test_name_or_tag_uses_the_courtyard_side_beyond_the_pads(k: int, kw: dict) -> None:
    got = _run(PADS, k, **kw)
    # pad bbox x in [-0.5, 0.5]: the courtyard reaches 11.5 beyond +x, 1.5 beyond -x
    assert got == _approx(_expect(k, (0.5, 0.0), 11.5, "name"))


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_leader_line_leads_to_the_edge_line(k: int) -> None:
    """KiCad's DVI footprints: 'PCB Edge' text (rot 0), a perpendicular arrow
    from beside the text to a horizontal Dwgs.User edge line 3.4 mm away."""
    text = {**TEXT, "x": 6.0, "y": 0.0, "rot": 0.0}             # horizontal: axis y
    leader = {"kind": "line", "layer": "Cmts.User", "x0": 6.0, "y0": -0.5, "x1": 6.0, "y1": -3.0}
    edge = {"kind": "line", "layer": "Dwgs.User", "x0": -2.0, "y0": -3.4, "x1": 10.0, "y1": -3.4}
    pads = [{**p, "y": p["y"] + 2.0} for p in PADS]             # pads below: outward -y
    box = (-2.0, -9.0, 12.0, 4.0)
    r = em.edge_from_primitives([_turn_prim(p, k) for p in (*pads, text, leader, edge)],
                                _turn_box(box, k))
    out = _turn(0.0, -1.0, k)
    anchor = _turn(4.0, -3.4, k)
    assert (r.source, r.outward) == ("marker_line", pytest.approx(out))
    assert r.axis == ("x" if out[0] else "y")
    assert r.edge_coord_mm == pytest.approx(anchor[0] if r.axis == "x" else anchor[1])
    assert r.overhang_mm == pytest.approx(5.6) and r.anchor_mm == pytest.approx(anchor)


# ----------------------------------------------------------------------------
# edge_from_primitives: which marker counts
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("label", ["PCB Edge", "pcb edge", "PCB EDGE", " PCB  edge ", "PCB_Edge"])
def test_marker_text_is_case_insensitive(label: str) -> None:
    assert em.edge_from_primitives([*PADS, {**TEXT, "text": label}], COURTYARD).source == \
        "marker_text"


@pytest.mark.parametrize("label", ["Board edge", "PCB Edges", "PCB connector edge",
                                   "^ Suggested board edge for RF ^", "${REFERENCE}"])
def test_other_texts_are_not_markers(label: str) -> None:
    r = em.edge_from_primitives([*PADS, LINE, {**TEXT, "text": label}], COURTYARD)
    assert r == em.NOT_EDGE_MOUNT


@pytest.mark.parametrize("layer", ["F.SilkS", "F.Fab", "Edge.Cuts"])
def test_marker_text_on_a_non_drawing_layer_is_ignored(layer: str) -> None:
    assert not em.edge_from_primitives([*PADS, {**TEXT, "layer": layer}], COURTYARD).edge_mount


def test_cmts_user_text_and_line_count() -> None:
    prims = [*PADS, {**LINE, "layer": "Cmts.User"}, {**TEXT, "layer": "Cmts.User"}]
    r = em.edge_from_primitives(prims, COURTYARD)
    assert (r.source, r.edge_coord_mm) == ("marker_line", 2.0)


@pytest.mark.parametrize("line, why", [
    ({**LINE, "layer": "F.SilkS"}, "not a drawing layer"),
    ({**LINE, "x0": 0.9, "x1": 0.9}, "1.6 mm from the text"),
    ({"kind": "line", "layer": "Dwgs.User", "x0": 1.5, "y0": -1.0, "x1": 3.5, "y1": 1.0},
     "diagonal"),
])
def test_lines_that_do_not_qualify_leave_the_text_position(line: dict, why: str) -> None:
    r = em.edge_from_primitives([*PADS, line, TEXT], COURTYARD)
    assert (r.source, r.edge_coord_mm) == ("marker_text", 2.5), why


def test_a_perpendicular_line_near_the_text_is_not_the_edge() -> None:
    """A horizontal tick through a vertical text is a leader with nothing at
    its far end: the text position stays the edge."""
    tick = {"kind": "line", "layer": "Dwgs.User", "x0": 2.0, "y0": 0.0, "x1": 3.0, "y1": 0.0}
    r = em.edge_from_primitives([*PADS, tick, TEXT], COURTYARD)
    assert (r.source, r.axis, r.edge_coord_mm) == ("marker_text", "x", 2.5)


def test_nearest_parallel_line_wins_then_the_longer_one() -> None:
    near = {**LINE, "x0": 2.3, "x1": 2.3, "y0": -1.0, "y1": 1.0}
    far = {**LINE, "x0": 1.6, "x1": 1.6}
    assert em.edge_from_primitives([*PADS, far, near, TEXT], COURTYARD).edge_coord_mm == 2.3
    long_ = {**LINE, "x0": 2.0, "x1": 2.0, "y0": -6.0, "y1": 6.0}
    short = {**LINE, "x0": 3.0, "x1": 3.0, "y0": -1.0, "y1": 1.0}       # both 0.5 from the text
    assert em.edge_from_primitives([*PADS, short, long_, TEXT], COURTYARD).edge_coord_mm == 2.0


@pytest.mark.parametrize("rot, axis", [(0, "y"), (180, "y"), (-180, "y"), (90, "x"),
                                       (270, "x"), (-90, "x"), (30, "y"), (60, "x"),
                                       (450, "x"), (135.5, "y"), (134.5, "x")])
def test_text_rotation_snaps_to_the_nearest_axis(rot: float, axis: str) -> None:
    r = em.edge_from_primitives([{**TEXT, "rot": rot}], COURTYARD)
    assert r.axis == axis


def test_marker_outward_without_pads_comes_from_the_courtyard() -> None:
    r = em.edge_from_primitives([LINE, TEXT], COURTYARD)
    assert (r.outward, r.overhang_mm) == ((1.0, 0.0), 10.0)
    r = em.edge_from_primitives([LINE, TEXT], (-12.0, -4.0, 2.5, 4.0))
    assert (r.outward, r.overhang_mm) == ((-1.0, 0.0), 14.0)


def test_marker_without_courtyard_or_pads_has_no_direction() -> None:
    r = em.edge_from_primitives([LINE, TEXT], None)
    assert r.edge_mount and r.source == "marker_line"
    assert (r.outward, r.overhang_mm) == ((0.0, 0.0), 0.0)
    r = em.edge_from_primitives([LINE, TEXT], (-3.0, -4.0, 7.0, 4.0))     # 5 mm either side
    assert (r.outward, r.overhang_mm) == ((0.0, 0.0), 0.0)


def test_marker_without_courtyard_keeps_the_pad_direction() -> None:
    r = em.edge_from_primitives([*PADS, LINE, TEXT], None)
    assert (r.outward, r.overhang_mm) == ((1.0, 0.0), 0.0)


def test_courtyard_that_stops_at_the_edge_gives_zero_overhang() -> None:
    r = em.edge_from_primitives([*PADS, LINE, TEXT], (-2.0, -4.0, 1.5, 4.0))
    assert r.edge_mount and r.outward == (1.0, 0.0) and r.overhang_mm == 0.0


def test_courtyard_corners_may_come_in_any_order() -> None:
    r = em.edge_from_primitives([*PADS, LINE, TEXT], (12.0, 4.0, -2.0, -4.0))
    assert r.overhang_mm == 10.0


def test_name_fallback_needs_pads_and_a_courtyard_that_reaches_beyond_them() -> None:
    assert em.edge_from_primitives([], COURTYARD, name="X_EdgeMount") == em.NOT_EDGE_MOUNT
    assert em.edge_from_primitives(PADS, None, name="X_EdgeMount") == em.NOT_EDGE_MOUNT
    inside = (-0.5, -3.0, 0.5, 3.0)                       # exactly the pad bbox
    assert em.edge_from_primitives(PADS, inside, name="X_EdgeMount") == em.NOT_EDGE_MOUNT


def test_no_marker_and_no_name_is_not_edge_mount() -> None:
    r = em.edge_from_primitives([*PADS, LINE], COURTYARD, name="U.FL_Hirose_Vertical",
                                tags="u.fl coaxial")
    assert r == em.NOT_EDGE_MOUNT
    assert (r.edge_mount, r.axis, r.source, r.overhang_mm, r.anchor_mm) == (
        False, None, None, 0.0, None)


def test_marker_beats_the_name() -> None:
    assert em.edge_from_primitives([*PADS, TEXT], COURTYARD, name="A_EdgeMount").source == \
        "marker_text"


def test_unknown_kinds_are_ignored_and_inputs_are_not_mutated() -> None:
    prims = [*PADS, LINE, TEXT, {"kind": "arc", "layer": "Dwgs.User"}]
    before = json.dumps(prims, sort_keys=True)
    assert em.edge_from_primitives(prims, list(COURTYARD)).source == "marker_line"
    assert json.dumps(prims, sort_keys=True) == before


@pytest.mark.parametrize("bad, key", [
    ({"kind": "line", "layer": "Dwgs.User", "x0": 0, "y0": 0, "x1": 1}, "y1"),
    ({"kind": "text", "layer": "Dwgs.User", "x": "a", "y": 0, "text": "PCB Edge"}, "x"),
    ({"kind": "pad", "x": 0.0, "y": float("nan")}, "y"),
    ({"kind": "text", "layer": "Dwgs.User", "x": 0, "y": 0}, "text"),
    ("not a dict", "kind"),
])
def test_malformed_primitives_raise_a_clear_error(bad: object, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        em.edge_from_primitives([*PADS, bad], COURTYARD, name="X_EdgeMount")


@pytest.mark.parametrize("box", [(0, 0, 1), ("a", 0, 1, 1), (0, 0, 1, float("inf"))])
def test_malformed_courtyard_raises(box: tuple) -> None:
    with pytest.raises(ValueError, match="courtyard"):
        em.edge_from_primitives([TEXT], box)


# ----------------------------------------------------------------------------
# straddle_issue
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("d", [0.0, 0.25, -0.25, 0.1])
def test_straddle_within_tolerance_is_fine(d: float) -> None:
    assert em.straddle_issue("J1", d) is None


@pytest.mark.parametrize("d, shown", [(3.0, "3.00"), (-0.26, "0.26"), (12.346, "12.35")])
def test_straddle_beyond_tolerance_names_the_distance(d: float, shown: str) -> None:
    assert em.straddle_issue("J7", d) == (
        f"J7: edge-mount connector does not straddle the board edge "
        f"(edge marker {shown} mm from the outline)")


def test_straddle_custom_tolerance() -> None:
    assert em.straddle_issue("J2", 0.4, tol=0.5) is None
    assert em.straddle_issue("J2", 0.6, tol=0.5) is not None


@pytest.mark.parametrize("d, tol", [(float("nan"), 0.25), (float("inf"), 0.25), (1.0, -0.1),
                                    (1.0, float("nan"))])
def test_straddle_rejects_bad_numbers(d: float, tol: float) -> None:
    with pytest.raises(ValueError):
        em.straddle_issue("J1", d, tol=tol)


# ----------------------------------------------------------------------------
# edge_from_kicad_mod / primitives_from_kicad_mod
# ----------------------------------------------------------------------------
K9_MOD = """(footprint "Test_EdgeMount_Synthetic"
\t(version 20241229)
\t(layer "F.Cu")
\t(tags "sma edge")
\t(fp_line (start 2.1 -4.1) (end 2.1 4.1) (stroke (width 0.1) (type solid)) (layer "Dwgs.User"))
\t(fp_line (start 9 9) (end 9 -9) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
\t(fp_rect (start -2.6 -4) (end 2.6 4) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
\t(fp_poly (pts (xy 2.6 -3.68) (xy 12.12 -3.68) (xy 12.12 3.68) (xy 2.6 3.68))
\t\t(stroke (width 0.05) (type solid)) (layer "B.CrtYd"))
\t(fp_text user "PCB Edge" (at 2.6 0 90) (layer "Dwgs.User")
\t\t(effects (font (size 0.5 0.5) (thickness 0.1))))
\t(fp_text user "${REFERENCE}" (at 0 0 0) (layer "F.Fab"))
\t(pad "1" smd roundrect (at 0 0 90) (size 1.27 3.2) (layers "F.Cu" "F.Mask"))
\t(pad "2" smd roundrect (at -0.25 -2.825 90) (size 1.35 3.7) (layers "F.Cu" "F.Mask"))
\t(pad "2" smd roundrect (at -0.25 2.825 90) (size 1.35 3.7) (layers "B.Cu" "B.Mask"))
)
"""

# KiCad 5 dialect: (module ...), bare layer names, a bare footprint name, and a
# KiCad 5 arc (start = centre, end = first point, angle = sweep in degrees)
K5_MOD = """(module 0805_EdgeMount (layer F.Cu) (tedit 5A02FF57)
  (fp_text reference REF** (at 0 -3) (layer F.SilkS))
  (fp_circle (center 0 0) (end 1.5 0) (layer F.CrtYd) (width 0.05))
  (fp_arc (start 1 1) (end 1 -4) (angle 126.87) (layer F.CrtYd) (width 0.05))
  (pad 01 smd rect (at -0.5 0) (size 1 2) (layers F.Cu F.Paste F.Mask))
  (pad 02 thru_hole circle (at 0.5 0) (size 1 1) (drill 0.6) (layers *.Cu *.Mask))
)
"""


def test_primitives_from_kicad_mod_reads_the_drawing_and_pads() -> None:
    fp = em.primitives_from_kicad_mod(K9_MOD)
    assert fp.name == "Test_EdgeMount_Synthetic" and fp.tags == "sma edge"
    assert fp.courtyard_bbox == pytest.approx((-2.6, -4.0, 12.12, 4.0))
    kinds = sorted(p["kind"] for p in fp.prims)
    assert kinds == ["line", "line", "pad", "pad", "pad", "text", "text"]
    text = next(p for p in fp.prims if p["kind"] == "text" and p["text"] == "PCB Edge")
    assert (text["x"], text["y"], text["rot"], text["layer"]) == (2.6, 0.0, 90.0, "Dwgs.User")
    pad1 = next(p for p in fp.prims if p["kind"] == "pad" and p["x"] == 0.0)
    assert (pad1["w"], pad1["h"]) == pytest.approx((3.2, 1.27))     # rotated 90


def test_edge_from_kicad_mod_on_a_kicad9_footprint() -> None:
    r = em.edge_from_kicad_mod(K9_MOD)
    assert (r.edge_mount, r.source, r.axis, r.outward) == (True, "marker_line", "x", (1.0, 0.0))
    assert r.edge_coord_mm == pytest.approx(2.1) and r.overhang_mm == pytest.approx(10.02)
    assert r.anchor_mm == pytest.approx((2.1, 0.0))


def test_edge_from_kicad_mod_on_a_kicad5_footprint_with_round_courtyard() -> None:
    fp = em.primitives_from_kicad_mod(K5_MOD)
    assert fp.name == "0805_EdgeMount"
    # circle r 1.5 at the origin; the arc (centre (1, 1), r 5) runs from (1, -4)
    # through its rightmost point (6, 1) - neither end - to (5, 4)
    assert fp.courtyard_bbox == pytest.approx((-1.5, -4.0, 6.0, 4.0), abs=1e-4)
    got = em.edge_from_kicad_mod(K5_MOD)
    assert (got.source, got.axis, got.outward) == ("name", "x", (1.0, 0.0))
    assert got.edge_coord_mm == pytest.approx(1.0)                   # pad 02 outer edge
    assert got.overhang_mm == pytest.approx(5.0)


def test_kicad_mod_poly_with_arc_and_rotated_pad() -> None:
    mod = """(footprint "P"
  (fp_poly (pts (xy 0 0) (arc (start 0 0) (mid 1 -1) (end 2 0)) (xy 2 3))
    (layer "F.CrtYd"))
  (pad "1" smd rect (at 0 1 45) (size 2 0))
  (pad "2" smd rect (at 1 1) (size 0.5 0.5) (layers "F.Cu"))
)"""
    fp = em.primitives_from_kicad_mod(mod)
    assert fp.courtyard_bbox == pytest.approx((0.0, -1.0, 2.0, 3.0))
    pad = next(p for p in fp.prims if p["kind"] == "pad" and p["x"] == 0.0)
    assert (pad["w"], pad["h"]) == pytest.approx((math.sqrt(2.0), math.sqrt(2.0)))


def test_kicad_mod_without_courtyard() -> None:
    fp = em.primitives_from_kicad_mod('(footprint "N" (pad "1" smd rect (at 0 0) (size 1 1)))')
    assert fp.courtyard_bbox is None and fp.tags == ""
    assert em.edge_from_kicad_mod('(footprint "N")') == em.NOT_EDGE_MOUNT


def test_kicad_mod_collinear_arc_counts_its_points() -> None:
    mod = '(footprint "A" (fp_arc (start 0 0) (mid 1 0) (end 2 0) (layer "B.CrtYd")))'
    assert em.primitives_from_kicad_mod(mod).courtyard_bbox == pytest.approx((0, 0, 2, 0))


@pytest.mark.parametrize("start, mid, end, box", [
    ("2 0", "1 -1", "0 0", (0.0, -1.0, 2.0, 0.0)),        # upper half, drawn right to left
    ("0 0", "1 -1", "2 0", (0.0, -1.0, 2.0, 0.0)),        # the same arc, left to right
    ("2 0", "1 1", "0 0", (0.0, 0.0, 2.0, 1.0)),          # lower half
    ("1 -1", "2 0", "0 0", (0.0, -1.0, 2.0, 1.0)),        # three quarters through (1, 1)
])
def test_kicad_mod_three_point_arcs_either_way_round(start: str, mid: str, end: str,
                                                     box: tuple) -> None:
    mod = f'(footprint "A" (fp_arc (start {start}) (mid {mid}) (end {end}) (layer "F.CrtYd")))'
    assert em.primitives_from_kicad_mod(mod).courtyard_bbox == pytest.approx(box, abs=1e-12)


# KiCad 5-7 write an unlocked (not keep-upright) text as (at x y [angle] unlocked);
# KiCad 8+ moved the flag into its own (unlocked yes) child
K7_UNLOCKED = """(footprint "Unlocked_Edge" (version 20221018) (generator pcbnew) (layer "F.Cu")
  (fp_text user "${REFERENCE}" (at 0 0 unlocked) (layer "F.Fab")
    (effects (font (size 1 1) (thickness 0.15))))
  (fp_text user "PCB Edge" (at 2.6 0 90 unlocked) (layer "Dwgs.User")
    (effects (font (size 0.5 0.5) (thickness 0.1))))
  (fp_rect (start -2 -4) (end 12 4) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))
)
"""
K5_UNLOCKED = """(module Unlocked_Edge (layer F.Cu) (tedit 5A02FF57)
  (fp_text user %R (at 0 0 unlocked) (layer F.Fab) (effects (font (size 1 1))))
  (fp_text user "PCB Edge" (at 2.6 0 90 unlocked) (layer Dwgs.User))
  (fp_line (start -2 -4) (end 12 4) (layer F.CrtYd) (width 0.05))
  (pad 1 smd rect (at 0 0) (size 1 1) (layers F.Cu F.Mask))
)
"""


@pytest.mark.parametrize("mod, ref", [(K7_UNLOCKED, "${REFERENCE}"), (K5_UNLOCKED, "%R")],
                         ids=["kicad7", "kicad5"])
def test_kicad_mod_unlocked_text_positions(mod: str, ref: str) -> None:
    fp = em.primitives_from_kicad_mod(mod)
    texts = {p["text"]: (p["x"], p["y"], p["rot"]) for p in fp.prims if p["kind"] == "text"}
    assert texts == {ref: (0.0, 0.0, 0.0), "PCB Edge": (2.6, 0.0, 90.0)}
    r = em.edge_from_kicad_mod(mod)
    assert (r.source, r.axis, r.outward) == ("marker_text", "x", (1.0, 0.0))
    assert r.edge_coord_mm == pytest.approx(2.6) and r.overhang_mm == pytest.approx(9.4)


def test_kicad_mod_with_bare_teardrop_keyword_reads_as_kicad_does() -> None:
    """Some 8.99/9.0 builds wrote `(curved_edges no)filter_ratio 0.9)` inside a
    pad's teardrops (KiCad's demo RoyalBlue54L FFC footprints); KiCad reads it."""
    mod = """(footprint "Teardrop_EdgeMount" (version 20240108)
  (fp_text user "PCB Edge" (at 2 0 90) (layer "Dwgs.User"))
  (fp_rect (start -2 -4) (end 12 4) (layer "F.CrtYd"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")
    (teardrops (best_length_ratio 0.5) (curved_edges no)filter_ratio 0.9)
      (enabled yes) (allow_two_segments yes) (prefer_zone_connections yes)))
)"""
    r = em.edge_from_kicad_mod(mod)
    assert (r.source, r.edge_coord_mm, r.overhang_mm) == ("marker_text", 2.0, 10.0)


def _curve(pts: str, layer: str = '"F.CrtYd"') -> str:
    return f'(footprint "C" (fp_curve (pts {pts}) (layer {layer}) (width 0.05)))'


@pytest.mark.parametrize("pts, box", [
    # symmetric bulge: x(0.5) = 14.5, well short of the control points' 20
    ("(xy -2 -4) (xy 20 -4) (xy 20 4) (xy -2 4)", (-2.0, -4.0, 14.5, 4.0)),
    # y: B'(t) is linear (one root, t = 0.5, y = 1.5); x: B'(t) is constant (no root)
    ("(xy 0 0) (xy 1 2) (xy 2 2) (xy 3 0)", (0.0, 0.0, 3.0, 1.5)),
    # monotonic in both axes: the ends span it
    ("(xy 0 0) (xy 1 1) (xy 2 2) (xy 3 3)", (0.0, 0.0, 3.0, 3.0)),
])
def test_kicad_mod_bezier_courtyard_exact_bbox(pts: str, box: tuple) -> None:
    for layer in ('"F.CrtYd"', "B.CrtYd"):                     # KiCad 6+ and KiCad 5 spellings
        bbox = em.primitives_from_kicad_mod(_curve(pts, layer)).courtyard_bbox
        assert bbox == pytest.approx(box, abs=1e-9)


def _bezier_sampled(p: list[tuple[float, float]], n: int = 20000) -> tuple:
    pts = []
    for t in (k / n for k in range(n + 1)):
        w = ((1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3)
        pts.append(tuple(sum(wk * q[i] for wk, q in zip(w, p, strict=True)) for i in (0, 1)))
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize("p", [
    [(0, 0), (4, 3), (-1, -2), (1, 5)],          # S-curve: two extremes per axis
    [(5, -1), (-3, 7), (9, 7), (2, -4)],
    [(0, 0), (0, 0), (3, 3), (3, 3)],            # coincident control points
])
def test_kicad_mod_bezier_bbox_matches_dense_sampling(p: list) -> None:
    pts = " ".join(f"(xy {x} {y})" for x, y in p)
    got = em.primitives_from_kicad_mod(_curve(pts)).courtyard_bbox
    assert got == pytest.approx(_bezier_sampled(p), abs=1e-6)


def test_kicad_mod_bezier_courtyard_sets_the_overhang() -> None:
    mod = """(footprint "Curved_EdgeMount"
  (fp_text user "PCB Edge" (at 2 0 90) (layer "Dwgs.User"))
  (fp_curve (pts (xy -2 -4) (xy 20 -4) (xy 20 4) (xy -2 4)) (layer "F.CrtYd"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))"""
    r = em.edge_from_kicad_mod(mod)
    assert (r.source, r.outward) == ("marker_text", (1.0, 0.0))
    assert r.overhang_mm == pytest.approx(12.5)


def test_kicad_mod_courtyard_is_made_of_shapes_only() -> None:
    """KiCad builds a courtyard from graphic shapes only (texts and zones on
    F.CrtYd do not count; the stock 9.0 library has 5 such zones)."""
    mod = """(footprint "Z"
  (fp_rect (start 0 0) (end 1 1) (layer "F.CrtYd"))
  (fp_text user "far" (at 50 50) (layer "F.CrtYd"))
  (fp_text_box "box" (start 40 40) (end 60 60) (layer "B.CrtYd"))
  (zone (layer "F.CrtYd") (polygon (pts (xy 40 40) (xy 60 40) (xy 60 60)))))"""
    assert em.primitives_from_kicad_mod(mod).courtyard_bbox == pytest.approx((0, 0, 1, 1))


@pytest.mark.parametrize("mod, what", [
    (_curve("(xy 0 0) (xy 1 1) (xy 2 2)"), "fp_curve needs 4 points"),
    ('(footprint "E" (fp_ellipse (center 0 0) (layer "F.CrtYd")))',
     r"unsupported courtyard shape \(fp_ellipse"),
    ('(footprint "A" (pad "1" smd rect (size 1 1)))', r"pad without \(at x y\)"),
    ('(footprint "A" (fp_line (start 0 0) (layer "Dwgs.User")))', r"fp_line without \(end"),
    ('(footprint "A" (fp_rect (start 0 0) (end 1) (layer "F.CrtYd")))', r"without \(end"),
])
def test_kicad_mod_missing_coordinates_raise(mod: str, what: str) -> None:
    with pytest.raises(ValueError, match=what):
        em.primitives_from_kicad_mod(mod)


@pytest.mark.parametrize("text", ['(kicad_pcb (version 1))', "", "(footprint"])
def test_kicad_mod_rejects_non_footprints(text: str) -> None:
    with pytest.raises(ValueError):
        em.edge_from_kicad_mod(text)


# ----------------------------------------------------------------------------
# module contract: small, stdlib only, Python 3.11 syntax
# ----------------------------------------------------------------------------
def test_module_is_small_and_stdlib_only() -> None:
    src = SOURCE.read_text(encoding="utf-8")
    assert len(src.splitlines()) < 300
    tree = ast.parse(src, feature_version=(3, 11))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module}
    lazy = {"sexpr_read"}                       # venv-side reader, imported inside a function
    assert names - lazy <= set(sys.stdlib_module_names) | {"__future__"}, names
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert not top & lazy


# ----------------------------------------------------------------------------
# live: the stock KiCad 9 library, KiCad 9's bundled Python
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module")
def kicad9() -> env.KiCadInstall:
    """The KiCad 9.x install (probed on first use, never at collection)."""
    found = [i for i in env.discover() if i.version_tuple[0] == 9]
    if not found:
        pytest.skip(f"no KiCad 9 install: {env.probe_errors()}")
    return found[0]


@pytest.fixture(scope="module")
def coax9(kicad9: env.KiCadInstall) -> Path:
    root = env.footprints_dir(kicad9)
    lib = root / "Connector_Coaxial.pretty" if root else None
    if lib is None or not lib.is_dir():
        pytest.skip(f"no KiCad 9 share/footprints/Connector_Coaxial.pretty under {kicad9.root}")
    return lib


def _stock(lib: Path, name: str) -> em.EdgeMount:
    path = lib / f"{name}.kicad_mod"
    if not path.is_file():
        pytest.skip(f"{path} missing")
    return em.edge_from_kicad_mod(path.read_text(encoding="utf-8"))


def test_live_stock_samtec_sma_edge_mount(coax9: Path) -> None:
    r = _stock(coax9, "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount")
    assert r.edge_mount and r.source == "marker_line"          # Dwgs.User line beside 'PCB Edge'
    assert (r.axis, r.outward) == ("x", (1.0, 0.0))
    assert r.edge_coord_mm == pytest.approx(2.1) and r.overhang_mm > 5.0
    assert r.overhang_mm == pytest.approx(12.12 - 2.1)


@pytest.mark.parametrize("name", ["U.FL_Hirose_U.FL-R-SMT-1_Vertical",
                                  "U.FL_Molex_MCRF_73412-0110_Vertical"])
def test_live_stock_vertical_ufl_is_not_edge_mount(coax9: Path, name: str) -> None:
    assert _stock(coax9, name) == em.NOT_EDGE_MOUNT


def test_live_stock_coaxial_library_classification(coax9: Path) -> None:
    """Every Connector_Coaxial footprint: EdgeMount names and 'PCB Edge'
    markers are edge-mount with a direction and an overhang; Vertical parts
    are not."""
    for path in sorted(coax9.glob("*.kicad_mod")):
        text = path.read_text(encoding="utf-8")
        r = em.edge_from_kicad_mod(text)
        marked = "EdgeMount" in path.stem or '"pcb edge"' in text.lower()
        assert r.edge_mount == marked, path.stem
        if marked:
            assert r.outward != (0.0, 0.0) and r.overhang_mm > 0.0, (path.stem, r)
        if "Vertical" in path.stem:
            assert not r.edge_mount, path.stem


def test_live_stock_name_only_sma(coax9: Path) -> None:
    r = _stock(coax9, "SMA_Amphenol_132289_EdgeMount")         # no marker: name + pads
    assert (r.source, r.axis, r.outward) == ("name", "x", (1.0, 0.0))
    assert r.edge_coord_mm == pytest.approx(2.54) and r.overhang_mm == pytest.approx(11.93)


def test_live_stock_dvi_leader_reaches_the_edge_line(kicad9: env.KiCadInstall) -> None:
    lib = (env.footprints_dir(kicad9) or Path("-")) / "Connector_Video.pretty"
    r = _stock(lib, "DVI-D_Molex_74320-4004_Horizontal")
    assert (r.source, r.axis, r.outward) == ("marker_line", "y", (0.0, 1.0))
    assert r.edge_coord_mm == pytest.approx(9.0424)


_LIVE = """import sys
sys.path.insert(0, sys.argv[1])
import edge_mount as em
r = em.edge_from_primitives([{"kind": "text", "layer": "Dwgs.User", "x": 2.5, "y": 0,
                              "rot": 90, "text": "PCB Edge"}], (-2, -4, 12, 4))
print(list(sys.version_info[:2]), r.source, r.axis, r.overhang_mm,
      em.straddle_issue("J1", 3.0) is not None,
      sorted(m for m in ("pcbnew", "sexpdata", "sexpr_read") if m in sys.modules))
"""


def test_live_imports_under_kicad9_python(kicad9: env.KiCadInstall) -> None:
    if kicad9.python is None:
        pytest.skip(f"KiCad {kicad9.version} has no bundled Python: {kicad9.python_error}")
    proc = subprocess.run([str(kicad9.python), "-c", _LIVE, str(TOOLS)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    line = proc.stdout.strip().splitlines()[-1]
    assert line == "[3, 11] marker_text x 9.5 True []", line

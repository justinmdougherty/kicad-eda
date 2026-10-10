"""G1 outline lint rules (F3-09): server/tools/lint_rules_outline.py.

outline_lint(design, base_dir) -> (errors, warnings) checks the netlist ``board``
block (ROADMAP_DECISIONS D2) and what sits on it before any board is built:
EO1-EO5 errors and WO1-WO5 warnings; legacy boards yield ([], []). The module
registers itself with lint_netlist (ORDER 30, check(design, ctx)). DXF designs
are generated in tmp with tests/fixtures/dxf_builders.py.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

import lint_netlist
import lint_rules_outline as lro
from fixtures.dxf_builders import Sketch, fusion_board, fusion_doc, new_doc, rect_poly, save
from lint_context import LintContext
from lint_rules_outline import outline_lint

ROOT = Path(__file__).resolve().parents[1]
T10 = json.loads((ROOT / "tests" / "fixtures" / "outline" / "t10_board.json")
                 .read_text(encoding="utf-8"))
T90 = math.tan(math.pi / 8)                      # bulge of a 90-degree arc
SHARP_L = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]
L_LOOP = [[0, 0], [32, 0], [32, 19, -T90], [33, 20], [60, 20], [60, 45], [0, 45]]  # R1 fillet
L_HOLES = {"positions": [[4, 4], [28, 4], [4, 41], [56, 41]]}
STADIUM_CENTRE = [30, 9]                         # T10 cutout 1: bbox (27, 4)-(33, 14)


def _part(ref: str, **extra: object) -> dict:
    return {"ref": ref, "lib_id": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric",
            **extra}


def _design(board: object, *parts: dict, **extra: object) -> dict:
    return {"components": list(parts) or [_part("R1")], "board": board, **extra}


def _t10(*parts: dict, **extra: object) -> dict:
    design = copy.deepcopy(T10)
    design["components"] += list(parts)
    return {**design, **extra}


def _l_design(**extra: object) -> dict:
    """A clean L-shaped board: fixed parts by at and x/y, auto parts, holes at given
    positions, a preroute, an antenna keep-out strip and a height zone."""
    parts = [_part("J1", footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                   at=[10, 10, 0]),
             _part("U1", x=45, y=30), _part("R1"), _part("C1", side="B")]
    preroute = {"net": "VIN", "width": 1.0, "via_size": [1.2, 0.6],
                "path": [["pad", "J1", "1"], ["xy", 20, 30], ["xy", 40, 30, 0.5]],
                "vias": [[40, 32]]}
    design = _design({"outline": {"loop": L_LOOP}}, *parts, mounting_holes=L_HOLES,
                     preroutes=[preroute], antenna_keepout={"edge": "right", "depth": 6},
                     height_zones=[{"x": 40, "y": 25, "w": 10, "h": 10, "max_height": 5}])
    return {**design, **extra}


def _codes(messages: list[str]) -> list[str]:
    return [m.split(" ", 1)[0] for m in messages]


# --- registration and the clean designs ----------------------------------------------------
def test_registered_with_lint_netlist_at_order_30() -> None:
    rules = {r.name: r for r in lint_netlist.rule_modules()}
    assert rules["lint_rules_outline"].order == lro.ORDER == 30
    assert rules["lint_rules_outline"].check is lro.check


def test_every_example_lints_clean(example_netlists: list[Path]) -> None:
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        assert outline_lint(design, path.parent) == ([], []), path.parent.name
        assert outline_lint(design) == ([], []), path.parent.name


@pytest.mark.parametrize("design", [_t10(), _l_design()], ids=["T10", "L"])
def test_t10_and_l_shape_designs_are_clean(design: dict) -> None:
    before = copy.deepcopy(design)
    assert outline_lint(design) == ([], [])
    assert design == before                                      # never mutated


@pytest.mark.parametrize("board", [None, {}, {"w": 60, "h": 40, "auto_x": 3},
                                   {"w": 60, "h": 40, "cutouts": []}],
                         ids=["absent", "empty", "rect-auto", "rect-no-cutouts"])
def test_legacy_boards_yield_nothing_even_with_odd_parts(board: object) -> None:
    design = _design(board, _part("R1", x=-50, y=500), mounting_holes={"positions": [[-9, -9]]},
                     preroutes=[{"net": "X", "width": 1, "path": [["xy", -5, -5]]}])
    if board is None:
        del design["board"]
    assert outline_lint(design) == ([], [])


# --- EO1 -------------------------------------------------------------------------------------
@pytest.mark.parametrize(("board", "needle"), [
    ({"w": 10, "h": 10, "corner_radius": 6}, "corner_radius 6 must be in [0, 5]"),
    ({"outline": {"loop": [[0, 0], [10, 10], [10, 0], [0, 10]]}}, "intersect"),
    ({"outline": {"loop": [[0, 0], [10, 0]]}}, "encloses no area"),
    ({"w": 20, "h": 20, "cutouts": [{"circle": [25, 5, 2]}]}, "cutout 1 lies outside"),
    ({"w": 20, "h": 20, "cutouts": [{"circle": [19, 5, 2]}]}, "cutout 1 crosses the outline"),
    ({"w": 20, "h": 20, "cutouts": [{"circle": [10, 10, 3]}, {"circle": [12, 10, 3]}]},
     "cutout 1 and cutout 2 overlap"),
    ({"outline": {"loop": [[0, 0], [9, 0], [9, 9]], "dxf": "a.dxf"}}, "both 'loop' and 'dxf'"),
    ({"auto_x": 5}, "give w and h"),
], ids=["radius", "self-crossing", "open", "cutout-outside", "cutout-crossing", "overlap",
        "loop-and-dxf", "no-geometry"])
def test_eo1_unresolvable_board_reports_the_resolver_text(board: dict, needle: str) -> None:
    errors, warnings = outline_lint(_design(board, _part("R1", x=1, y=1)))
    assert warnings == [] and len(errors) == 1
    assert errors[0].startswith("EO1 board outline: ") and needle in errors[0]


def test_eo1_missing_dxf_with_base_dir(tmp_path: Path) -> None:
    errors, warnings = outline_lint(_design({"outline": {"dxf": "gone.dxf"}}), tmp_path)
    assert _codes(errors) == ["EO1"] and "gone.dxf" in errors[0] and warnings == []


# --- EO2 -------------------------------------------------------------------------------------
def test_eo2_a_part_inside_the_stadium_cutout() -> None:
    errors, warnings = outline_lint(_t10(_part("R9", x=STADIUM_CENTRE[0], y=STADIUM_CENTRE[1])))
    assert errors == ["EO2 R9: fixed part's courtyard-bbox centre x/y (30.000, 9.000) is "
                      "inside cutout 1 (board frame, mm)"]
    assert warnings == []


def test_eo2_off_the_board_by_at_and_deep_in_the_l_bay() -> None:
    design = _l_design()
    design["components"] += [_part("R8", at=[75, 30, 90]), _part("R9", x=50, y=5)]
    errors, _ = outline_lint(design)
    assert errors == [
        "EO2 R8: fixed part's footprint origin 'at' (75.000, 30.000) is off the board, "
        "15.000 mm from its material (board frame, mm)",
        "EO2 R9: fixed part's courtyard-bbox centre x/y (50.000, 5.000) is off the board, "
        "15.000 mm from its material (board frame, mm)"]


def test_eo2_a_mid_mount_connector_in_an_edge_notch_is_a_warning() -> None:
    """KiCad's tiny_tapeout demo: a USB-C mid-mount receptacle sits in a 9 x 6.6 mm notch,
    its footprint origin off the material. Within OFF_BOARD_REACH_MM: warned, not refused."""
    notch = [[0, 0], [20, 0], [20, 6], [30, 6], [30, 0], [60, 0], [60, 40], [0, 40]]
    errors, warnings = outline_lint(_design({"outline": {"loop": notch}},
                                            _part("J1", at=[25, 2, 180])))
    assert errors == [] and lro.OFF_BOARD_REACH_MM == 10.0
    assert "EO2 J1: fixed part's footprint origin 'at' (25.000, 2.000) is off the material, " \
           "4.000 mm past the board outline: only a part straddling that edge (a mid-mount " \
           "connector in a notch, an overhang) may sit there; G3 checks the courtyard " \
           "(board frame, mm)" in warnings


def test_eo2_panel_cutouts_let_a_part_straddle_a_cutout_edge() -> None:
    design = _t10(_part("J9", x=STADIUM_CENTRE[0], y=STADIUM_CENTRE[1]))
    design["board"] = {**design["board"], "panel_cutouts": True}
    errors, warnings = outline_lint(design)
    assert errors == [] and warnings == [
        "EO2 J9: fixed part's courtyard-bbox centre x/y (30.000, 9.000) is off the material, "
        "3.000 mm past cutout 1: only a part straddling that edge (a mid-mount connector in a "
        "notch, an overhang) may sit there; G3 checks the courtyard (board frame, mm)"]


def test_eo2_warns_within_1_mm_of_an_edge_but_not_for_an_overhang_part() -> None:
    near = _part("R7", x=30, y=3.5)                       # 0.5 mm above the stadium's top
    hang = _part("AE1", x=59.5, y=20, overhang=5)        # 0.5 mm in from the outer edge
    errors, warnings = outline_lint(_t10(near, hang))
    assert errors == []
    assert warnings == ["EO2 R7: fixed part's courtyard-bbox centre x/y (30.000, 3.500) is "
                        "0.500 mm from cutout 1; its courtyard may cross it (G3 checks the "
                        "courtyard) (board frame, mm)"]


def test_eo2_malformed_position_is_an_error_not_a_crash() -> None:
    errors, _ = outline_lint(_t10(_part("R6", at=["a", 1, 0])))
    assert _codes(errors) == ["EO2"] and "R6" in errors[0] and "not a point" in errors[0]


# --- EO3 -------------------------------------------------------------------------------------
def test_eo3_default_corner_holes_on_the_l_shape() -> None:
    errors, _ = outline_lint(_l_design(mounting_holes=True))
    assert len(errors) == 1 and errors[0].startswith("EO3 ")
    assert lro.NON_RECT_HOLES in errors[0]
    assert lro.NON_RECT_HOLES == "give mounting_holes.positions for a non-rectangular outline"


def test_eo3_default_corner_holes_off_a_round_corner() -> None:
    board = {"w": 30, "h": 30, "corner_radius": 12}     # inset 4 sits outside the r12 corners
    errors, _ = outline_lint(_design(board, mounting_holes={}))
    assert errors == []                                   # {} = no holes requested
    errors, _ = outline_lint(_design(board, mounting_holes=True))
    assert _codes(errors) == ["EO3"] * 4
    assert errors[0].startswith("EO3 mounting hole H1: drill 3.2 mm at (4.000, 4.000) reaches "
                                "off the board")
    assert "give mounting_holes.positions or another inset" in errors[0]


def test_eo3_default_corner_holes_fit_a_rounded_rect() -> None:
    board = {"w": 60, "h": 40, "corner_radius": 3}
    assert outline_lint(_design(board, mounting_holes={"inset": 4.0})) == ([], [])
    errors, _ = outline_lint(_design(board, mounting_holes={"inset": "4"}))
    assert errors == ["EO3 mounting_holes.inset '4' is not a number (mm)"]
    errors, _ = outline_lint(_design(board, mounting_holes={"positions": {"a": 1}}))
    assert _codes(errors) == ["EO3"] and "must be a list of [x, y]" in errors[0]


def test_eo3_drill_from_the_footprint_name_and_edge_warning() -> None:
    holes = {"footprint": "MountingHole:MountingHole_2.2mm_M2", "positions": [[1.2, 20]]}
    errors, warnings = outline_lint(_l_design(mounting_holes=holes))
    assert errors == []                                   # 2.2 drill: 0.1 mm of material
    assert _codes(warnings) == ["EO3"] and "H1: drill 2.2 mm at (1.200, 20.000)" in warnings[0]
    assert "0.100 mm from the board outline" in warnings[0]
    holes = {"positions": [[1.2, 20]]}                    # default M3: 3.2 drill reaches off
    errors, _ = outline_lint(_l_design(mounting_holes=holes))
    assert errors == ["EO3 mounting hole H1: drill 3.2 mm at (1.200, 20.000) reaches off the "
                      "board (board frame, mm)"]


def test_eo3_hole_in_a_cutout_and_malformed_positions() -> None:
    holes = {"positions": [[30, 20], "x", [1, None]]}
    errors, _ = outline_lint(_t10(mounting_holes=holes))
    assert _codes(errors) == ["EO3"] * 3
    assert "positions[1]" in errors[0] and "positions[2]" in errors[1]
    assert "H1: drill 3.2 mm at (30.000, 20.000) reaches into cutout 2" in errors[2]


# --- EO4 -------------------------------------------------------------------------------------
def test_eo4_preroute_points_tracks_and_vias() -> None:
    pr = {"net": "VBUS", "width": 0.8,
          "path": [["xy", 30, 20], ["pad", "J1", "1"], ["xy", 20, 9], ["xy", 40, 9],
                   ["xy", 40, 30], ["xy", 0.2, 30]],
          "vias": [[59.8, 20], [10, 39]]}
    errors, warnings = outline_lint(_t10(preroutes=[{"net": "GND", "width": 1, "path": []}, pr]))
    assert warnings == []
    assert errors == [
        "EO4 preroutes[1] (net 'VBUS') path[0] (30.000, 20.000) is inside cutout 2 "
        "(board frame, mm)",
        "EO4 preroutes[1] track on net 'VBUS': copper extends into cutout 1 at "
        "(30.000, 9.000) (board frame, mm)",
        "EO4 preroutes[1] track on net 'VBUS': copper extends past the board outline at "
        "(-0.200, 30.000) (board frame, mm)",
        "EO4 preroutes[1] via on net 'VBUS': copper extends past the board outline at "
        "(60.400, 20.000) (board frame, mm)"]


def test_eo4_xy_point_off_the_board_and_malformed_entries() -> None:
    pr = {"net": "X", "width": 1, "path": [["xy", 70, 5], ["xy", "a", 1]], "vias": ["v"]}
    errors, _ = outline_lint(_t10(preroutes=[pr]))
    assert errors[0] == ("EO4 preroutes[0] (net 'X') path[0] (70.000, 5.000) is off the board "
                         "(board frame, mm)")
    assert _codes(errors) == ["EO4"] * 3 and "path[1]" in errors[1] and "vias[0]" in errors[2]


# --- EO5 / WO1 -------------------------------------------------------------------------------
def test_eo5_fab_error_board_too_small() -> None:
    errors, _ = outline_lint(_design({"w": 2.5, "h": 20, "corner_radius": 0.5}))
    assert _codes(errors) == ["EO5"] and "minimum is 3 x 3 mm" in errors[0]


def test_eo5_wo1_ignore_sub_micron_shortfalls_of_rounded_coordinates() -> None:
    """A 1 mm slot turned 30 degrees on KiCad's 1 nm grid (as the KiCad importer writes
    it) measures 0.9999995 mm with R0.4999995 ends: grid noise, not a fab violation."""
    slot = {"loop": [[36.883975, 28.397114], [38.616025, 27.397114, -1.0],
                     [38.116025, 26.531089], [36.383975, 27.531089, -1.000000999999]]}
    board = {"w": 40, "h": 30, "corner_radius": 2, "cutouts": [slot]}
    assert outline_lint(_design(board, _part("R1", x=10, y=10))) == ([], [])
    noisy = {"loop": [[30, 10], [34, 10], [34, 10.9999996], [30, 10.9999996]]}
    errors, _ = outline_lint(_design({**board, "cutouts": [noisy]}, _part("R1", x=10, y=10)))
    assert errors == []
    narrow = {"loop": [[30, 10], [34, 10], [34, 10.99], [30, 10.99]]}
    errors, warnings = outline_lint(_design({**board, "cutouts": [narrow]},
                                            _part("R1", x=10, y=10)))
    assert _codes(errors) == ["EO5"] and "milled gap 0.990 mm" in errors[0]
    assert _codes(warnings) == ["WO1"] * 4 and "sharp internal corner" in warnings[0]


def test_wo1_fab_warnings_keep_the_assumption_marker() -> None:
    thin = {"w": 40, "h": 40, "cutouts": [{"circle": [20, 3, 2.5]}]}   # 0.5 mm web at the top
    errors, warnings = outline_lint(_design(thin, _part("R1", x=10, y=30)))
    assert errors == [] and _codes(warnings) == ["WO1"]
    assert "0.500 mm wide" in warnings[0] and "[ASSUMPTION: JLCPCB publishes" in warnings[0]


def test_wo1_sharp_internal_corner_of_the_unfilleted_l() -> None:
    errors, warnings = outline_lint(_design({"outline": {"loop": SHARP_L}},
                                            _part("R1", x=10, y=40)))
    assert errors == [] and _codes(warnings) == ["WO1"]
    assert "sharp internal corner at (32.0000, 20.0000)" in warnings[0]


# --- WO2 -------------------------------------------------------------------------------------
def test_wo2_no_auto_field_when_the_explicit_window_is_off_the_board() -> None:
    board = {"w": 60, "h": 40, "corner_radius": 3, "auto_x": 100, "auto_y": 100, "auto_w": 5,
             "auto_h": 5}
    errors, warnings = outline_lint(_design(board, _part("R1"), _part("R2")))
    assert errors == [] and _codes(warnings) == ["WO2"]
    assert warnings[0].startswith("WO2 2 auto-placed part(s) but no auto-placement field: ")
    assert "keeps no usable part on the board" in warnings[0]


def test_wo2_inscribed_field_under_20_percent_of_the_board() -> None:
    ring = {"w": 40, "h": 40, "cutouts": [{"circle": [20, 20, 17]}]}
    errors, warnings = outline_lint(_design(ring, _part("R1")))
    assert errors == [] and _codes(warnings) == ["WO2"]
    assert "the inscribed auto-placement field" in warnings[0] and "(under 20%)" in warnings[0]
    fixed = _design(ring, _part("R1", x=2, y=20))         # no auto parts: no field needed
    assert outline_lint(fixed) == ([], [])


def test_wo2_clipped_explicit_field_note_passes_through_for_sides_with_parts() -> None:
    board = {"outline": {"loop": L_LOOP}, "auto_x": 30, "auto_y": 0, "auto_w": 20, "auto_h": 30}
    errors, warnings = outline_lint(_design(board, _part("R1")))
    assert errors == [] and len(warnings) == 1                       # no B parts: F only
    assert warnings[0].startswith("WO2 explicit F auto field (30.000, 2.000)-(50.000, 32.000) "
                                  "leaves the board") and "clipped to" in warnings[0]
    _, warnings = outline_lint(_design(board, _part("R1"), _part("C1", side="b")))
    assert [w[:21] for w in warnings] == ["WO2 explicit F auto f", "WO2 explicit B auto f"]


def test_wo2_explicit_field_is_not_size_checked() -> None:
    board = {"w": 60, "h": 40, "corner_radius": 3, "auto_x": 5, "auto_y": 3, "auto_w": 10,
             "auto_h": 8}                                 # 80 mm^2: 3% of the board, explicit
    assert outline_lint(_design(board, _part("R1"))) == ([], [])


# --- WO3 -------------------------------------------------------------------------------------
def test_wo3_keepout_rect_and_height_zone_outside_the_bbox() -> None:
    design = _t10(antenna_keepout={"rect": [50, -2, 20, 8]},
                  height_zones=[{"x": 0, "y": 0, "w": 60, "h": 40, "max_height": 9},
                                {"x": 55, "y": 30, "w": 10, "h": 20, "max_height": 3,
                                 "name": "LID"}])
    errors, warnings = outline_lint(design)
    assert errors == []
    assert warnings == [
        "WO3 antenna_keepout [50, -2, 20, 8] extends outside the outline bbox "
        "0..60 x 0..40 mm (board frame)",
        "WO3 height_zones[1] 'LID' [55, 30, 10, 20] extends outside the outline bbox "
        "0..60 x 0..40 mm (board frame)"]


def test_wo3_edge_strip_deeper_than_the_board() -> None:
    _, warnings = outline_lint(_t10(antenna_keepout={"edge": "top", "depth": 50}))
    assert _codes(warnings) == ["WO3"] and "[0, 0, 60, 50]" in warnings[0]


def test_wo3_malformed_keepout_and_zones_are_reported_not_raised() -> None:
    errors, warnings = outline_lint(_t10(antenna_keepout={"rect": [1, 2]},
                                         height_zones=["z", {"x": 1, "y": "a", "w": 1, "h": 1}]))
    assert errors == [] and _codes(warnings) == ["WO3"] * 3
    assert "antenna_keepout" in warnings[0] and "not checked" in warnings[0]
    assert "height_zones[0]" in warnings[1] and "height_zones[1]" in warnings[2]


# --- DXF: WO4 / WO5 --------------------------------------------------------------------------
def _fusion_design(tmp_path: Path, *parts: dict) -> dict:
    doc, msp = fusion_doc()
    fusion_board(Sketch(msp, k=10.0))      # 60 x 40 r3, 1.6 mm hole at (10, 10), slot
    (tmp_path / "mech").mkdir()
    save(doc, tmp_path / "mech", "outline.dxf")
    return _design({"outline": {"dxf": "mech/outline.dxf"}}, *parts)


def test_dxf_design_resolves_with_base_dir_and_wo5_without(tmp_path: Path) -> None:
    design = _fusion_design(tmp_path, _part("R1"), _part("R2", x=35, y=30))
    assert outline_lint(design, tmp_path) == ([], [])
    assert outline_lint(design, str(tmp_path)) == ([], [])
    errors, warnings = outline_lint(design)
    assert errors == [] and _codes(warnings) == ["WO5"]
    assert lro.NO_BASE_DIR in warnings[0]
    assert lro.NO_BASE_DIR == "DXF outline not checked: lint ran without base_dir"


def test_dxf_outline_is_checked_once_resolved(tmp_path: Path) -> None:
    design = _fusion_design(tmp_path, _part("R2", x=35, y=30))
    design["components"].append(_part("R3", x=10, y=10))          # on the DXF's 1.6 mm hole
    errors, _ = outline_lint(design, tmp_path)
    assert errors == ["EO2 R3: fixed part's courtyard-bbox centre x/y (10.000, 10.000) is "
                      "inside cutout 2 (board frame, mm)"]


def test_wo4_dxf_reader_warnings_pass_through(tmp_path: Path) -> None:
    doc, msp = new_doc(insunits=0)                         # unitless: read as mm, warned
    rect_poly(msp, 0, 0, 50, 30)
    save(doc, tmp_path, "plain.dxf")
    errors, warnings = outline_lint(_design({"outline": {"dxf": "plain.dxf"}},
                                            _part("R1", x=25, y=15)), tmp_path)
    assert errors == [] and _codes(warnings) == ["WO4"]
    assert "$INSUNITS" in warnings[0] and "plain.dxf" in warnings[0]


def test_check_takes_base_dir_from_the_lint_context(tmp_path: Path) -> None:
    design = _fusion_design(tmp_path, _part("R2", x=35, y=30))
    assert lro.check(design, LintContext(netlist_dir=tmp_path)) == ([], [])
    assert _codes(lro.check(design, LintContext())[1]) == ["WO5"]
    rep = lint_netlist.lint(_t10(_part("R9", x=30, y=9)))
    assert any(e.startswith("EO2 R9:") for e in rep["errors"]) and rep["ok"] is False

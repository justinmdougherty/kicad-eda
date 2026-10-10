"""swig_build_board's placement (F3-12): build_layout.py.

D3 "at" parts (footprint origin + final rotation, F and B side) are placed by
swig_anchor.place_at; x/y parts keep their courtyard-centre meaning; both are
fixed. Auto parts pack into board_field fields: legacy_fields on w/h boards
(bit-identical to the builder before F3-12), auto_fields on outline boards.
A component "edge" turns a connector to that edge (place_engine conn_edges)
and snaps it onto the outer board edge (board_field.snap_to_edge), hanging
its "overhang", else the overhang derived from an edge-mount footprint's
'PCB Edge' marker (edge_mount), past it. Every courtyard is checked with
outline_audit.courtyard_issues (result "containment" + warnings).

Unit tier (dev venv, no pcbnew; place_engine imported with a stub pcbnew).

Live tier (KiCad Python; 9.0.4 locally, 10.0.5 in kicad-eda:wave1, RULES.md "K10"):
 1. B-side "at" parts at rot 45 / 90 and F-side at rot 0 / 270 keep their
    origin (+/- 1 nm), rotation and side through the builder's save and a
    further SaveBoard / LoadBoard;
 2. an L-shaped board with a USB-C "at" in the bay and 6 auto parts: every
    auto courtyard lies on the board, none in the notch;
 3. the T10 board with J1 auto and "edge": "left": its mouth faces -x and its
    courtyard is within 3 mm of the left edge;
 4. rpi5_battery_hat places exactly as the builder at HEAD (ebbd57b): sorted
    (ref, position nm, rotation, side) == RPI5_GOLDEN. The golden was captured
    with tests/fixtures/outline/capture_legacy_geometry.py --builder <the HEAD
    swig_build_board.py from `git archive HEAD`> (git is not available in the
    K10 container, and HEAD stops being the reference once the wave is
    committed): on 10.0.5 with every HEAD tool, on 9.0.4 with the HEAD builder
    over the current tools (HEAD's place_engine read empty courtyard caches on
    9.0.4 and cannot pack this board); the pre-F3-12 builder matched both;
 5. a stock SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount with "edge": "right" and no
    overhang key ends with its 'PCB Edge' marker on the outline (+/- 0.05 mm),
    and derived_overhang reports it.
"""
from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import board_field as bfield
import build_features as feat
import outline as ol
import outline_resolve as orz
import toolchain as tc
from conftest import load_example
from fixtures import swig_python as sp


def _import_place_engine() -> types.ModuleType:
    """place_engine imports pcbnew at module level: stub it for this import only."""
    try:
        import pcbnew  # noqa: F401  (KiCad's bundled python only)
    except ImportError:
        pass
    else:
        return importlib.import_module("place_engine")
    stub = types.ModuleType("pcbnew")
    sys.modules["pcbnew"] = stub
    try:
        return importlib.import_module("place_engine")
    finally:
        if sys.modules.get("pcbnew") is stub:
            del sys.modules["pcbnew"]


pe = _import_place_engine()
import build_layout as bl  # noqa: E402  (after place_engine: its lazy import finds it)

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "server" / "tools" / "swig_build_board.py"
T10 = ROOT / "tests" / "fixtures" / "outline" / "t10_board.json"
BUILD_TIMEOUT_S = 600
L_SHAPE = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]
R0603, C0603 = "Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"
SOT23, SOIC8 = "Package_TO_SOT_SMD:SOT-23", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
SMA = "Connector_Coaxial:SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"
USBC = "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12"
HDR3 = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"
HDR6_H = "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Horizontal"


# --- unit tier ------------------------------------------------------------------------------
def _part(ref: str, fp: str = R0603, **kw: object) -> dict:
    return {"ref": ref, "value": "x", "footprint": fp, **kw}


def _stub_parts(design: dict, size: dict | None = None) -> tuple[dict, dict, dict]:
    """(extents, pincount, mouth): 2 x 1 mm 2-pad passives, 5 x 5 mm 8-pin ICs and
    8 x 6 mm connectors whose rot-0 mouth points down (+y)."""
    extents, pins, mouth = {}, {}, {}
    for c in design["components"]:
        ref = c["ref"]
        kind = "J" if ref[0] == "J" else ("U" if ref[0] == "U" else "P")
        w, h = (size or {}).get(ref, {"J": (8.0, 6.0), "U": (5.0, 5.0), "P": (2.0, 1.0)}[kind])
        extents[ref] = (w, h, 0.0, 0.0)
        pins[ref] = {"J": 4, "U": 8, "P": 2}[kind]
        mouth[ref] = (0.0, 1.0) if kind == "J" else (0.0, 0.0)
    return extents, pins, mouth


def _hat(**board: object) -> dict:
    return {"board": {"w": 60.0, "h": 40.0, **board},
            "components": [_part("U1", SOIC8), _part("C1", C0603), _part("R1"),
                           _part("J1", USBC, x=5.0, y=20.0, rot=270),
                           _part("R9", x=55.0, y=36.0, rot=0)],
            "nets": {"VCC": [["U1", "8"], ["C1", "1"], ["R1", "1"]],
                     "GND": [["U1", "4"], ["C1", "2"], ["J1", "A1"]]}}


def _boxes(layout: bl.Layout, extents: dict) -> dict:
    return {r: (x - extents[r][0] / 2, y - extents[r][1] / 2,
                x + extents[r][0] / 2, y + extents[r][1] / 2)
            for r, (x, y) in layout.targets.items()}


def test_fixed_refs_include_at_parts() -> None:
    design = {"components": [_part("R1", at=[1, 2, 0]), _part("R2", x=1, y=2),
                             _part("R3", x=1), _part("R4")]}
    assert bl.fixed_refs(design) == {"R1", "R2"}
    assert bl.anchor_refs(design) == ["R1"]


@pytest.mark.parametrize("comp,match", [
    (_part("R1", at=[1, 2, 0], x=1, y=2), "never both"),
    (_part("R1", at=[1, 2, 0], rot=90), "never both"),
    (_part("R1", at=[1, 2]), "'at'"),
    (_part("R1", at=[1, 2, "a"]), "'at'"),
    (_part("R1", at=[1, 2, 0], side="top"), "side"),
    (_part("J1", USBC, edge="north"), "edge")])
def test_bad_anchor_or_edge_is_refused(comp: dict, match: str) -> None:
    with pytest.raises(feat.BuildError, match=match):
        bl.check_design({"components": [comp]})


def test_conn_edges_skip_null() -> None:
    design = {"components": [_part("J1", edge="left"), _part("J2", edge=None), _part("J3")]}
    assert bl.conn_edges(design) == {"J1": "left"}


def test_override_replaces_at_without_mutating() -> None:
    design = {"components": [_part("R1", at=[1, 2, 45], side="B"), _part("R2", x=3, y=4)]}
    before = json.dumps(design, sort_keys=True)
    out = bl.apply_overrides(design, {"R1": [5, 6, 90], "R2": [7, 8, 0]})
    assert json.dumps(design, sort_keys=True) == before
    assert out["components"][0] == {**_part("R1", side="B"), "x": 5, "y": 6, "rot": 90}
    assert list(out["components"][1]) == ["ref", "value", "footprint", "x", "y", "rot"]
    assert bl.apply_overrides(design, {}) == design


def test_legacy_mixed_targets_use_the_legacy_fields() -> None:
    design = _hat(auto_x=20.0, auto_y=2.0, auto_w=30.0, auto_h=25.0)
    parts = _stub_parts(design)
    layout = bl.compute_targets(design, None, (50.0, 25.0), parts, 0.3)
    fields = bfield.legacy_fields(design["board"], 60.0, 40.0, scale=1.0, has_conn=False)
    assert layout.fields == tuple(fields) and (layout.w, layout.h) == (60.0, 40.0)
    assert layout.targets["J1"] == (5.0, 20.0)
    packed, _rots, used_w, _h = pe.functional_place(
        {**design, "components": design["components"][:3]}, *parts, margin=1.5,
        width_limit=fields[0].w)
    x0, y0 = bfield.cluster_origin(fields[0], used_w)
    assert {r: layout.targets[r] for r in packed} == {
        r: (x0 + bx, y0 + by) for r, (bx, by) in packed.items()}


def test_auto_sized_board_is_unchanged_by_the_refactor() -> None:
    design = {"components": [_part("U1", SOIC8), _part("C1", C0603), _part("J1", USBC)],
              "nets": {"VCC": [["U1", "8"], ["C1", "1"]], "GND": [["U1", "4"], ["J1", "A1"]]}}
    parts = _stub_parts(design)
    layout = bl.compute_targets(design, None, (50.0, 25.0), parts, 0.3)
    packed, rots, used_w, used_h = pe.functional_place(design, *parts, margin=1.5)
    assert (layout.w, layout.h) == (round(used_w + 2 * 2.0, 2), round(used_h + 1.2 + 2.0, 2))
    assert layout.targets == {r: (x + 2.0, y + 1.2) for r, (x, y) in packed.items()}
    assert layout.rots == rots and layout.fields == ()


def test_outline_board_packs_into_the_inscribed_field() -> None:
    design = {"board": {"outline": {"loop": L_SHAPE}},
              "components": [_part("J1", USBC, at=[46.0, 24.0, 180]), _part("U1", SOIC8),
                             *(_part(f"C{i}", C0603) for i in range(1, 4)), _part("R1"),
                             _part("R2")],
              "nets": {"VCC": [["U1", "8"], ["C1", "1"], ["C2", "1"], ["R1", "1"]],
                       "GND": [["U1", "4"], ["C1", "2"], ["C3", "2"], ["R2", "2"]]}}
    outline = ol.from_board_block(design["board"])
    parts = _stub_parts(design)
    layout = bl.compute_targets(design, outline, (50.0, 25.0), parts, 0.3)
    field = bfield.auto_fields(design["board"], outline, 0.3, [])[0][0]
    assert layout.fields[0].rect == pytest.approx(field.rect)
    assert (layout.w, layout.h) == (60.0, 45.0) and "J1" not in layout.targets
    for ref, box in _boxes(layout, parts[0]).items():
        assert field.x0 - 1e-9 <= box[0] and box[2] <= field.x1 + 1e-9, (ref, box)
        assert field.y0 - 1e-9 <= box[1] and box[3] <= field.y1 + 1e-9, (ref, box)
        assert ol.contains_rect(outline, box, 0.3), (ref, box)


def test_outline_board_without_a_field_is_refused() -> None:
    design = {"board": {"outline": {"loop": [[0, 0], [1.5, 0], [1.5, 1.5], [0, 1.5]]}},
              "components": [_part("R1", x=1, y=1), _part("R2")], "nets": {}}
    outline = ol.from_board_block(design["board"])
    with pytest.raises(feat.BuildError, match="F-side"):
        bl.compute_targets(design, outline, (50.0, 25.0), _stub_parts(design), 0.3)


@pytest.mark.parametrize("edge", ["left", "right", "bottom"])
def test_edge_connector_cluster_hugs_its_field_side(edge: str) -> None:
    design = _hat(auto_x=20.0, auto_y=2.0, auto_w=30.0, auto_h=25.0)
    design["components"][3] = _part("J1", USBC, edge=edge)
    parts = _stub_parts(design)
    layout = bl.compute_targets(design, None, (50.0, 25.0), parts, 0.3)
    field = layout.fields[0]
    box = _boxes(layout, {**parts[0], "J1": parts[0]["J1"][1::-1] + (0.0, 0.0)
                          if edge != "bottom" else parts[0]["J1"]})["J1"]
    side = {"left": (0, field.x0), "right": (2, field.x1), "bottom": (3, field.y1)}[edge]
    assert box[side[0]] == pytest.approx(side[1]), (edge, box, field)
    if edge in ("left", "right"):          # a side-column-only cluster: centred vertically
        sized = {**parts[0], "J1": (6.0, 8.0, 0.0, 0.0)}
        boxes = [b for r, b in _boxes(layout, sized).items() if r not in ("J1", "R9")] + [box]
        top, bottom = min(b[1] for b in boxes), max(b[3] for b in boxes)
        assert top - field.y0 == pytest.approx(field.y1 - bottom), (top, bottom, field)


def test_edge_parts_skip_the_legacy_overhang_shift() -> None:
    design = _hat()
    design["components"][3] = _part("J1", USBC, overhang=3.0)
    parts = _stub_parts(design)
    base = bl.compute_targets(design, None, (50.0, 25.0), parts, 0.3).targets["J1"]
    design["components"][3] = _part("J1", USBC, overhang=3.0, edge="top")
    snapped = bl.compute_targets(design, None, (50.0, 25.0), parts, 0.3).targets["J1"]
    assert snapped == (base[0], base[1] + 3.0)


def test_edge_on_a_fixed_part_is_a_warning() -> None:
    design = _hat()
    design["components"][3] = _part("J1", USBC, x=5.0, y=20.0, rot=270, edge="left")
    layout = bl.compute_targets(design, None, (50.0, 25.0), _stub_parts(design), 0.3)
    assert [w for w in layout.warnings if w.startswith("J1: 'edge' is ignored")]


def test_edge_on_a_non_connector_is_refused() -> None:
    design = _hat()
    design["components"][0] = _part("U1", SOIC8, edge="left")
    with pytest.raises(feat.BuildError, match="U1"):
        bl.compute_targets(design, None, (50.0, 25.0), _stub_parts(design), 0.3)


@pytest.mark.parametrize("v,rot,flipped,want", [
    ((1, 0), 0, False, (1, 0)), ((1, 0), 90, False, (0, -1)), ((1, 0), 180, False, (-1, 0)),
    ((0, 1), 270, False, (-1, 0)), ((0, 1), 0, True, (0, -1)), ((1, 0), 0, True, (1, 0)),
    ((0, 1), 90, True, (-1, 0))])
def test_world_dir(v: tuple, rot: float, flipped: bool, want: tuple) -> None:
    assert bl.world_dir(v, rot, flipped) == pytest.approx(want, abs=1e-12)


def test_snap_target_hangs_the_overhang_past_the_outer_edge() -> None:
    o = ol.from_board_block(feat_t10())
    box = (40.0, 16.0, 54.765, 24.0)                       # SMA-like courtyard, local x -2.6
    new = bl.snap_target(o, box, "right", 10.015)
    assert new[0] + (box[2] - box[0]) / 2 == pytest.approx(70.015) and new[1] == 20.0
    # the round cutout at (30, 20) sits between the part and the left edge: ignored
    left = bl.snap_target(o, (34.0, 16.0, 40.0, 24.0), "left", 0.0)
    assert left == pytest.approx((3.0, 20.0))
    assert bl.snap_target(o, (70.0, 16.0, 80.0, 24.0), "right", 0.0) is None


def feat_t10() -> dict:
    return json.loads(T10.read_text(encoding="utf-8"))["board"]


def test_part_overhang_prefers_the_key_then_the_derived_value() -> None:
    marker = bl.em.EdgeMount(True, "x", 2.1, (1.0, 0.0), 10.015, "marker_line", (2.1, 0.0))
    assert bl.part_overhang({"overhang": 2.5}, marker) == (2.5, None)
    assert bl.part_overhang({}, marker) == (10.015, {"overhang_mm": 10.015,
                                                      "source": "marker_line"})
    assert bl.part_overhang({"overhang": None}, bl.em.NOT_EDGE_MOUNT) == (0.0, None)
    assert bl.part_overhang({}, None) == (0.0, None)


def test_containment_records_and_issues() -> None:
    o = ol.from_board_block({"w": 40, "h": 30})
    polys = {"R1": [[(1, 1), (3, 1), (3, 2), (1, 2)]],
             "J1": [[(35, 10), (45, 10), (45, 14), (35, 14)]],
             "J2": [[(-2, 20), (2, 20), (2, 22), (-2, 22)]]}
    overhang = {"J1": 6.0}
    issues = bl.containment_issues(o, polys, overhang)
    assert [i["ref"] for i in issues] == ["J2"] and issues[0]["code"] == "courtyard_outside"
    assert bl.containment_issues(o, polys, {"J1": 4.0, "J2": 3.0})[0]["code"] == \
        "courtyard_overhang"


def test_file_budgets() -> None:
    assert len(BUILDER.read_text(encoding="utf-8").splitlines()) < 650
    layout = BUILDER.parent / "build_layout.py"
    assert len(layout.read_text(encoding="utf-8").splitlines()) < 500
    assert "DEFAULT_MOUNTING_HOLE_FOOTPRINT" in BUILDER.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_pack_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PACK_SCALE", raising=False)


# --- live tier ------------------------------------------------------------------------------
# rpi5_battery_hat without the J1.SH shield pad (the stock USB-C names it per
# release): (x nm, y nm, rotation, side) of every footprint as the HEAD builder
# placed it on 9.0.4; 10.0.5 differs only in J5 (its courtyard is 30 um narrower).
RPI5_GOLDEN_K9 = {
    "C1": (134225000, 113455000, 0, "F"), "C10": (164000000, 96750000, 0, "F"),
    "C11": (173000000, 88750000, 0, "F"), "C12": (156000000, 98250000, 0, "F"),
    "C13": (152000000, 98250000, 0, "F"), "C14": (130680000, 101395000, 0, "F"),
    "C15": (124500000, 92750000, 0, "F"), "C16": (160000000, 118250000, 0, "F"),
    "C17": (128000000, 92750000, 0, "F"), "C18": (126000000, 96750000, 0, "F"),
    "C19": (133000000, 96750000, 0, "F"), "C2": (137775000, 113455000, 0, "F"),
    "C20": (134500000, 92950000, 0, "F"), "C21": (141500000, 92950000, 0, "F"),
    "C22": (158500000, 123445000, 0, "B"), "C23": (162050000, 123445000, 0, "B"),
    "C24": (165600000, 123445000, 0, "B"), "C3": (141920000, 100775000, 0, "F"),
    "C4": (141325000, 113455000, 0, "F"), "C5": (149000000, 87750000, 0, "F"),
    "C6": (123300000, 87750000, 0, "F"), "C7": (149000000, 91750000, 0, "F"),
    "C8": (156500000, 91750000, 0, "F"), "C9": (152000000, 93250000, 0, "F"),
    "D1": (151310000, 117375000, 0, "B"), "D2": (165690000, 117375000, 0, "B"),
    "D3": (141700000, 103075000, 0, "F"), "D4": (156185000, 110335000, 0, "B"),
    "H1": (119500000, 80250000, 0, "F"), "H2": (177500000, 80250000, 0, "F"),
    "H3": (119500000, 129250000, 0, "F"), "H4": (177500000, 129250000, 0, "F"),
    "J1": (118440000, 119750000, 270, "F"), "J2": (176505000, 104250000, 90, "F"),
    "J3": (177500000, 113940000, 0, "F"), "J4": (130965000, 107815000, 0, "F"),
    "J5": (124400000, 81540000, 270, "B"), "L1": (163500000, 87750000, 0, "F"),
    "L2": (138000000, 87750000, 0, "F"), "Q1": (150000000, 103750000, 0, "F"),
    "Q2": (150890000, 104495000, 0, "B"), "Q3": (163000000, 109750000, 0, "F"),
    "Q4": (164130000, 104495000, 0, "B"), "Q7": (151315000, 110884999, 0, "B"),
    "R1": (156500000, 95250000, 0, "F"), "R10": (138000000, 92950000, 0, "F"),
    "R11": (164155000, 109914999, 0, "B"), "R12": (130675000, 113455000, 0, "F"),
    "R13": (150865000, 113905000, 0, "B"), "R14": (154415000, 113905000, 0, "B"),
    "R15": (157965000, 113905000, 0, "B"), "R2": (152180000, 119845000, 0, "B"),
    "R3": (164820000, 119845000, 0, "B"), "R4": (160605000, 109914999, 0, "B"),
    "R5": (151400000, 123445000, 0, "B"), "R6": (154950000, 123445000, 0, "B"),
    "R7": (153000000, 107750000, 0, "F"), "R8": (169500000, 109750000, 0, "F"),
    "R9": (131500000, 92750000, 0, "F"), "RS1": (156500000, 103750000, 0, "F"),
    "RS2": (165000000, 103750000, 0, "F"), "TP1": (134577254, 107295999, 0, "F"),
    "TP2": (167001254, 113750000, 0, "F"), "TP3": (137501254, 97250000, 0, "F"),
    "TP4": (137670254, 107295999, 0, "F"), "TP5": (140762254, 107295999, 0, "F"),
    "U1": (136600000, 102425000, 0, "F"), "U2": (156000000, 87750000, 0, "F"),
    "U3": (130000000, 87750000, 0, "F"), "U4": (158500000, 118975000, 0, "B"),
    "U5": (160000000, 114250000, 0, "F"), "U6": (157510000, 105495000, 0, "B")}
RPI5_GOLDEN = {9: RPI5_GOLDEN_K9,
               10: {**RPI5_GOLDEN_K9, "J5": (124370000, 81520000, 270, "B")}}

INSPECT = sp.PREAMBLE + r"""
import board_outline as bo
board = bo.load_board(sys.argv[1])
_outline, (ox, oy) = bo.outline_from_board(board, include_footprints=False)


def mm(v):
    return [v.x / 1e6 - ox, v.y / 1e6 - oy]


def courtyard(fp):
    ps = bo.courtyard(fp, pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    return [[mm(ps.COutline(i).CPoint(j)) for j in range(ps.COutline(i).PointCount())]
            for i in range(ps.OutlineCount())]


def marker_lines(fp):
    return [[mm(it.GetStart()), mm(it.GetEnd())] for it in fp.GraphicalItems()
            if it.GetLayer() == pcbnew.Dwgs_User and it.GetClass() == "PCB_SHAPE"
            and it.ShowShape() == "Line"]


def snapshot(b):
    return {fp.GetReference(): {
        "pos_nm": [fp.GetPosition().x, fp.GetPosition().y],
        "rot": round(float(fp.GetOrientationDegrees()), 6) % 360.0,
        "flipped": bool(fp.IsFlipped()), "courtyard": courtyard(fp),
        "pads": [mm(p.GetPosition()) for p in fp.Pads()], "dwgs": marker_lines(fp)}
        for fp in b.GetFootprints()}


again = None
if len(sys.argv) > 2:                  # one more SaveBoard / LoadBoard round trip
    pcbnew.SaveBoard(sys.argv[2], board)
    again = snapshot(bo.load_board(sys.argv[2]))
print(json.dumps({"origin": [ox, oy], "fps": snapshot(board), "again": again,
                  "major": int(str(pcbnew.GetBuildVersion()).lstrip("(").split(".")[0])}))
"""


@pytest.fixture(scope="module")
def kicad() -> tuple[str, Path]:
    """(KiCad Python, the stock footprints root of the SAME install)."""
    kpy = sp.require_kicad_python()
    return kpy, sp.kicad_footprints(kpy, "Package_SO.pretty").parent


def build(kicad: tuple[str, Path], design: dict, folder: Path) -> tuple[int, dict, Path]:
    """(rc, result JSON, board path) of swig_build_board.py on `design`."""
    kpy, footprints = kicad
    folder.mkdir(parents=True, exist_ok=True)
    netlist = folder / "netlist.json"
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    out = folder / f"{design.get('name', 'board')}.kicad_pcb"
    env = {k: v for k, v in os.environ.items() if k not in (orz.ENV_VAR, "PACK_SCALE")}
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(out), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S, env=env)
    result = tc.last_json(proc.stdout) or {"stdout": proc.stdout}
    return proc.returncode, {**result, "stderr": proc.stderr[-2000:]}, out


def inspect(pcb: Path, tmp_path: Path, resave: bool = False) -> dict:
    args = [pcb, tmp_path / "resaved.kicad_pcb"] if resave else [pcb]
    return sp.run_child(INSPECT, args, tmp_path)


def _bbox(polys: list) -> tuple[float, float, float, float]:
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


AT_PARTS = [("U1", SOT23, [12.345678, 8.765432, 45], "B"),
            ("R1", R0603, [20.1234567, 9.87654, 90], "B"),
            ("U2", SOIC8, [8.111111, 20.2222, 0], "F"),
            ("J1", HDR3, [30.5, 18.25, 270], "F")]


def test_live_1_at_round_trip(kicad, tmp_path) -> None:
    comps = [{**_part(ref, fp), "at": at, "side": side} for ref, fp, at, side in AT_PARTS]
    design = {"name": "anchors", "board": {"w": 40.0, "h": 30.0},
              "components": [*comps, _part("C1", C0603)],
              "nets": {"GND": [["U1", "2"], ["R1", "2"], ["U2", "4"], ["J1", "1"], ["C1", "2"]],
                       "VCC": [["U1", "3"], ["U2", "8"], ["C1", "1"], ["J1", "2"]],
                       "SIG": [["R1", "1"], ["U2", "1"], ["J1", "3"]]}}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    assert res["anchors"] == ["U1", "R1", "U2", "J1"] and len(res["fields"]) == 2
    ox, oy = res["outline"]["origin_sheet"]
    got = inspect(pcb, tmp_path, resave=True)
    assert got["origin"] == pytest.approx([ox, oy], abs=1e-9)
    for snap in (got["fps"], got["again"]):                 # saved file, then re-saved
        for ref, _fp, (x, y, rot), side in AT_PARTS:
            rec = snap[ref]
            want = (round((ox + x) * 1e6), round((oy + y) * 1e6))
            assert abs(rec["pos_nm"][0] - want[0]) <= 1, (ref, rec["pos_nm"], want)
            assert abs(rec["pos_nm"][1] - want[1]) <= 1, (ref, rec["pos_nm"], want)
            assert math.isclose(rec["rot"] % 360.0, rot % 360.0, abs_tol=1e-6), (ref, rec)
            assert rec["flipped"] is (side == "B"), (ref, rec)


def test_live_2_l_shape_auto_parts_stay_on_the_board(kicad, tmp_path) -> None:
    design = {"name": "lshape", "board": {"outline": {"loop": L_SHAPE}},
              "components": [{**_part("J1", USBC), "at": [46.0, 24.5, 180]},
                             _part("U1", SOIC8), _part("C1", C0603), _part("C2", C0603),
                             _part("C3", C0603), _part("R1"), _part("R2")],
              "nets": {"GND": [["J1", "A1"], ["U1", "4"], ["C1", "2"], ["C2", "2"],
                               ["C3", "2"], ["R2", "2"]],
                       "VBUS": [["J1", "A4"], ["U1", "8"], ["C1", "1"], ["C2", "1"],
                                ["R1", "1"]],
                       "FB": [["U1", "2"], ["R1", "2"], ["R2", "1"], ["C3", "1"]]}}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    assert res["anchors"] == ["J1"] and res["board_mm"] == [60.0, 45.0]
    outline = ol.from_board_block(design["board"])
    for x0, y0, x1, y1 in res["fields"]:
        assert ol.contains_rect(outline, (x0, y0, x1, y1), 0.3)
    got = inspect(pcb, tmp_path)
    autos = ["U1", "C1", "C2", "C3", "R1", "R2"]
    for ref in autos:
        for poly in got["fps"][ref]["courtyard"]:
            assert ol.contains_polygon(outline, poly), (ref, poly)
            x0, y0, x1, y1 = _bbox([poly])
            assert not (x1 > 32.0 and y0 < 20.0), (ref, "in the notch", poly)
    assert not [i for i in res["containment"] if i["ref"] in autos], res["containment"]


def test_live_3_t10_left_edge_connector(kicad, tmp_path) -> None:
    design = json.loads(T10.read_text(encoding="utf-8"))
    design["components"] = [
        {"ref": "J1", "value": "IO", "footprint": HDR6_H, "edge": "left"}
        if c["ref"] == "J1" else c for c in design["components"]]
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    j1 = inspect(pcb, tmp_path)["fps"]["J1"]
    x0, y0, x1, y1 = _bbox(j1["courtyard"])
    pads = j1["pads"]
    mouth = ((x0 + x1) / 2 - sum(p[0] for p in pads) / len(pads),
             (y0 + y1) / 2 - sum(p[1] for p in pads) / len(pads))
    assert mouth[0] < -1.0 and abs(mouth[1]) < abs(mouth[0]), (mouth, j1["rot"])
    assert -1e-3 <= x0 <= 3.0, (x0, j1["courtyard"])
    assert "J1" not in res["derived_overhang"]
    # centred on the left edge's straight run, clear of the r=4 corners
    assert not [i for i in res["containment"] if i["ref"] == "J1"], res["containment"]


def test_live_4_rpi5_places_as_the_head_builder(kicad, tmp_path) -> None:
    design = load_example("rpi5_battery_hat")
    design["nets"] = {n: [p for p in conns if p != ["J1", "SH"]]
                      for n, conns in design["nets"].items()}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    assert res["anchors"] == [] and res["board_mm"] == [65.0, 56.5]
    assert res["fields"] == [[8.5, 23.0, 31.5, 48.0], [30.0, 26.0, 55.0, 48.0]]
    got = inspect(pcb, tmp_path)
    if got["major"] not in RPI5_GOLDEN:
        pytest.fail(f"no rpi5 golden for pcbnew major {got['major']} (goldens: 9, 10)")
    placed = {ref: (*rec["pos_nm"], rec["rot"], "B" if rec["flipped"] else "F")
              for ref, rec in got["fps"].items()}
    assert sorted(placed.items()) == sorted(RPI5_GOLDEN[got["major"]].items())


def test_live_5_edge_mount_sma_marker_on_the_outline(kicad, tmp_path) -> None:
    design = {"name": "sma", "board": {"w": 40.0, "h": 30.0},
              "components": [_part("R1", x=10.0, y=20.0, rot=0),
                             {**_part("J1", SMA), "edge": "right"}, _part("C1", C0603)],
              "nets": {"RF": [["J1", "1"], ["C1", "1"], ["R1", "1"]],
                       "GND": [["J1", "2"], ["C1", "2"], ["R1", "2"]]}}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    derived = res["derived_overhang"]["J1"]
    assert derived["source"] == "marker_line" and 9.9 < derived["overhang_mm"] < 10.1, derived
    j1 = inspect(pcb, tmp_path)["fps"]["J1"]
    marker = [ln for ln in j1["dwgs"] if abs(ln[0][0] - ln[1][0]) < 1e-6]
    assert len(marker) == 1, j1["dwgs"]
    assert marker[0][0][0] == pytest.approx(40.0, abs=0.05), marker
    assert not [i for i in res["containment"] if i["ref"] == "J1"], res["containment"]


def _mouth(rec: dict) -> tuple[float, float]:
    x0, y0, x1, y1 = _bbox(rec["courtyard"])
    pads = rec["pads"]
    return ((x0 + x1) / 2 - sum(p[0] for p in pads) / len(pads),
            (y0 + y1) / 2 - sum(p[1] for p in pads) / len(pads))


def test_live_6_b_side_edge_and_overhang_key(kicad, tmp_path) -> None:
    """A B-side USB-C on the bottom edge (the flip mirrors the packer's turn; the
    builder turns it back) and a header on the top edge hanging its 1.5 mm
    "overhang" key past it."""
    design = {"name": "edges", "board": {"w": 50.0, "h": 40.0},
              "components": [_part("R9", x=25.0, y=20.0, rot=0),
                             {**_part("J1", USBC), "side": "B", "edge": "bottom"},
                             {**_part("J2", HDR6_H), "edge": "top", "overhang": 1.5}],
              "nets": {"GND": [["J1", "A1"], ["J2", "1"], ["R9", "1"]],
                       "VBUS": [["J1", "A4"], ["J2", "2"], ["R9", "2"]]}}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    fps = inspect(pcb, tmp_path)["fps"]
    j1, j2 = fps["J1"], fps["J2"]
    assert j1["flipped"] and _mouth(j1)[1] > 1.0, (_mouth(j1), j1["rot"])
    assert _bbox(j1["courtyard"])[3] == pytest.approx(40.0, abs=1e-3)
    assert not j2["flipped"] and _mouth(j2)[1] < -1.0, (_mouth(j2), j2["rot"])
    assert _bbox(j2["courtyard"])[1] == pytest.approx(-1.5, abs=1e-3)
    assert res["derived_overhang"] == {}
    assert not [i for i in res["containment"] if i["ref"] in ("J1", "J2")], res["containment"]

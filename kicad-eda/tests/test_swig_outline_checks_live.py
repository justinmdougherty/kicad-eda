"""swig_outline_checks + swig_gate_checks: the G3 / G4 outline gates (F3-13b).

Unit tier (the dev venv, no pcbnew): the pure helpers of swig_outline_checks -
footprint <-> board transforms, the connector mouth face, the outline_audit code
mapping, the JSON with its parallel ``codes`` / ``warning_codes`` / ``details``,
the "shaped board" test of the plane-island warning, the connector verdict (the
nearest-edge rule, overhanging mouths, cutouts) and the edge-mount straddle
verdict - plus the swig_gate_checks contracts (no module-level pcbnew-binding
import, the legacy connector texts, < 650 lines).

Live tier (KiCad's bundled Python: 9.0.4 locally, 10.0.5 in kicad-eda:wave1).
Boards are built by one child script (board_outline.draw_outline at sheet
(100, 50) + FootprintLoad of stock footprints); the REAL swig_gate_checks.py
then runs on them exactly as the harness runs it (`<mode> <pcb> <netlist>`,
stdin closed, UTF-8, timeout):
  (a) a 40 x 30 rect with a right-angle header facing inward -> connector_edge
      with the legacy plain text (T02: nothing was reported on 9.0.4 before);
  (b) the T10 outline with an 0603 inside the stadium slot -> courtyard_outside
      naming R1;
  (c) a via inside the 5 mm round hole -> copper_outside at the ROUTED stage only;
  (d) an Edge.Cuts rect with a 0.02 mm gap -> outline_open (and the connector rule
      falls back to the bbox rule);
  (e) on the rect board the outline rule gives exactly the old texts; pads and
      mounting-hole drills near the edge are judged (pad_edge_clearance / hole_edge);
  (f) an edge SMA whose 'PCB Edge' marker sits 3 mm inside the board ->
      edge_mount_off_edge (a blocker; a warning naming "edge" when the netlist
      leaves it to the auto placer); SMAs seated on the edge are clean;
  plus the plane-island warning on a slotted board and the routed mode on a board
  without Edge.Cuts (a warning, never an issue).
"""
from __future__ import annotations

import ast
import itertools
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import edge_mount as em
import outline as ol
import swig_outline_checks as soc
from fixtures.swig_python import (PREAMBLE, T10_BOARD, TOOLS, kicad_footprints,
                                  require_kicad_python, run_child)

GATE = TOOLS / "swig_gate_checks.py"
GATE_TIMEOUT_S = 180
INWARD = "{}: mating face points INTO the board interior (unusable in an enclosure)"
FAR = re.compile(r"^(J\d+): mating face is (\d+\.\d) mm from the board edge it faces "
                 r"\(must be at the edge, <=3 mm\)$")
RECT = ol.from_board_block({"w": 40, "h": 30})
LSHAPE = ol.from_board_block({"outline": {"loop": [[0, 0], [40, 0], [40, 30], [20, 30],
                                                    [20, 15], [0, 15]]}})


# ---------------------------------------------------------------------------
# unit: transforms and the mouth face
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rot", [0.0, 90.0, 180.0, 270.0, 33.0])
@pytest.mark.parametrize("flipped", [False, True])
def test_to_local_inverts_to_world(rot: float, flipped: bool) -> None:
    pl = soc.Placement(12.5, -3.0, rot, flipped)
    for p in [(0.0, 0.0), (2.1, 0.0), (-4.0, 7.5)]:
        back = soc.to_local(soc.to_world(p, pl), pl)
        assert back == pytest.approx(p, abs=1e-9)


def test_to_world_follows_the_kicad_rotation_convention() -> None:
    # KiCad's displayed rotation is CCW on screen (Y down): +90 turns +x into -y
    assert soc.to_world((1.0, 0.0), soc.Placement(0, 0, 90.0, False)) == pytest.approx((0, -1))
    assert soc.to_world((0.0, 1.0), soc.Placement(0, 0, 90.0, False)) == pytest.approx((1, 0))
    # a flipped part is mirrored in Y first, then turned
    assert soc.to_world((0.0, 1.0), soc.Placement(5, 5, 0.0, True)) == pytest.approx((5, 4))


@pytest.mark.parametrize("mouth,face", [((1, 0), (9, 2)), ((-1, 0), (1, 2)),
                                        ((0, 1), (5, 3)), ((0, -1), (5, 1))])
def test_face_point_is_the_courtyard_face_the_mouth_opens_through(
        mouth: tuple, face: tuple) -> None:
    assert soc.face_point((1.0, 1.0, 9.0, 3.0), mouth) == pytest.approx(face)


def test_face_point_refuses_a_diagonal_mouth() -> None:
    with pytest.raises(ValueError, match="axis"):
        soc.face_point((0, 0, 1, 1), (0.7, 0.7))


# ---------------------------------------------------------------------------
# unit: codes, findings and the JSON
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("audit,routed,code", [
    ("courtyard_outside", False, "courtyard_outside"),
    ("courtyard_in_cutout", False, "courtyard_outside"),
    ("courtyard_overhang", False, "courtyard_outside"),
    ("courtyard_edge_clearance", False, "courtyard_outside"),
    ("pad_outside", False, "pad_edge_clearance"),
    ("pad_edge_clearance", False, "pad_edge_clearance"),
    ("pad_outside", True, "copper_outside"),
    ("copper_outside", True, "copper_outside"),
    ("copper_edge_clearance", True, "copper_outside"),
    ("hole_outside", False, "hole_edge"),
    ("hole_edge", False, "hole_edge"),
    ("fab_feature_width", False, "fab_outline"),
    ("connector_edge", False, "connector_edge"),
])
def test_outline_audit_codes_map_to_the_gate_codes(audit: str, routed: bool, code: str) -> None:
    assert soc.gate_code(audit, routed) == code


def test_an_unknown_outline_audit_code_is_refused() -> None:
    with pytest.raises(ValueError, match="no gate code"):
        soc.gate_code("teleport_outside")


def test_findings_keep_the_audit_severity_and_message() -> None:
    issues = [ol_issue("hole_edge", "warning", "H1: near"), ol_issue("hole_outside", "error",
                                                                     "H2: off")]
    got = soc.from_audit(issues)
    assert [(f.code, f.severity, f.text) for f in got] == [
        ("hole_edge", "warning", "H1: near"), ("hole_edge", "error", "H2: off")]


def ol_issue(code: str, severity: str, message: str) -> Any:
    import outline_audit
    return outline_audit.Issue(code, severity, None, message)


def test_to_json_keeps_the_contract_keys_and_parallel_lists() -> None:
    found = [soc.Finding("connector_edge", "error", "J1: x", "J1: x; nearest edge"),
             soc.Finding("plane_islands", "warning", "GND: y"),
             soc.Finding("outline_open", "error", "board outline: z")]
    out = soc.to_json(found)
    assert out == {"issues": ["J1: x", "board outline: z"], "warnings": ["GND: y"],
                   "codes": ["connector_edge", "outline_open"],
                   "warning_codes": ["plane_islands"],
                   "details": ["J1: x; nearest edge", None]}
    assert soc.split(found) == (["J1: x", "board outline: z"], ["GND: y"])
    assert soc.tagged("ep_via_farm", "error", ["a", "b"]) == [
        soc.Finding("ep_via_farm", "error", "a"), soc.Finding("ep_via_farm", "error", "b")]


def test_a_finding_needs_a_known_severity() -> None:
    with pytest.raises(ValueError, match="severity"):
        soc.Finding("x", "fatal", "t")


# ---------------------------------------------------------------------------
# unit: shaped boards (the plane-island warning's premise)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("outline,shaped", [
    (RECT, False),
    (ol.from_board_block({"w": 40, "h": 30, "corner_radius": 3}), False),
    (LSHAPE, True),
    (ol.from_board_block(T10_BOARD), True),                    # cutouts
])
def test_is_shaped(outline: ol.Outline, shaped: bool) -> None:
    assert soc.is_shaped(outline) is shaped


# ---------------------------------------------------------------------------
# unit: the connector verdict (outline_audit.connector_edge_issue + the mouth face)
# ---------------------------------------------------------------------------
# (pad centroid, courtyard box, the old bbox rule's verdict) on a 40 x 30 board,
# encoded from tests/test_gate_checks_interior.py
GATE_CASES = [((20, 4), (17, 3, 23, 9), ("inward", None)),
              ((10, 4), (7, 3, 13, 9), ("inward", None)),
              ((20, 12), (17, 6, 23, 13), ("far", 6.0)),
              ((20, 5), (17, 0.5, 23, 6), None),
              ((36, 15), (35, 12, 39.5, 18), None),
              ((4, 15), (0.5, 12, 5, 18), None),
              ((20, 25), (17, 24, 23, 29.5), None)]


def _mouth(pad: tuple, box: tuple) -> tuple[float, float]:
    dx, dy = (box[0] + box[2]) / 2 - pad[0], (box[1] + box[3]) / 2 - pad[1]
    return (math.copysign(1.0, dx), 0.0) if abs(dx) >= abs(dy) else (0.0, math.copysign(1.0, dy))


@pytest.mark.parametrize("pad,box,old", GATE_CASES)
def test_connector_verdict_matches_the_old_bbox_rule_on_a_rect(
        pad: tuple, box: tuple, old: tuple) -> None:
    mouth = _mouth(pad, box)
    v = soc.connector_verdict(RECT, "J1", box, mouth)
    if old is None:
        assert v is None
        return
    assert v is not None and v.kind == old[0]
    if old[1] is not None:
        assert v.dist == pytest.approx(old[1])
    assert v.detail.startswith(INWARD.format("J1") if old[0] == "inward" else "J1: mating face is")
    assert "nearest edge" in v.detail and "board frame, mm" in v.detail


def _old_rule(box: tuple, mouth: tuple, w: float = 40.0, h: float = 30.0) -> tuple | None:
    """The pre-outline G3 rule (swig_gate_checks._bbox_connector's arithmetic on a
    w x h board): inward when the mouth points at the board centre from the
    courtyard centre, else far when the faced side is > 3 mm away."""
    x0, y0, x1, y1 = box
    mx, my = mouth
    if mx * ((x0 + x1) / 2 - w / 2) + my * ((y0 + y1) / 2 - h / 2) <= 0:
        return ("inward", None)
    dist = {(1, 0): w - x1, (-1, 0): x0, (0, 1): h - y1, (0, -1): y0}[mouth]
    return ("far", dist) if dist > 3.0 else None


@pytest.mark.parametrize("box,mouth,old", [
    ((2.0, 0.2, 9.0, 3.0), (-1, 0), None),            # top-left corner, 2 mm from the left edge
    ((34.0, 26.5, 37.5, 29.5), (1, 0), None),          # bottom-right corner, 2.5 mm from the right
    ((4.0, 0.2, 9.0, 3.0), (-1, 0), ("far", 4.0)),     # 4 mm from the edge it faces
])
def test_a_connector_tucked_into_a_corner_is_judged_by_the_edge_it_faces(
        box: tuple, mouth: tuple, old: tuple | None) -> None:
    # the face's NEAREST edge is the perpendicular one; the old rule judged the
    # side the mouth faces, and so must the outline rule (review finding, round 1)
    assert _old_rule(box, mouth) == old                                   # premise
    v = soc.connector_verdict(RECT, "J1", box, mouth)
    if old is None:
        assert v is None
        return
    assert v is not None and (v.kind, v.dist) == pytest.approx(old)
    assert v.detail.startswith("J1: mating face is 4.0 mm from the board edge it faces"), v.detail
    assert "the edge it faces: the board outline at (0.000, 1.600)" in v.detail, v.detail
    assert "nearest edge" in v.detail and "board frame, mm" in v.detail


CONNECTOR_SIZES = ((6, 3), (3, 6), (10, 5), (5, 10), (4, 4))
MOUTHS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@pytest.mark.parametrize("w,h", [(40, 30), (60, 40)])
def test_on_a_rect_the_outline_rule_flags_exactly_what_the_old_rule_flags(w: int, h: int) -> None:
    """Every connector body up to 10 mm, every mouth, on a 1 mm grid with its
    courtyard centre on the board: the same connectors are flagged; where both
    say 'far' the distance is the same. The wording may differ only for a body
    whose centre is on or past the board's centre line while the mouth face's
    nearest edge is the one it faces: the old rule said 'inward', the outline
    rule says 'far' (both block, napkin_policy turns both into face_connector_out)."""
    rect = ol.from_board_block({"w": w, "h": h})
    flagged, worded = [], []
    for (bw, bh), x0, y0, mouth in itertools.product(CONNECTOR_SIZES, range(-5, w + 1),
                                                     range(-5, h + 1), MOUTHS):
        box = (float(x0), float(y0), float(x0 + bw), float(y0 + bh))
        if not (0 < x0 + bw / 2 < w and 0 < y0 + bh / 2 < h):
            continue
        old, v = _old_rule(box, mouth, w, h), soc.connector_verdict(rect, "J1", box, mouth)
        if (old is None) != (v is None):
            flagged.append((box, mouth, old, v))
        elif old is not None and (old[0], v.kind) != ("inward", "far") and (
                old[0] != v.kind or (old[1] is not None and abs(old[1] - v.dist) > 1e-6)):
            worded.append((box, mouth, old, v))
    assert flagged == [] and worded == []


def test_an_overhanging_mouth_is_at_the_edge_however_far_it_hangs() -> None:
    # an edge SMA hangs 10 mm past the right edge: the old rule's distance went
    # negative; the outline rule must not read 10 mm as "far from the edge"
    box = (35.0, 12.0, 50.0, 18.0)
    assert soc.connector_verdict(RECT, "J1", box, (1, 0)) is None
    v = soc.connector_verdict(RECT, "J1", box, (0, 1))
    assert v is not None and v.kind == "inward"          # hanging off, but facing along the edge


def test_a_mouth_facing_a_cutout_counts_only_with_panel_cutouts() -> None:
    t10 = ol.from_board_block(T10_BOARD)
    box = (27.0, 23.0, 33.0, 26.0)                        # mouth face under the round hole
    v = soc.connector_verdict(t10, "J2", box, (0, -1))
    assert v is not None and v.kind == "inward" and "cutout" in v.detail
    assert soc.connector_verdict(t10, "J2", box, (0, -1), panel_cutouts=True) is None


def test_the_l_bay_wall_is_an_edge_the_bbox_rule_could_not_see() -> None:
    # a connector in the L's bay facing the x = 20 wall: the bbox rule called the
    # bay "interior"; the outline knows that wall is a board edge
    assert soc.connector_verdict(LSHAPE, "J1", (21.0, 22.0, 27.0, 28.0), (-1, 0)) is None
    v = soc.connector_verdict(LSHAPE, "J1", (15.0, 22.0, 21.0, 28.0), (1, 0))
    assert v is not None and v.kind == "inward"


# ---------------------------------------------------------------------------
# unit: edge-mount straddle
# ---------------------------------------------------------------------------
SAMTEC = em.EdgeMount(True, "x", 2.1, (1.0, 0.0), 10.02, "marker_line", (2.1, 0.0))


@pytest.mark.parametrize("pl,off", [
    (soc.Placement(37.9, 8.0, 0.0, False), None),               # marker on the right edge
    (soc.Placement(34.9, 22.0, 0.0, False), (3.0, (37, 22))),   # 3 mm inside
    (soc.Placement(12.0, 2.1, 90.0, False), None),              # rot 90: marker on the top edge
    (soc.Placement(2.1, 15.0, 180.0, False), None),             # rot 180: on the left edge
    (soc.Placement(20.0, 27.9, 270.0, False), None),            # rot 270: on the bottom edge
    (soc.Placement(37.9, 15.0, 0.0, True), None),               # flipped: mirrored in Y only
    (soc.Placement(2.1, 15.0, 180.0, True), None),              # flipped, turned: left edge
    (soc.Placement(2.1, 15.0, 0.0, True), (4.2, (4.2, 15))),    # flipped, unturned: 4.2 mm in
])
def test_edge_mount_verdict(pl: soc.Placement, off: tuple | None) -> None:
    f = soc.edge_mount_verdict("J2", SAMTEC, pl, RECT, (0.0, 0.0))
    if off is None:
        assert f is None
        return
    dist, (x, y) = off
    assert f == soc.Finding("edge_mount_off_edge", "error", em.straddle_issue("J2", dist),
                            f.detail)
    assert f.text == ("J2: edge-mount connector does not straddle the board edge "
                      f"(edge marker {dist:.2f} mm from the outline)")
    assert f"({x:.3f}, {y:.3f})" in f.detail and "marker_line" in f.detail
    assert f.detail.endswith("seat the marker on the board edge with the body overhanging it "
                             "by 10.02 mm"), f.detail


def test_edge_mount_verdict_ignores_parts_that_are_not_edge_mount() -> None:
    assert soc.edge_mount_verdict("J1", em.NOT_EDGE_MOUNT, soc.Placement(0, 0, 0, False), RECT,
                                  (0.0, 0.0)) is None


@pytest.mark.parametrize("comp,severity", [
    (None, "error"),                                   # not in the netlist: no excuse
    ({"ref": "J2"}, "warning"),                        # left to the auto placer
    ({"ref": "J2", "edge": None, "x": None}, "warning"),
    ({"ref": "J2", "edge": "right"}, "error"),         # the netlist asked for an edge
    ({"ref": "J2", "overhang": 2.0}, "error"),         # ... or for an overhang
    ({"ref": "J2", "at": [30, 22, 0]}, "error"),       # the netlist placed it
    ({"ref": "J2", "x": 30, "y": 22}, "error"),
])
def test_edge_mount_severity_follows_who_chose_the_position(
        comp: dict | None, severity: str) -> None:
    assert soc.edge_mount_severity(comp) == severity


def test_an_auto_placed_edge_mount_part_off_the_edge_warns_and_names_the_fix() -> None:
    f = soc.edge_mount_verdict("J2", SAMTEC, soc.Placement(34.9, 22.0, 0.0, False), RECT,
                               (0.0, 0.0), severity="warning")
    assert f is not None and (f.code, f.severity) == ("edge_mount_off_edge", "warning")
    assert f.text.startswith(em.straddle_issue("J2", 3.0) + "; it is auto-placed"), f.text
    assert '"edge"' in f.text and "marker" in f.text, f.text
    assert f.text.endswith('"right" so the builder can seat the marker on the board edge with '
                           'the body overhanging it by 10.02 mm'), f.text


def test_edge_mount_verdict_refuses_an_unknown_severity() -> None:
    with pytest.raises(ValueError, match="severity"):
        soc.edge_mount_verdict("J2", SAMTEC, soc.Placement(34.9, 22.0, 0.0, False), RECT,
                               (0.0, 0.0), severity="fatal")


@pytest.mark.parametrize("info,clearance", [(SAMTEC, 0.0), (em.NOT_EDGE_MOUNT, 0.3),
                                            (None, 0.3)])
def test_edge_mount_pads_may_reach_the_edge_they_straddle(
        info: Any, clearance: float) -> None:
    # an edge-launch part's pads run to the edge by design (SMA_Amphenol_132289's
    # end at it): judged on the material only; every other pad keeps the clearance
    assert soc.pad_clearance(info, 0.3) == clearance


def test_courtyard_overhang_is_the_netlist_key_else_the_derived_one() -> None:
    assert soc.part_overhang({"overhang": 2.5}, SAMTEC) == 2.5
    assert soc.part_overhang({"overhang": None}, SAMTEC) == 10.02
    assert soc.part_overhang({}, SAMTEC) == 10.02
    assert soc.part_overhang({}, em.NOT_EDGE_MOUNT) is None
    assert soc.part_overhang({}, None) is None


# ---------------------------------------------------------------------------
# unit: stand-in boards and the swig_gate_checks contracts
# ---------------------------------------------------------------------------
class _StandIn:
    """What tests/test_gate_checks_interior.py hands check_placement: no GetDrawings."""

    def GetBoardEdgesBoundingBox(self) -> None:  # noqa: N802 (pcbnew API)
        raise AssertionError("read_shape must not measure a stand-in board")


def test_a_stand_in_board_has_no_outline_and_no_finding() -> None:
    shape = soc.read_shape(_StandIn())
    assert shape.outline is None and shape.origin is None and shape.findings == ()


class _Layers:
    def CuStack(self) -> list[int]:  # noqa: N802 (pcbnew API)
        return [31]


class _UnfilledZone:
    """A plane whose fill pcbnew cannot hand back (SWIG maps the C++ throw to OSError)."""

    def GetIsRuleArea(self) -> bool:  # noqa: N802 (pcbnew API)
        return False

    def GetLayerSet(self) -> _Layers:  # noqa: N802
        return _Layers()

    def GetFilledPolysList(self, layer: int) -> None:  # noqa: N802
        raise OSError(f"no fill for layer {layer}")

    def GetNetname(self) -> str:  # noqa: N802
        return "GND"


class _ZoneBoard:
    def Zones(self) -> list:  # noqa: N802
        return [_UnfilledZone()]

    def GetLayerName(self, layer: int) -> str:  # noqa: N802
        return "B.Cu"


def test_an_unreadable_plane_fill_is_a_warning_never_silent() -> None:
    found = soc.plane_island_findings(_ZoneBoard(), soc.BoardShape(LSHAPE, (0.0, 0.0)))
    assert found == [soc.Finding(
        "plane_islands_unread", "warning", "GND: the fill on B.Cu could not be read (OSError: "
        "no fill for layer 31); its islands were not counted")]


def _top_level_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_swig_gate_checks_never_binds_pcbnew_users_at_import() -> None:
    """tests import swig_gate_checks with a stub pcbnew for the import only:
    board_outline / swig_outline_checks bind pcbnew when first imported, so they
    are imported inside the functions that need them."""
    assert not _top_level_imports(GATE) & {"board_outline", "swig_outline_checks"}


def test_swig_gate_checks_keeps_its_contracts_and_size() -> None:
    source = GATE.read_text(encoding="utf-8")
    assert len(source.splitlines()) < 650
    for text in ("mating face points INTO the board interior", "mating face is ",
                 "fractured into separate islands", "dissipating exposed-pad part has only"):
        assert text in source, text


def test_swig_outline_checks_is_stdlib_plus_pcbnew_and_small() -> None:
    path = TOOLS / "swig_outline_checks.py"
    allowed = {"__future__", "math", "os", "re", "sys", "collections.abc", "dataclasses",
               "pathlib", "typing", "kicad_quiet", "pcbnew", "board_outline", "edge_mount",
               "outline", "outline_audit", "swig_copper"}
    assert _top_level_imports(path) <= allowed
    assert len(path.read_text(encoding="utf-8").splitlines()) < 800


# ---------------------------------------------------------------------------
# live: boards built under KiCad's Python, the real swig_gate_checks.py on them
# ---------------------------------------------------------------------------
NAMES = ("rect", "t10", "gap", "sma", "split", "bare")
LIBS = ("Connector_PinHeader_2.54mm.pretty", "Resistor_SMD.pretty", "MountingHole.pretty",
        "Connector_Coaxial.pretty")

BUILD = PREAMBLE + r'''
import board_outline as bo
import board_shapes
import outline as ol

names = json.loads(sys.argv[1])
paths = dict(zip(names, sys.argv[2:2 + len(names)]))
header_dir, res_dir, hole_dir, coax_dir = sys.argv[2 + len(names):6 + len(names)]
T10 = json.loads(sys.argv[6 + len(names)])
OX, OY = 100.0, 50.0
HEADER = "PinHeader_1x04_P2.54mm_Horizontal"
R0603 = "R_0603_1608Metric"
HOLE = "MountingHole_3.2mm_M3"
SMA = "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"


def at(x, y):
    return pcbnew.VECTOR2I(bo.nm(OX + x), bo.nm(OY + y))


def new_board(block=None):
    board = pcbnew.BOARD()
    board.GetDesignSettings().m_CopperEdgeClearance = bo.nm(0.3)
    if block is not None:
        bo.draw_outline(board, ol.from_board_block(block), OX, OY)
    return board


def place(board, lib_dir, name, ref, x, y, rot=0.0):
    fp = pcbnew.FootprintLoad(lib_dir, name)
    if fp is None:
        raise SystemExit(f"FootprintLoad returned None for {name} in {lib_dir}")
    fp.SetReference(ref)
    fp.SetPosition(at(x, y))
    fp.SetOrientationDegrees(rot)
    board.Add(fp)
    return fp


def add_net(board, name):
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni)
    return ni


def add_via(board, ni, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(at(x, y))
    v.SetWidth(bo.nm(0.6))
    v.SetDrill(bo.nm(0.3))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(ni)
    board.Add(v)


def mm(v):
    return (pcbnew.ToMM(v.x), pcbnew.ToMM(v.y))


def marker_mid(fp):
    """Middle of the Dwgs.User segment nearest the 'PCB Edge' text, as KiCad
    placed it (sheet mm): the test seats parts by KiCad's own transform."""
    items = list(fp.GraphicalItems())
    text = next(mm(it.GetPosition()) for it in items
                if it.GetClass() == "PCB_TEXT" and it.GetText() == "PCB Edge")
    mids = [((mm(it.GetStart())[0] + mm(it.GetEnd())[0]) / 2,
             (mm(it.GetStart())[1] + mm(it.GetEnd())[1]) / 2) for it in items
            if it.GetClass() == "PCB_SHAPE" and it.GetLayer() == pcbnew.Dwgs_User
            and it.GetShape() == pcbnew.SHAPE_T_SEGMENT]
    return min(mids, key=lambda m: (m[0] - text[0]) ** 2 + (m[1] - text[1]) ** 2)


def seat(fp, axis, coord):
    """Move fp so its edge marker lies on board-frame x (or y) = coord."""
    mx, my = marker_mid(fp)
    dx = OX + coord - mx if axis == "x" else 0.0
    dy = OY + coord - my if axis == "y" else 0.0
    fp.Move(pcbnew.VECTOR2I(bo.nm(dx), bo.nm(dy)))


def mouth_down(fp):
    """Turn a flipped fp until its body (courtyard) lies below its pads."""
    for rot in (0.0, 90.0, 180.0, 270.0):
        fp.SetOrientationDegrees(rot)
        fp.BuildCourtyardCaches()
        box = fp.GetCourtyard(pcbnew.B_CrtYd).BBox()
        pads = [mm(p.GetPosition()) for p in fp.Pads()]
        dy = pcbnew.ToMM(box.GetCenter().y) - sum(p[1] for p in pads) / len(pads)
        dx = pcbnew.ToMM(box.GetCenter().x) - sum(p[0] for p in pads) / len(pads)
        if dy > abs(dx):
            return rot
    raise SystemExit("no rotation turns the flipped SMA's body down")


def flip(fp):
    try:
        fp.Flip(fp.GetPosition(), True)
    except TypeError:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


# (a) / (e): the rect board
rect = new_board({"w": 40, "h": 30})
place(rect, header_dir, HEADER, "J1", 4, 2)            # mouth +x: into the board
place(rect, header_dir, HEADER, "J2", 24, 17)          # mouth +x: ~5.5 mm short of the edge
place(rect, header_dir, HEADER, "J3", 12, 28, 180)     # mouth -x: at the left edge
place(rect, res_dir, R0603, "R2", 20, 0.6)             # pads ~0.13 mm from the top edge
place(rect, hole_dir, HOLE, "H1", 1.8, 15)             # drill 0.2 mm from the left edge
place(rect, hole_dir, HOLE, "H2", 39.5, 25)            # drill reaches off the right edge
pcbnew.SaveBoard(paths["rect"], rect)

# (b) / (c): T10 (60 x 40, r 4 corners, a stadium slot, a 5 mm round hole)
t10 = new_board(T10)
place(t10, res_dir, R0603, "R1", 30, 9)                # inside the stadium slot
place(t10, res_dir, R0603, "R3", 10, 20)               # on the material
vin = add_net(t10, "VIN")
add_via(t10, vin, 30, 20)                              # inside the round hole
add_via(t10, vin, 45, 20)                              # on the material
pcbnew.SaveBoard(paths["t10"], t10)

# (d): a rect whose last Edge.Cuts segment stops 0.02 mm short
gap = new_board()
corners = [(0, 0), (40, 0), (40, 30), (0, 30), (0, 0.02)]
for a, b in zip(corners, corners[1:]):
    seg = pcbnew.PCB_SHAPE(gap)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(bo.nm(0.1))
    seg.SetStart(at(*a))
    seg.SetEnd(at(*b))
    gap.Add(seg)
place(gap, header_dir, HEADER, "J1", 4, 2)
pcbnew.SaveBoard(paths["gap"], gap)

# (f): edge SMAs on a rect
sma = new_board({"w": 40, "h": 30})
seat(place(sma, coax_dir, SMA, "J1", 30, 8), "x", 40.0)          # on the right edge
seat(place(sma, coax_dir, SMA, "J2", 30, 22), "x", 37.0)         # 3 mm inside it
seat(place(sma, coax_dir, SMA, "J3", 12, 5, 90), "y", 0.0)       # rot 90, on the top edge
j4 = place(sma, coax_dir, SMA, "J4", 20, 20)
flip(j4)
j4_rot = mouth_down(j4)
seat(j4, "y", 30.0)                                              # flipped, on the bottom edge
# no 'PCB Edge' marker: edge-mount by name, its pads end at the edge it straddles
j5 = place(sma, coax_dir, "SMA_Amphenol_132289_EdgeMount", "J5", 8, 15, 180)
pad_x0 = min(pcbnew.ToMM(p.GetBoundingBox().GetLeft()) for p in j5.Pads())
j5.Move(pcbnew.VECTOR2I(bo.nm(OX - pad_x0), 0))                 # pads end on the left edge
pcbnew.SaveBoard(paths["sma"], sma)

# a slot (0.4 mm material left at each end) splits a B.Cu GND plane in two
split = new_board({"w": 40, "h": 30,
                   "cutouts": [{"loop": [[19, 0.4], [21, 0.4], [21, 29.6], [19, 29.6]]}]})
zone = board_shapes.add_plane(split, pcbnew.B_Cu, add_net(split, "GND"), 40, 30, OX, OY, 0.3)
zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
pcbnew.SaveBoard(paths["split"], split)
filled = bo.load_board(paths["split"])
filled.BuildConnectivity()
pcbnew.ZONE_FILLER(filled).Fill(filled.Zones())
pcbnew.SaveBoard(paths["split"], filled)

# no Edge.Cuts at all, one track
bare = new_board()
track = pcbnew.PCB_TRACK(bare)
track.SetStart(at(1, 1))
track.SetEnd(at(9, 1))
track.SetWidth(bo.nm(0.25))
track.SetLayer(pcbnew.F_Cu)
track.SetNet(add_net(bare, "SIG"))
bare.Add(track)
pcbnew.SaveBoard(paths["bare"], bare)

print(json.dumps({"version": pcbnew.Version(), "j4_rot": j4_rot}))
'''


@pytest.fixture(scope="module")
def boards(tmp_path_factory: pytest.TempPathFactory) -> dict:
    kpy = require_kicad_python()
    work = tmp_path_factory.mktemp("outline_checks")
    paths = {name: work / f"{name}.kicad_pcb" for name in NAMES}
    libs = [kicad_footprints(kpy, lib) for lib in LIBS]
    res = run_child(BUILD, [json.dumps(NAMES), *paths.values(), *libs,
                            json.dumps(T10_BOARD)], work)
    return {"kpy": kpy, "work": work, **paths, **res}


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"swig_gate_checks printed no JSON line:\n{stdout}")


def _gate(boards: dict, mode: str, name: str, design: dict | None = None,
          tag: str = "") -> dict:
    """The real swig_gate_checks.py as rp_kicad.gate_check runs it; asserts rc 0
    and the JSON contract (parallel codes / warning_codes / details)."""
    netlist = boards["work"] / f"{name}_{mode}{tag}.json"
    netlist.write_text(json.dumps(design or {}), encoding="utf-8")
    try:
        proc = subprocess.run([boards["kpy"], str(GATE), mode, str(boards[name]), str(netlist)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL,
                              timeout=GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"swig_gate_checks hung for {GATE_TIMEOUT_S} s: {e.stdout!r} {e.stderr!r}")
    out = _last_json(proc.stdout)
    assert proc.returncode == 0, (out, proc.stderr)
    assert len(out["codes"]) == len(out["issues"]) == len(out["details"]), out
    assert len(out["warning_codes"]) == len(out["warnings"]), out
    return out


def _with(out: dict, code: str, key: str = "issues") -> list[str]:
    codes = out["codes" if key == "issues" else "warning_codes"]
    return [t for t, c in zip(out[key], codes, strict=True) if c == code]


def _about(texts: list[str], ref: str) -> list[str]:
    return [t for t in texts if t.startswith(f"{ref}:") or t.startswith(f"{ref} ")]


def test_live_premise_kicad_version(boards: dict) -> None:
    assert boards["version"].startswith(("9.", "10.")), boards


def test_a_inward_header_is_a_connector_edge_issue_with_the_legacy_text(boards: dict) -> None:
    out = _gate(boards, "placement", "rect")
    conn = _with(out, "connector_edge")
    assert INWARD.format("J1") in conn, out                          # T02: nothing before
    i = out["issues"].index(INWARD.format("J1"))
    detail = out["details"][i]
    assert detail.startswith(INWARD.format("J1")) and "nearest edge" in detail, detail
    far = [m for m in map(FAR.match, conn) if m]
    assert [m.group(1) for m in far] == ["J2"] and 4.5 < float(far[0].group(2)) < 6.5, out
    assert _about(conn, "J3") == [] and len(conn) == 2, out          # J3 mates at the edge


def test_a_placement_routed_mode_runs_the_outline_rules_too(boards: dict) -> None:
    out = _gate(boards, "placement_routed", "rect")
    assert sorted(t.split(":")[0] for t in _with(out, "connector_edge")) == ["J1", "J2"], out
    assert _about(_with(out, "pad_edge_clearance"), "R2"), out


def test_e_pads_and_mounting_hole_drills_near_the_edge(boards: dict) -> None:
    out = _gate(boards, "placement", "rect")
    pads = _with(out, "pad_edge_clearance")
    assert pads and all(t.startswith("R2 pad ") for t in pads), out
    assert _about(_with(out, "courtyard_outside"), "R2"), out        # its courtyard crosses too
    assert [t[:3] for t in _with(out, "hole_edge")] == ["H2:"], out  # off the board: a blocker
    near = _with(out, "hole_edge", "warnings")
    assert len(near) == 1 and near[0].startswith("H1: drill 3.2 mm"), out
    # mounting holes are judged by their drill, never their courtyard
    assert not [t for t in _with(out, "courtyard_outside") if t.startswith(("H1", "H2"))], out
    assert _with(out, "outline_open") == [] and _with(out, "fab_outline") == [], out


def test_b_resistor_in_the_stadium_is_courtyard_outside(boards: dict) -> None:
    out = _gate(boards, "placement", "t10")
    yards = _with(out, "courtyard_outside")
    assert len(yards) == 1 and yards[0].startswith("R1: courtyard ") and "cutout" in yards[0], out
    assert _about(out["issues"], "R3") == [], out
    assert _with(out, "copper_outside") == [], out                   # copper is a G4 check


def test_c_via_in_the_round_hole_is_copper_outside_when_routed(boards: dict) -> None:
    out = _gate(boards, "routed", "t10")
    copper = _with(out, "copper_outside")
    vias = [t for t in copper if t.startswith("via on net 'VIN'")]
    assert len(vias) == 1 and "into cutout" in vias[0] and "(30.000, 20.000)" in vias[0], out
    assert not [t for t in copper if "(45.000, 20.000)" in t], out
    assert {t.split(" pad ")[0] for t in copper if " pad " in t} == {"R1"}, out


def test_d_open_outline_is_outline_open_and_connectors_fall_back(boards: dict) -> None:
    out = _gate(boards, "placement", "gap")
    opened = _with(out, "outline_open")
    assert len(opened) == 1 and "Edge.Cuts" in opened[0], out
    assert INWARD.format("J1") in _with(out, "connector_edge"), out   # the bbox rule
    routed = _gate(boards, "routed", "gap")
    assert len(_with(routed, "outline_open")) == 1, routed


J2_OFF_EDGE = ("J2: edge-mount connector does not straddle the board edge (edge marker 3.00 mm "
               "from the outline)")
SMA_PARTS = [{"ref": ref, "value": "SMA", "footprint": f"Connector_Coaxial:{name}"}
             for ref, name in (("J1", "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"),
                               ("J2", "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"),
                               ("J3", "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"),
                               ("J4", "SMA_Samtec_SMA-J-P-H-ST-EM1_EdgeMount"),
                               ("J5", "SMA_Amphenol_132289_EdgeMount"))]


def test_f_edge_sma_3mm_inside_is_flagged_seated_ones_are_clean(boards: dict) -> None:
    out = _gate(boards, "placement", "sma")              # no netlist parts: a blocker
    assert _with(out, "edge_mount_off_edge") == [J2_OFF_EDGE], out
    for ref in ("J1", "J3", "J4", "J5"):                 # front, rot 90, flipped, by name
        assert _about(out["issues"], ref) == [], (ref, boards["j4_rot"], out)
    # J2's body hangs 7 mm past the edge: inside its derived overhang, no courtyard issue
    assert _about(_with(out, "courtyard_outside"), "J2") == [], out


def test_f_an_edge_the_netlist_asked_for_blocks_when_missed(boards: dict) -> None:
    parts = [{**p, "edge": "right"} if p["ref"] == "J2" else p for p in SMA_PARTS]
    out = _gate(boards, "placement", "sma", {"components": parts}, tag="_edge")
    assert _with(out, "edge_mount_off_edge") == [J2_OFF_EDGE], out
    assert _with(out, "edge_mount_off_edge", "warnings") == [], out


def test_f_an_auto_placed_sma_off_the_edge_warns_and_names_the_fix(boards: dict) -> None:
    # the netlist leaves J2 to the auto placer (no edge / at / x / y): until the
    # builder seats edge-mount parts by itself the finding is a warning naming
    # the "edge" key, so boards placed that way keep their verdict (F3-13b review)
    out = _gate(boards, "placement", "sma", {"components": SMA_PARTS}, tag="_auto")
    assert _with(out, "edge_mount_off_edge") == [], out
    warned = _with(out, "edge_mount_off_edge", "warnings")
    assert len(warned) == 1 and warned[0].startswith(J2_OFF_EDGE + "; it is auto-placed"), out
    for ref in ("J1", "J2", "J3", "J4", "J5"):
        assert _about(out["issues"], ref) == [], (ref, out)


def test_plane_islands_warn_on_a_slotted_board(boards: dict) -> None:
    out = _gate(boards, "placement", "split")
    islands = _with(out, "plane_islands", "warnings")
    assert len(islands) == 1 and islands[0].startswith("GND: plane on B.Cu fills as 2 "), out
    assert "plane_islands" not in out["codes"], out


def test_routed_mode_without_edge_cuts_warns_and_adds_no_issue(boards: dict) -> None:
    out = _gate(boards, "routed", "bare")
    assert out["issues"] == [] and out["warning_codes"] == ["outline_missing"], out

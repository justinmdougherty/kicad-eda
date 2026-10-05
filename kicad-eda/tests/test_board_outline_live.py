"""board_outline + board_shapes on the real pcbnew (KiCad 9.0.4 locally, 10.0.5 in
the kicad-eda:wave1 image). Every check runs a child script under KiCad's bundled
Python (tests/fixtures/swig_python.py) and skips cleanly without one.

  (a) the T10 outline drawn at (100.5, 115): one closed outline, two holes, the
      model's area; (b) SaveBoard + load_board + outline_from_board round-trips it;
  (c) an Edge.Cuts gap of 0.01 mm (and a stray extra ring) raises OutlineBuildError;
  (d) add_plane_outline on In1.Cu fills to ONE island (safe fill order only);
  (e) keep-out rule areas cover every copper layer and block pours on 9 and 10;
  (f) set_datum persists as (aux_axis_origin ...) + (grid_origin ...);
  (g) courtyard() of a stock SOIC-8 after load_board is non-empty;
  (h) load_board refuses a missing path before pcbnew is called;
  plus the legacy rectangle helpers keep their exact geometry.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

import outline as ol
from fixtures.swig_python import (PREAMBLE, T10_BOARD, assert_same_outline, kicad_footprints,
                                  require_kicad_python, run_child)

OX, OY = 100.5, 115.0
T10_AREA = 2400 - 4 * (16 - 4 * math.pi) - (24 + 9 * math.pi) - 6.25 * math.pi
SAVED_FORMATS = ("20241229", "20260206")      # 9.0 / 10.0 board files

DRAW_T10 = PREAMBLE + """
import board_outline as bo
import outline as ol

block, out = json.loads(sys.argv[1]), sys.argv[2]
model = ol.from_board_block(block)
board = pcbnew.CreateEmptyBoard()
shapes = bo.draw_outline(board, model, 100.5, 115.0)
sp = bo.polygon_outlines(board)
bo.set_datum(board, model.datum[0] + 100.5, model.datum[1] + 115.0)
pcbnew.SaveBoard(out, board)
print(json.dumps({"version": pcbnew.Version(), "shapes": [s.ShowShape() for s in shapes],
                  "outlines": sp.OutlineCount(), "holes": sp.HoleCount(0),
                  "area": sp.Area() / 1e12}))
"""


@pytest.fixture(scope="module")
def t10(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, Path]:
    require_kicad_python()
    work = tmp_path_factory.mktemp("t10")
    pcb = work / "t10.kicad_pcb"
    res = run_child(DRAW_T10, [json.dumps(T10_BOARD), pcb], work)
    return res, pcb


def test_a_t10_draws_one_closed_outline_with_two_holes(t10):
    res, pcb = t10
    assert res["outlines"] == 1 and res["holes"] == 2
    assert res["area"] == pytest.approx(2314.36, abs=0.05)
    assert res["area"] == pytest.approx(T10_AREA, abs=0.05)
    assert len(res["shapes"]) == 13 and res["shapes"].count("Arc") == 6
    assert re.search(r"\(version (\d+)\)", pcb.read_text(encoding="utf-8")).group(1) \
        in SAVED_FORMATS


READ_BACK = PREAMBLE + """
import board_outline as bo
import outline as ol

board = bo.load_board(sys.argv[1])
o, origin = bo.outline_from_board(board)
print(json.dumps({"block": ol.to_board_block(o), "origin": origin}))
"""


def test_b_save_load_round_trips_the_model(t10, tmp_path):
    _, pcb = t10
    res = run_child(READ_BACK, [pcb], tmp_path)
    assert res["origin"] == pytest.approx([OX, OY], abs=1e-6)
    assert_same_outline(ol.from_board_block(res["block"]), ol.from_board_block(T10_BOARD), 1e-3)


GAP = PREAMBLE + """
import board_outline as bo
import outline as ol

M = 1_000_000

def seg(board, a, b):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(M // 10)
    s.SetStart(pcbnew.VECTOR2I(*a))
    s.SetEnd(pcbnew.VECTOR2I(*b))
    board.Add(s)

def outcome(fn):
    try:
        fn()
    except bo.OutlineBuildError as e:
        return "OutlineBuildError: " + str(e)
    return "ok"

def open_rect(gap_nm):
    # a 30 x 20 mm rect whose last segment ends gap_nm short of its start
    board = pcbnew.CreateEmptyBoard()
    pts = [(100 * M, 100 * M), (130 * M, 100 * M), (130 * M, 120 * M), (100 * M, 120 * M),
           (100 * M, 100 * M + gap_nm)]
    for a, b in zip(pts[:-1], pts[1:]):
        seg(board, a, b)
    return board

res = {}
for gap in (10_000, 11_000, 20_000, 100_000):
    board = open_rect(gap)
    res[str(gap)] = {"polygon": outcome(lambda: bo.polygon_outlines(board)),
                     "from_board": outcome(lambda: bo.outline_from_board(board))}
stray = pcbnew.CreateEmptyBoard()
square = [(10 * M, 10 * M), (20 * M, 10 * M), (20 * M, 20 * M), (10 * M, 20 * M)]
for a, b in zip(square, square[1:] + square[:1]):
    seg(stray, a, b)
model = ol.from_board_block({"w": 30, "h": 20})
res["stray"] = outcome(lambda: bo.draw_outline(stray, model, 100, 100))
print(json.dumps(res))
"""
CLOSED = "OutlineBuildError: Edge.Cuts does not form one closed outline"


def test_c_a_gap_of_a_hundredth_of_a_mm_raises(tmp_path):
    res = run_child(GAP, [], tmp_path)
    # the adapter chains at 1 um: a 0.01 mm gap is open on every pcbnew version
    for gap in ("10000", "11000", "20000", "100000"):
        assert res[gap]["from_board"].startswith(CLOSED + ": the Edge.Cuts chain is open"), gap
    # KiCad's own chaining closes gaps up to 10000 nm on 9.0.4 and 10.0.5 (9.0.4 is not
    # even deterministic at exactly 10000 nm), so its verdict is asserted from 11000 nm
    for gap in ("11000", "20000", "100000"):
        assert res[gap]["polygon"] == CLOSED, gap
    assert res["10000"]["polygon"] in ("ok", CLOSED)
    # a second, stray ring: KiCad sees more area than the model, draw_outline refuses
    assert res["stray"].startswith("OutlineBuildError: KiCad's Edge.Cuts polygon has area")


SHAPES = PREAMBLE + """
import board_outline as bo
import outline as ol

V = lambda x, y: pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))

def edge(parent, kind):
    s = pcbnew.PCB_SHAPE(parent)
    s.SetShape(kind)
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(pcbnew.FromMM(0.1))
    return s

board = pcbnew.CreateEmptyBoard()
rect = edge(board, pcbnew.SHAPE_T_RECTANGLE)          # outer: 40 x 30 at (100, 100)
rect.SetStart(V(100, 100))
rect.SetEnd(V(140, 130))
radius = 2.0 if hasattr(rect, "SetCornerRadius") else 0.0       # 10.x only
if radius:
    rect.SetCornerRadius(pcbnew.FromMM(radius))
board.Add(rect)
tri = edge(board, pcbnew.SHAPE_T_POLY)                # cutout 1: a triangle
ps = tri.GetPolyShape()
ps.NewOutline()
for x, y in ((105, 105), (112, 105), (105, 112)):
    ps.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
board.Add(tri)
bez = edge(board, pcbnew.SHAPE_T_BEZIER)              # cutout 2: a D of bezier + chord
bez.SetStart(V(120, 112))
bez.SetBezierC1(V(120, 106))
bez.SetBezierC2(V(130, 106))
bez.SetEnd(V(130, 112))
bez.RebuildBezierToSegmentsPointsList(pcbnew.FromMM(0.005))
board.Add(bez)
chord = edge(board, pcbnew.SHAPE_T_SEGMENT)
chord.SetStart(V(130, 112))
chord.SetEnd(V(120, 112))
board.Add(chord)
fp = pcbnew.FOOTPRINT(board)                          # cutout 3: inside a footprint
board.Add(fp)
hole = edge(fp, pcbnew.SHAPE_T_CIRCLE)
hole.SetCenter(V(130, 122))
hole.SetEnd(V(132, 122))
fp.Add(hole)
kicad = bo.polygon_outlines(board).Area() / 1e12
o, origin = bo.outline_from_board(board)
bare, _ = bo.outline_from_board(board, include_footprints=False)
print(json.dumps({"radius": radius, "kicad_area": kicad, "area": ol.area(o),
                  "bare_area": ol.area(bare), "origin": origin, "bbox": ol.bbox(o),
                  "cutouts": [type(c).__name__ for c in o.cutouts],
                  "bare_cutouts": len(bare.cutouts)}))
"""


def test_outline_from_board_reads_rect_poly_bezier_and_footprint_edges(tmp_path):
    res = run_child(SHAPES, [], tmp_path)
    r = res["radius"]
    outer = 1200 - (4 - math.pi) * r * r
    assert res["origin"] == pytest.approx([100, 100]) and res["bbox"] == pytest.approx(
        [0, 0, 40, 30])
    assert sorted(res["cutouts"]) == ["Circle", "Loop", "Loop"] and res["bare_cutouts"] == 2
    # triangle 24.5, bezier D = 3/5 of 6 x 10 (exact for this cubic), circle 4 pi
    assert res["area"] == pytest.approx(outer - 24.5 - 36 - 4 * math.pi, abs=0.05)
    assert res["bare_area"] == pytest.approx(res["area"] + 4 * math.pi, abs=1e-6)
    assert res["area"] == pytest.approx(res["kicad_area"], abs=0.05 + 0.001 * res["area"])


PLANE = PREAMBLE + """
import board_outline as bo
import board_shapes
import outline as ol

block, out = json.loads(sys.argv[1]), sys.argv[2]
board = pcbnew.CreateEmptyBoard()
board.SetCopperLayerCount(4)
bo.draw_outline(board, ol.from_board_block(block), 100.5, 115.0)
gnd = pcbnew.NETINFO_ITEM(board, "GND")
board.Add(gnd)
via = pcbnew.PCB_VIA(board)
via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(110.5), pcbnew.FromMM(145)))
via.SetWidth(pcbnew.FromMM(0.6))
via.SetDrill(pcbnew.FromMM(0.3))
via.SetNet(gnd)
board.Add(via)
zone = board_shapes.add_plane_outline(board, pcbnew.In1_Cu, gnd, 0.3, local_clearance_mm=0.2)
z = zone.Outline()
before = {"area": z.Area() / 1e12, "outlines": z.OutlineCount(), "holes": z.HoleCount(0),
          "clearance": zone.GetLocalClearance()}
pcbnew.SaveBoard(out, board)
b2 = bo.load_board(out)
b2.BuildConnectivity()
pcbnew.ZONE_FILLER(b2).Fill(b2.Zones())
pcbnew.SaveBoard(out, b2)
z2 = [z for z in b2.Zones() if b2.GetLayerName(z.GetLayer()) == "In1.Cu"][0]
fill = pcbnew.SHAPE_POLY_SET(z2.GetFilledPolysList(z2.GetLayer()))
fill.Unfracture()
print(json.dumps({**before, "islands": fill.OutlineCount(), "fill_area": fill.Area() / 1e12,
                  "fill_holes": fill.HoleCount(0) if fill.OutlineCount() else -1,
                  "net": z2.GetNetname()}))
"""


def test_d_outline_plane_fills_to_one_island(tmp_path):
    res = run_child(PLANE, [json.dumps(T10_BOARD), tmp_path / "plane.kicad_pcb"], tmp_path)
    assert 0 < res["area"] < T10_AREA
    assert res["outlines"] == 1 and res["holes"] == 2
    assert res["clearance"] == 200_000
    assert res["islands"] == 1 and res["net"] == "GND"
    assert res["fill_holes"] >= 2 and 0 < res["fill_area"] <= res["area"]


KEEPOUT = PREAMBLE + """
import board_outline as bo
import board_shapes
import outline as ol

block, out = json.loads(sys.argv[1]), sys.argv[2]
board = pcbnew.CreateEmptyBoard()
board.SetCopperLayerCount(4)
bo.draw_outline(board, ol.from_board_block(block), 100.5, 115.0)
pts = [(105, 118), (125, 118), (125, 121), (105, 121)]
poly = board_shapes.add_keepout_polygon(board, pts, "ANT_PTS")
whole = board_shapes.add_keepout_polygon(board, bo.plane_polyset(board, 1.0), "ANT_SET",
                                         layers=["F.Cu"], no_vias=True)
legacy = board_shapes.add_keepout(board, 100.5, 115.0, 60.0, 8.0)

def info(z):
    cu = [board.GetLayerName(l) for l in z.GetLayerSet().CuStack()]
    return {"cu": cu, "blocks": bo.blocks_pour(z), "rule": z.GetIsRuleArea(),
            "vias": z.GetDoNotAllowVias(), "tracks": z.GetDoNotAllowTracks(),
            "pads": z.GetDoNotAllowPads(), "area": z.Outline().Area() / 1e12}

res = {"poly": info(poly), "whole": info(whole), "legacy": info(legacy)}
pcbnew.SaveBoard(out, board)
b2 = bo.load_board(out)
res["reloaded"] = sorted(z.GetZoneName() for z in b2.Zones() if bo.blocks_pour(z))
print(json.dumps(res))
"""


def test_e_keepouts_cover_every_copper_layer_and_block_pours(tmp_path):
    res = run_child(KEEPOUT, [json.dumps(T10_BOARD), tmp_path / "ko.kicad_pcb"], tmp_path)
    all_cu = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    poly, whole, legacy = res["poly"], res["whole"], res["legacy"]
    assert poly["cu"] == all_cu and poly["blocks"] and poly["rule"]
    assert poly["area"] == pytest.approx(60.0, abs=1e-6)
    assert not poly["vias"] and not poly["tracks"] and not poly["pads"]
    assert whole["cu"] == ["F.Cu"] and whole["vias"] and 0 < whole["area"] < T10_AREA
    # the legacy antenna keep-out: a ZONE on 9.0.4 too, no pour / vias / pads, tracks ok
    assert legacy["cu"] == all_cu and legacy["blocks"] and legacy["vias"] and legacy["pads"]
    assert not legacy["tracks"] and legacy["area"] == pytest.approx(480.0, abs=1e-6)
    assert res["reloaded"] == ["ANTENNA", "ANT_PTS", "ANT_SET"]


DATUM = PREAMBLE + """
import board_outline as bo

out = sys.argv[1]
board = pcbnew.CreateEmptyBoard()
bo.set_datum(board, 138.5, 145)
pcbnew.SaveBoard(out, board)
print(json.dumps({"aux": bo.get_aux_origin(bo.load_board(out))}))
"""


def test_f_set_datum_persists_aux_and_grid_origin(tmp_path):
    pcb = tmp_path / "datum.kicad_pcb"
    res = run_child(DATUM, [pcb], tmp_path)
    text = pcb.read_text(encoding="utf-8")
    assert "(aux_axis_origin 138.5 145)" in text
    assert "(grid_origin 138.5 145)" in text
    assert res["aux"] == pytest.approx([138.5, 145.0])


COURTYARD = PREAMBLE + """
import board_outline as bo

lib, out = sys.argv[1], sys.argv[2]
board = pcbnew.CreateEmptyBoard()
fp = pcbnew.FootprintLoad(lib, "SOIC-8_3.9x4.9mm_P1.27mm")
if fp is None:
    raise SystemExit("FootprintLoad returned None for SOIC-8 in " + lib)
fp.SetReference("U1")
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(120), pcbnew.FromMM(90)))
board.Add(fp)
pcbnew.SaveBoard(out, board)
loaded = bo.load_board(out)
fp2 = loaded.GetFootprints()[0]
raw = fp2.GetCourtyard(pcbnew.F_CrtYd).OutlineCount()
cy = bo.courtyard(fp2, pcbnew.F_CrtYd)
print(json.dumps({"raw_outlines": raw, "outlines": cy.OutlineCount(), "area": cy.Area() / 1e12,
                  "x": pcbnew.ToMM(cy.BBox().Centre().x)}))
"""


def test_g_courtyard_of_a_loaded_soic8_is_not_empty(tmp_path):
    lib = kicad_footprints(require_kicad_python(), "Package_SO.pretty")
    res = run_child(COURTYARD, [lib, tmp_path / "cy.kicad_pcb"], tmp_path)
    assert res["outlines"] == 1 and res["area"] > 20     # 7.4 x 5.4 mm courtyard
    assert res["x"] == pytest.approx(120.0, abs=0.01)


LOAD = PREAMBLE + """
import board_outline as bo

missing, garbage = sys.argv[1], sys.argv[2]
calls = []
real = pcbnew.LoadBoard
pcbnew.LoadBoard = lambda p: calls.append(p)
try:
    bo.load_board(missing)
    first = "no error"
except FileNotFoundError as e:
    first = "FileNotFoundError: " + str(e)
pcbnew.LoadBoard = real
try:
    bo.load_board(garbage)
    second = "no error"
except RuntimeError as e:
    second = "RuntimeError: " + str(e)
print(json.dumps({"missing": first, "calls": calls, "garbage": second}))
"""


def test_h_load_board_refuses_a_missing_path_before_pcbnew(tmp_path):
    garbage = tmp_path / "garbage.kicad_pcb"
    garbage.write_text("this is not a board\n", encoding="utf-8")
    res = run_child(LOAD, [tmp_path / "missing.kicad_pcb", garbage], tmp_path)
    assert res["missing"].startswith("FileNotFoundError: board file not found")
    assert res["calls"] == []
    assert res["garbage"].startswith("RuntimeError:") and "garbage.kicad_pcb" in res["garbage"]


LEGACY = PREAMBLE + """
import board_outline as bo
import board_shapes

board = pcbnew.CreateEmptyBoard()
board.SetCopperLayerCount(4)
# builder-style numbers whose FromMM truncates: ox + w = 158.51999999999998 -> 158519999
# nm and ox + w - 0.3 = 158.21999999999997 -> 158219999 nm (rounding would say ...000)
w, h, ox, oy = 20.04, 30.0, 138.48, 90.0
segs = board_shapes.add_outline(board, w, h, ox, oy)
gnd = pcbnew.NETINFO_ITEM(board, "GND")
board.Add(gnd)
plane = board_shapes.add_plane(board, pcbnew.In1_Cu, gnd, w, h, ox, oy)
F = pcbnew.FromMM
want_segs = [[F(ox), F(oy), F(ox + w), F(oy)], [F(ox + w), F(oy), F(ox + w), F(oy + h)],
             [F(ox + w), F(oy + h), F(ox), F(oy + h)], [F(ox), F(oy + h), F(ox), F(oy)]]
got_segs = [[s.GetStart().x, s.GetStart().y, s.GetEnd().x, s.GetEnd().y] for s in segs]
chain = plane.Outline().COutline(0)
got_plane = [[chain.CPoint(i).x, chain.CPoint(i).y] for i in range(chain.PointCount())]
want_plane = [[F(ox + 0.3), F(oy + 0.3)], [F(ox + w - 0.3), F(oy + 0.3)],
              [F(ox + w - 0.3), F(oy + h - 0.3)], [F(ox + 0.3), F(oy + h - 0.3)]]
print(json.dumps({"segs": got_segs == want_segs, "plane": got_plane == want_plane,
                  "widths": sorted({s.GetWidth() for s in segs}), "got": got_segs,
                  "want": want_segs, "plane_net": plane.GetNetname(),
                  "truncates": [F(v) != round(v * 1e6) for v in (ox + w, ox + w - 0.3)]}))
"""


def test_legacy_rectangle_helpers_keep_their_exact_geometry(tmp_path):
    res = run_child(LEGACY, [], tmp_path)
    assert res["truncates"] == [True, True], "the case must exercise FromMM truncation"
    assert res["segs"], (res["got"], res["want"])
    assert res["plane"] and res["plane_net"] == "GND"
    assert res["widths"] == [100_000]

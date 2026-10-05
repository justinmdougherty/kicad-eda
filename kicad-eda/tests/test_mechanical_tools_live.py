"""mechanical_tools against the real kicad-cli (KiCad 9.0.4 locally, 10.0.5 in the
kicad-eda:wave1 image). The T10 board (60 x 40, r=4 corners, a stadium slot and a
round hole, datum (10, 30)) is drawn on Edge.Cuts at sheet (100.5, 115) by a
KiCad-python child through board_outline (F3-01: draw_outline + set_datum, no
footprints), so the aux origin sits on the datum. Then, in the datum (Fusion) frame:

  * export_outline_dxf: the DXF read back matches the board's Edge.Cuts within
    0.01 mm (Hausdorff), and the pre-build write_outline_dxf exchange file agrees;
  * export_step(board_only=True): the STEP spans x -10..50, y -10..30 (T10 VERIFIED);
  * export_pos_datum runs (an empty board writes a header-only CSV).

Skips cleanly without a KiCad Python that imports pcbnew (swig_python fixture).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import dxf_io
import mech_export as mx
import mechanical_tools as mt
import outline as ol
import toolchain as tc
from fixtures.swig_python import PREAMBLE, T10_BOARD, require_kicad_python, run_child

SAVED_FORMATS = ("20241229", "20260206")      # 9.0 / 10.0 board files
STEP_TOL_MM = 0.05

DRAW_T10 = PREAMBLE + """
import board_outline as bo
import outline as ol

block, out = json.loads(sys.argv[1]), sys.argv[2]
model = ol.from_board_block(block)
board = pcbnew.CreateEmptyBoard()
bo.draw_outline(board, model, 100.5, 115.0)
bo.set_datum(board, model.datum[0] + 100.5, model.datum[1] + 115.0)
pcbnew.SaveBoard(out, board)
print(json.dumps({"version": pcbnew.Version()}))
"""


@pytest.fixture(scope="module")
def t10(tmp_path_factory: pytest.TempPathFactory) -> Path:
    require_kicad_python()
    work = tmp_path_factory.mktemp("mech_live")
    pcb = work / "t10.kicad_pcb"
    run_child(DRAW_T10, [json.dumps(T10_BOARD), pcb], work)
    if not tc.board_cli(str(pcb)):
        pytest.skip("no kicad-cli for the T10 board")
    return pcb


def test_the_board_is_a_pre_11_file_with_the_aux_origin_on_the_datum(t10):
    text = t10.read_text(encoding="utf-8")
    assert re.search(r"\(version (\d+)\)", text).group(1) in SAVED_FORMATS
    assert re.search(r"\(aux_axis_origin 110\.5 145\)", text)
    assert mx.board_file_outline(t10).datum == pytest.approx((10, 30))


def test_export_outline_dxf_matches_the_board_edge_cuts(t10):
    out = t10.with_name("t10_edge.dxf")
    res = mt.export_outline_dxf(str(t10), str(out))
    assert res["ok"] is True, res
    assert res["hausdorff_mm"] <= 0.01
    assert res["command"][1:] == ["pcb", "export", "dxf", "--layers", "Edge.Cuts",
                                  "--use-drill-origin", "--ou", "mm", "--drill-shape-opt", "0",
                                  "--mode-single", "-o", str(out), str(t10)]
    back = dxf_io.read_board_outline(out, units="mm")
    assert back.datum == pytest.approx((10, 30), abs=1e-3)
    assert len(back.model.cutouts) == 2
    assert mx.hausdorff(back.model, ol.from_board_block(T10_BOARD)) <= 0.01


def test_the_pre_build_exchange_dxf_agrees_with_the_export(t10, tmp_path):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps({"board": T10_BOARD, "components": []}), encoding="utf-8")
    pre = mt.write_outline_dxf(str(netlist), "pre.dxf")
    post = mt.export_outline_dxf(str(t10), str(tmp_path / "post.dxf"))
    assert pre["ok"] is True and post["ok"] is True, (pre, post)
    a = dxf_io.read_board_outline(pre["dxf"])
    b = dxf_io.read_board_outline(post["dxf"], units="mm")
    assert mx.frame_hausdorff(a.model, b.model) <= 0.01


def _has_step_export(t10: Path) -> bool:
    proc = subprocess.run([tc.board_cli(str(t10)), "pcb", "export", "step", "--help"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=60)
    return "--drill-origin" in proc.stdout + proc.stderr


def test_export_step_board_only_lands_in_the_fusion_frame(t10):
    out = t10.with_name("t10_board.step")
    res = mt.export_step(str(t10), str(out), board_only=True)
    if not res["ok"] and not _has_step_export(t10):
        pytest.skip(f"this kicad-cli has no STEP export: {res['reason']}")
    assert res["ok"] is True, res
    box = res["bbox"]
    assert box["x"] == pytest.approx([-10, 50], abs=STEP_TOL_MM), box
    assert box["y"] == pytest.approx([-10, 30], abs=STEP_TOL_MM), box
    assert box["z"][0] == pytest.approx(0, abs=STEP_TOL_MM) and box["z"][1] > 0.5
    assert box["basis"] == "edges" and box["unit"] == "mm"


def test_export_pos_datum_runs_on_the_empty_board(t10):
    out = t10.with_name("t10_datum_pos.csv")
    res = mt.export_pos_datum(str(t10), str(out))
    assert res["ok"] is True, res
    assert res["rows"] == 0 and out.read_text(encoding="utf-8").startswith("Ref,")
    assert res["command"][1:6] == ["pcb", "export", "pos", "--use-drill-file-origin", "--units"]

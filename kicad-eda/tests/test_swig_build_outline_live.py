"""swig_build_board's outline frame (F3-11, ROADMAP_DECISIONS D2).

The builder takes its board from the env KICAD_EDA_OUTLINE sidecar
(outline_resolve), else the netlist board block; outline.loop /
corner_radius / cutouts / datum boards get exact Edge.Cuts, the aux origin at
the datum, outline-following planes and the smallest sheet that holds them;
an unresolved outline.dxf is refused; w/h and auto-sized boards keep the
legacy geometry. KIIDs are seeded from the inputs.

Unit tier (dev venv, no pcbnew): build_features' pure helpers - outline
source classification and the sidecar swap (no input mutation), sheet choice,
paper splice, KIID seed, keep-out strips, corner holes.

Live tier (KiCad Python; KiCad 9.0.4 locally and 10.0.5 in kicad-eda:wave1,
RULES.md "K10"; skipped without a KiCad Python that imports pcbnew):
 1. examples/lm358_buffer geometry == the golden captured with the builder as
    it was before F3-11 (tests/fixtures/outline/legacy_lm358_geometry_k<N>.json,
    tests/fixtures/outline/capture_legacy_geometry.py);
 2. the T10 board: KiCad's Edge.Cuts polygon closes, area 2314.36 +/- 0.05,
    aux origin == datum + (ox, oy), the In1.Cu GND zone has 2 holes and fills;
 3. outline.dxf without a sidecar -> ok false; with outline_resolve's
    sidecar -> builds;
 4. rpi5_battery_hat + corner_radius 3 -> 4 Edge.Cuts arcs;
 5. a 300 x 200 loop board -> (paper "A3"), centred on the sheet;
 6. three T10 builds -> the same footprint order (and byte-identical files);
 7. default corner mounting holes on an L-shape -> ok false.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import build_features as bf
import outline as ol
import outline_resolve as orz
import toolchain as tc
from conftest import load_example
from fixtures import swig_python as sp

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "server" / "tools" / "swig_build_board.py"
OUTLINE_FIXTURES = ROOT / "tests" / "fixtures" / "outline"
T10 = OUTLINE_FIXTURES / "t10_board.json"
CAPTURE = OUTLINE_FIXTURES / "capture_legacy_geometry.py"
BUILD_TIMEOUT_S = 600
T10_AREA = 2314.36
_REF = re.compile(r'\(property "Reference" "([^"]+)"')
L_SHAPE = [[0, 0], [32, 0], [32, 20], [60, 20], [60, 45], [0, 45]]


# --- unit tier: build_features' pure helpers ----------------------------------------------
@pytest.mark.parametrize("board,legacy", [
    (None, True), ({}, True), ({"w": 50, "h": 30}, True),
    ({"w": 50, "h": 30, "corner_radius": 0}, True), ({"w": 50, "h": 30, "cutouts": []}, True),
    ({"w": 50, "h": 30, "auto_x": 2.0}, True), ({"auto_y": 3.0}, True),
    ({"w": 50, "h": 30, "corner_radius": 3}, False),
    ({"w": 50, "h": 30, "datum": [0, 30]}, False),
    ({"w": 50, "h": 30, "cutouts": [{"circle": [10, 10, 2]}]}, False),
    ({"outline": {"loop": L_SHAPE}}, False)])
def test_is_legacy_block(board, legacy):
    assert bf.is_legacy_block(board) is legacy


def test_legacy_source_keeps_the_design_and_needs_no_resolver():
    design = {"components": [], "board": {"auto_y": 3.0}}       # resolve_board would refuse
    src = bf.resolve_outline_source(design, {})
    assert (src.legacy, src.source, src.outline, src.sidecar) == (True, "rect", None, None)
    assert src.design == design and bf.outline_dims(src) is None
    assert bf.resolve_outline_source({"components": []}, {}).source == "none"


def test_board_block_outline_is_resolved_and_validated():
    design = {"components": [], "board": {"w": 60, "h": 40, "corner_radius": 3}}
    src = bf.resolve_outline_source(design, {})
    assert (src.legacy, src.source) == (False, "rounded_rect")
    assert bf.outline_dims(src) == pytest.approx((60, 40))
    with pytest.raises(bf.BuildError, match="board outline"):
        bf.resolve_outline_source({"board": {"outline": {"loop": [[5, 5], [9, 5], [9, 9]]}}}, {})


def test_dxf_without_sidecar_is_refused():
    design = {"board": {"outline": {"dxf": "mech/outline.dxf"}}}
    with pytest.raises(bf.BuildError) as exc:
        bf.resolve_outline_source(design, {})
    assert str(exc.value) == bf.DXF_UNRESOLVED
    assert bf.DXF_UNRESOLVED == ("board.outline.dxf must be resolved first "
                                 "(build_board_from_netlist / outline_resolve.py does this)")


def test_sidecar_replaces_the_board_without_mutating_the_design(tmp_path):
    design = {"components": [{"ref": "R1"}], "board": {"outline": {"dxf": "x.dxf"}}}
    before = json.dumps(design, sort_keys=True)
    resolved = orz.resolve_board({"board": {"outline": {"loop": L_SHAPE}}}, None)
    path = orz.write_sidecar(resolved, tmp_path / "b.outline.json")
    src = bf.resolve_outline_source(design, {orz.ENV_VAR: str(path)})
    assert json.dumps(design, sort_keys=True) == before
    assert src.design is not design and src.design["board"] == resolved.block
    assert src.design["components"] == design["components"]
    assert (src.source, src.legacy, src.sidecar) == ("loop", False, str(path))
    assert src.sidecar_bytes == path.read_bytes()
    bad = tmp_path / "bad.outline.json"
    bad.write_text('{"version": 2}', encoding="utf-8")
    with pytest.raises(bf.BuildError, match=orz.ENV_VAR):
        bf.resolve_outline_source(design, {orz.ENV_VAR: str(bad)})


def test_kiid_seed_is_32_bit_and_depends_on_both_inputs():
    a = bf.kiid_seed(b"netlist")
    assert a == int.from_bytes(hashlib.sha256(b"netlist").digest()[:4], "big")
    assert 0 <= a < 2 ** 32 and a == bf.kiid_seed(b"netlist", b"")
    assert bf.kiid_seed(b"netlist", b"sidecar") != a


@pytest.mark.parametrize("w,h,sheet", [
    (60, 40, ("A4", 297.0, 210.0)), (257, 170, ("A4", 297.0, 210.0)),
    (258, 170, ("A3", 420.0, 297.0)), (300, 200, ("A3", 420.0, 297.0)),
    (500, 300, ("A2", 594.0, 420.0)), (1149, 801, ("A0", 1189.0, 841.0)),
    (1300, 300, ("User", 1340.0, 340.0)), (100.2, 900, ("User", 141.0, 940.0))])
def test_choose_sheet(w, h, sheet):
    assert bf.choose_sheet(w, h) == sheet


def test_paper_splice():
    text = b'(kicad_pcb\n\t(version 20241229)\n\t(paper "A4")\n\t(layers)\n)\n'
    assert bf.splice_paper_bytes(text, ("A3", 420.0, 297.0)) == text.replace(
        b'"A4"', b'"A3"')
    assert b'(paper "User" 1340 340)' in bf.splice_paper_bytes(text, ("User", 1340.0, 340.0))
    assert bf.splice_paper_bytes(text, bf.LEGACY_SHEET) == text           # already there
    for bad in (text.replace(b'(paper "A4")', b""), text + b'(paper "A4")'):
        with pytest.raises(bf.BuildError, match="paper"):
            bf.splice_paper_bytes(bad, ("A3", 420.0, 297.0))


def test_paper_splice_file_keeps_line_endings(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_bytes(b'(kicad_pcb\r\n\t(paper "A4")\r\n)\r\n')
    bf.splice_paper(str(pcb), ("A2", 594.0, 420.0))
    assert pcb.read_bytes() == b'(kicad_pcb\r\n\t(paper "A2")\r\n)\r\n'
    assert list(tmp_path.iterdir()) == [pcb]


def test_keepout_rect_is_the_legacy_arithmetic():
    ox, oy, w, h = 118.5, 85.0, 60.0, 40.0
    assert bf.keepout_rect({"edge": "bottom", "depth": 6}, w, h, ox, oy) == (
        ox, oy + h - 6.0, w, 6.0)
    assert bf.keepout_rect({"edge": "right"}, w, h, ox, oy) == (ox + w - 8.0, oy, 8.0, h)
    assert bf.keepout_rect({"edge": "nowhere"}, w, h) == (0.0, 0.0, w, 8.0)
    assert bf.keepout_rect({"rect": [1, 2, 3, 4]}, w, h, ox, oy) == (ox + 1, oy + 2, 3, 4)
    assert not bf.leaves_bbox(bf.keepout_rect({"edge": "left"}, w, h), w, h)
    assert bf.leaves_bbox((55, 0, 10, 5), w, h) and bf.leaves_bbox((-1, 0, 5, 5), w, h)


def test_corner_positions_and_datum_default():
    assert bf.corner_positions(60, 40, 4) == [(4, 4), (56, 4), (56, 36), (4, 36)]
    o = ol.from_board_block({"w": 60, "h": 40})
    assert bf.datum_board(o) == (0.0, 40.0)
    assert bf.datum_board(ol.from_board_block({"w": 60, "h": 40, "datum": [10, 30]})) == (10, 30)


def test_default_planes_heuristic():
    nets = {"GND": [], "VIN": [], "+3V3": [], "SIG": []}
    assert bf.default_planes({"nets": nets}) == {"In1.Cu": "GND", "In2.Cu": "+3V3"}
    assert bf.default_planes({"nets": {"GND": [], "VBUS": []}}) == {"In1.Cu": "GND",
                                                                     "In2.Cu": "VBUS"}
    assert bf.default_planes({"nets": {"GND": [], "SIG": []}}) == {"In1.Cu": "GND"}
    assert bf.default_planes({"planes": {"B.Cu": "GND"}}) == {"B.Cu": "GND"}


def test_builder_keeps_the_mounting_hole_default_and_its_size():
    src = BUILDER.read_text(encoding="utf-8")
    assert "DEFAULT_MOUNTING_HOLE_FOOTPRINT" in src                 # test_libtable drift
    assert len(src.splitlines()) < 650
    assert len((BUILDER.parent / "build_features.py").read_text(encoding="utf-8")
               .splitlines()) < 450


# --- live tier ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def kicad() -> tuple[str, Path]:
    """(KiCad Python, the stock footprints root of the SAME install)."""
    kpy = sp.require_kicad_python()
    return kpy, sp.kicad_footprints(kpy, "Package_SO.pretty").parent


def build(kicad: tuple[str, Path], netlist: Path, out: Path,
          env: dict | None = None) -> tuple[int, dict]:
    """(rc, result JSON) of swig_build_board.py, stdin closed, no outline env
    unless `env` gives one."""
    kpy, footprints = kicad
    child_env = {k: v for k, v in os.environ.items() if k not in (orz.ENV_VAR, "PACK_SCALE")}
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(out), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S,
                          env={**child_env, **(env or {})})
    result = tc.last_json(proc.stdout) or {"stdout": proc.stdout}
    return proc.returncode, {**result, "stderr": proc.stderr[-2000:]}


def write_netlist(folder: Path, design: dict, name: str = "netlist.json") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(design, indent=1), encoding="utf-8")
    return path


INSPECT = sp.PREAMBLE + r"""
import board_outline as bo
board = bo.load_board(sys.argv[1])
shapes = bo.edge_shapes(board, include_footprints=False)
kinds = {}
for s in shapes:
    kinds[s.ShowShape()] = kinds.get(s.ShowShape(), 0) + 1
outline, origin = bo.outline_from_board(board, include_footprints=False)
zones = []
for z in board.Zones():
    o = z.Outline()
    filled = sum(z.GetFilledPolysList(lid).Area() for lid in z.GetLayerSet().Seq()
                 if z.HasFilledPolysForLayer(lid))
    zones.append({"layer": board.GetLayerName(z.GetLayer()), "net": z.GetNetname(),
                  "rule_area": bool(z.GetIsRuleArea()), "outlines": o.OutlineCount(),
                  "holes": [o.HoleCount(i) for i in range(o.OutlineCount())],
                  "filled": bool(z.IsFilled()), "filled_mm2": filled / 1e12})
print(json.dumps({"area": bo.polygon_outlines(board).Area() / 1e12, "kinds": kinds,
                  "origin": list(origin), "aux": list(bo.get_aux_origin(board)),
                  "datum": list(outline.datum) if outline.datum else None,
                  "zones": zones, "major": int(str(pcbnew.GetBuildVersion()).split(".")[0])}))
"""


def inspect(pcb: Path, tmp_path: Path) -> dict:
    return sp.run_child(INSPECT, [pcb], tmp_path)


def test_live_1_legacy_lm358_geometry_matches_the_pre_f3_11_golden(kicad, tmp_path):
    kpy, footprints = kicad
    out = tmp_path / "lm358_geometry.json"
    proc = subprocess.run([kpy, str(CAPTURE), str(out), "--footprints", str(footprints),
                           "--work", str(tmp_path / "work")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    new = json.loads(out.read_text(encoding="utf-8"))
    golden_path = OUTLINE_FIXTURES / f"legacy_lm358_geometry_k{new['major']}.json"
    if not golden_path.is_file():
        pytest.skip(f"no legacy golden for pcbnew {new['pcbnew']} (goldens: 9.0.4, 10.0.5)")
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    for key in ("footprints", "edge_cuts", "zones", "board_mm"):
        assert new[key] == golden[key], key
    assert len(new["footprints"]) == 4 and len(new["edge_cuts"]) == 4 and len(new["zones"]) == 2


@pytest.fixture(scope="module")
def t10_build(kicad, tmp_path_factory) -> tuple[Path, dict]:
    work = tmp_path_factory.mktemp("t10")
    out = work / "t10.kicad_pcb"
    rc, res = build(kicad, T10, out)
    assert rc == 0 and res.get("ok"), res
    return out, res


def test_live_2_t10_board(t10_build, tmp_path):
    pcb, res = t10_build
    info = res["outline"]
    assert (info["source"], info["legacy"], info["cutouts"]) == ("loop", False, 2)
    assert res["board_mm"] == pytest.approx([60.0, 40.0]) and info["paper"] == "A4"
    assert info["area_mm2"] == pytest.approx(T10_AREA, abs=0.05)
    got = inspect(pcb, tmp_path)                       # polygon_outlines raised if open
    assert got["area"] == pytest.approx(T10_AREA, abs=0.05)
    ox, oy = got["origin"]                             # Edge.Cuts bbox top-left, sheet
    assert [ox, oy] == pytest.approx(info["origin_sheet"], abs=1e-6)
    assert got["aux"] == pytest.approx([10 + ox, 30 + oy], abs=1e-6)
    assert info["aux_origin_sheet"] == pytest.approx(got["aux"], abs=1e-6)
    assert info["datum_board"] == [10.0, 30.0] and got["datum"] == pytest.approx([10, 30])
    gnd = [z for z in got["zones"] if z["layer"] == "In1.Cu" and z["net"] == "GND"]
    assert len(gnd) == 1 and gnd[0]["outlines"] == 1 and gnd[0]["holes"] == [2], got["zones"]
    assert gnd[0]["filled"] and gnd[0]["filled_mm2"] > 1000, gnd
    assert sorted(res["zones"]) == ["In1.Cu:GND", "In2.Cu:+3V3"]


def _dxf_design() -> dict:
    return {"name": "dxf_board", "board": {"outline": {"dxf": "board.dxf"}},
            "components": [
                {"ref": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric",
                 "x": 45, "y": 30, "rot": 0},
                {"ref": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0603_1608Metric",
                 "x": 50, "y": 30, "rot": 0}],
            "nets": {"GND": [["R1", "2"], ["C1", "2"]], "+3V3": [["R1", "1"], ["C1", "1"]]}}


def test_live_3_dxf_outline_needs_the_sidecar(kicad, tmp_path):
    from fixtures.dxf_builders import Sketch, fusion_board, fusion_doc, save
    doc, msp = fusion_doc()
    fusion_board(Sketch(msp, k=10.0))                  # 60 x 40, r 3, hole, slot; origin (10, 10)
    save(doc, tmp_path, "board.dxf")
    design = _dxf_design()
    netlist = write_netlist(tmp_path, design)
    out = tmp_path / "dxf_board.kicad_pcb"
    rc, res = build(kicad, netlist, out)
    assert rc == 1 and res["ok"] is False and res["error"] == bf.DXF_UNRESOLVED, res
    assert "trace" not in res and not out.exists()
    resolved = orz.resolve_board(design, tmp_path)     # the venv side (ezdxf)
    sidecar = orz.write_sidecar(resolved, orz.sidecar_path(out))
    rc, res = build(kicad, netlist, out, env={orz.ENV_VAR: str(sidecar)})
    assert rc == 0 and res["ok"], res
    info = res["outline"]
    assert (info["source"], info["cutouts"]) == ("dxf", 2)
    assert info["datum_board"] == pytest.approx([10, 10])
    assert res["board_mm"] == pytest.approx([60, 40])
    got = inspect(out, tmp_path)
    assert got["area"] == pytest.approx(ol.area(resolved.outline), abs=0.05)
    assert got["aux"] == pytest.approx([10 + got["origin"][0], 10 + got["origin"][1]], abs=1e-6)


def test_live_4_rpi5_hat_with_corner_radius_has_four_arcs(kicad, tmp_path):
    design = load_example("rpi5_battery_hat")
    design["board"] = {**design["board"], "corner_radius": 3}
    # the stock USB-C footprints name the shield pad differently per release;
    # the shield net is not what this test is about
    design["nets"] = {n: [p for p in conns if p != ["J1", "SH"]]
                      for n, conns in design["nets"].items()}
    netlist = write_netlist(tmp_path, design)
    out = tmp_path / "rpi5_r3.kicad_pcb"
    rc, res = build(kicad, netlist, out)
    assert rc == 0 and res["ok"], res
    assert res["outline"]["source"] == "rounded_rect" and res["board_mm"] == [65.0, 56.5]
    assert res["mounting_holes"] == ["H1", "H2", "H3", "H4"]
    got = inspect(out, tmp_path)
    assert got["kinds"] == {"Arc": 4, "Line": 4}, got["kinds"]
    assert got["area"] == pytest.approx(65 * 56.5 - 4 * (9 - 9 * 3.141592653589793 / 4),
                                        abs=0.05)


def test_live_5_large_loop_board_gets_a3_and_is_centred(kicad, tmp_path):
    design = {"name": "big", "board": {"outline": {"loop": [[0, 0], [300, 0], [300, 200],
                                                            [0, 200]]}},
              "components": [
                  {"ref": "R1", "value": "1k", "footprint": "Resistor_SMD:R_0603_1608Metric",
                   "x": 20, "y": 20, "rot": 0},
                  {"ref": "R2", "value": "1k", "footprint": "Resistor_SMD:R_0603_1608Metric",
                   "x": 280, "y": 180, "rot": 0}],
              "nets": {"N1": [["R1", "1"], ["R2", "1"]], "GND": [["R1", "2"], ["R2", "2"]]}}
    out = tmp_path / "big.kicad_pcb"
    rc, res = build(kicad, write_netlist(tmp_path, design), out)
    assert rc == 0 and res["ok"], res
    text = out.read_text(encoding="utf-8")
    assert '(paper "A3")' in text and '(paper "A4")' not in text
    assert res["outline"]["paper"] == "A3" and res["outline"]["sheet_mm"] == [420.0, 297.0]
    got = inspect(out, tmp_path)
    ox, oy = got["origin"]
    assert (ox + 150, oy + 100) == pytest.approx((210.0, 148.5), abs=1e-6)
    assert got["aux"] == pytest.approx([ox, oy + 200], abs=1e-6)       # default datum


def test_live_6_t10_rebuilds_are_deterministic(kicad, t10_build, tmp_path, record_property):
    first, _ = t10_build
    files = [first]
    for i in range(2):
        out = tmp_path / f"t10_{i}.kicad_pcb"
        rc, res = build(kicad, T10, out)
        assert rc == 0 and res["ok"], res
        files.append(out)
    orders = [_REF.findall(p.read_text(encoding="utf-8")) for p in files]
    assert len(orders[0]) == 14 and orders[0] == orders[1] == orders[2], orders
    identical = len({p.read_bytes() for p in files}) == 1
    record_property("t10_byte_identical", identical)
    print(f"T10 rebuilds byte-identical: {identical}")
    assert identical


def test_live_7_default_corner_holes_on_an_l_shape_are_refused(kicad, tmp_path):
    design = {"name": "lshape", "board": {"outline": {"loop": L_SHAPE}},
              "mounting_holes": True,
              "components": [{"ref": "R1", "value": "1k",
                              "footprint": "Resistor_SMD:R_0603_1608Metric",
                              "x": 10, "y": 30, "rot": 0}],
              "nets": {"N1": [["R1", "1"]], "GND": [["R1", "2"]]}}
    out = tmp_path / "lshape.kicad_pcb"
    rc, res = build(kicad, write_netlist(tmp_path, design), out)
    assert rc == 1 and res["ok"] is False and res["error"] == bf.HOLES_NEED_POSITIONS, res
    design["mounting_holes"] = {"positions": [[4, 4], [56, 41], [30.5, 10]]}  # H3 in the notch
    rc, res = build(kicad, write_netlist(tmp_path, design), out)
    assert rc == 0 and res["ok"], res
    assert res["mounting_holes"] == ["H1", "H2", "H3"]
    assert [w for w in res["warnings"] if w.startswith("mounting hole: H3")], res["warnings"]
    assert not [w for w in res["warnings"] if w.startswith(("mounting hole: H1",
                                                             "mounting hole: H2"))]

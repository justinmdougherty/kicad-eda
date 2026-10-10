"""swig_build_board keep-mode round trip (F46-19): D3 "at" + D4 attributes.

A keep-mode rebuild hands the builder every part by its footprint origin and
displayed rotation ("at", F and B side) plus the per-component flags
``board_only`` / ``dnp`` / ``in_bom``. The builder places "at" parts with
swig_anchor.place_at (F3-12), loads, places and nets board_only parts like
any other part, turns the flags into footprint attributes with
swig_anchor.apply_attrs on every footprint, and reports "anchored" (the number
of "at" parts), "board_only" (refs) and "attr_warnings" (setters this pcbnew
lacks; also in "warnings").

Unit tier (dev venv): build_layout.check_design refuses a non-boolean flag.

Live tier (KiCad Python; 9.0.4 locally, 10.0.5 in kicad-eda:wave1, RULES.md "K10"):
 1. KEEP: stock footprints, every part "at" (a 45-degree B-side SOT-23, F and B
    parts at 0 / 90 / 180 / 270 / -45 / 30 degrees), a board_only
    MountingHole_3.2mm_M3 on no net, a board_only TestPoint_Pad_D1.5mm on GND, a
    dnp resistor and an in_bom=false capacitor. Read back with
    kicad_import_pcb.read_board in the importer's own board frame
    (board_frame): every origin within 1e-3 mm, rotation mod 360, side; the
    attributes in the file; net_equivalence.pcb_pad_nets == netlist_pad_nets.
 2. MIXED: board_only parts placed by x/y and by the auto packer are on the
    board with their attributes, and the pad nets still match.
 3. A non-boolean flag is a clean build refusal (no traceback).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import build_features as feat
import build_layout as bl
import kicad_import_pcb as kpcb
import net_equivalence as ne
import outline_resolve as orz
import swig_anchor as sa
import toolchain as tc
from fixtures import swig_python as sp

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "server" / "tools" / "swig_build_board.py"
BUILD_TIMEOUT_S = 600
TOL_MM = 1e-3
SAVED_FORMATS = (20241229, 20260206)              # 9.0 / 10.0 board files
R0603, C0603 = "Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"
SOT23, SOIC8 = "Package_TO_SOT_SMD:SOT-23", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
HDR3 = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"
HOLE, TESTPOINT = "MountingHole:MountingHole_3.2mm_M3", "TestPoint:TestPoint_Pad_D1.5mm"
FIDUCIAL = "Fiducial:Fiducial_1mm_Mask2mm"


def _part(ref: str, fp: str, **kw: object) -> dict:
    return {"ref": ref, "value": ref, "footprint": fp, **kw}


# --- unit tier ------------------------------------------------------------------------------
@pytest.mark.parametrize("flags", [{"dnp": "yes"}, {"in_bom": 0}, {"board_only": 1},
                                   {"board_only": "true"}])
def test_check_design_refuses_a_non_boolean_flag(flags: dict) -> None:
    (key,) = flags
    design = {"components": [_part("R1", R0603, x=1.0, y=2.0), _part("R2", R0603, **flags)]}
    with pytest.raises(feat.BuildError, match=rf"^R2: '{key}' must be a boolean"):
        bl.check_design(design)


def test_check_design_accepts_boolean_and_null_flags() -> None:
    comps = [_part("R1", R0603, board_only=True, dnp=False, in_bom=None),
             _part("R2", R0603, at=[1.0, 2.0, 90], side="B", dnp=True, in_bom=False),
             _part("H1", HOLE, board_only=False)]
    assert bl.check_design({"components": comps}) is None


def test_file_budget() -> None:
    assert len(BUILDER.read_text(encoding="utf-8").splitlines()) < 800


# --- live tier ------------------------------------------------------------------------------
KEEP_PARTS = [  # ref, footprint, at, side, flags
    ("U1", SOT23, [12.345678, 8.765432, 45], "B", {}),
    ("R1", R0603, [20.1234567, 18.5, 90], "F", {}),
    ("R2", R0603, [26.25, 12.125, 180], "F", {"dnp": True}),
    ("C1", C0603, [31.5, 22.75, 270], "B", {}),
    ("C2", C0603, [35.0, 30.0, -45], "F", {"in_bom": False}),
    ("J1", HDR3, [40.5, 14.0, 30], "F", {}),
    ("H1", HOLE, [5.0, 5.0, 0], "F", {"board_only": True}),
    ("TP1", TESTPOINT, [44.0, 34.0, 0], "F", {"board_only": True})]
KEEP = {"name": "keep", "board": {"w": 50.0, "h": 40.0},
        "components": [{**_part(ref, fp), "at": at, "side": side, **flags}
                       for ref, fp, at, side, flags in KEEP_PARTS],
        "nets": {"GND": [["U1", "2"], ["R1", "2"], ["C1", "2"], ["C2", "2"], ["J1", "1"],
                         ["TP1", "1"]],
                 "VCC": [["U1", "3"], ["R2", "1"], ["C1", "1"], ["J1", "2"]],
                 "SIG": [["U1", "1"], ["R1", "1"], ["R2", "2"], ["C2", "1"], ["J1", "3"]]}}
MIXED = {"name": "mixed", "board": {"w": 40.0, "h": 30.0},
         "components": [_part("U1", SOIC8), _part("C1", C0603),
                        _part("R1", R0603, x=6.0, y=25.0, rot=0),
                        _part("H1", HOLE, x=35.0, y=25.0, rot=0, board_only=True),
                        _part("FID1", FIDUCIAL, board_only=True, dnp=True),
                        _part("TP1", TESTPOINT, board_only=True)],
         "nets": {"GND": [["U1", "4"], ["C1", "2"], ["R1", "2"], ["TP1", "1"]],
                  "VCC": [["U1", "8"], ["C1", "1"]], "SIG": [["U1", "1"], ["R1", "1"]]},
         "no_connect": [["U1", p] for p in ("2", "3", "5", "6", "7")]}


@pytest.fixture(scope="module")
def kicad() -> tuple[str, Path]:
    """(KiCad Python, the stock footprints root of the SAME install)."""
    kpy = sp.require_kicad_python()
    return kpy, sp.kicad_footprints(kpy, "MountingHole.pretty").parent


def build(kicad: tuple[str, Path], design: dict, folder: Path) -> tuple[int, dict, Path]:
    """(rc, result JSON, board path) of swig_build_board.py on `design` (stdin
    closed, a timeout; no outline sidecar, no PACK_SCALE from the caller)."""
    kpy, footprints = kicad
    folder.mkdir(parents=True, exist_ok=True)
    netlist = folder / "netlist.json"
    netlist.write_text(json.dumps(design, indent=1), encoding="utf-8")
    out = folder / f"{design['name']}.kicad_pcb"
    env = {k: v for k, v in os.environ.items() if k not in (orz.ENV_VAR, "PACK_SCALE")}
    proc = subprocess.run([kpy, str(BUILDER), str(netlist), str(out), str(footprints)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_S, env=env)
    result = tc.last_json(proc.stdout) or {"stdout": proc.stdout[-4000:]}
    return proc.returncode, {**result, "stderr": proc.stderr[-2000:]}, out


def _read(pcb: Path) -> tuple[str, kpcb.BoardDesign, dict, kpcb.BoardFrame]:
    text = pcb.read_text(encoding="utf-8")
    board = kpcb.read_board(text)
    return text, board, {fp.ref: fp for fp in board.footprints}, kpcb.board_frame(board)


@pytest.fixture(scope="module")
def keep(kicad, tmp_path_factory: pytest.TempPathFactory) -> dict:
    rc, res, pcb = build(kicad, KEEP, tmp_path_factory.mktemp("keep"))
    assert rc == 0 and res.get("ok"), res
    text, board, fps, frame = _read(pcb)
    return {"res": res, "text": text, "board": board, "fps": fps, "frame": frame}


def test_live_keep_result_keys(keep) -> None:
    res = keep["res"]
    refs = [c["ref"] for c in KEEP["components"]]
    assert res["anchors"] == refs and res["anchored"] == len(refs) == 8
    assert res["board_only"] == ["H1", "TP1"]
    assert res["attr_warnings"] == [] and res["missing_pads"] == []
    assert res["footprints"] == 8 and res["board_mm"] == [50.0, 40.0]
    assert res["containment"] == [], res["containment"]


def test_live_keep_file_and_frame(keep) -> None:
    assert keep["board"].format_version in SAVED_FORMATS
    assert sorted(keep["fps"]) == sorted(c["ref"] for c in KEEP["components"])
    frame = keep["frame"]
    assert frame.problem is None and frame.info["kind"] == "edge_cuts"
    assert (frame.block["w"], frame.block["h"]) == (50.0, 40.0)
    assert [frame.x0, frame.y0] == pytest.approx(keep["res"]["outline"]["origin_sheet"],
                                                 abs=1e-6)


def test_live_keep_at_round_trips_through_the_importer(keep) -> None:
    """The importer's board frame and origins give back every "at" (1e-3 mm,
    rotation mod 360, side) and the footprint the netlist named."""
    frame = keep["frame"]
    for c in KEEP["components"]:
        fp = keep["fps"][c["ref"]]
        x, y, rot = c["at"]
        got = (fp.x - frame.x0, fp.y - frame.y0)
        assert got == pytest.approx((x, y), abs=TOL_MM), (c["ref"], got, c["at"])
        assert sa.same_rot(fp.rot, rot) and 0.0 <= fp.rot < 360.0, (c["ref"], fp.rot, rot)
        assert fp.side == c["side"] and fp.lib_id == c["footprint"], (c["ref"], fp)


def test_live_keep_45_degree_back_part(keep) -> None:
    fp, frame = keep["fps"]["U1"], keep["frame"]
    assert fp.side == "B" and fp.rot == pytest.approx(45.0, abs=1e-6)
    assert fp.x - frame.x0 == pytest.approx(12.345678, abs=TOL_MM)
    assert fp.y - frame.y0 == pytest.approx(8.765432, abs=TOL_MM)


def _assert_attrs(design: dict, fps: dict) -> None:
    """Every flag's (attr ...) tokens are in the file; unflagged stock parts
    carry none of the managed tokens (a library's own attrs stay)."""
    for c in design["components"]:
        fp, want = fps[c["ref"]], sa.attr_tokens(c)
        assert want <= fp.attrs, (c["ref"], sorted(fp.attrs), sorted(want))
        if c["footprint"] in (R0603, C0603, SOT23, SOIC8, HDR3):
            assert fp.attrs & sa.MANAGED_TOKENS == want, (c["ref"], sorted(fp.attrs))
        assert fp.board_only is (c.get("board_only") is True), (c["ref"], sorted(fp.attrs))


def test_live_keep_attributes_in_the_file(keep) -> None:
    _assert_attrs(KEEP, keep["fps"])
    fps = keep["fps"]
    assert {"board_only", "exclude_from_bom", "exclude_from_pos_files"} <= fps["H1"].attrs
    assert {"board_only", "exclude_from_bom", "exclude_from_pos_files"} <= fps["TP1"].attrs
    assert {"dnp", "exclude_from_pos_files"} <= fps["R2"].attrs
    assert "exclude_from_bom" not in fps["R2"].attrs and "dnp" not in fps["C2"].attrs
    assert "exclude_from_bom" in fps["C2"].attrs


def test_live_keep_board_only_parts_are_netted(keep) -> None:
    fps = keep["fps"]
    assert fps["H1"].pads == ()                                  # NPTH only: no pad key
    assert [(p.number, p.net) for p in fps["TP1"].pads] == [("1", "GND")]


def test_live_keep_pad_nets_equal_the_netlist(keep) -> None:
    assert ne.pcb_pad_nets(keep["text"]) == ne.netlist_pad_nets(KEEP)
    assert ne.pcb_pad_nets(keep["text"], exclude_board_only=True) == \
        ne.netlist_pad_nets(KEEP, exclude_board_only=True)


def test_live_mixed_board_only_parts_placed_like_any_other(kicad, tmp_path) -> None:
    rc, res, pcb = build(kicad, MIXED, tmp_path / "b")
    assert rc == 0 and res["ok"], res
    assert res["anchors"] == [] and res["anchored"] == 0
    assert res["board_only"] == ["H1", "FID1", "TP1"] and res["attr_warnings"] == []
    text, _board, fps, frame = _read(pcb)
    assert sorted(fps) == sorted(c["ref"] for c in MIXED["components"])
    _assert_attrs(MIXED, fps)
    assert "dnp" in fps["FID1"].attrs
    for ref, fp in fps.items():                   # every origin on the 40 x 30 board
        assert 0.0 < fp.x - frame.x0 < 40.0 and 0.0 < fp.y - frame.y0 < 30.0, (ref, fp)
    assert [(p.number, p.net) for p in fps["TP1"].pads] == [("1", "GND")]
    assert ne.pcb_pad_nets(text) == ne.netlist_pad_nets(MIXED)


def test_live_non_boolean_flag_is_a_clean_refusal(kicad, tmp_path) -> None:
    design = {**KEEP, "components": [
        {**c, "dnp": "yes"} if c["ref"] == "R2" else c for c in KEEP["components"]]}
    rc, res, pcb = build(kicad, design, tmp_path / "b")
    assert rc == 1 and res["ok"] is False, res
    assert res["error"].startswith("R2: 'dnp' must be a boolean"), res
    assert "trace" not in res and not pcb.exists()

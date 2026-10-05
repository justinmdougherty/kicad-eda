"""server/tools/rebuild_variant.py and design_paths' footprint helpers (F46-10).

The Feather-shaped fixture is synthetic (no CC-BY-SA content): 44 parts shaped
like an Eagle import of an Adafruit Feather M0 RFM95 (38 schematic parts, four
board-only artwork parts, two board-only NPTH holes, ``at`` everywhere, a
``source`` block and a project library). ``feather_design()`` /
``write_feather()`` are imported by tests/test_design_blocks.py as the compose
base; they are plain functions, not tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import design_paths as dp
import lint_netlist
import net_equivalence as ne
import netlist_schema
import rebuild_variant as rv

LIB = "Feather_imported"
FEATHER_W, FEATHER_H = 50.8, 22.86
REFLOW_BOARD = {"w": 40.0, "h": 30.0, "corner_radius": 3.0}

# (ref, symbol, footprint, value)
SCHEMATIC_PARTS: tuple[tuple[str, str, str, str], ...] = (
    ("U1", "ATSAMD21G18A", "TQFP-48_7x7mm_P0.5mm", "ATSAMD21G18A"),
    ("U2", "RFM95W", "RFM95W", "RFM95W-915"),
    ("U3", "AP2112K-3.3", "SOT-23-5", "AP2112K-3.3"),
    ("U4", "MCP73831", "SOT-23-5", "MCP73831"),
    ("J1", "USB_B_Micro", "USB_Micro-B", "USB"),
    ("J2", "Conn_01x02", "JST_PH_2", "BATT"),
    ("J3", "Conn_01x16", "PinHeader_1x16", "HDR16"),
    ("J4", "Conn_01x12", "PinHeader_1x12", "HDR12"),
    ("J5", "Conn_Coaxial", "U.FL", "U.FL"),
    ("Y1", "Crystal", "Crystal_2012", "32.768kHz"),
    ("D1", "LED", "LED_0603", "CHG"),
    ("D2", "LED", "LED_0603", "RED"),
    ("D3", "D_Schottky", "SOD-123", "MBR120"),
    ("SW1", "SW_Push", "SW_EVQP7A", "RESET"),
    ("FB1", "Ferrite", "L_0805", "600R"),
    *((f"C{i}", "C", "C_0603", "10uF" if i < 4 else "100nF") for i in range(1, 15)),
    *((f"R{i}", "R", "R_0603", "10k") for i in range(1, 10)),
)
ARTWORK = (("LOGO1", "Logo_Adafruit"), ("LOGO2", "Logo_OSHW"),
           ("LOGO3", "Silk_Feather_Text"), ("LOGO4", "Silk_Pin_Labels"))
HOLES = (("H1", "Hole_2.5mm_NPTH"), ("H2", "Hole_2.5mm_NPTH"))
HOLE_FOOTPRINT = f"{LIB}:Hole_2.5mm_NPTH"
SOURCE_PATH, REFERENCE = "orig/feather_m0_rfm95.brd", "reference_padnets.json"
SOURCE_STUB = "synthetic stand-in for an Eagle board (F46-10 fixture)\n"

_NETS = {
    "GND": "U1.10 U1.28 U1.35 U1.42 U2.1 U2.8 U2.10 U3.2 U4.2 J1.5 J1.6 J2.2 J3.4 J5.2 "
           + " ".join(f"C{i}.2" for i in range(1, 15)) + " SW1.2 R5.2 R9.2 D2.2",
    "+3V3": "U1.6 U1.17 U1.36 U1.44 U2.13 U3.5 J3.2 C4.1 C5.1 C6.1 C7.1 C8.1 C14.1 R1.1 R4.1 "
            "R6.1",
    "VBUS_RAW": "J1.1 FB1.1", "VBUS": "FB1.2 U4.4 D3.1 D1.1 C1.1 J4.12",
    "VBAT": "J2.1 U4.3 C2.1 J4.11 R8.1", "VSYS": "D3.2 U3.1 C3.1 R7.1 J4.10",
    "EN": "R7.2 U3.3", "VDIV": "R8.2 R9.1 U1.8", "USB_DM": "J1.2 U1.33", "USB_DP": "J1.3 U1.34",
    "XIN32": "Y1.1 U1.1 C9.1", "XOUT32": "Y1.2 U1.2 C10.1", "RESET": "U1.40 SW1.1 R1.2 J3.1 C11.1",
    "AREF": "U1.5 J3.3 C12.1", "VDDCORE": "U1.43 C13.1", "CHG_STAT": "U4.1 R2.1",
    "CHG_LED": "R2.2 D1.2", "PROG": "U4.5 R5.1", "LED13": "U1.32 R3.1", "LED13_K": "R3.2 D2.1",
    "SWCLK": "R6.2 U1.45", "RF_MISO": "U2.2 U1.21", "RF_MOSI": "U2.3 U1.19",
    "RF_SCK": "U2.4 U1.20", "RF_CS": "U2.5 U1.15", "RF_RST": "U2.6 U1.14 R4.2",
    "RF_IRQ": "U2.14 U1.16", "RF_DIO1": "U2.15 J4.1", "ANT": "U2.9 J5.1",
    "A0": "U1.3 J3.5", "A1": "U1.7 J3.6", "A2": "U1.9 J3.7", "A3": "U1.4 J3.8",
    "A4": "U1.11 J3.9", "A5": "U1.12 J3.10", "SCK": "U1.48 J3.11", "MOSI": "U1.47 J3.12",
    "MISO": "U1.46 J3.13", "RX": "U1.31 J3.14", "TX": "U1.30 J3.15", "SDA": "U1.13 J4.2",
    "SCL": "U1.27 J4.3", "D5": "U1.25 J4.4", "D6": "U1.29 J4.5", "D9": "U1.37 J4.6",
    "D10": "U1.38 J4.7", "D11": "U1.39 J4.8", "D12": "U1.41 J4.9",
}
_NO_CONNECT = "J1.4 J3.16 U1.22 U1.23"

NPTH_HOLE_MOD = """(footprint "Hole_2.5mm_NPTH" (version 20241229) (generator "pcbnew")
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -3) (layer "F.SilkS"))
  (attr exclude_from_pos_files exclude_from_bom)
  (fp_circle (center 0 0) (end 1.25 0) (layer "Cmts.User") (width 0.15))
  (pad "" np_thru_hole circle (at 0 0) (size 2.5 2.5) (drill 2.5) (layers "*.Cu" "*.Mask"))
)
"""
LOGO_MOD = """(footprint "{name}" (version 20241229) (generator "pcbnew")
  (layer "F.Cu")
  (property "Description" "artwork (pad 1 smd) text inside a string")
  (attr board_only exclude_from_pos_files exclude_from_bom)
  (fp_poly (pts (xy 0 0) (xy 2 0) (xy 2 1)) (layer "F.SilkS") (width 0.1))
)
"""
PLATED_HOLE_MOD = """(footprint "MountingHole_3.2mm_M3_Pad" (version 20241229)
  (pad "1" thru_hole circle (at 0 0) (size 6 6) (drill 3.2) (layers "*.Cu" "*.Mask"))
)
"""
SMD_MOD = """(module C_0603 (layer F.Cu)
  (pad 1 smd rect (at -0.8 0) (size 0.9 0.95) (layers F.Cu F.Paste F.Mask))
  (pad 2 smd rect (at 0.8 0) (size 0.9 0.95) (layers F.Cu F.Paste F.Mask))
)
"""
FIDUCIAL_MOD = """(footprint "Fiducial_1mm"
  (pad "" smd circle (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))
)
"""


# --- the Feather-shaped fixture ---------------------------------------------------------
def _pads(spec: str) -> list[list[str]]:
    return [p.split(".") for p in spec.split()]


def _at(i: int) -> list[float]:
    return [round(2.0 + (i % 12) * 4.0, 3), round(2.0 + (i // 12) * 5.0, 3), 0.0]


def feather_design() -> dict[str, Any]:
    """A fresh Feather-shaped imported design (44 parts, `at` everywhere)."""
    comps: list[dict[str, Any]] = [
        {"ref": ref, "lib_id": f"{LIB}:{sym}", "value": value, "footprint": f"{LIB}:{fp}",
         "side": "F"} for ref, sym, fp, value in SCHEMATIC_PARTS]
    comps += [{"ref": ref, "board_only": True, "value": ref, "footprint": f"{LIB}:{fp}",
               "side": "F"} for ref, fp in (*ARTWORK, *HOLES)]
    comps = [{**c, "at": _at(i)} for i, c in enumerate(comps)]
    return {
        "name": "feather_m0_rfm95", "title": "Feather M0 RFM95 (synthetic)",
        "source": {"kind": "eagle", "path": SOURCE_PATH,
                   "sha256": hashlib.sha256(SOURCE_STUB.encode()).hexdigest(),
                   "tool": "kicad-cli 10.99 import + kicad_import",
                   "reference": REFERENCE, "erc_baseline": "source_erc.json",
                   "import_report": "import_report.json"},
        "rebuild": "keep", "copper_layers": 2,
        "board": {"w": FEATHER_W, "h": FEATHER_H,
                  "outline": {"loop": [[0, 0], [FEATHER_W, 0], [FEATHER_W, FEATHER_H],
                                       [0, FEATHER_H]]},
                  "datum": [0.0, FEATHER_H]},
        "libraries": {"symbols": {LIB: f"libs/{LIB}.kicad_sym"},
                      "footprints": {LIB: f"libs/{LIB}.pretty"}},
        "components": comps,
        "nets": {name: _pads(spec) for name, spec in _NETS.items()},
        "no_connect": _pads(_NO_CONNECT),
        "net_specs": {"ANT": {"impedance": 50}, "VBUS": {"voltage": 5.0, "current": 0.5}},
        "assumptions": [{"id": "A1", "topic": "import", "value": "J1.4 unconnected",
                         "rationale": "the source leaves the USB ID pin open",
                         "confidence": "high"}],
    }


def write_feather(directory: Path, design: dict[str, Any] | None = None) -> Path:
    """Write the fixture into `directory`: netlist.json, the footprints, the
    source stub (its sha256 matches source.sha256) and the frozen pad-net
    reference of feather_design(); returns the netlist path."""
    (directory / SOURCE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (directory / SOURCE_PATH).write_text(SOURCE_STUB, encoding="utf-8", newline="\n")
    clean = feather_design()
    ne.write_reference(directory / REFERENCE, ne.netlist_pad_nets(clean),
                       board_only=[c["ref"] for c in clean["components"] if c.get("board_only")],
                       source=clean["source"])
    pretty = directory / "libs" / f"{LIB}.pretty"
    pretty.mkdir(parents=True, exist_ok=True)
    for _ref, name in ARTWORK:
        (pretty / f"{name}.kicad_mod").write_text(LOGO_MOD.format(name=name), encoding="utf-8")
    (pretty / "Hole_2.5mm_NPTH.kicad_mod").write_text(NPTH_HOLE_MOD, encoding="utf-8")
    (pretty / "MountingHole_3.2mm_M3_Pad.kicad_mod").write_text(PLATED_HOLE_MOD, encoding="utf-8")
    (pretty / "C_0603.kicad_mod").write_text(SMD_MOD, encoding="utf-8")
    (pretty / "Fiducial_1mm.kicad_mod").write_text(FIDUCIAL_MOD, encoding="utf-8")
    path = directory / "netlist.json"
    path.write_text(json.dumps(design or feather_design(), indent=1), encoding="utf-8")
    return path


@pytest.fixture
def feather(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    src = tmp_path / "import"
    write_feather(src)
    return feather_design(), src


def _refs(design: dict[str, Any]) -> list[str]:
    return [c["ref"] for c in design["components"]]


def _comp(design: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(c for c in design["components"] if c["ref"] == ref)


# --- design_paths: footprints -----------------------------------------------------------
def test_fixture_shape() -> None:
    d = feather_design()
    assert len(d["components"]) == 44
    assert sum(c.get("board_only") is True for c in d["components"]) == 6
    assert all("at" in c for c in d["components"])
    assert netlist_schema.validate(d) == []
    assert lint_netlist.lint(d)["errors"] == []


def test_footprint_file_resolves_block_and_stock(feather: tuple, tmp_path: Path) -> None:
    design, src = feather
    hit = dp.footprint_file(design, src, HOLE_FOOTPRINT)
    assert hit == src / "libs" / f"{LIB}.pretty" / "Hole_2.5mm_NPTH.kicad_mod"
    assert dp.footprint_file(design, src, f"{LIB}:Nope") is None
    assert dp.footprint_file(design, src, "no_colon") is None
    assert dp.footprint_file(design, src, "Stock:Thing") is None
    stock = tmp_path / "stock"
    (stock / "Stock.pretty").mkdir(parents=True)
    (stock / "Stock.pretty" / "Thing.kicad_mod").write_text(SMD_MOD, encoding="utf-8")
    assert dp.footprint_file(design, src, "Stock:Thing", stock) == \
        stock / "Stock.pretty" / "Thing.kicad_mod"
    assert dp.footprint_file({"components": []}, src, "Stock:Thing", stock) is not None


def test_footprint_locator_bad_block_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="libraries"):
        dp.footprint_locator({"libraries": {"bogus": {}}}, tmp_path)


def test_pad_summary_kinds(tmp_path: Path) -> None:
    files = {"npth": NPTH_HOLE_MOD, "logo": LOGO_MOD.format(name="L"), "plated": PLATED_HOLE_MOD,
             "smd": SMD_MOD, "fid": FIDUCIAL_MOD}
    got = {}
    for key, text in files.items():
        (tmp_path / f"{key}.kicad_mod").write_text(text, encoding="utf-8")
        got[key] = dp.footprint_pad_summary(tmp_path / f"{key}.kicad_mod")
    assert got["npth"] == {"pads": 1, "numbered": 0, "npth": 1, "drilled": 1, "electrical": 0,
                           "copper": False}
    assert got["logo"] == {"pads": 0, "numbered": 0, "npth": 0, "drilled": 0, "electrical": 0,
                           "copper": False}
    assert got["plated"]["copper"] is True and got["plated"]["electrical"] == 1
    assert got["smd"] == {"pads": 2, "numbered": 2, "npth": 0, "drilled": 0, "electrical": 2,
                          "copper": True}
    assert got["fid"]["copper"] is True and got["fid"]["numbered"] == 0


def test_pad_summary_edge_cases(tmp_path: Path) -> None:
    odd = tmp_path / "odd.kicad_mod"
    odd.write_text('(footprint "x"\n  (pad "A1" connect rect (at 0 0) (size 1 1))\n'
                   '  (pad "2" smd rect (layers "F.Paste"))\n', encoding="utf-8")
    s = dp.footprint_pad_summary(odd)
    assert s["pads"] == 2 and s["copper"] is False and s["electrical"] == 2
    bad = tmp_path / "bad.kicad_mod"
    bad.write_text("(kicad_pcb (version 1))", encoding="utf-8")
    with pytest.raises(ValueError, match="not a footprint"):
        dp.footprint_pad_summary(bad)
    with pytest.raises(OSError):
        dp.footprint_pad_summary(tmp_path / "missing.kicad_mod")
    cut = tmp_path / "cut.kicad_mod"
    cut.write_text('(footprint "x" (pad "1" thru_hole circle (layers "*.Cu")', encoding="utf-8")
    assert dp.footprint_pad_summary(cut)["copper"] is True
    assert dp.split_lib_id(None) is None and dp.split_lib_id("A:") is None
    assert dp.split_lib_id("A:B:C") == ("A", "B:C")


# --- classification -----------------------------------------------------------------------
@pytest.mark.parametrize(("comp", "summary", "want"), [
    ({"ref": "H1", "footprint": "X:Hole", "board_only": True},
     {"pads": 1, "drilled": 1, "electrical": 0, "copper": False}, "hole"),
    ({"ref": "H9", "footprint": "MountingHole:MountingHole_3.2mm_M3_Pad"}, None, "hole"),
    ({"ref": "G1", "footprint": "X:Logo", "board_only": True},
     {"pads": 0, "drilled": 0, "electrical": 0, "copper": False}, "artwork"),
    ({"ref": "FID1", "footprint": "X:Fid", "board_only": True},
     {"pads": 1, "drilled": 0, "electrical": 0, "copper": True}, None),
    ({"ref": "G2", "footprint": "X:Logo"},
     {"pads": 0, "drilled": 0, "electrical": 0, "copper": False}, None),
    ({"ref": "G3", "footprint": "X:Unknown", "board_only": True}, None, None),
])
def test_classify_part(comp: dict, summary: dict | None, want: str | None) -> None:
    assert rv.classify_part(comp, summary) == want


# --- keep ---------------------------------------------------------------------------------
def test_keep_returns_design_unchanged(feather: tuple) -> None:
    design, src = feather
    before = copy.deepcopy(design)
    res = rv.make_variant(design, src, mode="keep")
    assert res.ok and res.errors == () and res.design == before
    assert res.design is not design and design == before


def test_keep_lists_parts_without_at(feather: tuple) -> None:
    design, src = feather
    comps = [{k: v for k, v in c.items() if k != "at"} if c["ref"] in ("R3", "U2", "H1") else c
             for c in design["components"]]
    res = rv.make_variant({**design, "components": comps}, src, mode="keep")
    assert not res.ok
    assert len(res.errors) == 1 and "U2, R3" in res.errors[0] and "H1" not in res.errors[0]
    assert res.design["components"] == comps


# --- reflow: the acceptance criterion -----------------------------------------------------
def test_reflow_feather_into_rounded_rect(feather: tuple) -> None:
    design, src = feather
    before = copy.deepcopy(design)
    res = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD)
    assert res.ok, res.errors
    assert design == before                          # the input is never mutated
    out = res.design
    assert res.dropped_refs == ("H1", "H2", "LOGO1", "LOGO2", "LOGO3", "LOGO4")
    assert len(out["components"]) == 38
    assert out["mounting_holes"] == {"footprint": HOLE_FOOTPRINT, "inset": rv.HOLE_INSET_MM}
    assert out["board"] == REFLOW_BOARD and out["rebuild"] == "reflow"
    assert not any(k in c for c in out["components"] for k in ("at", "x", "y", "rot"))
    assert out["variant"] == {"mode": "reflow", "from": "netlist.json",
                              "dropped_refs": list(res.dropped_refs), "holes": "source"}
    assert out["source"] == design["source"] and out["libraries"] == design["libraries"]
    assert netlist_schema.validate(out) == []
    assert lint_netlist.lint(out)["errors"] == []
    source_minus = ne.compare(ne.netlist_pad_nets(out), ne.netlist_pad_nets(design),
                              ignore_refs=res.dropped_refs)
    assert source_minus.equal, source_minus.summary()
    ids = [a["id"] for a in res.assumptions]
    assert ids == [f"variant-{i}" for i in range(1, len(ids) + 1)]
    assert {a["topic"] for a in res.assumptions} == {"mounting holes", "artwork"}
    assert all(a["confidence"] == "med" for a in res.assumptions)
    assert out["assumptions"] == design["assumptions"] + list(res.assumptions)


@pytest.mark.parametrize(("holes", "want"), [("corners", True), ("none", None)])
def test_hole_policies(feather: tuple, holes: str, want: object) -> None:
    design, src = feather
    res = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD, holes=holes)
    assert res.ok and "H1" in res.dropped_refs and res.design["variant"]["holes"] == holes
    assert res.design.get("mounting_holes") == want


def test_keep_graphics_and_keep_refs(feather: tuple) -> None:
    design, src = feather
    res = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD, drop_graphics=False,
                          keep_refs=("J1", "H1"))
    assert res.ok, res.errors
    assert res.dropped_refs == ("H2",)
    assert _comp(res.design, "J1")["at"] == _comp(design, "J1")["at"]
    assert _comp(res.design, "H1")["at"] == _comp(design, "H1")["at"]
    assert "at" not in _comp(res.design, "LOGO1")


def test_keep_refs_errors(feather: tuple) -> None:
    design, src = feather
    comps = [{**{k: v for k, v in c.items() if k != "at"}, "x": 45.0, "y": 20.0}
             if c["ref"] == "J4" else
             {k: v for k, v in c.items() if k != "at"} if c["ref"] == "J2" else c
             for c in design["components"]]
    res = rv.make_variant({**design, "components": comps}, src, mode="reflow",
                          board=REFLOW_BOARD, keep_refs=["J4", "J2", "NOPE"])
    text = "\n".join(res.errors)
    assert "NOPE" in text and "J2" in text and "J4" in text and "outside" in text


def test_invalid_board_and_args(feather: tuple) -> None:
    design, src = feather
    bad = rv.make_variant(design, src, mode="reflow", board={"w": 10, "h": 10,
                                                             "corner_radius": 9})
    assert not bad.ok and "corner_radius" in bad.errors[0]
    with pytest.raises(ValueError, match="mode"):
        rv.make_variant(design, src, mode="squash")
    with pytest.raises(ValueError, match="holes"):
        rv.make_variant(design, src, mode="reflow", holes="maybe")
    with pytest.raises(TypeError, match="keep_refs"):
        rv.make_variant(design, src, mode="reflow", keep_refs="J1")
    with pytest.raises(TypeError, match="keep_refs"):
        rv.make_variant(design, src, mode="reflow", keep_refs=["J1", 2])
    with pytest.raises(ValueError, match="components"):
        rv.make_variant({"components": "x"}, src, mode="reflow")
    for key, value in (("nets", {"GND": [["U1"]]}), ("nets", ["GND"]),
                       ("no_connect", ["J1.4"]), ("assumptions", {"id": "A1"}),
                       ("assumptions", [{"topic": "t", "value": 1}]),
                       ("variant", {"mode": "reflow", "dropped_refs": "H1"})):
        for mode in rv.MODES:
            with pytest.raises(ValueError, match=f"'{key}'"):
                rv.make_variant({**design, key: value}, src, mode=mode)


def test_reflow_without_board_keeps_source_board(feather: tuple) -> None:
    design, src = feather
    res = rv.make_variant(design, src, mode="reflow", keep_refs=["U1"])
    assert res.ok and res.design["board"] == design["board"]
    assert any("keeps the source board" in n for n in res.notes)
    bare = {k: v for k, v in design.items() if k != "board"}
    none = rv.make_variant(bare, src, mode="reflow", keep_refs=["U1"])
    assert not none.ok and "outline" in none.errors[0]
    assert "board" not in rv.make_variant(bare, src, mode="reflow").design


def test_dxf_board_containment(feather: tuple) -> None:
    dxf_io = pytest.importorskip("dxf_io")
    design, src = feather
    dxf_io.write_outline_dxf(src / "mech" / "outline.dxf", {"w": 40.0, "h": 30.0})
    board = {"outline": {"dxf": "mech/outline.dxf"}}
    res = rv.make_variant(design, src, mode="reflow", board=board, keep_refs=["U1"])
    assert res.ok, res.errors
    assert res.design["board"] == board
    passed = rv.make_variant(design, src, mode="reflow", board=board)
    assert passed.ok and any("DXF" in n for n in passed.notes)
    missing = rv.make_variant(design, src, mode="reflow",
                              board={"outline": {"dxf": "mech/none.dxf"}}, keep_refs=["U1"])
    assert not missing.ok


def test_dropped_connections_and_lone_pads(feather: tuple) -> None:
    design, src = feather
    comps = design["components"] + [
        {"ref": "H3", "board_only": True, "footprint": f"{LIB}:MountingHole_3.2mm_M3_Pad",
         "at": [5.0, 5.0, 0.0]},
        {"ref": "H4", "board_only": True, "footprint": f"{LIB}:MountingHole_3.2mm_M3_Pad",
         "at": [6.0, 5.0, 0.0]},
        {"ref": "H5", "board_only": True, "footprint": f"{LIB}:MountingHole_3.2mm_M3_Pad",
         "at": [7.0, 5.0, 0.0]}]
    nets = {**design["nets"], "GND": design["nets"]["GND"] + [["H3", "1"]],
            "SHIELD": [["H4", "1"], ["J5", "3"]], "HOLE5": [["H5", "1"]]}
    d = {**design, "components": comps, "nets": nets, "power_flags": ["SHIELD", "GND"],
         "net_specs": {**design["net_specs"], "SHIELD": {"current": 1.0}},
         "no_connect": design["no_connect"] + [["H5", "2"]],
         "preroutes": [{"net": "GND", "width": 1.0, "path": [["xy", 1, 1], ["xy", 2, 2]]}],
         "variant": {"mode": "reflow", "dropped_refs": ["TP9"]}}
    res = rv.make_variant(d, src, mode="reflow", board=REFLOW_BOARD)
    out = res.design
    assert res.ok, res.errors
    assert {"H3", "H4", "H5", "TP9"} <= set(out["variant"]["dropped_refs"])
    assert "TP9" not in res.dropped_refs
    assert ["H3", "1"] not in out["nets"]["GND"]
    assert "SHIELD" not in out["nets"] and "HOLE5" not in out["nets"]
    assert ["J5", "3"] in out["no_connect"] and ["H5", "2"] not in out["no_connect"]
    assert out["power_flags"] == ["GND"] and "SHIELD" not in out["net_specs"]
    assert "preroutes" not in out
    topics = [a["topic"] for a in res.assumptions]
    assert topics.count("dropped connection") == 2 and "no_connect" in topics
    assert lint_netlist.lint(out)["errors"] == []
    eq = ne.compare(ne.netlist_pad_nets(out), ne.netlist_pad_nets(d),
                    ignore_refs=res.dropped_refs)
    assert eq.equal, eq.summary()
    assert out["mounting_holes"]["footprint"] == f"{LIB}:MountingHole_3.2mm_M3_Pad"
    assert any("Hole_2.5mm_NPTH" in n for n in res.notes)


def test_unknown_footprint_and_custom_summary(feather: tuple) -> None:
    design, src = feather
    comps = design["components"] + [{"ref": "G9", "board_only": True,
                                      "footprint": f"{LIB}:Nowhere", "at": [1.0, 1.0, 0.0]}]
    res = rv.make_variant({**design, "components": comps}, src, mode="reflow",
                          board=REFLOW_BOARD)
    assert "G9" not in res.dropped_refs and any("G9" in n for n in res.notes)
    calls: list[str] = []

    def summary(fp_id: str) -> dict | None:
        calls.append(fp_id)
        return None

    none = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD, pad_summary=summary)
    assert none.dropped_refs == () and calls
    assert none.design["variant"]["dropped_refs"] == []
    # an unreadable footprint file and an unresolvable libraries block keep the parts
    (src / "libs" / f"{LIB}.pretty" / "Logo_OSHW.kicad_mod").write_text("(kicad_pcb)",
                                                                        encoding="utf-8")
    broken = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD)
    assert "LOGO2" not in broken.dropped_refs and "LOGO1" in broken.dropped_refs
    logo2 = [n for n in broken.notes if n.startswith("LOGO2:")]
    assert len(logo2) == 1 and "not a footprint" in logo2[0] and "not found" not in logo2[0]

    def locked(fp_id: str) -> dict | None:
        if fp_id.endswith("Logo_OSHW"):
            raise OSError(f"locked: {fp_id}")
        return None

    raising = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD, pad_summary=locked)
    assert raising.ok and "LOGO2" not in raising.dropped_refs
    assert any(n.startswith("LOGO2:") and "locked" in n for n in raising.notes)
    bad_libs = rv.make_variant({**design, "libraries": {"bogus": {}}}, src, mode="reflow",
                               board=REFLOW_BOARD)
    assert bad_libs.dropped_refs == ()
    assert any("cannot be resolved" in n for n in bad_libs.notes)


def test_existing_assumption_ids_are_skipped(feather: tuple) -> None:
    design, src = feather
    d = {**design, "assumptions": [{"id": "variant-1", "topic": "t", "value": 1,
                                    "rationale": "r", "confidence": "low"}],
         "mounting_holes": True}
    res = rv.make_variant(d, src, mode="reflow", board=REFLOW_BOARD)
    assert res.assumptions[0]["id"] == "variant-2"
    assert any("mounting_holes" in n for n in res.notes)


def test_result_as_dict(feather: tuple) -> None:
    design, src = feather
    res = rv.make_variant(design, src, mode="reflow", board=REFLOW_BOARD)
    d = res.as_dict()
    assert d["ok"] is True and d["dropped_refs"] == list(res.dropped_refs)
    json.dumps(d)


# --- CLI ----------------------------------------------------------------------------------
def test_cli_reflow_writes_relocated_netlist(feather: tuple, tmp_path: Path,
                                             capsys: pytest.CaptureFixture) -> None:
    _design, src = feather
    out_dir = tmp_path / "reflow"
    board = tmp_path / "board.json"
    board.write_text(json.dumps(REFLOW_BOARD), encoding="utf-8")
    rc = rv.main([str(src / "netlist.json"), str(out_dir), "--mode", "reflow", "--board",
                  str(board), "--keep-refs", "J1,J3", "--holes", "corners"])
    assert rc == 0, capsys.readouterr()
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["netlist"] == str(out_dir / "netlist.json")
    written = json.loads((out_dir / "netlist.json").read_text(encoding="utf-8"))
    assert written["libraries"]["footprints"][LIB] == f"../import/libs/{LIB}.pretty"
    assert written["source"]["reference"] == "../import/reference_padnets.json"
    assert written["variant"]["from"] == "../import/netlist.json"
    assert written["mounting_holes"] is True and "at" in _comp(written, "J3")
    assert dp.footprint_file(written, out_dir, HOLE_FOOTPRINT) is not None
    # file-aware lint from the new directory: the relocated source files are found
    rep = lint_netlist.lint(written, ctx=lint_netlist.cli_context(out_dir / "netlist.json"))
    assert rep["errors"] == [] and not [w for w in rep["warnings"] if w.startswith("IM-")]


def test_cli_inline_board_dxf_and_keep(feather: tuple, tmp_path: Path,
                                       capsys: pytest.CaptureFixture,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _design, src = feather
    monkeypatch.chdir(tmp_path)             # an inline board's relative DXF is cwd-relative
    board = json.dumps({"outline": {"dxf": "mech/outline.dxf"}})
    rc = rv.main([str(src / "netlist.json"), str(tmp_path / "dxf"), "--board", board])
    assert rc == 0, capsys.readouterr()
    written = json.loads((tmp_path / "dxf" / "netlist.json").read_text(encoding="utf-8"))
    assert written["board"]["outline"]["dxf"] == "../mech/outline.dxf"
    assert rv.main([str(src / "netlist.json"), str(tmp_path / "keep"), "--mode", "keep",
                    "--keep-graphics"]) == 0
    assert (tmp_path / "keep" / "netlist.json").is_file()


def test_cli_errors(feather: tuple, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _design, src = feather
    netlist = str(src / "netlist.json")
    assert rv.main([netlist, str(src), "--board", json.dumps(REFLOW_BOARD)]) == 2
    assert "overwrite" in capsys.readouterr().err
    assert rv.main([str(tmp_path / "none.json"), str(tmp_path / "o")]) == 2
    assert rv.main([netlist, str(tmp_path / "o"), "--board", "{not json"]) == 2
    assert rv.main([netlist, str(tmp_path / "o"), "--board", "[1, 2]"]) == 2
    assert rv.main([netlist, str(tmp_path / "o2"), "--keep-refs", "NOPE"]) == 1
    assert not (tmp_path / "o2" / "netlist.json").exists()
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    assert rv.main([str(tmp_path / "list.json"), str(tmp_path / "o3")]) == 2
    odd = tmp_path / "odd" / "netlist.json"
    write_feather(odd.parent, {**feather_design(), "nets": {"GND": "U1.1"}})
    capsys.readouterr()
    assert rv.main([str(odd), str(tmp_path / "o4")]) == 2
    assert "'nets'" in capsys.readouterr().err
    assert not (tmp_path / "o4").exists()

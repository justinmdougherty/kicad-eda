"""review_mechanical: the 'Mechanical' section of the hands-off review (X-03, F3 / D2).

Pure unit tier, no KiCad. The full case is report['mechanical'] exactly as
rp_feature_outline.g5_mechanical writes it (tests/test_rp_feature_outline.py asserts
that shape); the legacy case is its {'skipped': ...} record; the absent case is a run
that stopped before G5 or predates the outline feature.
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

import review_mechanical as rm

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
MECH = "/work/t10/t10_mech"
STEP_BBOX = {"x": [-10.0, 50.0], "y": [-10.0, 30.0], "z": [0.0, 1.51], "unit": "mm",
             "scale": 1.0, "basis": "edges", "vertices": 24, "arcs": 4, "points": 60}
FULL = {"ok": True, "dir": MECH,
        "files": [f"{MECH}/t10.step", f"{MECH}/t10_outline.dxf", f"{MECH}/t10_datum_cpl.csv"],
        "step": f"{MECH}/t10.step", "dxf": f"{MECH}/t10_outline.dxf",
        "datum_cpl": f"{MECH}/t10_datum_cpl.csv", "hausdorff_mm": 0.0,
        "step_bbox": STEP_BBOX, "warnings": []}
FAILED = {"ok": False, "dir": MECH, "files": [f"{MECH}/t10_outline.dxf"], "step": None,
          "dxf": f"{MECH}/t10_outline.dxf", "datum_cpl": None, "hausdorff_mm": 0.2,
          "step_bbox": None,
          "warnings": ["STEP export failed: no 3D models",
                       "outline DXF export failed: differs by 0.2 mm",
                       "outline DXF: flag --ou dropped",
                       "datum CPL export failed: RuntimeError: kicad-cli crashed"]}
LEGACY = {"skipped": "rectangular board without datum"}
T10 = {"name": "t10", "components": [], "nets": {},
       "board": {"w": 60, "h": 40, "datum": [10, 30], "cutouts": [{"circle": [30, 20, 3]}]}}
GATES = [{"gate": g, "name": g, "feeds": "x", "passed": True, "blockers": [], "warnings": [],
          "tasks": []} for g in ("G1", "G2", "G3", "G4", "G5")]


def _report(**extra) -> dict:
    return {"netlist": "/work/t10/t10.json", "gates": copy.deepcopy(GATES), **extra}


# --- full ------------------------------------------------------------------------------

def test_full_record_renders_step_bbox_dxf_readback_and_datum_cpl():
    lines = rm.render(_report(mechanical=FULL, fab={"ok": True}), T10)
    text = "\n".join(lines)
    assert lines[0] == f"- Exports: 3 of 3 written to `{MECH}`; all ok"
    assert (f"- STEP: `{MECH}/t10.step` (board only); bbox x [-10, 50], y [-10, 30], "
            f"z [0, 1.51] mm, from the B-rep edges ({rm.DATUM_FRAME})") in text
    assert (f"- Outline DXF: `{MECH}/t10_outline.dxf`; read back against the board's "
            f"Edge.Cuts: Hausdorff 0 mm") in text
    assert (f"- Datum CPL: `{MECH}/t10_datum_cpl.csv` (positions relative to board.datum; "
            f"the fab CPL stays absolute)") in text
    assert "- Datum: (10, 30) in the board frame (mm, Y down, origin = the outline bbox " \
           "top-left)" in text
    assert "- Warnings: none" in text
    assert all("\n" not in ln for ln in lines)


def test_failed_exports_say_not_written_and_list_the_warnings():
    lines = rm.render(_report(mechanical=FAILED, fab={"ok": False}), T10)
    text = "\n".join(lines)
    assert lines[0] == f"- Exports: 1 of 3 written to `{MECH}`; with problems (G5 warnings)"
    assert "- STEP: not written (see the warnings); bbox not computed" in text
    assert (f"- Outline DXF: `{MECH}/t10_outline.dxf`; read back against the board's "
            f"Edge.Cuts: Hausdorff 0.2 mm") in text
    assert "- Datum CPL: not written (see the warnings)" in text
    assert "- Warnings (4):" in text
    assert "  - STEP export failed: no 3D models" in text
    assert "  - datum CPL export failed: RuntimeError: kicad-cli crashed" in text


def test_datum_line_without_a_declared_datum_and_without_a_design():
    design = {"board": {"outline": {"loop": [[0, 0], [60, 0], [60, 40]]}}}
    text = "\n".join(rm.render(_report(mechanical=FULL), design))
    assert ("- Datum: not declared (board.datum); the exports use the board's aux / drill "
            "origin") in text
    assert not any(ln.startswith("- Datum:") for ln in rm.render(_report(mechanical=FULL)))


def test_many_warnings_are_capped():
    mech = {**FULL, "ok": False, "warnings": [f"warning {i}" for i in range(rm.LISTED + 3)]}
    lines = rm.render(_report(mechanical=mech), T10)
    assert f"- Warnings ({rm.LISTED + 3}):" in lines
    assert "  - (+3 more)" in lines and f"  - warning {rm.LISTED}" not in lines


# --- legacy ----------------------------------------------------------------------------

def test_legacy_board_renders_one_line():
    lines = rm.render(_report(mechanical=LEGACY, fab={"ok": True}), {"board": {"w": 1, "h": 1}})
    assert lines == ["- Skipped: rectangular board without datum (a legacy board has no Fusion "
                     "round-trip exports; the fab package is unaffected)"]


# --- absent ----------------------------------------------------------------------------

def test_absent_record_before_g5_is_not_produced():
    assert rm.render(_report(), T10) == [rm.NOT_PRODUCED]
    assert rm.render({"netlist": "", "gates": []}) == [rm.NOT_PRODUCED]
    assert "the run stopped before G5" in rm.NOT_PRODUCED


def test_absent_record_after_a_fab_export_is_not_recorded():
    assert rm.render(_report(fab={"ok": True, "zip": "x.zip"}), T10) == [rm.NOT_RECORDED]


# --- odd shapes -------------------------------------------------------------------------

@pytest.mark.parametrize("mech", ["junk", 5, [], None])
def test_a_malformed_record_is_one_line_not_a_crash(mech):
    lines = rm.render(_report(mechanical=mech), T10)
    assert len(lines) == 1 and lines[0].startswith("- Malformed: ")


def test_odd_values_inside_the_record_never_crash():
    mech = {"ok": "yes", "dir": None, "files": "x", "step": 3, "dxf": ["a"], "datum_cpl": {},
            "hausdorff_mm": "far", "step_bbox": {"x": [1], "y": "no", "z": None},
            "warnings": "one string"}
    lines = rm.render(_report(mechanical=mech), {"board": {"datum": "nope"}})
    assert any(ln.startswith("- Exports:") for ln in lines)
    assert any("bbox not computed" in ln for ln in lines)
    assert all(isinstance(ln, str) and "\n" not in ln for ln in lines)


def test_text_is_flattened_and_inputs_are_not_mutated():
    mech = {**FULL, "dir": "/work/t10\n## injected", "warnings": ["line one\nline two"]}
    report = _report(mechanical=mech)
    before = copy.deepcopy((report, T10))
    lines = rm.render(report, T10)
    assert all("\n" not in ln and not ln.startswith("#") for ln in lines)
    assert "  - line one line two" in lines
    assert (report, T10) == before


# --- purity ----------------------------------------------------------------------------

def test_module_is_small_pure_and_stdlib_only():
    text = (TOOLS / "review_mechanical.py").read_text(encoding="utf-8")
    assert len(text.splitlines()) < 800
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno < 50, node.name
    assert rm.SECTION_TITLE == "Mechanical"

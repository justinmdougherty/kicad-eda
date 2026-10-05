"""server/tools/import_review.py: the 'Import / rebuild' section of the review.

The import reports here are synthetic. research_report() has the shape of
kicad_import.convert's import_report.json for a research import (an Eagle
board through the KiCad 10.99 importer: footprints not extracted, symbols in
the 10.99 format, footprint / not_in_schematic / value mismatches); its refs,
names and texts are invented, so no content comes from a CC-BY-SA design.
wave2_report() adds the wave-2 keys (D0 library route, foreign, value_repairs,
eagle_library_check, vision). pipeline_report() carries the faithful record
stages_faithful writes (equivalence per stage, ERC baseline, DRC split,
relaxed rules).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import import_review as ir

SHA = "0123456789abcdef" * 4
SRC_SHA = "fedcba9876543210" * 4
# top-level keys of kicad_import.convert's import_report.json (the research run)
RESEARCH_KEYS = frozenset({
    "ok", "source", "outputs", "libraries", "counts", "board_format", "board_generator",
    "schematic_tool", "mismatches", "sch_vs_pcb", "assumptions", "no_connect", "board_only",
    "dropped", "renamed_refs", "unsupported", "net_name_map", "outline",
    "copper_layers_source", "checks"})
EQUAL_SUMMARY = "equivalent: 12 nets over 40 connected pads (12 matched 1:1)"
SYM_K11 = ("the schematic is newer than KiCad 9; its symbols are written as format 20251024, "
           "which KiCad 9 cannot load")
FP_REASON = ("the board is format 20260901, newer than KiCad 9's 20241229: its footprints are "
             "not extracted")
HUMAN_STEP = {
    "id": "gui_only_board:demo.json", "input": "C:/synthetic/demo.json", "role": "board",
    "format": "EasyEDA Pro board", "why": "EasyEDA Pro boards import only in the KiCad GUI",
    "action": "Open demo.json in a KiCad PCB Editor, save it as out/demo.kicad_pcb, then "
              "convert it with the KiCad converter",
    "expected_output": "out/demo.kicad_pcb", "blocking": True}


# --- synthetic inputs ------------------------------------------------------------------
def _equiv(equal: bool = True, summary: str = EQUAL_SUMMARY) -> dict[str, Any]:
    return {"summary": summary, "equal": equal, "split_nets": [], "merged_nets": [],
            "missing_pins": [], "extra_pins": [],
            "details": {"ignore_single_pin": True, "nets_a": 12, "nets_b": 12,
                        "missing_by_net": {}, "extra_by_net": {}, "net_name_map": {}}}


def _mismatches() -> list[dict[str, str]]:
    art = [("LOGO1", "ART_LOGO"), ("LOGO2", "ART_REV"), ("H1", "dummyfp0"), ("H2", "dummyfp1")]
    return ([{"kind": "not_in_schematic", "ref": ref,
              "detail": f"board footprint demo-import-fps:{fp} has no symbol and is not marked "
                        f"board-only; imported as board_only"} for ref, fp in art]
            + [{"kind": "footprint", "ref": f"R{i}",
                "detail": "schematic footprint field 'demo:R_0603', board footprint "
                          "'demo-import-fps:R_0603'; the board wins (D4)"} for i in range(1, 26)]
            + [{"kind": "value", "ref": "J1",
                "detail": "schematic value 'HDR|1X4', board value ''; the schematic wins (D4)"},
               {"kind": "value", "ref": "J2",
                "detail": "schematic value 'HDR-1X2\nROUND', board value ''; the schematic "
                          "wins (D4)"}])


def research_report() -> dict[str, Any]:
    """Shaped like the research import_report.json (synthetic content)."""
    out = "C:\\synthetic\\demo\\netlist_json"
    return {
        "ok": True,
        "source": {"kind": "kicad", "path": "C:\\synthetic\\demo\\demo.kicad_pro",
                   "sha256": SHA, "tool": "kicad-cli 10.99.0",
                   "files": {"demo.kicad_pcb": SRC_SHA, "demo.kicad_pro": SHA}},
        "outputs": {"netlist": f"{out}\\netlist.json", "report": f"{out}\\import_report.json"},
        "libraries": {
            "symbols": {"file": f"{out}\\libs\\demo_imported.kicad_sym", "format": 20251024,
                        "symbols": 9, "variants": [], "missing": [], "problems": [],
                        "kicad11_path_required": SYM_K11},
            "footprints": {"file": f"{out}\\libs\\demo_imported.pretty", "extracted": False,
                           "reason": FP_REASON, "kicad11_path_required": True}},
        "counts": {"schematic_components": 30, "board_footprints": 34, "board_only": 4,
                   "no_connect": 2, "assumptions": 2, "mismatches": 31},
        "board_format": 20260901, "board_generator": "10.99",
        "schematic_tool": "Eeschema 10.99.0-synthetic",
        "mismatches": _mismatches(),
        "sch_vs_pcb": _equiv(),
        "assumptions": [{"id": f"import-nc-U1-{p}", "topic": "no_connect", "value": ["U1", p],
                         "rationale": "the pin is unconnected in the source design",
                         "confidence": "high"} for p in ("4", "5")],
        "no_connect": [{"pad": ["U1", p], "reason": "unconnected"} for p in ("4", "5")],
        "board_only": ["H1", "H2", "LOGO1", "LOGO2"],
        "dropped": [], "renamed_refs": [], "unsupported": [], "net_name_map": {},
        "outline": {"datum_source": "file_origin", "footprint_edges": ["J3"],
                    "kind": "edge_cuts", "cutouts": 2, "frame_origin": [0.0, -20.0]},
        "copper_layers_source": ["F.Cu", "B.Cu"],
        "checks": {"schema_errors": [], "lint": {"ok": True, "errors": [], "warnings": []},
                   "netlist_vs_pcb": _equiv(), "library_paths": {"too_long": []}, "ok": True},
    }


def wave2_report() -> dict[str, Any]:
    """research_report() plus the wave-2 keys (import_pipeline, D0 library route)."""
    return {
        **research_report(),
        "libraries": {
            "symbols": {"format": 20241209, "d0_dialect": True, "k9_verified": True,
                        "route": "downgrade", "refused": []},
            "footprints": {"extracted": True, "route": "importer_library", "format": 20241229,
                           "d0_dialect": True, "k9_verified": True, "models_dropped": 2,
                           "refused": [{"name": "ART_LOGO",
                                        "reason": "fp_text_box is newer than the D0 dialect"}]}},
        "foreign": {"status": "degraded", "human_steps": [HUMAN_STEP],
                    "warnings": ["demo.brd: 2 polygons approximated"]},
        "human_steps": [HUMAN_STEP],                    # the same step twice: shown once
        "eagle_library_check": {
            "footprints": {"identical": 20, "differing": [], "missing": []},
            "symbols": {"identical": 40, "differing": ["SW_PUSH", "LED"], "missing": 0}},
        "value_repairs": [{"ref": "C3", "from": "10\ufffdF", "to": "10uF",
                           "assumption": "import-value-1"}, "C6: 10\ufffdF -> 10uF"],
        "vision": {"verdict": "review", "score": 0.9375, "disagreements": [
            {"kind": "field_mismatch", "severity": "med", "subject": "R2",
             "message": "R2 value: A reads '10k', B reads '1k'"},
            {"kind": "net_split", "severity": "high", "subject": "SDA",
             "message": "net SDA|SCL\nis split"},
            {"kind": "no_connect_mismatch", "severity": "low", "subject": "U1.7",
             "message": "U1.7 no-connect in A only", "resolved": True}]},
        "renamed_refs": [{"original": "U1", "ref": "U1_2", "index": 7, "footprint": "demo:SOT23"}],
        "dropped": [{"ref": "R30", "reason": "exclude_from_board"}],
        "unsupported": [f"J{i}: a KiCad 5 arc is not supported" for i in range(1, 13)],
    }


def pipeline_report(**faithful: Any) -> dict[str, Any]:
    diffs = [f"source net 'N{i}' is split here into N{i}: U1.{i} | (no net): R5.{i}"
             for i in range(1, 5)]
    record = {
        "active": True, "kind": "eagle", "rebuild": "reflow",
        "reference": "C:/synthetic/out/reference_padnets.json",
        "import_report": "C:/synthetic/out/import_report.json",
        "equivalence": {
            "G4": {"equal": False, "summary": "differs: 4 split net(s)", "differences": diffs,
                   "differences_total": 6},
            "G2": {"equal": True, "summary": EQUAL_SUMMARY, "differences": []},
            "G3": {"equal": True, "summary": EQUAL_SUMMARY, "differences": []}},
        "erc": {"total": 7, "new": ["pin_not_connected: U1.9"], "baseline_total": 6},
        "drc": {"ours": ["DRC clearance: track too close [Track [SDA] on F.Cu]"],
                "placement": ["DRC courtyards_overlap from the source placement: x",
                              "DRC silk_overlap from the source placement: y"],
                "intrinsic": ["DRC shorting_items inside JP1 (intrinsic to the footprint): z"]},
        "relaxed": ["G2: faithful: the source design has no bypass on +3V3 at U1 (kept as "
                    "designed)", {"gate": "G3", "rule": "J1 mating face kept where the source "
                                                       "has it"}],
    }
    return {"netlist": "out/netlist.json", "PASS": False,
            "gates": [{"gate": g, "name": g, "feeds": "-", "passed": g != "G4", "blockers": [],
                       "warnings": []} for g in ("G1", "G2", "G3", "G4")],
            "faithful": {**record, **faithful}}


def rebuild_design(**overrides: Any) -> dict[str, Any]:
    design = {
        "name": "demo_rebuild",
        "source": {"kind": "eagle", "path": "src/demo.brd", "sha256": SRC_SHA,
                   "tool": "eagle import (kicad-cli 10.99.0) + kicad_import",
                   "via": "_foreign/demo.kicad_pro", "reference": "reference_padnets.json",
                   "erc_baseline": "source_erc.json",
                   "files": {"demo.brd": SRC_SHA, "demo.sch": SHA}},
        "rebuild": "reflow",
        "variant": {"mode": "reflow", "from": "import/netlist.json",
                    "dropped_refs": ["LOGO1", "LOGO2"], "holes": "source"},
        "components": [{"ref": "R1", "value": "10k", "footprint": "demo:R_0603"}],
        "nets": {"GND": [["R1", "1"]]},
    }
    return {**design, **overrides}


# --- helpers ---------------------------------------------------------------------------
def _section(pipeline: Any = None, design: Any = None, imp: Any = None) -> list[str]:
    return ir.render_import_section(pipeline_report() if pipeline is None else pipeline,
                                    rebuild_design() if design is None else design, imp)


def _unescaped_pipes(line: str) -> int:
    count, i = 0, 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        count += line[i] == "|"
        i += 1
    return count


def _tables(lines: list[str]) -> list[list[str]]:
    tables: list[list[str]] = []
    current: list[str] = []
    for line in lines + [""]:
        if line.startswith("|"):
            current.append(line)
        elif current:
            tables.append(current)
            current = []
    return tables


def _row(lines: list[str], first_cell: str) -> str:
    return next(line for line in lines if line.startswith(f"| {first_cell} |"))


def _line(lines: list[str], prefix: str) -> str:
    return next(line for line in lines if line.startswith(prefix))


# --- table cell / inline helpers -------------------------------------------------------
def test_cell_escapes_pipes_and_newlines() -> None:
    assert ir._cell("a|b\nc\r\nd\u2028e") == "a\\|b c d e"


def test_cell_doubles_a_backslash_before_a_pipe() -> None:
    cell = ir._cell("x\\|y")
    assert cell == "x\\\\\\|y"
    assert _unescaped_pipes(cell) == 0


def test_cell_empty_and_non_text() -> None:
    assert ir._cell(None) == ir._cell("") == ir._cell("  \n ") == "-"
    assert ir._cell({"a": 1}) == '{"a": 1}'
    assert ir._cell(20241229) == "20241229"


def test_code_survives_backticks() -> None:
    assert ir._code("a`b") == "``a`b``"
    assert ir._code("`x") == "`` `x ``"


# --- load_import_report ----------------------------------------------------------------
def _write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_load_needs_a_source(tmp_path: Path) -> None:
    _write(tmp_path / "import_report.json", research_report())
    netlist = tmp_path / "netlist.json"
    assert ir.load_import_report({"components": []}, netlist) is None
    assert ir.load_import_report({"source": "eagle"}, netlist) is None
    assert ir.load_import_report(None, netlist) is None   # type: ignore[arg-type]


def test_load_default_beside_the_netlist(tmp_path: Path) -> None:
    _write(tmp_path / "import_report.json", research_report())
    got = ir.load_import_report(rebuild_design(), str(tmp_path / "netlist.json"))
    assert got == research_report()
    assert ir.load_import_report(rebuild_design(), tmp_path / "x" / "netlist.json") is None
    assert ir.load_import_report(rebuild_design(), None) is None


@pytest.mark.parametrize("name", ["sub/rep.json", "${KIPRJMOD}/sub/rep.json", "absolute"])
def test_load_named_report(tmp_path: Path, name: str) -> None:
    path = _write(tmp_path / "sub" / "rep.json", wave2_report())
    raw = str(path) if name == "absolute" else name
    source = {**rebuild_design()["source"], "import_report": raw}
    design = rebuild_design(source=source)
    assert ir.load_import_report(design, tmp_path / "netlist.json") == wave2_report()
    if name == "absolute":
        assert ir.load_import_report(design, None) == wave2_report()


def _named(raw: Any) -> dict[str, Any]:
    return rebuild_design(source={**rebuild_design()["source"], "import_report": raw})


def test_load_named_but_missing_is_an_error(tmp_path: Path) -> None:
    got = ir.load_import_report(_named("gone.json"), tmp_path / "netlist.json")
    assert set(got) == {"error"} and "gone.json" in got["error"] and "not found" in got["error"]


@pytest.mark.parametrize("text, problem", [("{not json", "not valid JSON"),
                                           ("[1, 2]", "not a JSON object")])
def test_load_unparsable_is_an_error(tmp_path: Path, text: str, problem: str) -> None:
    _write(tmp_path / "import_report.json", text)
    got = ir.load_import_report(rebuild_design(), tmp_path / "netlist.json")
    assert set(got) == {"error"} and problem in got["error"]
    assert "import_report.json" in got["error"]


def test_load_unreadable_path_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "import_report.json").mkdir()
    got = ir.load_import_report(rebuild_design(), tmp_path / "netlist.json")
    assert set(got) == {"error"} and "cannot read" in got["error"]


def test_load_bad_import_report_value(tmp_path: Path) -> None:
    got = ir.load_import_report(_named(42), tmp_path / "netlist.json")
    assert "must be a path string" in got["error"]
    got = ir.load_import_report(_named("rel/rep.json"), None)
    assert "relative" in got["error"] and "rel/rep.json" in got["error"]


# --- render_import_section: contract ---------------------------------------------------
@pytest.mark.parametrize("design", [{}, {"components": []}, {"source": None},
                                    {"source": "eagle"}, None, ["source"]])
def test_no_source_renders_nothing(design: Any) -> None:
    assert ir.render_import_section(pipeline_report(), design, wave2_report()) == []
    assert ir.open_items(pipeline_report(), design, wave2_report()) == []


def test_heading_first_and_every_line_is_one_line() -> None:
    lines = _section(imp=wave2_report())
    assert lines[0] == ir.HEADING == "## Import / rebuild"
    assert all("\n" not in line and "\r" not in line and "\u2028" not in line
               for line in lines)
    assert sum(line.startswith("## ") for line in lines) == 1


def test_inputs_are_not_mutated() -> None:
    args = (pipeline_report(), rebuild_design(), wave2_report())
    before = copy.deepcopy(args)
    ir.render_import_section(*args)
    ir.open_items(*args)
    assert args == before


def test_tables_escape_pipes_and_newlines() -> None:
    imp = wave2_report()
    imp["mismatches"] = [{"kind": "va|lue\nkind", "ref": "R|1\n", "detail": "a|b\r\nc"}]
    imp["libraries"]["footprints"]["reason"] = "x|y"
    imp["libraries"]["footprints"]["extracted"] = False
    pipe = pipeline_report()
    pipe["faithful"]["equivalence"]["G4"]["summary"] = "differs|badly\nhere"
    lines = _section(pipeline=pipe, imp=imp)
    tables = _tables(lines)
    assert len(tables) == 4             # libraries, mismatches, vision, connectivity
    for table in tables:
        width = _unescaped_pipes(table[0])
        assert all(_unescaped_pipes(row) == width for row in table), table
    text = "\n".join(lines)
    assert "| va\\|lue kind | 1 | R\\|1 | a\\|b c |" in text
    assert "differs\\|badly here" in text and "not extracted: x\\|y" in text


# --- render: source, rebuild, variant --------------------------------------------------
def test_source_line_shows_kind_path_tool_and_short_sha() -> None:
    lines = _section(imp=wave2_report())
    src = _line(lines, "- Source:")
    assert src == ("- Source: eagle `src/demo.brd` (sha256 fedcba987654, tool eagle import "
                   "(kicad-cli 10.99.0) + kicad_import)")
    assert SRC_SHA[:13] not in "\n".join(lines)
    assert _line(lines, "- Imported through") == ("- Imported through `_foreign/demo.kicad_pro` "
                                                  "(the intermediate KiCad project)")
    assert _line(lines, "- Source files hashed") == "- Source files hashed: demo.brd, demo.sch"
    assert _line(lines, "- Baselines:") == ("- Baselines: reference pad nets "
                                            "`reference_padnets.json`; ERC `source_erc.json`")


def test_source_falls_back_to_the_import_report() -> None:
    design = rebuild_design(source={"kind": "kicad", "path": "", "copper": "carry",
                                    "board": "demo.kicad_pcb", "drc_baseline": "drc.json"})
    lines = _section(design=design, imp=research_report())
    assert _line(lines, "- Source:") == ("- Source: kicad `C:\\synthetic\\demo\\demo.kicad_pro` "
                                         "(sha256 0123456789ab, tool kicad-cli 10.99.0)")
    assert _line(lines, "- Baselines:") == ("- Baselines: reference pad nets not recorded; ERC "
                                            "not recorded; DRC `drc.json`")
    assert _line(lines, "- Copper:") == "- Copper: carry from `demo.kicad_pcb`"
    assert not any(line.startswith(("- Imported through", "- Source files hashed"))
                   for line in lines)


@pytest.mark.parametrize("sha, shown", [(None, "not recorded"), ("", "not recorded"),
                                        ("xyz", "malformed"), (12, "malformed"),
                                        ("ABCDEF0123456789", "abcdef012345")])
def test_source_sha(sha: Any, shown: str) -> None:
    design = rebuild_design(source={"kind": "vision", "path": "a.pdf", "sha256": sha})
    src = _line(_section(design=design, imp=None), "- Source:")
    assert f"(sha256 {shown}, tool not recorded)" in src


def test_source_without_kind_or_path() -> None:
    src = _line(_section(design=rebuild_design(source={"tool": "t"}), imp=None), "- Source:")
    assert src == "- Source: unknown kind (path not recorded) (sha256 not recorded, tool t)"


def test_rebuild_and_variant_lines() -> None:
    lines = _section(imp=None)
    assert _line(lines, "- Rebuild:") == ("- Rebuild: reflow (re-placed by the pipeline, e.g. a "
                                          "new form factor)")
    assert _line(lines, "- Variant:") == "- Variant: reflow from `import/netlist.json`"
    assert _line(lines, "- Dropped references") == "- Dropped references (2): LOGO1, LOGO2"
    assert _line(lines, "- Holes policy:") == ("- Holes policy: source (the source's hole parts "
                                               "become mounting_holes with their footprint)")


def test_keep_rebuild_without_variant() -> None:
    lines = _section(design=rebuild_design(rebuild="keep", variant=None), imp=None)
    assert _line(lines, "- Rebuild:").startswith("- Rebuild: keep (the source placement")
    assert _line(lines, "- Variant:") == ("- Variant: none (the design is the import as "
                                          "converted)")
    assert not any(line.startswith(("- Dropped", "- Holes")) for line in lines)
    design = {k: v for k, v in rebuild_design().items() if k != "rebuild"}
    assert "not set" in _line(_section(design=design, imp=None), "- Rebuild:")
    assert _line(_section(design=rebuild_design(rebuild="odd"), imp=None),
                 "- Rebuild:") == "- Rebuild: odd"


@pytest.mark.parametrize("holes, shown", [("corners", "corners (corner mounting holes"),
                                          ("none", "none (the source's hole parts are dropped"),
                                          ("weird", "weird"), (None, "not recorded"),
                                          ({"a": 1}, '{"a": 1}')])
def test_holes_policies(holes: Any, shown: str) -> None:
    variant = {"mode": "reflow", "dropped_refs": [], "holes": holes}
    lines = _section(design=rebuild_design(variant=variant), imp=None)
    assert _line(lines, "- Holes policy:").startswith(f"- Holes policy: {shown}")
    assert _line(lines, "- Dropped references") == "- Dropped references: none"
    assert _line(lines, "- Variant:") == "- Variant: reflow"


def test_many_dropped_refs_and_blocks() -> None:
    variant = {"dropped_refs": [f"A{i}" for i in range(1, 26)]}
    blocks =[{"name": "GNSS", "rebuild": "keep", "source": {"kind": "kicad"},
               "ref_map": {"U1": "U10", "C1": "C20"}}, {"name": "BLE", "ref_map": {}}, "junk"]
    lines = _section(design=rebuild_design(variant=variant, blocks=blocks), imp=None)
    dropped = _line(lines, "- Dropped references")
    assert dropped.startswith("- Dropped references (25): A1, A2,") and "A20 (+5 more)" in dropped
    assert _line(lines, "- Variant:") == "- Variant: mode not recorded"
    assert _line(lines, "- Composed blocks") == (
        "- Composed blocks (2): GNSS (2 ref(s), rebuild keep, source kicad); "
        "BLE (0 ref(s), rebuild -, source -)")


# --- render: the import report ---------------------------------------------------------
def test_research_shaped_report() -> None:
    assert set(research_report()) == RESEARCH_KEYS
    lines = _section(imp=research_report())
    assert "### Import report" in lines and "### Libraries" in lines
    assert _line(lines, "- Counts:") == (
        "- Counts: schematic components 30, board footprints 34, board only 4, no connect 2, "
        "assumptions 2, mismatches 31")
    assert _line(lines, "- Source files as read:") == (
        "- Source files as read: board format 20260901 (generator 10.99), schematic tool "
        "Eeschema 10.99.0-synthetic")
    assert _line(lines, "- Unsupported constructs") == "- Unsupported constructs: none"
    assert _row(lines, "Symbols") == (
        f"| Symbols | - | 20251024 | no (by format) | not checked | KiCad 11 path required: "
        f"{SYM_K11} |")
    assert _row(lines, "Footprints") == (
        f"| Footprints | - | - | - | not checked | not extracted: {FP_REASON}; KiCad 11 path "
        f"required |")
    assert not any(line.startswith(("- Foreign import", "- Value repairs", "- Eagle library",
                                    "- Renamed", "- Left off"))
                   for line in lines)
    assert "### Human steps" not in lines and "### Vision cross-check" not in lines


def test_research_mismatches_by_kind() -> None:
    lines = _section(imp=research_report())
    assert lines[lines.index("### Mismatches") + 2].startswith("31 mismatch(es) between")
    refs = ", ".join(f"R{i}" for i in range(1, 21))
    assert _row(lines, "footprint") == (
        f"| footprint | 25 | {refs} (+5 more) | schematic footprint field 'demo:R_0603', board "
        f"footprint 'demo-import-fps:R_0603'; the board wins (D4) |")
    assert _row(lines, "not_in_schematic").startswith(
        "| not_in_schematic | 4 | LOGO1, LOGO2, H1, H2 | board footprint")
    assert _row(lines, "value") == ("| value | 2 | J1, J2 | schematic value 'HDR\\|1X4', board "
                                    "value ''; the schematic wins (D4) |")
    table = _tables(lines[lines.index("### Mismatches"):])[0]
    assert [row.split(" | ")[0] for row in table[2:]] == ["| footprint", "| not_in_schematic",
                                                         "| value"]


def test_mismatch_kinds_are_capped() -> None:
    imp = {**research_report(), "mismatches": [{"kind": f"k{i:02d}", "ref": f"R{i}"}
                                               for i in range(25)] + ["junk"]}
    lines = _section(imp=imp)
    table = _tables(lines[lines.index("### Mismatches"):])[0]
    assert len(table) == 2 + 20 and _row(lines, "k00") == "| k00 | 1 | R0 | - |"
    assert "(+5 more kind(s); see import_report.json)" in lines


def test_no_mismatches_and_odd_shapes() -> None:
    imp = {"counts": "x", "mismatches": [{"ref": "Q1"}], "libraries": {"symbols": "x"},
           "board_format": None, "schematic_tool": "", "unsupported": "x"}
    lines = _section(imp=imp)
    assert _row(lines, "unknown") == "| unknown | 1 | Q1 | - |"
    assert "- Counts: not recorded" in lines and "- Libraries: not recorded" in lines
    assert not any(line.startswith("- Source files as read") for line in lines)
    lines = _section(imp={**research_report(), "mismatches": []})
    assert "- None: the source schematic and board agree on every part" in lines


def test_wave2_libraries_and_report_lines() -> None:
    lines = _section(imp=wave2_report())
    assert _row(lines, "Symbols") == "| Symbols | downgrade | 20241209 | yes | yes | - |"
    assert _row(lines, "Footprints") == (
        "| Footprints | importer_library | 20241229 | yes | yes | refused 1: ART_LOGO: "
        "fp_text_box is newer than the D0 dialect; 3D models dropped: 2 |")
    assert _line(lines, "- Foreign import:") == (
        "- Foreign import: status degraded, 1 warning(s): demo.brd: 2 polygons approximated")
    assert _line(lines, "- Renamed references") == "- Renamed references (1): U1 -> U1_2"
    assert _line(lines, "- Left off the board") == ("- Left off the board (1): R30: "
                                                    "exclude_from_board")
    assert _line(lines, "- Unsupported constructs") == ("- Unsupported constructs: 12 (listed "
                                                        "under Open items)")
    assert _line(lines, "- Eagle library cross-check") == (
        "- Eagle library cross-check: footprints 20 identical, 0 differing, 0 missing; symbols "
        "40 identical, 2 differing (SW_PUSH, LED), 0 missing")


def test_value_repairs_show_the_lost_character() -> None:
    lines = _section(imp=wave2_report())
    at = lines.index("- Value repairs (2):")
    assert lines[at + 1] == "  - C3: '10\ufffdF' -> '10uF' (assumption import-value-1)"
    assert lines[at + 2] == "  - C6: 10\ufffdF -> 10uF"
    many = {**wave2_report(), "value_repairs": [{"ref": f"C{i}", "old": "1\ufffdF", "new": "1uF"}
                                                for i in range(23)] + [{"id": "x"}]}
    lines = _section(imp=many)
    at = lines.index("- Value repairs (24):")
    assert lines[at + 1] == "  - C0: '1\ufffdF' -> '1uF'"
    assert lines[at + 20] == "  - C19: '1\ufffdF' -> '1uF'"
    assert lines[at + 21] == "  - (+4 more)"


def test_human_steps_once_each() -> None:
    lines = _section(imp=wave2_report())
    at = lines.index("### Human steps")
    assert lines[at + 2] == (
        "- [blocking] gui_only_board:demo.json: Open demo.json in a KiCad PCB Editor, save it "
        "as out/demo.kicad_pcb, then convert it with the KiCad converter (why: EasyEDA Pro "
        "boards import only in the KiCad GUI); expected output `out/demo.kicad_pcb`")
    assert lines[at + 3] == ""
    imp = {**research_report(), "human_steps": ["run the GUI import", {"blocking": False}]}
    lines = _section(imp=imp)
    at = lines.index("### Human steps")
    assert lines[at + 2:at + 4] == ["- run the GUI import", "- step: (no action recorded)"]


def test_vision_open_disagreements_high_first() -> None:
    lines = _section(imp=wave2_report())
    at = lines.index("### Vision cross-check")
    assert lines[at + 2] == "- Score 0.9375, verdict review; 2 open disagreement(s)"
    table = _tables(lines[at:])[0]
    assert table[0] == "| Severity | Kind | Subject | Detail |"
    assert table[2] == "| high | net_split | SDA | net SDA\\|SCL is split |"
    assert table[3].startswith("| med | field_mismatch | R2 |") and len(table) == 4


def test_vision_shapes() -> None:
    open_list = [{"severity": "low", "kind": "k", "subject": f"S{i}", "message": "m"}
                 for i in range(22)] + ["free text", {"severity": "odd"}]
    lines = _section(imp={**research_report(), "vision": {"open": open_list}})
    at = lines.index("### Vision cross-check")
    assert lines[at + 2] == "- Score not recorded, verdict -; 24 open disagreement(s)"
    assert len(_tables(lines[at:])[0]) == 22 and "- (+4 more)" in lines
    lines = _section(imp={**research_report(), "vision": {"score": 1.0, "verdict": "agree",
                                                           "open_disagreements": []}})
    assert "- Score 1.0, verdict agree; 0 open disagreement(s)" in lines
    lines = _section(imp={**research_report(), "vision": {"open": ["x"] * 3}})
    assert "| - | - | - | x |" in lines


def test_eagle_check_shapes() -> None:
    def eagle(check: Any) -> str:
        return _line(_section(imp={**research_report(), "eagle_library_check": check}),
                     "- Eagle library cross-check")
    assert eagle({"error": "no KiCad 9 CLI"}) == ("- Eagle library cross-check: not done: no "
                                                  "KiCad 9 CLI")
    assert eagle("skipped") == "- Eagle library cross-check: skipped"
    assert eagle({}) == "- Eagle library cross-check: no results recorded"
    assert eagle({"footprints": {"identical": [1, 2], "differing": {"QFN": "pad 3"}}}) == (
        "- Eagle library cross-check: footprints 2 identical, 1 differing (QFN), - missing")


def test_unreadable_import_report_is_one_line() -> None:
    problem = "the import report C:/x/import_report.json is not valid JSON: Expecting value"
    lines = _section(imp={"error": problem})
    assert [line for line in lines if "not valid JSON" in line] == [
        f"- Import report: unreadable: {problem}"]
    for heading in ("### Import report", "### Libraries", "### Mismatches", "### Human steps"):
        assert heading not in lines
    assert not any(line.startswith(("- Counts", "| import:")) for line in lines)
    assert _line(lines, "- Source:") and "### Rebuild checks" in lines
    assert _row(lines, "G4").startswith("| G4 | no |")


def test_no_import_report() -> None:
    lines = _section(imp=None)
    assert _line(lines, "- Import report:") == (
        "- Import report: none found (source.import_report is unset and no import_report.json "
        "is beside the netlist)")
    assert "### Libraries" not in lines


# --- render: rebuild checks ------------------------------------------------------------
def test_connectivity_per_stage() -> None:
    lines = _section(imp=research_report())
    table = _tables(lines[lines.index("### Rebuild checks"):])[0]
    assert table[0] == "| Check | Equal | Summary | Differences |"
    assert [row.split(" | ")[0] for row in table[2:]] == [
        "| import: source schematic vs board", "| import: netlist.json vs source board",
        "| G2", "| G3", "| G4"]
    assert table[2] == f"| import: source schematic vs board | yes | {EQUAL_SUMMARY} | - |"
    assert _row(lines, "G4") == (
        "| G4 | no | differs: 4 split net(s) | source net 'N1' is split here into N1: U1.1 \\| "
        "(no net): R5.1; source net 'N2' is split here into N2: U1.2 \\| (no net): R5.2; "
        "source net 'N3' is split here into N3: U1.3 \\| (no net): R5.3 (+3 more) |")


def test_erc_drc_and_relaxed_rules() -> None:
    lines = _section(imp=research_report())
    assert _line(lines, "- ERC against the source baseline") == (
        "- ERC against the source baseline: 7 violation(s) in the rebuild, 6 in the source; "
        "1 new: pin_not_connected: U1.9")
    at = lines.index("- DRC split: 1 ours (blockers), 2 from the source placement (warnings), "
                     "1 intrinsic to one footprint (warnings)")
    assert lines[at + 1] == "  - ours: DRC clearance: track too close [Track [SDA] on F.Cu]"
    assert lines[at + 4] == ("  - intrinsic: DRC shorting_items inside JP1 (intrinsic to the "
                             "footprint): z")
    at = lines.index("- Relaxed gate rules (D4 faithful rebuild, 2):")
    assert lines[at + 1].startswith("  - G2: faithful: the source design has no bypass")
    assert lines[at + 2] == "  - G3: J1 mating face kept where the source has it"


def test_rebuild_check_shapes() -> None:
    faithful = {"active": True, "relaxed": [f"r{i}" for i in range(23)],
                "erc": {"total": 3, "new": None, "baseline_total": None},
                "drc": {"ours": [], "ours_errors": 0, "placement": [f"p{i}" for i in range(12)],
                        "intrinsic": 2, "unconnected": 1},
                "equivalence": {"G5": {"equal": True}, "G3": "skipped: no board",
                                "G4": {"skipped": "no routed board"},
                                "G2": {"design": {"equal": True, "summary": "ok"},
                                       "schematic": {"equal": None, "warning": "no kicad-cli"}}}}
    pipe = {"faithful": faithful}
    lines = _section(pipeline=pipe, imp=None)
    assert _line(lines, "- ERC:") == ("- ERC: 3 violation(s); no source baseline "
                                      "(source.erc_baseline), so ERC is judged as usual")
    assert _line(lines, "- DRC split:") == (
        "- DRC split: 0 ours (blockers), 12 from the source placement (warnings), 2 intrinsic "
        "to one footprint (warnings), 1 unconnected (blockers)")
    assert "  - (+2 more)" in lines and "  - (+3 more)" in lines
    table = _tables(lines[lines.index("### Rebuild checks"):])[0]
    assert table[2:] == ["| G2 design | yes | ok | - |", "| G2 schematic | - | no kicad-cli | - |",
                         "| G3 | - | skipped: no board | - |", "| G4 | - | no routed board | - |",
                         "| G5 | yes | - | - |"]
    pipe = {"faithful": {"erc": {"total": None, "new": 2, "baseline_total": 4}, "drc": {}}}
    lines = _section(pipeline=pipe, imp=None)
    assert _line(lines, "- ERC against") == ("- ERC against the source baseline: ? violation(s) "
                                             "in the rebuild, 4 in the source; 2 new")
    assert _line(lines, "- DRC split:").startswith("- DRC split: ? ours (blockers), ? from")
    assert "- Relaxed gate rules: none recorded" in lines


def test_rebuild_checks_not_recorded() -> None:
    lines = _section(pipeline={"gates": []}, imp=research_report())
    at = lines.index("### Rebuild checks")
    assert lines[-1] == ir.NO_FAITHFUL
    assert _tables(lines[at:])[0][2].startswith("| import: source schematic vs board | yes |")
    lines = _section(pipeline={"faithful": {"active": False}}, imp=None)
    assert "faithful.active is false" in lines[lines.index("### Rebuild checks") + 2]
    assert "- ERC against the source baseline: not recorded" in lines
    assert "- DRC split: not recorded" in lines
    lines = ir.render_import_section(None, rebuild_design(), None)   # type: ignore[arg-type]
    assert lines[-1] == ir.NO_FAITHFUL


# --- open_items ------------------------------------------------------------------------
def test_open_items_of_a_wave2_rebuild() -> None:
    items = ir.open_items(pipeline_report(), rebuild_design(), wave2_report())
    assert items[0] == (
        "Human step: [blocking] gui_only_board:demo.json: Open demo.json in a KiCad PCB "
        "Editor, save it as out/demo.kicad_pcb, then convert it with the KiCad converter (why: "
        "EasyEDA Pro boards import only in the KiCad GUI); expected output "
        "`out/demo.kicad_pcb`")
    assert sum(item.startswith("Human step:") for item in items) == 1
    assert ("Footprints: 1 item(s) refused by the KiCad 9 dialect downgrade: ART_LOGO: "
            "fp_text_box is newer than the D0 dialect") in items
    unsupported = [i for i in items if i.startswith("Unsupported in the import: ")]
    assert len(unsupported) == 10 and unsupported[0].endswith("J1: a KiCad 5 arc is not supported")
    assert "2 more unsupported construct(s): see import_report.json" in items
    assert "Eagle library cross-check: 2 symbols differing: SW_PUSH, LED" in items
    assert ("Settle 2 open vision disagreement(s) against the drawing (score 0.9375)"
            in items)
    assert items[-1] == "G4: connectivity differs from the source: differs: 4 split net(s)"
    assert all(isinstance(i, str) and "\n" not in i for i in items)


def test_open_items_of_a_research_import() -> None:
    items = ir.open_items(pipeline_report(), rebuild_design(), research_report())
    assert items[:2] == [
        f"Symbols are not in the KiCad 9 dialect: the rebuild must take the KiCad 11 path "
        f"({SYM_K11})",
        f"Footprints are not in the KiCad 9 dialect: the rebuild must take the KiCad 11 path "
        f"({FP_REASON})"]
    assert len(items) == 3


def test_open_items_odd_shapes() -> None:
    imp = {**research_report(), "foreign": {"status": "incomplete"},
           "sch_vs_pcb": _equiv(False, "differs: 1 merged net"),
           "libraries": {"footprints": {"k9_verified": False, "kicad11_path_required": True},
                         "symbols": {"kicad11_path_required": "too new"}},
           "eagle_library_check": {"footprints": {"missing": 3}, "symbols": "x"},
           "vision": {"disagreements": [{"resolved": True}]}}
    items = ir.open_items({"faithful": {"active": False, "equivalence": {"G2": {"equal": False}}}},
                          rebuild_design(), imp)
    assert items == [
        "Foreign import incomplete: a board or schematic import is missing (see the human "
        "steps of the import)",
        "Symbols are not in the KiCad 9 dialect: the rebuild must take the KiCad 11 path "
        "(too new)",
        "Footprints are not in the KiCad 9 dialect: the rebuild must take the KiCad 11 path",
        "Footprints: the KiCad 9 load check failed (k9_verified false)",
        "Eagle library cross-check: 3 footprints missing",
        "The pipeline ran without the faithful-rebuild rules (faithful.active is false) "
        "although the design has a source",
        "import: source schematic vs board: not equivalent: differs: 1 merged net",
        "G2: connectivity differs from the source: (no summary)"]


def test_open_items_unreadable_report_and_missing_checks() -> None:
    gates = {"gates": [{"gate": "G1"}, {"gate": "G2"}]}
    items = ir.open_items(gates, rebuild_design(), {"error": "cannot read X"})
    assert items == ["Import report unreadable: cannot read X",
                     "Connectivity against the source was not checked: the pipeline report has "
                     "no faithful-rebuild record"]
    assert ir.open_items({"gates": [{"gate": "G1"}, "x"]}, rebuild_design(), None) == []
    assert ir.open_items({}, rebuild_design(), {"libraries": {"symbols": "x"}}) == []
    assert ir.open_items({"gates": "x"}, rebuild_design(), None) == []
    assert ir.open_items(None, rebuild_design(), None) == []   # type: ignore[arg-type]

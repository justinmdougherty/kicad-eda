"""server/tools/kicad_import*.py: KiCad project -> netlist.json (ROADMAP D4).

Unit tier (no KiCad; the CI unit job): the committed fixture project
tests/fixtures/kicad/kimport (self-made with make_kimport.py: KiCad 9
schematic + board + the schematic's kicad-cli XML export) read by both
sides of the converter, ``build_design`` and ``convert`` with the XML given
and footprint extraction off (or run by a stand-in SWIG script), plus small
synthetic boards and sheets for the edge cases: placement (at / side /
rotation, KiCad 11 transforms), the Edge.Cuts outline with arcs, a cutout
and a footprint-internal slot, board-only parts, no_connect + assumptions,
net names, symbol extraction, sheet resolution and staging, long paths and
every kind of schematic-vs-board mismatch in the import report.

The footprint library side is tests/test_kicad_import_fplib.py; the live
tier (real kicad-cli + pcbnew, KiCad demos) is tests/test_kicad_import_live.py.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

import kicad_import as ki
import kicad_import_checks as kchk
import kicad_import_fplib as kf
import kicad_import_pcb as kp
import kicad_import_sch as ks
import lint_netlist
import net_equivalence as ne
import outline

FIX = Path(__file__).resolve().parent / "fixtures" / "kicad" / "kimport"
EQUIV = Path(__file__).resolve().parent / "fixtures" / "equiv"
SCH, PCB, XML = FIX / "kimport.kicad_sch", FIX / "kimport.kicad_pcb", FIX / "kimport.xml"
PRO = FIX / "kimport.kicad_pro"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _design(board_text: str | None = None, xml_text: str | None = None,
            **opts) -> tuple[dict, dict]:
    sch = ks.parse_netlist_xml(xml_text if xml_text is not None else _text(XML))
    board = kp.read_board(board_text if board_text is not None else _text(PCB))
    return ki.build_design(sch, board, ki.ImportOptions(name="kimport", **opts))


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


def _schema_errors(design: dict) -> list[str]:
    netlist_schema = pytest.importorskip("netlist_schema")
    pytest.importorskip("jsonschema")
    return netlist_schema.validate(design)


def _footprint_block(text: str, ref: str) -> tuple[int, int]:
    """(start, end) of the board footprint whose Reference is `ref`."""
    for head, a, b in ks.child_spans(text, 0, len(text)):
        if head == "footprint" and re.search(rf'\(property "Reference" "{ref}"', text[a:b]):
            return a, b
    raise AssertionError(f"no footprint {ref} in the board")


def _edit_footprint(text: str, ref: str, old: str, new: str) -> str:
    a, b = _footprint_block(text, ref)
    block = text[a:b]
    assert old in block, f"{old!r} not in {ref}"
    return text[:a] + block.replace(old, new, 1) + text[b:]


def _drop_footprint(text: str, ref: str) -> str:
    a, b = _footprint_block(text, ref)
    return text[:a] + text[b:]


def _copy_pad(text: str, ref: str, pad: str, number: str) -> str:
    """The board with a copy of footprint `ref`'s pad `pad`, numbered `number`."""
    a, b = _footprint_block(text, ref)
    for head, s, e in ks.child_spans(text, a, b):
        if head == "pad" and text.startswith(f'(pad "{pad}"', s):
            copy = text[s:e].replace(f'(pad "{pad}"', f'(pad "{number}"', 1)
            return text[:e] + "\n\t\t" + copy + text[e:]
    raise AssertionError(f"{ref} has no pad {pad}")


# ----------------------------------------------------------------------------
# Schematic side: the XML export and the embedded symbols
# ----------------------------------------------------------------------------
def test_xml_reader_reads_components_flags_fields_and_pins() -> None:
    sch = ks.parse_netlist_xml(_text(XML))
    assert sch.tool == "Eeschema 9.0.4"
    assert [c.ref for c in sch.components] == ["C1", "C2", "J1", "J2", "R1", "R2", "TP1", "U1"]
    by = {c.ref: c for c in sch.components}
    assert by["C2"].dnp and by["C2"].flags == {"dnp"}
    assert by["R2"].flags == {ks.FLAG_NO_BOM}
    assert by["TP1"].flags == {ks.FLAG_NO_BOARD}
    assert ("LCSC", "C21190") in by["R1"].fields and by["R1"].symbol_key == "Device:R"
    assert {p.number for p in sch.pins_of(by["U1"])} == {str(n) for n in range(1, 9)}
    nc = [n for net in sch.nets for n in net.nodes if n.flagged_nc]
    assert [(n.ref, n.pin) for n in nc] == [("J2", "2")]
    # exported with its .kicad_pro: project-relative, no machine path
    assert sch.source == "kimport.kicad_sch"
    assert {c.sheet_file for c in sch.components} == {"kimport.kicad_sch"}


@pytest.mark.parametrize("text, match", [
    ("<html/>", "not a KiCad netlist"), ("<export/>", "lacks"), ("not xml", "well-formed")])
def test_xml_reader_rejects_what_is_not_a_netlist(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ks.parse_netlist_xml(text)


def test_sheets_and_embedded_symbols_are_read_from_the_schematic() -> None:
    sheets = ks.read_sheets(SCH)
    assert [name for name, _ in sheets] == ["kimport.kicad_sch"]
    assert ks.schematic_version(sheets[0][1]) == 20250114
    emb = ks.embedded_symbols(sheets)["kimport.kicad_sch"]
    assert {"Device:R", "Device:C", "Amplifier_Operational:LM358"} <= set(emb)


def test_read_sheets_follows_sub_sheets_and_fails_on_a_missing_one(tmp_path: Path) -> None:
    root = ('(kicad_sch (version 20250114) (lib_symbols)\n'
            '  (sheet (at 0 0) (property "Sheetname" "A")'
            ' (property "Sheetfile" "sub/a.kicad_sch")))')
    (tmp_path / "sub").mkdir()
    (tmp_path / "top.kicad_sch").write_text(root, encoding="utf-8")
    (tmp_path / "sub" / "a.kicad_sch").write_text(
        '(kicad_sch (version 20250114) (lib_symbols (symbol "L:X" (symbol "X_1_1"))))',
        encoding="utf-8")
    sheets = ks.read_sheets(tmp_path / "top.kicad_sch")
    assert [n for n, _ in sheets] == ["top.kicad_sch", "sub/a.kicad_sch"]
    assert set(ks.embedded_symbols(sheets)["sub/a.kicad_sch"]) == {"L:X"}
    (tmp_path / "sub" / "a.kicad_sch").unlink()
    with pytest.raises(ValueError, match="not found"):
        ks.read_sheets(tmp_path / "top.kicad_sch")


def _sheet_file(path: Path, *refs: str) -> None:
    sheets = " ".join(f'(sheet (at 0 0) (property "Sheetname" "S{i}") (property "Sheetfile" '
                      f'"{ref}"))' for i, ref in enumerate(refs))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"(kicad_sch (version 20250114) (lib_symbols) {sheets})", encoding="utf-8")


def test_read_sheets_resolves_sheet_files_the_way_kicad_does(tmp_path: Path) -> None:
    """kicad-cli 9 and 10.99 resolve a relative Sheetfile against the sheet
    that references it first, then the project directory (checked with a
    part in each of two same-named sheets)."""
    _sheet_file(tmp_path / "top.kicad_sch", "sub/a.kicad_sch")
    _sheet_file(tmp_path / "sub" / "a.kicad_sch", "b.kicad_sch", "c.kicad_sch")
    for path in ("sub/b.kicad_sch", "b.kicad_sch", "c.kicad_sch"):
        _sheet_file(tmp_path / path)
    names = [n for n, _ in ks.read_sheets(tmp_path / "top.kicad_sch")]
    assert names == ["top.kicad_sch", "sub/a.kicad_sch", "sub/b.kicad_sch", "c.kicad_sch"]
    with pytest.raises(ValueError, match="no sub/d.kicad_sch or d.kicad_sch"):
        ks.sheet_name(tmp_path, "sub/a.kicad_sch", "d.kicad_sch")


def _files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


def test_sheets_outside_the_project_are_staged_inside_the_work_dir(tmp_path: Path) -> None:
    proj, work = tmp_path / "w" / "x" / "proj", tmp_path / "work"
    elsewhere = (tmp_path / "abs" / "t.kicad_sch").as_posix()
    _sheet_file(proj / "top.kicad_sch", "../../lib/s.kicad_sch", elsewhere)
    _sheet_file(tmp_path / "w" / "lib" / "s.kicad_sch")
    _sheet_file(Path(elsewhere))
    (proj / "top.kicad_pro").write_text("{}", encoding="utf-8")
    names = [n for n, _ in ks.read_sheets(proj / "top.kicad_sch")]
    assert names == ["top.kicad_sch", "../../lib/s.kicad_sch", elsewhere]
    before = _files_under(tmp_path)
    top = ks.stage_sheets(proj, names, [proj / "top.kicad_pro"], work / "src")
    assert top == work / "src" / "_up1" / "_up2" / "top.kicad_sch"
    assert (work / "src" / "lib" / "s.kicad_sch").is_file()
    assert (top.parent / "top.kicad_pro").is_file()
    assert _files_under(tmp_path) - _files_under(work) == before    # nothing written outside
    # the staged copy resolves to the same sheets: '../..' stays inside src,
    # the absolute sheet is read in place
    assert [n for n, _ in ks.read_sheets(top)] == names


def _sym(key: str, body: str = "", sheet: str = "s.kicad_sch") -> ks.EmbeddedSymbol:
    bare = ks.bare_name(key)
    text = (f'(symbol "{key}" (property "Value" "{bare}") {body}'
            f'(symbol "{bare}_0_1" (rectangle)) (symbol "{bare}_1_1" (pin passive line '
            f'(at 0 0 0) (length 1) (name "~") (number "1"))))')
    return ks.EmbeddedSymbol(key, sheet, text)


def _comp_for(ref: str, key: str, sheet: str = "s.kicad_sch") -> ks.SchComponent:
    lib, _, part = key.rpartition(":")
    return ks.SchComponent(ref=ref, value="", footprint="", datasheet="", description="",
                           lib=lib, part=part, fields=(), flags=frozenset(),
                           sheet_names="/", sheet_file=sheet)


def test_rename_symbol_renames_the_units_with_it() -> None:
    text = ks.rename_symbol(_sym("Device:R"), "Device_R")
    assert re.findall(r'\(symbol "([^"]+)"', text) == ["Device_R", "Device_R_0_1", "Device_R_1_1"]
    assert '(property "Value" "R")' in text            # field values are untouched


def test_plan_symbols_names_shares_and_disambiguates() -> None:
    a1, a2 = _sym("Lib1:R"), _sym("Lib1:R", body="(edited)", sheet="t.kicad_sch")
    b = _sym("Lib2:R")
    embedded = {"s.kicad_sch": {"Lib1:R": a1, "Lib2:R": b}, "t.kicad_sch": {"Lib1:R": a2}}
    comps = [_comp_for("R1", "Lib1:R"), _comp_for("R2", "Lib1:R"), _comp_for("R3", "Lib2:R"),
             _comp_for("R4", "Lib1:R", sheet="t.kicad_sch"), _comp_for("R5", "Lib9:Gone")]
    plan = ks.plan_symbols(comps, embedded)
    assert plan.names == {"R1": "R", "R2": "R", "R3": "Lib2_R", "R4": "R_2"}
    assert plan.missing == ("R5",)
    assert [n for n, _ in plan.entries] == ["R", "Lib2_R", "R_2"]
    assert {v["name"] for v in plan.variants} == {"Lib2_R", "R_2"}


def test_symbol_library_round_trips_through_symlib(tmp_path: Path) -> None:
    sch = ks.parse_netlist_xml(_text(XML))
    comps = [c for c in sch.components if c.ref != "TP1"]
    plan, info = ks.write_symbol_library(comps, ks.read_sheets(SCH), tmp_path / "k.kicad_sym")
    assert info["format"] == ks.K9_SYM_FORMAT and not info["missing"] and not info["problems"]
    import symlib
    lib = symlib.load_lib(tmp_path, "k")
    assert set(lib) == {"C", "R", "LM358", "Conn_01x06", "Conn_01x02"}
    for comp in comps:                       # every pin the export lists is a symbol pin
        flat = symlib.flatten({}, tmp_path, "k", plan.names[comp.ref])
        assert {p.number for p in sch.pins_of(comp)} == set(symlib.pin_numbers(flat))
    assert symlib.unit_pins(symlib.flatten({}, tmp_path, "k", "LM358")) == {
        1: ["3", "2", "1"], 2: ["5", "6", "7"], 3: ["8", "4"]}


# ----------------------------------------------------------------------------
# Board side
# ----------------------------------------------------------------------------
def test_board_reader_reads_placement_attributes_and_pads() -> None:
    board = kp.read_board(_text(PCB))
    assert board.format_version == 20241229 and board.generator_version == "9.0"
    assert board.copper_layers == ("F.Cu", "B.Cu") and board.aux_origin == (100.0, 110.0)
    by = {fp.ref: fp for fp in board.footprints}
    assert (by["C1"].x, by["C1"].y, by["C1"].rot, by["C1"].side) == (120.5, 101.5, 45.0, "B")
    assert by["J1"].rot == 270.0 and by["R1"].rot == 90.0         # the file stores J1 at -90
    assert by["H1"].board_only and by["H2"].board_only and not by["U1"].board_only
    assert "dnp" in by["C2"].attrs and "exclude_from_bom" in by["R2"].attrs
    assert by["H1"].pads == (kp.BoardPad("1", "/GND"),)
    assert by["H2"].pads == ()                                     # NPTH: no pad number
    assert by["J2"].pads[1] == kp.BoardPad("2", "unconnected-(J2-Pin_2-Pad2)")


def test_board_reader_pad_nets_match_net_equivalence() -> None:
    text = _text(PCB)
    assert ki.board_pad_nets(kp.read_board(text)) == ne.pcb_pad_nets(text)


def test_to_sheet_rotates_like_kicad() -> None:
    # StickHub D4 at (150.4, 96.75, -90): local pad (-0.45, 0) sits at (150.4, 96.3)
    x, y = kp.to_sheet((-0.45, 0.0), (150.4, 96.75, -90.0))
    assert (round(x, 9), round(y, 9)) == (150.4, 96.3)


def test_footprint_edge_cuts_are_moved_into_the_sheet_frame() -> None:
    h2 = next(fp for fp in kp.read_board(_text(PCB)).footprints if fp.ref == "H2")
    assert [p["kind"] for p in h2.edge_cuts].count("arc") == 2 and len(h2.edge_cuts) == 4
    # the slot's local centre (0, 4) under H2 at (135.5, 104) rot 30
    cx, cy = kp.to_sheet((0.0, 4.0), (135.5, 104.0, 30.0))
    pts = [p for e in h2.edge_cuts for p in (e["start"], e["end"])]
    centre = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    assert math.dist(centre, (cx, cy)) < 1e-6


@pytest.mark.parametrize("version, expected", [
    ("k11", {"R1": (100.0, 100.0, 0.0, "F"), "C1": (108.0, 100.0, 180.0, "B")}),
    ("k5", {"R1": (100.0, 100.0, 0.0, "F")}),
])
def test_board_reader_reads_old_and_new_placement_syntax(version: str, expected: dict) -> None:
    board = kp.read_board(_text(EQUIV / f"board_{version}.sexpr"))
    by = {fp.ref: (fp.x, fp.y, fp.rot, fp.side) for fp in board.footprints}
    for ref, want in expected.items():
        assert by[ref] == want


def _mini_board(*items: str, layers: str = '(0 "F.Cu" signal) (2 "B.Cu" signal)') -> str:
    return (f'(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")'
            f' (layers {layers} (25 "Edge.Cuts" user)) (net 0 "") ' + " ".join(items) + ")")


def test_edge_shapes_become_lines_arcs_and_circles() -> None:
    board = kp.read_board(_mini_board(
        '(gr_rect (start 0 0) (end 10 5) (layer "Edge.Cuts"))',
        '(gr_poly (pts (xy 2 1) (arc (start 3 1) (mid 3.5 1.5) (end 4 1)) (xy 4 2)) '
        '(layer "Edge.Cuts"))',
        '(gr_circle (center 7 2.5) (end 8 2.5) (layer "Edge.Cuts"))',
        '(gr_line (start 0 0) (end 1 1) (layer "F.SilkS"))'))
    kinds = [p["kind"] for p in board.edge_cuts]
    assert kinds == ["line"] * 4 + ["line", "arc", "line", "line"] + ["circle"]
    assert board.edge_cuts[-1]["radius"] == 1.0
    o = outline.from_kicad_primitives(board.edge_cuts)
    assert len(o.cutouts) == 2 and outline.bbox(o) == (0.0, 0.0, 10.0, 5.0)


def test_bezier_and_kicad5_arcs_are_reported() -> None:
    board = kp.read_board(_mini_board(
        '(gr_curve (pts (xy 0 0) (xy 1 1) (xy 2 1) (xy 3 0)) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (end 1 0) (angle 90) (layer "Edge.Cuts"))'))
    assert len(board.edge_cuts) == kp.BEZIER_SEGMENTS
    assert any("Bezier" in n for n in board.notes) and any("KiCad 5 arc" in n for n in board.notes)


def test_board_reader_fails_loudly_on_garbage() -> None:
    with pytest.raises(ValueError, match="not a KiCad board"):
        kp.read_board("(kicad_sch)")
    with pytest.raises(ValueError, match="unbalanced"):
        kp.read_board("(kicad_pcb (footprint")


# ----------------------------------------------------------------------------
# build_design on the fixture
# ----------------------------------------------------------------------------
def test_placement_is_carried_as_at_side_and_rotation() -> None:
    design, _ = _design()
    assert _comp(design, "C1")["at"] == [20.5, 21.5, 45.0] and _comp(design, "C1")["side"] == "B"
    assert _comp(design, "J1")["at"] == [4.0, 18.0, 270.0]
    assert _comp(design, "R1")["at"] == [25.0, 12.0, 90.0]
    assert _comp(design, "H2")["at"] == [35.5, 24.0, 30.0]
    assert all("x" not in c and "rot" not in c for c in design["components"])


def test_outline_loops_carry_arcs_cutouts_and_the_datum() -> None:
    design, report = _design()
    board = design["board"]
    assert (board["w"], board["h"], board["datum"]) == (40.0, 30.0, [0.0, 30.0])
    bulges = [v[2] for v in board["outline"]["loop"] if len(v) == 3]
    assert len(bulges) == 4 and all(abs(b - math.tan(math.pi / 8)) < 1e-5 for b in bulges)
    circle, slot = board["cutouts"]
    assert circle == {"circle": [31.0, 15.0, 1.5]}
    assert sum(1 for v in slot["loop"] if len(v) == 3 and abs(abs(v[2]) - 1) < 1e-5) == 2
    o = outline.from_board_block(board)                    # D2 contract reads it back
    assert outline.contains(o, (20.0, 15.0)) and not outline.contains(o, (31.0, 15.0))
    assert report["outline"]["footprint_edges"] == ["H2"]
    assert report["outline"]["datum_source"] == "aux_axis_origin"


def test_board_only_parts_have_no_symbol_and_keep_their_pad_nets() -> None:
    design, report = _design()
    for ref in ("H1", "H2"):
        comp = _comp(design, ref)
        assert comp["board_only"] is True and "lib_id" not in comp and comp["in_bom"] is False
    assert ["H1", "1"] in design["nets"]["GND"]
    assert report["board_only"] == ["H1", "H2"]


def test_unconnected_pins_become_no_connect_with_an_assumption_unless_flagged() -> None:
    design, report = _design()
    assert design["no_connect"] == [["J1", "6"], ["J2", "2"]]
    assert {tuple(e["pad"]): e["reason"] for e in report["no_connect"]} == {
        ("J1", "6"): "unconnected", ("J2", "2"): "flagged"}
    assert [a["id"] for a in design["assumptions"]] == ["import-nc-J1-6"]
    assert design["assumptions"][0]["confidence"] == "high"


def test_nets_follow_the_board_with_d4_names() -> None:
    design, report = _design()
    assert set(design["nets"]) == {"GND", "IN_A", "IN_A_R", "IN_B", "OUT_A", "OUT_B", "P5V"}
    assert report["net_name_map"]["IN_A"] == "/IN_A"
    assert all(len(pads) >= 2 for pads in design["nets"].values())


def test_schematic_fields_flags_and_sourcing_are_carried() -> None:
    design, report = _design()
    assert _comp(design, "C2")["dnp"] is True and _comp(design, "R2")["in_bom"] is False
    assert _comp(design, "R1")["lcsc"] == "C21190"
    assert _comp(design, "R1")["fields"] == {"LCSC": "C21190", "Tolerance": "1%"}
    assert _comp(design, "U1")["mpn"] == "LM358DR2G"
    assert "TP1" not in {c["ref"] for c in design["components"]}
    assert report["dropped"] == [{"ref": "TP1", "reason": "exclude_from_board"}]


def test_converted_design_validates_lints_and_round_trips() -> None:
    design, report = _design(symbol_lib="kimport_imported",
                             symbol_names={r: r for r in ("C1", "C2", "J1", "J2", "R1", "R2",
                                                          "U1")})
    assert _schema_errors(design) == []
    assert lint_netlist.lint(design)["ok"]
    res = ne.compare(ne.netlist_pad_nets(design), ne.pcb_pad_nets(_text(PCB)))
    assert res.equal, res.summary()
    assert report["mismatches"] == [] and report["sch_vs_pcb"]["equal"]


def test_reflow_import_leaves_placement_to_the_pipeline() -> None:
    design, report = _design(rebuild="reflow")
    assert design["rebuild"] == "reflow"
    assert all("at" not in c for c in design["components"])
    refs = {c["ref"] for c in design["components"]}
    assert {p["ref"] for p in report["source_placement"]} == refs
    with pytest.raises(ValueError, match="rebuild"):
        _design(rebuild="sideways")


# ----------------------------------------------------------------------------
# Import report: schematic vs board mismatches (the board wins)
# ----------------------------------------------------------------------------
def _kinds(report: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in report["mismatches"]:
        out.setdefault(m["kind"], []).append(m)
    return out


def test_a_pad_moved_to_another_net_is_reported_and_the_board_wins() -> None:
    board = _edit_footprint(_text(PCB), "R1", '(net 3 "/IN_A_R")', '(net 1 "/GND")')
    assert '(net 1 "/GND")' in board
    design, report = _design(board)
    kinds = _kinds(report)
    assert not report["sch_vs_pcb"]["equal"]
    assert {m["net"] for m in kinds["net_split"]} == {"/IN_A_R"}
    assert {m["net"] for m in kinds["net_merged"]} == {"/GND"}
    assert ["R1", "2"] in design["nets"]["GND"] and "IN_A_R" not in design["nets"]
    assert ["U1", "3"] in design["no_connect"]          # left alone on the board
    nc = {tuple(e["pad"]): e["reason"] for e in report["no_connect"]}
    assert nc[("U1", "3")] == "isolated_on_board"
    assert ne.compare(ne.netlist_pad_nets(design), ne.pcb_pad_nets(board)).equal


def test_value_footprint_and_flag_disagreements_are_reported() -> None:
    board = _edit_footprint(_text(PCB), "R2", '(property "Value" "10k"', '(property "Value" "22k"')
    board = _edit_footprint(board, "J2", '"Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_'
                            'Vertical"', '"MyLib:Header2"')
    board = _edit_footprint(board, "C2", "(attr smd dnp)", "(attr smd)")
    design, report = _design(board)
    kinds = _kinds(report)
    assert [m["ref"] for m in kinds["value"]] == ["R2"] and _comp(design, "R2")["value"] == "10k"
    assert [m["ref"] for m in kinds["footprint"]] == ["J2"]
    assert _comp(design, "J2")["footprint"] == "MyLib:Header2"
    assert [m["ref"] for m in kinds["dnp"]] == ["C2"] and _comp(design, "C2")["dnp"] is True


def test_parts_on_one_side_only_are_reported() -> None:
    board = _drop_footprint(_text(PCB), "U1")
    board = _edit_footprint(board, "H1",
                            "(attr board_only exclude_from_pos_files exclude_from_bom)",
                            "(attr exclude_from_pos_files exclude_from_bom)")
    design, report = _design(board)
    kinds = _kinds(report)
    assert [m["ref"] for m in kinds["missing_from_board"]] == ["U1"]
    assert [m["ref"] for m in kinds["not_in_schematic"]] == ["H1"]
    assert _comp(design, "H1")["board_only"] is True
    assert "U1" not in {c["ref"] for c in design["components"]}
    # every connected U1 pin is now a schematic pin without a board pad
    assert {(m["ref"], m["pad"]) for m in kinds["pin_missing_on_board"]} == {
        ("U1", str(n)) for n in range(1, 9)}
    assert len(report["sch_vs_pcb"]["missing_pins"]) == 8


def test_a_pad_number_on_two_nets_is_a_pad_conflict() -> None:
    board = _edit_footprint(_text(PCB), "U1", '(pad "3"', '(pad "1"')     # 3 (IN_A_R) -> 1
    design, report = _design(board)
    conflict = _kinds(report)["pad_conflict"]
    assert [(m["ref"], m["pad"]) for m in conflict] == [("U1", "1")]
    assert "'/IN_A_R'" in conflict[0]["detail"]
    assert ["U1", "1"] in design["nets"]["OUT_A"]          # the first pad keeps its net
    assert ["U1", "3"] in design["no_connect"]             # the symbol pin lost its pad


def test_a_board_pad_the_schematic_lacks_is_reported() -> None:
    board = _copy_pad(_text(PCB), "J2", "1", "3")           # J2.3 on GND, no such pin
    design, report = _design(board)
    extra = _kinds(report)["pad_missing_in_schematic"]
    assert [(m["ref"], m["pad"]) for m in extra] == [("J2", "3")]
    assert report["sch_vs_pcb"]["extra_pins"] == [["J2", "3"]]
    assert ["J2", "3"] in design["nets"]["GND"]             # the board wins


def test_a_symbol_excluded_from_the_board_but_placed_is_reported() -> None:
    marker = '<property name="Tolerance" value="1%"/>'      # R1's own field
    xml = _text(XML).replace(marker, marker + '<property name="exclude_from_board"/>', 1)
    design, report = _design(xml_text=xml)
    kinds = _kinds(report)
    assert [m["ref"] for m in kinds["excluded_but_on_board"]] == ["R1"]
    assert _comp(design, "R1")["lib_id"]                    # still imported: the board wins
    assert {m["pad"] for m in kinds["pad_missing_in_schematic"] if m["ref"] == "R1"} == {"1", "2"}
    assert report["dropped"] == [{"ref": "TP1", "reason": "exclude_from_board"}]


def test_a_bom_flag_disagreement_is_reported_and_the_schematic_wins() -> None:
    board = _edit_footprint(_text(PCB), "R2", "(attr smd exclude_from_bom)", "(attr smd)")
    design, report = _design(board)
    assert [m["ref"] for m in _kinds(report)["in_bom"]] == ["R2"]
    assert _comp(design, "R2")["in_bom"] is False


def test_a_symbol_pin_without_a_pad_is_reported_and_marked_no_connect() -> None:
    board = _edit_footprint(_text(PCB), "J1", '(pad "6"', '(pad ""')
    design, report = _design(board)
    assert [m["pad"] for m in _kinds(report)["pin_without_pad"]] == ["6"]
    assert {tuple(e["pad"]): e["reason"] for e in report["no_connect"]}[("J1", "6")] == "no_pad"
    assert ["J1", "6"] in design["no_connect"]


def _padded_hole(text: str) -> int:
    return next(fp.index for fp in kp.read_board(text).footprints if fp.lib_id.endswith("_Pad"))


def test_duplicate_references_are_renamed_and_checked_where_they_sit() -> None:
    text = _edit_footprint(_text(PCB), "H2", '(property "Reference" "H2"',
                           '(property "Reference" "H1"')
    design, report = _design(text)
    # the slotted hole comes first in the file and keeps "H1"; the padded one,
    # the second "H1", takes the first free H number
    renamed = report["renamed_refs"]
    assert renamed == [{"original": "H1", "ref": "H2", "index": _padded_hole(text),
                        "footprint": "MountingHole:MountingHole_3.2mm_M3_Pad"}]
    assert [m["kind"] for m in report["mismatches"]] == ["duplicate_ref"]
    refs = [c["ref"] for c in design["components"]]
    assert len(refs) == len(set(refs)) and lint_netlist.lint(design)["ok"]
    # the board read under the import's names: H2's GND pad is compared, nothing is ignored
    assert ("H2", "1") in ne.pcb_pad_nets(kchk.board_as_imported(text, renamed))["/GND"]
    checks = kchk.check_design(design, text, renamed)
    assert checks["netlist_vs_pcb"]["equal"]
    assert checks["netlist_vs_pcb"]["details"]["ignored_refs"] == []
    assert checks["netlist_vs_pcb"]["renamed_parts"] == {"H2": _padded_hole(text)}


def test_a_renamed_part_on_the_wrong_net_fails_the_check() -> None:
    text = _edit_footprint(_text(PCB), "H2", '(property "Reference" "H2"',
                           '(property "Reference" "H1"')
    design, report = _design(text)
    design["nets"]["GND"].remove(["H2", "1"])
    design["nets"]["P5V"].append(["H2", "1"])
    checks = kchk.check_design(design, text, report["renamed_refs"])
    assert not checks["netlist_vs_pcb"]["equal"] and not checks["ok"]
    # a rename that does not match the board footprint at its index is an error, not a pass
    u1 = next(fp.index for fp in kp.read_board(text).footprints if fp.ref == "U1")
    wrong = [{**report["renamed_refs"][0], "index": u1}]
    checks = kchk.check_design(_design(text)[0], text, wrong)
    assert not checks["ok"] and f"footprint {u1}" in checks["netlist_vs_pcb"]["error"]


@pytest.mark.parametrize("new, original, ref", [
    ('(property "Reference" ""', "", "X1"),                 # an empty reference
    ('(property "Ref_moved" "H1"', "", "X1"),               # no reference at all
    ('(fp_text reference "H2"', "H2", "H1"),                # KiCad <= 7 text, a duplicate
])
def test_every_kind_of_renamed_reference_is_named_on_the_board(new: str, original: str,
                                                               ref: str) -> None:
    text = _edit_footprint(_text(PCB), "H1", '(property "Reference" "H1"', new)
    design, report = _design(text)
    assert report["renamed_refs"] == [{"original": original, "ref": ref,
                                       "index": _padded_hole(text),
                                       "footprint": "MountingHole:MountingHole_3.2mm_M3_Pad"}]
    imported = kchk.board_as_imported(text, report["renamed_refs"])
    refs = [fp.ref for fp in kp.read_board(imported).footprints]
    assert refs[_padded_hole(text)] == ref and len(refs) == len(set(refs))
    assert (ref, "1") in ne.pcb_pad_nets(imported)["/GND"]
    assert kchk.check_design(design, text, report["renamed_refs"])["netlist_vs_pcb"]["equal"]


# ----------------------------------------------------------------------------
# Helpers: net names, copper layers, outline fallbacks, footprint identity
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("/CH1/OUT", "CH1_OUT"), ("/U2D+", "U2D+"), ("GND", "GND"), ("+5V", "+5V"),
    ("Net-(C4-Pad2)", "Net-(C4-Pad2)"), ("unconnected-(U1-TEST#/SDA-Pad2)",
                                          "unconnected-(U1-TEST#/SDA-Pad2)"),
    ("/CH1/Net-(U1-TEST#/SDA)", "CH1_Net-(U1-TEST#/SDA)"), ("/", "unnamed")])
def test_sanitize_net_name(name: str, expected: str) -> None:
    assert ki.sanitize_net_name(name) == expected


def test_net_name_collisions_get_a_suffix() -> None:
    names = ki._net_names(["/A_B", "/A/B", "A_B"])
    assert names["A_B"] == "A_B" and sorted([names["/A_B"], names["/A/B"]]) == ["A_B_2", "A_B_3"]


def _with(board: kp.BoardDesign, **changes) -> kp.BoardDesign:
    return dataclasses.replace(board, **changes)


def test_copper_layer_counts_the_schema_cannot_hold_are_reported() -> None:
    board = kp.read_board(_text(PCB))
    sch = ks.parse_netlist_xml(_text(XML))
    one, _ = ki.build_design(sch, _with(board, copper_layers=("F.Cu",)), ki.ImportOptions("k"))
    assert one["copper_layers"] == 2
    assert any(a["id"] == "import-copper-layers" for a in one["assumptions"])
    eight = tuple(f"In{i}.Cu" for i in range(1, 7)) + ("F.Cu", "B.Cu")
    many, rep = ki.build_design(sch, _with(board, copper_layers=eight), ki.ImportOptions("k"))
    assert "copper_layers" not in many and any("8 copper layers" in u for u in rep["unsupported"])


def test_outline_fallbacks_are_recorded() -> None:
    board = kp.read_board(_text(PCB))
    sch = ks.parse_netlist_xml(_text(XML))
    bare = _with(board, edge_cuts=(), footprints=tuple(
        dataclasses.replace(fp, edge_cuts=()) for fp in board.footprints))
    design, report = ki.build_design(sch, bare, ki.ImportOptions("k"))
    assert "outline" not in design["board"] and report["outline"]["kind"] == "fallback_rectangle"
    # frame = the footprint origins' extent (x from J1 at 104, y from J2 at 86) + the margin
    m = kp.FALLBACK_MARGIN_MM
    assert _comp(design, "J1")["at"][:2] == [m, 98.0 - (86.0 - m)]
    assert design["board"]["w"] == (135.5 + m) - (104.0 - m)
    broken = _with(board, edge_cuts=board.edge_cuts[1:])        # an open chain
    design, report = ki.build_design(sch, broken, ki.ImportOptions("k"))
    assert any("not usable" in u for u in report["unsupported"])
    assert any(a["id"] == "import-outline" for a in design["assumptions"])


def test_library_paths_kicad_cannot_open_are_named(monkeypatch) -> None:
    root = os.path.abspath(os.sep)
    fits = root + "a" * (kf.KICAD_MAX_PATH - len(root))
    over = root + "b" * (kf.KICAD_MAX_PATH + 1 - len(root))
    assert kf.too_long_for_kicad([fits, over, "\\\\?\\" + over], platform="win32") == [over, over]
    assert kf.too_long_for_kicad([over], platform="linux") == []
    design, _ = _design(symbol_lib="k", symbol_names={r: r for r in ("C1", "C2", "J1", "J2",
                                                                     "R1", "R2", "U1")})
    monkeypatch.setattr(sys, "platform", "win32")
    checks = kchk.check_design(design, _text(PCB), library_files=[fits, over])
    assert checks["library_paths"]["too_long"] == [over] and not checks["ok"]
    assert "shorter output directory" in checks["library_paths"]["error"]


def test_a_schema_check_that_could_not_run_is_not_a_pass(tmp_path: Path, monkeypatch,
                                                         capsys) -> None:
    netlist_schema = pytest.importorskip("netlist_schema")
    design, _ = _design(symbol_lib="k", symbol_names={r: r for r in ("C1", "C2", "J1", "J2",
                                                                     "R1", "R2", "U1")})
    assert kchk.check_design(design, _text(PCB))["schema_validated"] is True

    def no_validator(_design: dict) -> list[str]:
        raise RuntimeError("validating netlist.json needs jsonschema")
    monkeypatch.setattr(netlist_schema, "validate", no_validator)
    checks = kchk.check_design(design, _text(PCB))
    assert checks["schema_validated"] is False and checks["schema_errors"] is None
    assert "jsonschema" in checks["schema_note"] and not checks["ok"]
    assert checks["lint"]["ok"] and checks["netlist_vs_pcb"]["equal"]    # only the schema
    rc = ki.main([str(FIX), str(tmp_path), "--netlist-xml", str(XML), "--no-footprints"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["schema_validated"] is False and out["ok"] is False


# ----------------------------------------------------------------------------
# convert() and the CLI without KiCad (XML given, no footprint extraction)
# ----------------------------------------------------------------------------
def test_locate_project(tmp_path: Path) -> None:
    files = ki.locate_project(FIX)
    assert (files.stem, files.sch.name, files.pcb.name) == ("kimport", SCH.name, PCB.name)
    assert ki.locate_project(PCB).pro == FIX.resolve() / "kimport.kicad_pro"
    (tmp_path / "a.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "a.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="a.kicad_pcb"):
        ki.locate_project(tmp_path)
    (tmp_path / "b.kicad_pro").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="2 projects"):
        ki.locate_project(tmp_path)


def test_convert_with_a_given_export_writes_netlist_libraries_and_report(tmp_path: Path) -> None:
    res = ki.convert(FIX, tmp_path, netlist_xml=str(XML), extract_footprints=False)
    assert res["ok"], json.dumps(res["report"]["checks"])[:2000]
    design = json.loads((tmp_path / "netlist.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "import_report.json").read_text(encoding="utf-8"))
    assert design == res["design"]
    assert design["libraries"] == {
        "symbols": {"kimport_imported": "libs/kimport_imported.kicad_sym"}}
    assert _comp(design, "U1")["lib_id"] == "kimport_imported:LM358"
    assert _comp(design, "U1")["footprint"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    src = design["source"]
    assert src["kind"] == "kicad" and src["tool"] == "Eeschema 9.0.4"
    # sha256 is the hash of source.path; files: every input, verifiable one by one
    assert src["path"] == str(PRO.resolve())
    assert src["sha256"] == hashlib.sha256(PRO.read_bytes()).hexdigest()
    assert src["files"] == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (SCH, PCB, PRO)}
    assert report["source"] == src
    assert report["libraries"]["footprints"]["extracted"] is False
    assert report["checks"]["netlist_vs_pcb"]["equal"] and report["checks"]["lint"]["ok"]
    assert report["checks"]["library_paths"] == {"too_long": []}
    assert (tmp_path / "libs" / "kimport_imported.kicad_sym").is_file()


def test_convert_writes_into_a_directory_past_max_path(tmp_path: Path) -> None:
    top = tmp_path / ("d" * 40)
    deep = top.joinpath(*["d" * 40] * 6)                  # > 260 characters
    try:
        res = ki.convert(FIX, deep, netlist_xml=str(XML), extract_footprints=False)
        for name in ("netlist.json", "import_report.json", "libs/kimport_imported.kicad_sym"):
            assert ks.io_path(deep / name).is_file(), name
        too_long = res["report"]["checks"]["library_paths"]["too_long"]
        if sys.platform == "win32":                       # KiCad could not open the library
            assert too_long == [os.path.abspath(deep / "libs" / "kimport_imported.kicad_sym")]
            assert not res["ok"]
        else:
            assert too_long == [] and res["ok"]
    finally:
        shutil.rmtree(ks.io_path(top), ignore_errors=True)


def test_cli_prints_a_summary_and_exits_by_the_checks(tmp_path: Path, capsys) -> None:
    rc = ki.main([str(FIX), str(tmp_path), "--netlist-xml", str(XML), "--no-footprints"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["netlist_vs_pcb"].startswith("equivalent")
    assert ki.main([str(tmp_path / "nope"), str(tmp_path)]) == 2
    assert "error" in json.loads(capsys.readouterr().out)

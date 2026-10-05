"""review_sources: the library source of every part (stock | project |
generated | unknown) for the review report (ROADMAP D5), resolved through
libtable's D1 order on tmp libraries. Pure unit tier: no KiCad.
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest

import libtable as lt
import review_report as rr
import review_sources as rs

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

KICAD_EDA_SYM = ('(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "kicad-eda")\n'
                 '\t(generator_version "9.0")\n\t(symbol "LG290P")\n)\n')
EDITOR_SYM = ('(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")\n'
              '  (symbol "R"))\n')
KICAD_EDA_FP = '(footprint "{n}" (version 20241229) (generator "kicad-eda") (layer "F.Cu"))\n'
EDITOR_FP = '(footprint "{n}" (version 20241229) (generator "pcbnew") (layer "F.Cu"))\n'


# --- helpers ----------------------------------------------------------------
def sym_lib(path: Path, text: str = EDITOR_SYM) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fp_lib(path: Path, footprints: dict[str, str]) -> Path:
    """A .pretty with one .kicad_mod per {name: template}."""
    path.mkdir(parents=True, exist_ok=True)
    for name, template in footprints.items():
        (path / f"{name}.kicad_mod").write_text(template.format(n=name), encoding="utf-8")
    return path


def row(name: str, uri: str, tail: str = "") -> str:
    return f'(lib (name "{name}")(type "KiCad")(uri "{uri}")(options "")(descr ""){tail})'


def comp(ref: str, lib_id: str | None, footprint: str | None, **extra) -> dict:
    c = {"ref": ref, "value": ref}
    if lib_id is not None:
        c["lib_id"] = lib_id
    if footprint is not None:
        c["footprint"] = footprint
    return {**c, **extra}


@pytest.fixture
def stock(tmp_path: Path) -> tuple[Path, Path]:
    syms, fps = tmp_path / "stock" / "symbols", tmp_path / "stock" / "footprints"
    sym_lib(syms / "Device.kicad_sym")
    sym_lib(syms / "power.kicad_sym")
    fp_lib(fps / "Resistor_SMD.pretty", {"R_0603_1608Metric": EDITOR_FP})
    fp_lib(fps / "MountingHole.pretty", {"MountingHole_3.2mm_M3": EDITOR_FP})
    return syms, fps


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A netlist dir with a project sym-lib-table / fp-lib-table and an
    authored (kicad-eda) library declared in the netlist block."""
    d = tmp_path / "proj"
    sym_lib(d / "libs" / "Proj.kicad_sym")
    fp_lib(d / "libs" / "Proj.pretty", {"P1": EDITOR_FP})
    sym_lib(d / "libs" / "SPS_parts.kicad_sym", KICAD_EDA_SYM)
    fp_lib(d / "libs" / "SPS_parts.pretty", {"LG290P": KICAD_EDA_FP, "Hand": EDITOR_FP})
    (d / "sym-lib-table").write_text(
        f'(sym_lib_table\n {row("Proj", "${KIPRJMOD}/libs/Proj.kicad_sym")}\n'
        f' {row("Off", "${KIPRJMOD}/libs/Proj.kicad_sym", "(disabled)")}\n)\n',
        encoding="utf-8")
    (d / "fp-lib-table").write_text(
        f'(fp_lib_table\n {row("Proj", "${KIPRJMOD}/libs/Proj.pretty")}\n)\n',
        encoding="utf-8")
    return d


BLOCK = {"symbols": {"SPS_parts": "libs/SPS_parts.kicad_sym"},
         "footprints": {"SPS_parts": "${KIPRJMOD}/libs/SPS_parts.pretty"}}


def sources(design: dict, proj: Path, stock: tuple[Path, Path]) -> dict:
    return rs.library_sources(design, proj, stock[0], stock[1])


# --- each source ------------------------------------------------------------
def test_stock_parts(proj, stock):
    design = {"components": [comp("R1", "Device:R", "Resistor_SMD:R_0603_1608Metric")]}
    assert sources(design, proj, stock) == {"R1": {"symbol": "stock", "footprint": "stock"}}


def test_project_table_parts(proj, stock):
    design = {"components": [comp("U1", "Proj:X", "Proj:P1")]}
    assert sources(design, proj, stock) == {"U1": {"symbol": "project", "footprint": "project"}}


def test_netlist_block_parts_read_as_project(proj, stock):
    """libtable's 'netlist' source is shown as project (review_report.SOURCE_ALIASES)."""
    sym_lib(proj / "libs" / "Blocky.kicad_sym")
    design = {"libraries": {"symbols": {"Blocky": "libs/Blocky.kicad_sym"},
                            "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}},
              "components": [comp("U2", "Blocky:X", "SPS_parts:Hand")]}
    assert sources(design, proj, stock) == {"U2": {"symbol": "project", "footprint": "project"}}
    assert rr.SOURCE_ALIASES[lt.NETLIST] == "project"


def test_generated_parts(proj, stock):
    """(generator "kicad-eda") in the symbol library header, or in the
    part's own .kicad_mod: the footprint check is per part, so a hand-made
    footprint in the same .pretty stays project."""
    design = {"libraries": BLOCK,
              "components": [comp("U3", "SPS_parts:LG290P", "SPS_parts:LG290P"),
                             comp("U4", "SPS_parts:LG290P", "SPS_parts:Hand")]}
    assert sources(design, proj, stock) == {
        "U3": {"symbol": "generated", "footprint": "generated"},
        "U4": {"symbol": "generated", "footprint": "project"}}


def test_generated_wins_over_the_table_a_library_came_from(proj, stock):
    sym_lib(proj / "libs" / "Proj.kicad_sym", KICAD_EDA_SYM)          # authored, then tabled
    fp_lib(proj / "libs" / "Proj.pretty", {"P2": KICAD_EDA_FP})
    design = {"components": [comp("U5", "Proj:X", "Proj:P2")]}
    assert sources(design, proj, stock) == {"U5": {"symbol": "generated",
                                                   "footprint": "generated"}}


def test_unknown_parts(proj, stock):
    design = {"components": [
        comp("U6", "Nowhere:X", "Nowhere:Y"),                         # resolves nowhere
        comp("U7", "Off:X", "Resistor_SMD:R_0603_1608Metric")]}       # disabled project row
    assert sources(design, proj, stock) == {
        "U6": {"symbol": "unknown", "footprint": "unknown"},
        "U7": {"symbol": "unknown", "footprint": "stock"}}


def test_a_declared_library_whose_file_is_missing_is_unknown(proj, stock):
    """The resolver still names a declared library whose file is gone (lint
    reports it); the review cannot vouch for a part from a library that is
    not on disk."""
    design = {"libraries": {"symbols": {"Gone": "libs/Gone.kicad_sym"}},
              "components": [comp("U8", "Gone:X", "Proj:P1")]}
    assert sources(design, proj, stock) == {"U8": {"symbol": "unknown", "footprint": "project"}}


def test_without_stock_dirs_stock_parts_are_unknown(proj):
    design = {"components": [comp("R1", "Device:R", "Resistor_SMD:R_0603_1608Metric")]}
    assert rs.library_sources(design, proj, None, None) == {
        "R1": {"symbol": "unknown", "footprint": "unknown"}}


def test_a_board_only_part_gets_only_a_footprint_entry(proj, stock):
    design = {"components": [comp("H1", None, "MountingHole:MountingHole_3.2mm_M3",
                                  board_only=True)]}
    assert sources(design, proj, stock) == {"H1": {"footprint": "stock"}}


def test_malformed_component_entries(proj, stock):
    design = {"components": [comp("X1", "NoColon", "AlsoNoColon"), "not a part",
                             {"lib_id": "Device:R"}]}
    assert sources(design, proj, stock) == {"X1": {}, "?": {"symbol": "stock"}}
    assert rs.library_sources({"components": None}, proj, *stock) == {}
    assert rs.library_sources({}, proj, *stock) == {}


# --- errors -----------------------------------------------------------------
def test_a_malformed_block_raises_libtable_error(proj, stock):
    with pytest.raises(lt.LibTableError, match="libraries"):
        sources({"libraries": {"symbols": 5}, "components": []}, proj, stock)


def test_a_malformed_project_table_raises_libtable_error(proj, stock):
    (proj / "sym-lib-table").write_text("(sym_lib_table (lib (name x)", encoding="utf-8")
    with pytest.raises(lt.LibTableError):
        sources({"components": [comp("R1", "Device:R", None)]}, proj, stock)


def test_a_design_that_is_not_an_object_raises_libtable_error(proj, stock):
    with pytest.raises(lt.LibTableError):
        rs.library_sources(["R1"], proj, *stock)


# --- contract with the review -----------------------------------------------
def test_output_feeds_render_review_and_never_mutates_the_design(proj, stock, buck_design):
    design = {"libraries": BLOCK, **buck_design}
    design["components"] = [*design["components"],
                            comp("U9", "SPS_parts:LG290P", "SPS_parts:LG290P")]
    before = copy.deepcopy(design)
    got = sources(design, proj, stock)
    assert design == before
    assert all(v in rr.LIBRARY_SOURCES for entry in got.values() for v in entry.values())
    assert got["U9"] == {"symbol": "generated", "footprint": "generated"}
    assert got["C1"] == {"symbol": "stock", "footprint": "unknown"}   # no Capacitor_SMD here
    md = rr.render_review({"netlist": "n.json", "gates": []}, [], design, None, got)
    assert "| U9 |" in md and "generated" in md


def test_vocabulary_matches_the_review():
    assert rs.SOURCES == ("stock", "project", "generated", "unknown")
    assert set(rs.SOURCES) == set(rr.LIBRARY_SOURCES)


def test_module_is_stdlib_plus_libtable_only():
    tree = ast.parse((TOOLS / "review_sources.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - {"libtable"} <= set(sys.stdlib_module_names), imported

"""lint_rules_libs (F2-11): library integrity lint rules LIB-E1..E5, LIB-W1..W3.

A scratch world per test: stock libraries (stock/symbols, stock/footprints)
and a project (proj/) whose netlist ``libraries`` block declares Proj_parts.
Every rule has a case that fires and a clean case; dir mode (no resolver)
runs only LIB-E3 / LIB-E4 against the stock libraries."""
from __future__ import annotations

import ast
import copy
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

import fp_reader
import libtable
import lint_netlist
import lint_rules_libs as libs
import symlib
from fixtures.kicad_install import KICAD
from lint_context import LintContext

MODULE = Path(libs.__file__).resolve()
PROJ_BLOCK = {"symbols": {"Proj_parts": "libs/Proj_parts.kicad_sym"},
              "footprints": {"Proj_parts": "libs/Proj_parts.pretty"}}
COURTYARD = '  (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type solid)) (layer "{}"))\n'


# --- library writers ----------------------------------------------------------------
def _pin(etype: str, x: float, y: float, name: str, number: str, hidden: bool = False) -> str:
    hide = " (hide yes)" if hidden else ""
    return (f'      (pin {etype} line (at {x} {y} 0) (length 2.54){hide} (name "{name}") '
            f'(number "{number}"))\n')


def _symbol(name: str, units: dict[str, list[str]]) -> str:
    """One symbol; ``units`` maps a sub-symbol suffix ('1_1', '2_1', '1_2') to pins."""
    subs = "".join(f'    (symbol "{name}_{suffix}"\n{"".join(pins)}    )\n'
                   for suffix, pins in units.items())
    return (f'  (symbol "{name}" (in_bom yes) (on_board yes)\n'
            f'    (property "Reference" "U" (at 0 0 0))\n{subs}  )\n')


def _sym_lib(path: Path, *symbols: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('(kicad_symbol_lib (version 20241209) (generator "test")\n'
                    + "".join(symbols) + ")\n", encoding="utf-8")
    return path


def _footprint(pretty: Path, name: str, pads: Iterable[str], courtyard: str | None = "F.CrtYd",
               extra: str = "") -> Path:
    pretty.mkdir(parents=True, exist_ok=True)
    body = "".join(f'  (pad "{p}" smd rect (at {i} 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask"))\n'
                   for i, p in enumerate(pads))
    crt = COURTYARD.format(courtyard) if courtyard else ""
    path = pretty / f"{name}.kicad_mod"
    path.write_text(f'(footprint "{name}" (version 20241229) (generator "test") (layer "F.Cu")\n'
                    f'  (attr smd)\n{crt}{extra}{body})\n', encoding="utf-8")
    return path


STACK = _symbol("Stack", {"1_1": [_pin("power_in", -7.62, 2.54, "VCC", "1"),
                                  _pin("power_in", 0, -7.62, "GND", "2"),
                                  _pin("passive", 0, -7.62, "GND", "3", hidden=True),
                                  _pin("output", 7.62, 0, "OUT", "4")]})
RES = _symbol("R", {"1_1": [_pin("passive", 0, 2.54, "~", "1"),
                            _pin("passive", 0, -2.54, "~", "2")]})
# the same spot in another unit or in the alternate (De Morgan) body style is no stack
SPLIT = _symbol("Split", {"1_1": [_pin("input", 0, 0, "A", "1"), _pin("input", 0, 5, "B", "2")],
                          "2_1": [_pin("input", 0, 0, "C", "3")],
                          "1_2": [_pin("input", 9, 9, "A", "1"), _pin("input", 9, 9, "B", "2")]})
MOD = _symbol("Mod", {"1_1": [_pin("input", -5, 0, "IN", "1"), _pin("output", 5, 0, "OUT", "2"),
                              _pin("power_in", 0, -5, "GND", "3")]})
PWR = _symbol("PWR_FLAG", {"0_0": [_pin("power_out", 0, 0, "~", "1")]})


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """stock/{symbols,footprints} and proj/libs/Proj_parts.{kicad_sym,pretty}."""
    syms, fps = tmp_path / "stock" / "symbols", tmp_path / "stock" / "footprints"
    _sym_lib(syms / "power.kicad_sym", PWR)
    _sym_lib(syms / "Dev.kicad_sym", STACK, RES, SPLIT)
    _footprint(fps / "Dev.pretty", "R_0603", ["1", "2"])
    _footprint(fps / "Dev.pretty", "SO4", ["1", "2", "3", "4", ""], courtyard=None)
    _footprint(fps / "Dev.pretty", "SO3", ["1", "2", "3"])
    proj = tmp_path / "proj"
    _sym_lib(proj / "libs" / "Proj_parts.kicad_sym", MOD)
    pretty = proj / "libs" / "Proj_parts.pretty"
    _footprint(pretty, "Crt", ["1", "2", "3"], courtyard="B.CrtYd")
    _footprint(pretty, "NoCrt", ["1", "2", "3"], courtyard=None)
    _footprint(pretty, "Model", ["1", "2", "3"],
               extra='  (model "${KIPRJMOD}/mod.step" (offset (xyz 0 0 0)))\n')
    _footprint(pretty, "Prop", ["1", "2", "3"], extra='  (property "Height" "2.8")\n')
    return {"syms": syms, "fps": fps, "proj": proj}


def _design() -> dict:
    """Clean in full mode: every rule finds nothing."""
    return {"name": "t", "libraries": copy.deepcopy(PROJ_BLOCK),
            "components": [
                {"ref": "R1", "lib_id": "Dev:R", "footprint": "Dev:R_0603"},
                {"ref": "U1", "lib_id": "Dev:Stack", "footprint": "Dev:SO4"},
                {"ref": "M1", "lib_id": "Proj_parts:Mod", "footprint": "Proj_parts:Crt"}],
            "nets": {"VCC": [["U1", "1"], ["R1", "1"], ["M1", "1"]],
                     "GND": [["U1", "2"], ["U1", "3"], ["R1", "2"], ["M1", "3"]],
                     "OUT": [["U1", "4"], ["M1", "2"]]},
            "no_connect": []}


def _full(world: dict, design: dict, **kw) -> LintContext:
    resolver = libtable.LibraryResolver.for_design(world["proj"], design, world["syms"],
                                                   world["fps"])
    return LintContext(symbols=resolver, resolver=resolver, netlist_dir=world["proj"], **kw)


def _run(world: dict, design: dict, **kw) -> tuple[list[str], list[str]]:
    return libs.check(design, _full(world, design, **kw))


def _codes(messages: list[str]) -> list[str]:
    return [m.split(" ", 1)[0] for m in messages]


def _part(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


# --- the clean design, the module contract ------------------------------------------
def test_clean_design_finds_nothing_in_full_mode(world):
    assert _run(world, _design()) == ([], [])


def test_empty_context_and_empty_design_find_nothing(world):
    assert libs.check({}, LintContext()) == ([], [])
    assert libs.check(_design(), LintContext()) == ([], [])
    assert _run(world, {"libraries": PROJ_BLOCK}) == ([], [])


def test_registered_with_lint_netlist_at_order_10_after_the_core_rules(world):
    rule = {r.name: r for r in lint_netlist.rule_modules()}["lint_rules_libs"]
    assert (rule.order, rule.problem, rule.check) == (10, "", libs.check) and libs.ORDER == 10
    design = _design()
    design["nets"]["SOLO"] = [["U1", "9"]]                   # core E3 + LIB-E4
    rep = lint_netlist.lint(design, ctx=_full(world, design))
    assert rep["ok"] is False
    assert [e.split(" ", 1)[0] for e in rep["errors"]] == ["E3", "LIB-E4"]


def test_module_size_function_size_and_imports():
    text = MODULE.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 400
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno + 1 < 50, node.name
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names} | \
        {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert top - {"__future__", "collections", "collections.abc", "dataclasses", "os",
                  "pathlib", "re", "sys", "types", "typing"} == \
        {"fp_reader", "libtable", "lint_context"}                # stdlib-only siblings


# --- LIB-E1 / LIB-E2 --------------------------------------------------------------------
def test_e1_library_error(world):
    ctx = LintContext(library_error="libraries.symbols must be an object")
    assert libs.check(_design(), ctx) == (
        ["LIB-E1 the project libraries did not load: libraries.symbols must be an object"], [])
    assert _run(world, _design(), library_error="")[0] == []


def test_e1_silences_dir_mode_library_lookups_it_explains(world):
    ctx = LintContext(symbols=str(world["syms"]), library_error="bad block")
    errors, _ = libs.check(_design(), ctx)                  # Proj_parts is not in stock
    assert errors == ["LIB-E1 the project libraries did not load: bad block"]


def test_e2_unknown_and_disabled_nicknames(world):
    design = _design()
    design["components"].append({"ref": "X1", "lib_id": "Nope:X", "footprint": "Gone:Y"})
    errors, _ = _run(world, design)
    assert _codes(errors) == ["LIB-E2", "LIB-E2"]           # never an E3 for the same nick
    assert "symbol library 'Nope'" in errors[0] and "footprint library 'Gone'" in errors[1]
    (world["proj"] / "sym-lib-table").write_text(
        '(sym_lib_table (version 7)\n  (lib (name "Off")(type "KiCad")(uri "x.kicad_sym")'
        '(options "")(descr "")(disabled))\n)\n', encoding="utf-8")
    design = _design()
    design["components"].append({"ref": "X1", "lib_id": "Off:X", "footprint": "Dev:R_0603"})
    errors, _ = _run(world, design)
    assert len(errors) == 1 and errors[0].startswith("LIB-E2 ") and "disabled" in errors[0]


def test_e2_missing_declared_file_and_clean(world):
    design = _design()
    design["libraries"]["symbols"]["Proj_parts"] = "libs/Moved.kicad_sym"
    errors, warnings = _run(world, design)
    assert _codes(errors) == ["LIB-E2"] and "does not exist" in errors[0]
    assert warnings == []                                   # M1's symbol is not judged
    assert _run(world, _design())[0] == []


# --- LIB-E3 ---------------------------------------------------------------------------------
def test_e3_symbol_and_footprint_names_missing_from_their_libraries(world):
    design = _design()
    _part(design, "U1")["lib_id"] = "Dev:Missing"
    design["components"].append({"ref": "R2", "lib_id": "Dev:R", "footprint": "Dev:R_9999"})
    design["components"].append({"ref": "R3", "lib_id": "Dev:R", "footprint": "Dev:R_9999"})
    design["nets"]["VCC"] += [["R2", "1"], ["R3", "1"]]
    errors, _ = _run(world, design)
    assert errors == [
        "LIB-E3 symbol Dev:Missing (U1): symbol Dev:Missing not found in Dev.kicad_sym",
        f"LIB-E3 footprint Dev:R_9999 (R2, R3): R_9999.kicad_mod not found in "
        f"{world['fps'] / 'Dev.pretty'}"]


def test_e3_unreadable_libraries(world):
    _sym_lib(world["syms"] / "Bad.kicad_sym").write_text("(kicad_symbol_lib (", encoding="utf-8")
    (world["fps"] / "Dev.pretty" / "Junk.kicad_mod").write_text("(junk)", encoding="utf-8")
    design = _design()
    design["components"].append({"ref": "X1", "lib_id": "Bad:X", "footprint": "Dev:Junk"})
    errors, _ = _run(world, design)
    assert _codes(errors) == ["LIB-E3", "LIB-E3"]
    assert errors[0].startswith("LIB-E3 symbol Bad:X (X1): cannot parse")
    assert errors[1].startswith("LIB-E3 footprint Dev:Junk (X1): not a footprint")


def test_e3_board_only_parts_need_no_symbol(world):
    design = _design()
    design["components"].append({"ref": "H1", "footprint": "Dev:R_0603", "board_only": True,
                                 "lib_id": "Dev:NoSuchSymbol"})
    assert _run(world, design) == ([], [])


def test_e3_a_declared_library_that_is_not_a_file_is_e2_alone(world):
    design = _design()
    design["libraries"]["symbols"]["Proj_parts"] = "libs/Proj_parts.pretty"   # a directory
    errors, _ = _run(world, design)
    assert _codes(errors) == ["LIB-E2"] and "is not a .kicad_sym" in errors[0]


def test_e3_the_power_flag_the_schematic_build_always_embeds(world):
    _sym_lib(world["syms"] / "power.kicad_sym", RES)        # power without PWR_FLAG
    expected = ["LIB-E3 symbol power:PWR_FLAG (power flags): symbol power:PWR_FLAG not found "
                "in power.kicad_sym"]
    assert _run(world, _design())[0] == expected
    dir_design = _design()
    del dir_design["libraries"], dir_design["components"][2]
    assert libs.check(dir_design, LintContext(symbols=str(world["syms"])))[0] == expected


def test_e3_the_mounting_hole_footprint_the_board_build_loads(world):
    design = {**_design(), "mounting_holes": {"footprint": "Dev:NoHole", "inset": 3}}
    hole_error = (f"LIB-E3 footprint Dev:NoHole (mounting_holes): NoHole.kicad_mod not found "
                  f"in {world['fps'] / 'Dev.pretty'}")
    assert _run(world, design)[0] == [hole_error]
    design["components"].append({"ref": "H9", "footprint": "Dev:NoHole", "board_only": True})
    assert _run(world, design)[0] == [hole_error.replace("(mounting_holes)",
                                                         "(H9, mounting_holes)")]
    for holes in ({"footprint": "Dev:R_0603"}, False, {}):  # found, or no holes at all
        assert _run(world, {**_design(), "mounting_holes": holes}) == ([], [])
    errors, _ = _run(world, {**_design(), "mounting_holes": True})   # default footprint
    assert _codes(errors) == ["LIB-E2"] and "'MountingHole'" in errors[0]
    _footprint(world["fps"] / "MountingHole.pretty", "MountingHole_3.2mm_M3", ["1"])
    assert _run(world, {**_design(), "mounting_holes": True}) == ([], [])


# --- LIB-E4 ---------------------------------------------------------------------------------
def test_e4_netted_keys_must_be_footprint_pads(world):
    design = _design()
    design["nets"]["SOLO"] = [["U1", "9"], ["R1", "VCC"], ["U1", "9"]]
    design["nets"]["IN"] = [["U1", "VCC"], ["M1", "IN"]]
    errors, _ = _run(world, design)
    assert errors == [
        "LIB-E4 U1.9 (net 'SOLO') is not a pad of Dev:SO4",
        "LIB-E4 R1.VCC (net 'SOLO') is not a pad of Dev:R_0603",
        "LIB-E4 U1.VCC (net 'IN') is not a pad of Dev:SO4 (pin name 'VCC' is pin 1; "
        "the board build matches pad numbers)",
        "LIB-E4 M1.IN (net 'IN') is not a pad of Proj_parts:Crt (pin name 'IN' is pin 1; "
        "the board build matches pad numbers)"]


def test_e4_no_connect_keys_may_be_symbol_pins_without_pads(world):
    design = _design()
    _part(design, "U1")["footprint"] = "Dev:SO3"            # pin 4 has no pad
    design["nets"].pop("OUT")
    design["no_connect"] = [["U1", "4"], ["U1", "OUT"], ["M1", "2"]]
    errors, warnings = _run(world, design)
    assert errors == [] and _codes(warnings) == ["LIB-W1"]
    design["no_connect"].append(["U1", "7"])
    assert _run(world, design)[0] == [
        "LIB-E4 U1.7 (no_connect) is neither a pad of Dev:SO3 nor a pin of Dev:Stack"]


def test_e4_unknown_refs_and_unjudgeable_parts_are_left_to_others(world):
    design = _design()
    design["nets"]["X"] = [["Q9", "1"], ["U1", "1"]]        # core E4 names Q9
    _part(design, "M1")["footprint"] = "Proj_parts:Gone"    # E3; its pads are unknown
    design["no_connect"] = [["M1", "7"]]
    errors, _ = _run(world, design)
    assert _codes(errors) == ["LIB-E3"]


# --- LIB-E5 ---------------------------------------------------------------------------------
def test_e5_stacked_pins_on_different_nets(world):
    design = _design()
    design["nets"]["GND"] = [["U1", "2"], ["R1", "2"], ["M1", "3"]]
    design["nets"]["AGND"] = [["U1", "3"], ["R1", "1"]]
    design["nets"]["VCC"] = [["U1", "1"], ["M1", "1"]]
    errors, _ = _run(world, design)
    assert errors == ["LIB-E5 U1 (Dev:Stack): stacked pins 2, 3 sit on different nets "
                      "(2 -> GND, 3 -> AGND); KiCad joins stacked pins into one net"]


def test_e5_a_no_connect_stacked_pin_beside_a_netted_one(world):
    design = _design()
    design["nets"]["GND"] = [["U1", "2"], ["R1", "2"], ["M1", "3"]]
    design["no_connect"] = [["U1", "3"]]
    errors, _ = _run(world, design)
    assert errors == ["LIB-E5 U1 (Dev:Stack): stacked pins 2, 3 sit on different nets "
                      "(2 -> GND, 3 -> no_connect); KiCad joins stacked pins into one net"]


def test_e5_clean_cases(world):
    design = _design()                                       # 2 and 3 both on GND
    assert _run(world, design)[0] == []
    design["nets"]["GND"] = [["U1", "2"], ["R1", "2"], ["M1", "3"]]   # 3 left open: fine
    assert _run(world, design)[0] == []
    design["nets"]["GND"] = [["U1", "GND"], ["R1", "2"], ["M1", "3"]]  # name key -> pin 2
    design["nets"]["AGND"] = [["U1", "3"], ["R1", "1"]]
    errors, _ = _run(world, design)
    assert _codes(errors) == ["LIB-E4", "LIB-E5"]


def test_e5_ignores_other_units_and_the_alternate_body_style(world):
    design = _design()
    design["components"].append({"ref": "U2", "lib_id": "Dev:Split", "footprint": "Dev:SO3"})
    design["nets"]["A"] = [["U2", "1"], ["R1", "1"]]
    design["nets"]["B"] = [["U2", "2"], ["R1", "2"]]
    design["nets"]["C"] = [["U2", "3"], ["M1", "1"]]
    errors, warnings = _run(world, design)
    assert not [e for e in errors if e.startswith("LIB-E5")] and warnings == []


# --- LIB-W1 -----------------------------------------------------------------------------------
def test_w1_symbol_pins_without_pads_once_per_symbol_and_footprint(world):
    design = _design()
    _part(design, "U1")["footprint"] = "Dev:SO3"
    design["components"].append({"ref": "U2", "lib_id": "Dev:Stack", "footprint": "Dev:SO3"})
    design["nets"]["OUT"] = [["M1", "2"], ["R1", "1"]]
    design["no_connect"] = [["U1", "4"], ["U2", "4"]]
    design["nets"]["VCC"].append(["U2", "1"])
    design["nets"]["GND"] += [["U2", "2"], ["U2", "3"]]
    assert _run(world, design) == (
        [], ["LIB-W1 Dev:Stack on Dev:SO3 (U1, U2): symbol pin(s) 4 have no pad"])
    assert _run(world, _design())[1] == []


# --- LIB-W2 / LIB-W3 ------------------------------------------------------------------------
def test_w2_project_footprint_without_courtyard(world):
    design = _design()
    _part(design, "M1")["footprint"] = "Proj_parts:NoCrt"
    assert _run(world, design) == (
        [], ["LIB-W2 Proj_parts:NoCrt (M1): project footprint has no F.CrtYd/B.CrtYd "
             "courtyard"])
    assert _run(world, _design())[1] == []                  # B.CrtYd counts; stock SO4 is exempt


ZONES = [{"x": 0, "y": 0, "w": 10, "h": 10, "max_height": 3}]


def test_w3_height_zones_and_a_project_footprint_without_height(world):
    design = {**_design(), "height_zones": ZONES}
    assert _run(world, design) == (
        [], ["LIB-W3 Proj_parts:Crt (M1): the design has height_zones but this project "
             "footprint has no height (no 3D model or Height property); declare \"height\" "
             "on the part"])


@pytest.mark.parametrize("change", [{"footprint": "Proj_parts:Model"},
                                    {"footprint": "Proj_parts:Prop"}, {"height": 2.0}])
def test_w3_a_model_a_height_property_or_a_declared_height(world, change):
    design = {**_design(), "height_zones": ZONES}
    _part(design, "M1").update(change)
    assert _run(world, design) == ([], [])
    assert _run(world, _design()) == ([], [])               # no height_zones: no W3


# --- dir mode (no resolver) ------------------------------------------------------------------
def _dir_design() -> dict:
    """An E5 condition plus an E3 and an E4: dir mode reports only E3 / E4."""
    design = _design()
    del design["libraries"]
    design["components"][2] = {"ref": "M1", "lib_id": "Dev:R", "footprint": "Dev:SO3"}
    design["nets"] = {"VCC": [["U1", "1"], ["R1", "1"], ["M1", "1"]],
                      "GND": [["U1", "2"], ["R1", "2"], ["M1", "2"]],
                      "AGND": [["U1", "3"], ["M1", "3"]], "OUT": [["U1", "4"], ["U1", "9"]]}
    design["components"].append({"ref": "X1", "lib_id": "Dev:Nope", "footprint": "Dev:R_0603"})
    return design


def test_dir_mode_runs_only_e3_and_e4_against_stock(world):
    errors, warnings = libs.check(_dir_design(), LintContext(symbols=str(world["syms"])))
    assert errors == ["LIB-E3 symbol Dev:Nope (X1): symbol Dev:Nope not found in Dev.kicad_sym",
                      "LIB-E4 U1.9 (net 'OUT') is not a pad of Dev:SO4"]
    assert warnings == []


def test_dir_mode_reports_libraries_missing_from_stock(world):
    errors, _ = libs.check(_design(), LintContext(symbols=str(world["syms"])))
    assert errors == [
        f"LIB-E3 symbol Proj_parts:Mod (M1): symbol library not found: 'Proj_parts': "
        f"{world['syms'] / 'Proj_parts.kicad_sym'} does not exist",
        f"LIB-E3 footprint Proj_parts:Crt (M1): footprint library not found: 'Proj_parts': "
        f"{world['fps'] / 'Proj_parts.pretty'} does not exist"]


def test_dir_mode_footprints_dir_option_and_a_missing_one(world, tmp_path):
    other = tmp_path / "other"
    _footprint(other / "Dev.pretty", "SO4", ["1", "2", "3", "4", "9"])
    design = _dir_design()
    ctx = LintContext(symbols=str(world["syms"]), options={"footprints_dir": str(other)})
    errors, _ = libs.check(design, ctx)
    assert [e for e in errors if "U1.9" in e] == []         # pad 9 exists there
    assert [e.split(":", 2)[:2] for e in errors] == [
        ["LIB-E3 footprint Dev", "R_0603 (R1, X1)"], ["LIB-E3 footprint Dev", "SO3 (M1)"],
        ["LIB-E3 symbol Dev", "Nope (X1)"]]
    syms_only = tmp_path / "lonely" / "symbols"             # no sibling footprints/
    _sym_lib(syms_only / "Dev.kicad_sym", STACK, RES)
    _sym_lib(syms_only / "power.kicad_sym", PWR)
    errors, _ = libs.check(_dir_design(), LintContext(symbols=str(syms_only)))
    assert errors == ["LIB-E3 symbol Dev:Nope (X1): symbol Dev:Nope not found in Dev.kicad_sym"]


def test_dir_mode_without_stock_directories_finds_nothing(world, tmp_path):
    ghost = tmp_path / "ghost" / "symbols"                  # neither it nor footprints/ exist
    assert libs.check(_dir_design(), LintContext(symbols=str(ghost))) == ([], [])
    assert libs.check(_dir_design(), LintContext(symbols=ghost)) == ([], [])


def test_dir_mode_through_a_library_map(world):
    design = _design()
    resolver = libtable.LibraryResolver.for_design(world["proj"], design, world["syms"],
                                                   world["fps"])
    design["nets"]["X"] = [["M1", "7"], ["U1", "9"]]
    errors, warnings = libs.check(design, LintContext(symbols=resolver.library_map()))
    assert errors == ["LIB-E4 M1.7 (net 'X') is not a pad of Proj_parts:Crt",
                      "LIB-E4 U1.9 (net 'X') is not a pad of Dev:SO4"]
    assert warnings == []


def test_dir_mode_with_a_resolver_as_the_symbol_source(world):
    design = _design()
    resolver = libtable.LibraryResolver.for_design(world["proj"], design, world["syms"],
                                                   world["fps"])
    design["components"].append({"ref": "X1", "lib_id": "Nope:X", "footprint": "Gone:Y"})
    errors, _ = libs.check(design, LintContext(symbols=resolver))
    assert _codes(errors) == ["LIB-E3", "LIB-E3"]
    assert errors[0].startswith("LIB-E3 symbol Nope:X (X1): symbol library not found: 'Nope': ")
    assert errors[1].startswith("LIB-E3 footprint Gone:Y (X1): footprint library not found: "
                                "'Gone': ")


# --- robustness, caching ------------------------------------------------------------------------
def test_without_symlib_the_footprint_rules_still_run(world, monkeypatch):
    def no_symlib() -> None:
        raise ImportError("No module named 'sexpdata'")

    monkeypatch.setattr(libs, "_import_symlib", no_symlib)
    design = _design()
    design["nets"]["X"] = [["U1", "9"], ["R1", "1"]]
    errors, warnings = _run(world, design)
    assert errors == ["LIB-E4 U1.9 (net 'X') is not a pad of Dev:SO4"]
    assert warnings == ["LIB symbol checks skipped (cannot import symlib: No module named "
                        "'sexpdata')"]


def test_malformed_entries_are_skipped_not_raised(world):
    design = _design()
    design["components"] += ["junk", {"ref": "Z1"}, {"ref": "Z2", "lib_id": "NoColon",
                                                     "footprint": 7}]
    design["nets"]["W"] = [["U1"], "x", ["U1", "1", "extra"]]
    design["no_connect"] = [None, ["Z1", "1"]]
    assert _run(world, design) == ([], [])


def test_each_library_file_is_read_once_per_call(world, monkeypatch):
    reads: list[str] = []
    flats: list[str] = []
    real_parse, real_flatten = fp_reader.parse_footprint, symlib.flatten
    monkeypatch.setattr(fp_reader, "parse_footprint",
                        lambda text: reads.append(text[:40]) or real_parse(text))
    monkeypatch.setattr(symlib, "flatten",
                        lambda cache, src, lib, name: flats.append(name)
                        or real_flatten(cache, src, lib, name))
    design = _design()
    design["components"] += [{"ref": f"R{i}", "lib_id": "Dev:R", "footprint": "Dev:R_0603"}
                             for i in range(2, 30)]
    design["nets"]["VCC"] += [[f"R{i}", "1"] for i in range(2, 30)]
    assert _run(world, design) == ([], [])
    assert len(reads) == 3 and sorted(flats) == ["Mod", "PWR_FLAG", "R", "Stack"]
    _run(world, design)
    assert len(reads) == 6                                   # a new call reads afresh


# --- the shipped examples against a real KiCad install ---------------------------------------
KNOWN_KICAD9_GAPS = {"LIB-E4 J1.SH (net 'GND') is not a pad of "
                     "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12"}


def _stock_version() -> int | None:
    sample = KICAD.root / "share" / "kicad" / "footprints" / "Resistor_SMD.pretty" / \
        "R_0603_1608Metric.kicad_mod"
    return fp_reader.read_footprint(sample).version if sample.is_file() else None


@pytest.mark.skipif(KICAD is None, reason="KiCad libraries not installed")
def test_examples_have_no_library_errors_with_the_installed_libraries(example_netlists):
    share = KICAD.root / "share" / "kicad"
    version = _stock_version()
    if version is None:
        pytest.skip("the KiCad install has no stock footprints")
    allowed = KNOWN_KICAD9_GAPS if version <= 20241229 else set()
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        resolver = libtable.LibraryResolver.for_netlist(path, share / "symbols",
                                                        share / "footprints")
        full = LintContext(symbols=resolver, resolver=resolver, netlist_dir=path.parent)
        dir_mode = LintContext(symbols=str(share / "symbols"), netlist_dir=path.parent)
        for ctx in (full, dir_mode):
            errors, _ = libs.check(design, ctx)
            assert set(errors) <= allowed, (path.parent.name, errors)

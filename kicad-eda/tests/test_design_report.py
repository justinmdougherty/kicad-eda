"""design_report — decoupling / power-tree / floating-pin audit, and the
error reporting that replaced its silent empty-pin-map pass.

Real-library cases run examples/buck_5v3a against the installed KiCad symbols
and skip without one; the synthetic-library and no-symbol cases never skip.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import design_report
from fixtures.kicad_install import KICAD, requires_symbols

TESTS = Path(__file__).resolve().parent
FIXTURE_SYMBOLS = TESTS / "fixtures" / "symbols"
BUCK_LIB_IDS = 7          # unique lib_ids in examples/buck_5v3a


def _without(design: dict, *refs: str) -> dict:
    """A copy of ``design`` with the given parts and their connections removed."""
    drop = set(refs)
    return {**design,
            "components": [c for c in design["components"] if c["ref"] not in drop],
            "nets": {net: [[r, p] for r, p in conns if r not in drop]
                     for net, conns in design["nets"].items()}}


def _repad(design: dict, ref: str, mapping: dict[str, str]) -> dict:
    """A copy of ``design`` with ``ref``'s pad keys renamed (e.g. number -> name)."""
    return {**design,
            "nets": {net: [[r, mapping.get(p, p) if r == ref else p] for r, p in conns]
                     for net, conns in design["nets"].items()}}


def _synthetic_design() -> dict:
    """One decoupled IC from the fixture library plus a part from a library
    that does not exist."""
    return {"name": "mixed", "title": "Mixed",
            "components": [
                {"ref": "U1", "lib_id": "TestLib:DerivedAmp", "value": "amp", "footprint": "F"},
                {"ref": "U2", "lib_id": "Nope:Chip", "value": "?", "footprint": "F"},
                {"ref": "C1", "lib_id": "TestLib:PosFileR", "value": "100n", "footprint": "F"}],
            "nets": {"VCC": [["U1", "8"], ["C1", "1"], ["U2", "1"]],
                     "GND": [["U1", "4"], ["C1", "2"], ["U2", "2"]],
                     "LOOP": [["U1", "1"], ["U1", "2"]]},
            "no_connect": [["U1", "3"], ["U1", "5"], ["U1", "6"], ["U1", "7"]]}


# --- decoupling on the real buck example -------------------------------------

@requires_symbols
def test_buck_is_fully_decoupled(buck_design: dict):
    rep = design_report.analyze(buck_design, str(KICAD.symbols))
    assert rep["errors"] == []
    assert rep["symbols_available"] is True
    # LM2596S-5 power_in pins: 1 (VIN) and 3 (GND); FB (pin 4) is an input
    assert rep["decoupling"] == {
        "checked": [{"ref": "U1", "rails": ["GND", "VIN"], "decoupled": ["GND", "VIN"]}],
        "uncovered": []}
    assert rep["flags"]["floating_pins"] == []


@requires_symbols
def test_bulk_cap_alone_still_counts_as_decoupling(buck_design: dict):
    # C2 (100nF) gone, C1 (680uF bulk) still on VIN: the rail is covered
    rep = design_report.analyze(_without(buck_design, "C2"), str(KICAD.symbols))
    assert rep["decoupling"]["uncovered"] == []


@requires_symbols
def test_stripping_every_cap_on_a_rail_uncovers_it(buck_design: dict):
    rep = design_report.analyze(_without(buck_design, "C1", "C2"), str(KICAD.symbols))
    assert rep["decoupling"]["uncovered"] == [{"ref": "U1", "rails_without_cap": ["VIN"]}]
    assert rep["decoupling"]["checked"] == [
        {"ref": "U1", "rails": ["GND", "VIN"], "decoupled": ["GND"]}]
    assert "[WARN] **U1**: rail(s) without a bypass cap: VIN" in design_report.to_markdown(rep)


@requires_symbols
def test_gnd_is_never_reported_as_uncovered(buck_design: dict):
    rep = design_report.analyze(_without(buck_design, "C1", "C2", "C3", "C4"), str(KICAD.symbols))
    assert rep["decoupling"]["uncovered"] == [{"ref": "U1", "rails_without_cap": ["VIN"]}]


@requires_symbols
def test_pin_name_keys_resolve_through_aliases(buck_design: dict):
    keyed_by_name = _repad(buck_design, "U1", {"1": "VIN", "3": "GND", "2": "OUT"})
    rep = design_report.analyze(keyed_by_name, str(KICAD.symbols))
    assert rep["decoupling"]["uncovered"] == []
    assert rep["flags"]["floating_pins"] == []
    assert rep["power_tree"]["SW"]["drivers"] == ["U1.OUT"]


@requires_symbols
def test_floating_pin_flagged_unless_declared_no_connect(buck_design: dict):
    open_gnd = {**buck_design,
                "nets": {**buck_design["nets"],
                         "GND": [c for c in buck_design["nets"]["GND"] if c != ["U1", "5"]]}}
    assert design_report.analyze(open_gnd, str(KICAD.symbols))["flags"]["floating_pins"] == ["U1.5"]
    declared = {**open_gnd, "no_connect": [["U1", "5"]]}
    assert design_report.analyze(declared, str(KICAD.symbols))["flags"]["floating_pins"] == []


@requires_symbols
def test_power_tree_drivers_loads_and_specs(buck_design: dict):
    tree = design_report.analyze(buck_design, str(KICAD.symbols))["power_tree"]
    assert list(tree) == ["VIN_RAW", "VIN", "SW", "V5", "GND"]
    assert tree["SW"] == {"drivers": ["U1.2"], "loads": 2,
                          "spec": {"voltage": 12.0, "current": 3.0}}
    assert tree["VIN"] == {"drivers": ["(passive/flagged source)"], "loads": 4,
                           "spec": {"voltage": 12.0, "current": 1.5}}


@requires_symbols
def test_rails_missing_testpoint(buck_design: dict):
    rep = design_report.analyze(buck_design, str(KICAD.symbols))
    assert rep["flags"]["rails_missing_testpoint"] == ["SW", "V5", "VIN", "VIN_RAW"]   # GND is 0 V
    with_tp = {**buck_design,
               "components": buck_design["components"] + [
                   {"ref": "TP1", "lib_id": "Connector:TestPoint", "value": "TP",
                    "footprint": "F"}],
               "nets": {**buck_design["nets"], "V5": buck_design["nets"]["V5"] + [["TP1", "1"]]}}
    rep = design_report.analyze(with_tp, str(KICAD.symbols))
    assert rep["errors"] == []
    assert rep["flags"]["rails_missing_testpoint"] == ["SW", "VIN", "VIN_RAW"]


# --- symbol-independent parts ------------------------------------------------

def test_dfa_counts_need_no_symbols(buck_design: dict):
    rep = design_report.analyze(buck_design, "")
    assert rep["title"] == "LM2596 12V->5V 3A Buck Regulator"
    assert rep["dfa"] == {"placements": 10, "unique_line_items": 9,     # C2 == C4
                          "by_prefix": {"C": 4, "D": 1, "F": 1, "J": 2, "L": 1, "U": 1},
                          "array_candidates": []}
    four_r = {**buck_design, "components": buck_design["components"] + [
        {"ref": f"R{i}", "lib_id": "Device:R", "value": "10k", "footprint": "R_0603"}
        for i in range(1, 5)]}
    assert design_report.analyze(four_r, "")["dfa"]["array_candidates"] == [
        {"value": "10k", "footprint": "R_0603", "count": 4, "refs": ["R1", "R2", "R3", "R4"]}]


def test_empty_symbols_dir_is_a_config_choice_not_an_error(buck_design: dict):
    rep = design_report.analyze(buck_design, "")
    assert rep["errors"] == []
    assert rep["symbols_available"] is False
    assert rep["decoupling"] == {"checked": [], "uncovered": []}
    assert rep["flags"]["floating_pins"] == []
    assert list(rep["power_tree"]) == list(buck_design["net_specs"])   # rails via net_specs
    md = design_report.to_markdown(rep)
    assert "## Errors" not in md
    assert "- (no symbol pin types available — skipped)" in md


# --- error reporting ---------------------------------------------------------

def test_bogus_symbols_dir_reports_errors_instead_of_passing(buck_design: dict, tmp_path: Path):
    rep = design_report.analyze(buck_design, str(tmp_path / "nope"))
    assert len(rep["errors"]) == BUCK_LIB_IDS
    assert all("symbol library not found" in e for e in rep["errors"])
    assert any(e.startswith("pin types unavailable for Device:C (C2, C4): ") for e in rep["errors"])
    assert any(e.startswith("pin types unavailable for Regulator_Switching:LM2596S-5 (U1): ")
               for e in rep["errors"])
    # nothing was checked -- the report must say so rather than look clean
    assert rep["symbols_available"] is False
    assert rep["decoupling"]["checked"] == []


def test_to_markdown_puts_errors_heading_first(buck_design: dict, tmp_path: Path):
    rep = design_report.analyze(buck_design, str(tmp_path / "nope"))
    lines = design_report.to_markdown(rep).splitlines()
    assert lines[0] == "# Design Report — LM2596 12V->5V 3A Buck Regulator"
    assert lines[2] == "## Errors"
    assert lines[3].startswith("The following symbols could not be read;")
    assert sum(1 for line in lines
               if line.startswith("- pin types unavailable for ")) == BUCK_LIB_IDS
    assert lines.index("## Errors") < lines.index("## DFA / BOM")


def test_partial_symbol_failure_keeps_other_parts_covered():
    rep = design_report.analyze(_synthetic_design(), str(FIXTURE_SYMBOLS))
    assert len(rep["errors"]) == 1
    assert rep["errors"][0].startswith("pin types unavailable for Nope:Chip (U2): SymbolLibError: ")
    assert rep["symbols_available"] is True
    assert rep["decoupling"] == {
        "checked": [{"ref": "U1", "rails": ["GND", "VCC"], "decoupled": ["GND", "VCC"]}],
        "uncovered": []}
    assert rep["flags"]["floating_pins"] == []          # U2 has no pin types -> not judged
    assert "## Errors" in design_report.to_markdown(rep)


def test_pin_name_alias_on_synthetic_library():
    rep = design_report.analyze(_repad(_synthetic_design(), "U1", {"8": "V+", "4": "V-"}),
                                str(FIXTURE_SYMBOLS))
    assert rep["decoupling"]["uncovered"] == []
    assert rep["flags"]["floating_pins"] == []


def test_component_without_lib_id_is_reported_not_raised():
    design = {**_synthetic_design(),
              "components": _synthetic_design()["components"] + [{"ref": "X1", "value": "?"}]}
    rep = design_report.analyze(design, str(FIXTURE_SYMBOLS))
    assert any(e.startswith("pin types unavailable for X1: component has no lib_id")
               for e in rep["errors"])
    assert len(rep["errors"]) == 2


def test_components_not_a_list_of_objects_raises():
    with pytest.raises(design_report.DesignReportError, match="must be a list of objects"):
        design_report.analyze({"components": ["U1"], "nets": {}}, str(FIXTURE_SYMBOLS))


def test_main_exit_code_follows_errors(buck_design: dict, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_design), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["design_report.py", str(netlist), "--json"])
    assert design_report.main() == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    monkeypatch.setattr("sys.argv",
                        ["design_report.py", str(netlist), str(tmp_path / "nope"), "--json"])
    assert design_report.main() == 1
    assert len(json.loads(capsys.readouterr().out)["errors"]) == BUCK_LIB_IDS
    monkeypatch.setattr("sys.argv", ["design_report.py"])
    assert design_report.main() == 2


# --- flags.floating_detail (napkin floating-pin policy input, D5) -------------------

MCU_SPEC = {"name": "NapkinMCU", "reference": "U", "pins": [
    {"number": "1", "name": "VDD", "type": "power_in"},
    {"number": "2", "name": "GND", "type": "power_in"},
    {"number": "3", "name": "PA0", "type": "output"},
    {"number": "4", "name": "PA1", "type": "input"},
    {"number": "5", "name": "NC", "type": "passive"},
    {"number": "6", "name": "GND", "type": "power_in"},     # stacked on pin 2: passive
    {"number": "7", "name": "GND", "type": "power_in"},
    {"number": "8", "name": "VDD", "type": "power_in"}]}    # stacked on pin 1
CAP_SPEC = {"name": "Cap", "reference": "C", "pins": [
    {"number": "1", "name": "~", "type": "passive"},
    {"number": "2", "name": "~", "type": "passive"}]}
MCU_FP = "Package_QFP:LQFP-8"


@pytest.fixture
def napkin_lib(tmp_path: Path) -> Path:
    """A symbol directory holding NapkinLib (an MCU with D1 stacked supply
    pins, a capacitor) and LG290P, authored with sym_author."""
    import sym_author
    lib = tmp_path / "NapkinLib.kicad_sym"
    sym_author.author_symbol(MCU_SPEC, lib)
    sym_author.author_symbol(CAP_SPEC, lib)
    sym_author.author_symbol(sym_author.lg290p_spec(), lib)
    return tmp_path


def _mcu_design(**nets: list) -> dict:
    base = {"3V3": [["U1", "1"], ["C1", "1"]], "GND": [["U1", "2"], ["C1", "2"]]}
    return {"name": "mcu", "components": [
                {"ref": "U1", "lib_id": "NapkinLib:NapkinMCU", "value": "MCU",
                 "footprint": MCU_FP},
                {"ref": "C1", "lib_id": "NapkinLib:Cap", "value": "100n", "footprint": "C"}],
            "nets": {**base, **nets}}


def _detail(rep: dict) -> dict:
    return {d["pin"]: d for d in rep["flags"]["floating_detail"]}


def test_floating_detail_names_types_and_same_name_nets(napkin_lib: Path):
    rep = design_report.analyze(_mcu_design(), str(napkin_lib))
    assert rep["errors"] == []
    pins = ["U1.3", "U1.4", "U1.5", "U1.6", "U1.7", "U1.8"]
    assert rep["flags"]["floating_pins"] == pins
    assert [d["pin"] for d in rep["flags"]["floating_detail"]] == pins
    detail = _detail(rep)
    assert detail["U1.3"] == {"pin": "U1.3", "ref": "U1", "pad": "3", "type": "output",
                              "name": "PA0", "same_name_net": None,
                              "lib_id": "NapkinLib:NapkinMCU", "footprint": MCU_FP}
    assert (detail["U1.4"]["type"], detail["U1.4"]["name"]) == ("input", "PA1")
    assert (detail["U1.5"]["name"], detail["U1.5"]["same_name_net"]) == ("NC", None)
    # D1 stacked pins: hidden, typed passive, named like the visible pin
    for pin, name, net in (("U1.6", "GND", "GND"), ("U1.7", "GND", "GND"),
                           ("U1.8", "VDD", "3V3")):
        assert detail[pin]["type"] == "passive"
        assert (detail[pin]["name"], detail[pin]["same_name_net"]) == (name, net)


def test_floating_detail_follows_pin_name_keyed_nets(napkin_lib: Path):
    design = _mcu_design(GND=[["U1", "GND"], ["C1", "2"]], SIG=[["U1", "PA0"], ["U1", "PA1"]])
    rep = design_report.analyze(design, str(napkin_lib))
    assert "U1.3" not in rep["flags"]["floating_pins"]           # PA0 netted by name
    assert _detail(rep)["U1.6"]["same_name_net"] == "GND"


def test_same_name_net_is_none_when_same_name_pins_disagree(napkin_lib: Path):
    rep = design_report.analyze(_mcu_design(AGND=[["U1", "6"]]), str(napkin_lib))
    assert _detail(rep)["U1.7"]["same_name_net"] is None
    assert "U1.6" not in _detail(rep)


def test_floating_detail_is_empty_without_symbols(buck_design: dict):
    rep = design_report.analyze(buck_design, "")
    assert rep["flags"]["floating_detail"] == []


def test_lg290p_stacked_gnd_pads_all_join_gnd_under_the_napkin_policy(napkin_lib: Path):
    import napkin_policy
    design = {"name": "gnss", "components": [
        {"ref": "U1", "lib_id": "NapkinLib:LG290P", "value": "LG290P", "footprint": "SPS:LG290P"}],
        "nets": {"GND": [["U1", "10"]], "VCC": [["U1", "23"]]}}
    rep = design_report.analyze(design, str(napkin_lib))
    gnd = [d for d in rep["flags"]["floating_detail"] if d["name"] == "GND"]
    assert len(gnd) == 58                          # 59 GND pads, pad 10 netted
    assert all(d["same_name_net"] == "GND" for d in gnd)
    comp = design["components"][0]
    assert {napkin_policy.floating_pin_action(d, comp) for d in gnd} == {
        ("net_same_name_pin", "GND")}
    # RESERVED pins are typed no_connect in the symbol: never floating
    assert not any(d["name"] == "RESERVED" for d in rep["flags"]["floating_detail"])


# --- symbol sources: a directory, a LibraryResolver or a library map (F2-10, D1) ------

PROJECT_NICK = "SPS_parts"
PROJECT_LIB = f"libs/{PROJECT_NICK}.kicad_sym"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project directory whose libs/SPS_parts.kicad_sym holds NapkinMCU, Cap
    and LG290P (sym_author); TestLib is a stock library (FIXTURE_SYMBOLS)."""
    import sym_author
    lib = tmp_path / PROJECT_LIB
    lib.parent.mkdir()
    for spec in (MCU_SPEC, CAP_SPEC, sym_author.lg290p_spec()):
        sym_author.author_symbol(spec, lib)
    return tmp_path


def _project_design(nick: str = PROJECT_NICK, block: bool = True) -> dict:
    """U1/C1 from the project library, R1 from the stock TestLib."""
    design = {"name": "proj", "components": [
        {"ref": "U1", "lib_id": f"{nick}:NapkinMCU", "value": "MCU", "footprint": MCU_FP},
        {"ref": "C1", "lib_id": f"{nick}:Cap", "value": "100n", "footprint": "C"},
        {"ref": "R1", "lib_id": "TestLib:PosFileR", "value": "10k", "footprint": "R"}],
        "nets": {"3V3": [["U1", "1"], ["C1", "1"], ["R1", "1"]],
                 "GND": [["U1", "2"], ["C1", "2"], ["R1", "2"]]}}
    if block:
        design["libraries"] = {"symbols": {nick: PROJECT_LIB}}
    return design


def _resolver(project: Path, design: dict, stock: Path | None = FIXTURE_SYMBOLS):
    import libtable
    return libtable.LibraryResolver.for_design(project, design, stock, None)


def _write_netlist(project: Path, design: dict) -> Path:
    path = project / "netlist.json"
    path.write_text(json.dumps(design), encoding="utf-8")
    return path


PROJECT_FLOATING = ["U1.3", "U1.4", "U1.5", "U1.6", "U1.7", "U1.8"]


def test_resolver_reads_project_parts_and_stock_parts(project: Path):
    design = _project_design()
    rep = design_report.analyze(design, _resolver(project, design))
    assert rep["errors"] == [] and rep["symbols_available"] is True
    assert rep["decoupling"]["checked"] == [
        {"ref": "U1", "rails": ["3V3", "GND"], "decoupled": ["3V3", "GND"]}]
    assert rep["flags"]["floating_pins"] == PROJECT_FLOATING
    # F5-08's floating_detail keeps working for project-local parts
    detail = _detail(rep)
    assert detail["U1.3"] == {"pin": "U1.3", "ref": "U1", "pad": "3", "type": "output",
                              "name": "PA0", "same_name_net": None,
                              "lib_id": f"{PROJECT_NICK}:NapkinMCU", "footprint": MCU_FP}
    assert (detail["U1.8"]["name"], detail["U1.8"]["same_name_net"]) == ("VDD", "3V3")


def test_library_map_and_its_json_round_trip_match_the_resolver(project: Path):
    design = _project_design()
    resolver = _resolver(project, design)
    expected = design_report.analyze(design, resolver)
    lib_map = resolver.library_map()
    assert design_report.analyze(design, lib_map) == expected
    assert design_report.analyze(design, json.loads(json.dumps(lib_map))) == expected


def test_a_nickname_other_than_the_file_stem_needs_the_resolver(project: Path):
    design = _project_design(nick="GNSS")              # libs/SPS_parts.kicad_sym as GNSS
    rep = design_report.analyze(design, _resolver(project, design))
    assert rep["errors"] == [] and rep["flags"]["floating_pins"] == PROJECT_FLOATING
    # dir mode keeps the stock naming convention: <dir>/GNSS.kicad_sym does not exist
    rep = design_report.analyze(design, str(project / "libs"))
    assert [e.split(": ", 1)[0] for e in rep["errors"]] == [
        "pin types unavailable for GNSS:NapkinMCU (U1)", "pin types unavailable for GNSS:Cap (C1)",
        "pin types unavailable for TestLib:PosFileR (R1)"]


def test_project_sym_lib_table_resolves_without_a_libraries_block(project: Path):
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n  (lib (name "SPS_parts")(type "KiCad")'
        '(uri "${KIPRJMOD}/libs/SPS_parts.kicad_sym")(options "")(descr ""))\n)\n',
        encoding="utf-8")
    design = _project_design(block=False)
    rep = design_report.analyze(design, _resolver(project, design))
    assert rep["errors"] == [] and rep["flags"]["floating_pins"] == PROJECT_FLOATING


def test_unresolvable_nickname_is_reported_with_its_refs_and_others_stay_covered(
        project: Path):
    design = {**_project_design(), "components": _project_design()["components"] + [
        {"ref": "U2", "lib_id": "Missing:Chip", "value": "?", "footprint": "F"},
        {"ref": "U3", "lib_id": "Missing:Chip", "value": "?", "footprint": "F"}]}
    rep = design_report.analyze(design, _resolver(project, design))
    assert len(rep["errors"]) == 1
    assert rep["errors"][0].startswith(
        "pin types unavailable for Missing:Chip (U2, U3): SymbolLibError: "
        "symbol library not found: 'Missing': ")
    assert rep["decoupling"]["checked"][0]["ref"] == "U1"


def test_a_library_of_another_type_is_an_error_naming_it(project: Path):
    (project / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n  (lib (name "SPS_parts")(type "Legacy")'
        '(uri "${KIPRJMOD}/libs/SPS_parts.lib")(options "")(descr ""))\n)\n',
        encoding="utf-8")
    design = _project_design(block=False)
    rep = design_report.analyze(design, _resolver(project, design))
    assert [e.split(": ", 1)[0] for e in rep["errors"]] == [   # one per lib_id; R1 is stock
        f"pin types unavailable for {PROJECT_NICK}:NapkinMCU (U1)",
        f"pin types unavailable for {PROJECT_NICK}:Cap (C1)"]
    assert all("'SPS_parts'" in e and "has type 'Legacy'" in e for e in rep["errors"])


def test_symbols_keyword_overrides_the_positional_directory(project: Path):
    """The harness passes ctx.syms positionally and a feature's resolver as
    ``symbols=`` (rp_hooks analysis_kwargs)."""
    design = _project_design()
    resolver = _resolver(project, design)
    assert design_report.analyze(design, str(FIXTURE_SYMBOLS), symbols=resolver) == \
        design_report.analyze(design, resolver)
    off = design_report.analyze(design, str(FIXTURE_SYMBOLS), symbols="")
    assert off["symbols_available"] is False and off["errors"] == []


@pytest.mark.parametrize("empty", ["", None, {}])
def test_no_source_is_a_config_choice(buck_design: dict, empty):
    rep = design_report.analyze(buck_design, empty)
    assert rep["errors"] == [] and rep["symbols_available"] is False


@pytest.mark.parametrize("bad", [42, ["dir"], object()])
def test_a_source_of_another_kind_is_a_type_error(bad):
    with pytest.raises(TypeError, match="symbol source"):
        design_report.analyze(_synthetic_design(), bad)


def test_a_path_object_is_a_directory(buck_design: dict, tmp_path: Path):
    assert design_report.analyze(buck_design, tmp_path / "nope") == \
        design_report.analyze(buck_design, str(tmp_path / "nope"))


# --- stacked pins: a floating hidden passive pin suggests pin_groups (F2-10, D1) ----

def test_floating_stacked_pins_carry_a_pin_groups_hint(napkin_lib: Path):
    rep = design_report.analyze(_mcu_design(), str(napkin_lib))
    detail = _detail(rep)
    for pin in ("U1.3", "U1.4", "U1.5"):                  # not stacked: no new keys
        assert "stacked_on" not in detail[pin] and "hint" not in detail[pin]
    assert (detail["U1.6"]["stacked_on"], detail["U1.7"]["stacked_on"],
            detail["U1.8"]["stacked_on"]) == ("2", "2", "1")
    assert detail["U1.6"]["hint"] == (
        "hidden pin stacked on U1.2 ('GND'): put every pad of pin_groups['GND'] (2, 6, 7) "
        "on net GND; KiCad joins stacked pins in the schematic, but the board takes "
        "pad nets from the netlist")
    assert detail["U1.7"]["hint"] == detail["U1.6"]["hint"]
    assert "pin_groups['VDD'] (1, 8) on net 3V3" in detail["U1.8"]["hint"]


def test_stacked_hint_without_a_netted_group_names_no_net(napkin_lib: Path):
    design = _mcu_design()
    design["nets"] = {"3V3": [["C1", "1"]], "GND": [["C1", "2"]]}
    hint = _detail(design_report.analyze(design, str(napkin_lib)))["U1.6"]["hint"]
    assert "pin_groups['GND'] (2, 6, 7) on one net;" in hint


def test_markdown_lists_each_stack_once(napkin_lib: Path):
    md = design_report.to_markdown(design_report.analyze(_mcu_design(), str(napkin_lib)))
    stack_lines = [line for line in md.splitlines() if line.startswith("- Stacked pins")]
    assert stack_lines == [
        "- Stacked pins floating on U1: pad(s) 6, 7 — hidden pin stacked on U1.2 ('GND'): put "
        "every pad of pin_groups['GND'] (2, 6, 7) on net GND; KiCad joins stacked pins in "
        "the schematic, but the board takes pad nets from the netlist",
        "- Stacked pins floating on U1: pad(s) 8 — hidden pin stacked on U1.1 ('VDD'): put "
        "every pad of pin_groups['VDD'] (1, 8) on net 3V3; KiCad joins stacked pins in the "
        "schematic, but the board takes pad nets from the netlist"]
    assert md.index("- Floating pins:") < md.index("- Stacked pins")


def test_lg290p_hint_compacts_the_gnd_pad_list(project: Path):
    design = {"name": "gnss", "libraries": {"symbols": {PROJECT_NICK: PROJECT_LIB}},
              "components": [{"ref": "U1", "lib_id": f"{PROJECT_NICK}:LG290P",
                              "value": "LG290P", "footprint": "SPS:LG290P"}],
              "nets": {"GND": [["U1", "10"]], "VCC": [["U1", "23"]]}}
    rep = design_report.analyze(design, _resolver(project, design))
    gnd = [d for d in rep["flags"]["floating_detail"] if d["name"] == "GND"]
    assert len(gnd) == 58 and {d["stacked_on"] for d in gnd} == {"10"}
    assert "pin_groups['GND'] (10, 12, 13, 24-79) on net GND" in gnd[0]["hint"]
    md = design_report.to_markdown(rep)
    assert [line.split(" — ")[0] for line in md.splitlines()
            if line.startswith("- Stacked pins")] == [
        "- Stacked pins floating on U1: pad(s) 12, 13, 24-79"]
    # non-stacked floating signal pins carry no hint
    assert all("hint" not in d for d in rep["flags"]["floating_detail"] if d["name"] != "GND")


OLD_FORMAT_LIB = """(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)
  (symbol "Old" (in_bom yes) (on_board yes)
    (property "Reference" "U" (id 0) (at 0 0 0))
    (symbol "Old_1_1"
      (pin power_in line (at 0 -5.08 90) (length 2.54)
        (name "GND") (number "1"))
      (pin passive line (at 0 -5.08 90) (length 2.54) hide
        (name "GND") (number "2"))
      (pin passive line (at 2.54 -5.08 90) (length 2.54) hide
        (name "SHIELD") (number "3"))
      (pin passive line (at 0 -5.08 90) (length 2.54)
        (name "GND") (number "4"))
      (pin input line (at 0 -5.08 90) (length 2.54) (hide yes)
        (name "GND") (number "5"))
    )
  )
)
"""


def test_stacked_means_hidden_passive_on_an_earlier_pins_point(tmp_path: Path):
    """KiCad 6-8 write a bare ``hide`` token: pin 2 is stacked on pin 1; pin 3
    is hidden but alone, pin 4 shares the point but is visible, pin 5 is
    hidden on the point but typed input: none of those is a stacked pin."""
    (tmp_path / "OldLib.kicad_sym").write_text(OLD_FORMAT_LIB, encoding="utf-8")
    design = {"name": "old", "components": [
        {"ref": "U1", "lib_id": "OldLib:Old", "value": "Old", "footprint": "F"}],
        "nets": {"GND": [["U1", "1"]]}}
    detail = _detail(design_report.analyze(design, str(tmp_path)))
    assert sorted(detail) == ["U1.2", "U1.3", "U1.4", "U1.5"]
    assert {pin: d.get("stacked_on") for pin, d in detail.items()} == {
        "U1.2": "1", "U1.3": None, "U1.4": None, "U1.5": None}


UNNAMED_STACK_LIB = """(kicad_symbol_lib (version 20241209) (generator "kicad-eda-tests")
  (symbol "Tab" (in_bom yes) (on_board yes)
    (property "Reference" "J" (at 0 0 0))
    (symbol "Tab_1_1"
      (pin passive line (at 0 0 0) (length 2.54) (name "~") (number "1"))
      (pin passive line (at 0 0 0) (length 2.54) (hide yes) (name "~") (number "2"))
      (pin passive line (at 0 0 0) (length 2.54) (hide yes) (name "~") (number "3"))
      (pin passive line (at 2.54 0 0) (length 2.54) (name "~") (number "4"))
      (pin passive line (at 5.08 0 0) (length 2.54))
    )
  )
)
"""


def test_an_unnamed_stack_suggests_netting_its_own_pads(tmp_path: Path):
    """No pin name, so no pin_groups entry: the hint names the stack's pads
    (a pin without a number is ignored, as everywhere else)."""
    (tmp_path / "TabLib.kicad_sym").write_text(UNNAMED_STACK_LIB, encoding="utf-8")
    design = {"name": "tab", "components": [
        {"ref": "J1", "lib_id": "TabLib:Tab", "value": "Tab", "footprint": "F"}],
        "nets": {"SIG": [["J1", "1"]]}}
    rep = design_report.analyze(design, str(tmp_path))
    detail = _detail(rep)
    assert sorted(detail) == ["J1.2", "J1.3", "J1.4"]
    assert detail["J1.2"]["hint"] == detail["J1.3"]["hint"] == (
        "hidden pin stacked on J1.1: put every pad of the stack at J1.1 (1-3) on net SIG; "
        "KiCad joins stacked pins in the schematic, but the board takes pad nets from the "
        "netlist")
    assert "stacked_on" not in detail["J1.4"]


def test_markdown_lists_consolidation_opportunities(buck_design: dict):
    four_r = {**buck_design, "components": buck_design["components"] + [
        {"ref": f"R{i}", "lib_id": "Device:R", "value": "10k", "footprint": "R:R_0603"}
        for i in range(1, 5)]}
    md = design_report.to_markdown(design_report.analyze(four_r, ""))
    assert "  - 4× `10k` R_0603 (R1, R2, R3, R4)" in md.splitlines()


@requires_symbols
def test_stock_stacked_gnd_pins_get_the_hint():
    """KiCad's RF_GPS:L70-R stacks hidden passive GND pins 10 and 12 on pin 1."""
    design = {"name": "gps", "components": [
        {"ref": "U1", "lib_id": "RF_GPS:L70-R", "value": "L70-R", "footprint": "F"}],
        "nets": {"GND": [["U1", "1"]]}}
    detail = _detail(design_report.analyze(design, str(KICAD.symbols)))
    assert (detail["U1.10"]["stacked_on"], detail["U1.12"]["stacked_on"]) == ("1", "1")
    assert "pin_groups['GND'] (1, 10, 12) on net GND" in detail["U1.10"]["hint"]


# --- CLI: <symbols> goes through the netlist's resolver, as sch_build's does ----------

def test_cli_resolves_the_netlist_libraries_block(project: Path, capsys: pytest.CaptureFixture):
    netlist = _write_netlist(project, _project_design())
    assert design_report.main([str(netlist), str(FIXTURE_SYMBOLS), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["errors"] == [] and rep["flags"]["floating_pins"] == PROJECT_FLOATING


def test_cli_takes_a_library_map_file(project: Path, capsys: pytest.CaptureFixture):
    design = _project_design(nick="GNSS")
    netlist = _write_netlist(project, design)
    lib_map = project / "libmap.json"
    lib_map.write_text(json.dumps(_resolver(project, design).library_map()), encoding="utf-8")
    assert design_report.main([str(netlist), str(lib_map), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["flags"]["floating_pins"] == PROJECT_FLOATING


def test_cli_markdown_mode_and_a_bad_libraries_block(project: Path,
                                                     capsys: pytest.CaptureFixture):
    netlist = _write_netlist(project, _project_design())
    assert design_report.main([str(netlist), str(FIXTURE_SYMBOLS)]) == 0
    assert capsys.readouterr().out.startswith("# Design Report — proj")
    netlist = _write_netlist(project, {**_project_design(), "libraries": {"symbols": []}})
    assert design_report.main([str(netlist), str(FIXTURE_SYMBOLS), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "libraries.symbols must be an object" in out["error"]
    assert design_report.main([str(netlist), str(FIXTURE_SYMBOLS)]) == 1
    assert capsys.readouterr().out.startswith("error: ")


@pytest.mark.parametrize("argv", [[], ["a.json", "syms", "extra"], ["--json"]])
def test_cli_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture):
    assert design_report.main(argv) == 2
    assert "usage: design_report.py" in capsys.readouterr().out

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
    assert tree["SW"] == {"drivers": ["U1.2"], "loads": 2, "spec": {"voltage": 12.0, "current": 3.0}}
    assert tree["VIN"] == {"drivers": ["(passive/flagged source)"], "loads": 4,
                           "spec": {"voltage": 12.0, "current": 1.5}}


@requires_symbols
def test_rails_missing_testpoint(buck_design: dict):
    rep = design_report.analyze(buck_design, str(KICAD.symbols))
    assert rep["flags"]["rails_missing_testpoint"] == ["SW", "V5", "VIN", "VIN_RAW"]   # GND is 0 V
    with_tp = {**buck_design,
               "components": buck_design["components"] + [
                   {"ref": "TP1", "lib_id": "Connector:TestPoint", "value": "TP", "footprint": "F"}],
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
        {"ref": f"R{i}", "lib_id": "Device:R", "value": "10k", "footprint": "R_0603"} for i in range(1, 5)]}
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
    assert sum(1 for line in lines if line.startswith("- pin types unavailable for ")) == BUCK_LIB_IDS
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
                                       monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    netlist = tmp_path / "netlist.json"
    netlist.write_text(json.dumps(buck_design), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["design_report.py", str(netlist), "--json"])
    assert design_report.main() == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    monkeypatch.setattr("sys.argv", ["design_report.py", str(netlist), str(tmp_path / "nope"), "--json"])
    assert design_report.main() == 1
    assert len(json.loads(capsys.readouterr().out)["errors"]) == BUCK_LIB_IDS
    monkeypatch.setattr("sys.argv", ["design_report.py"])
    assert design_report.main() == 2

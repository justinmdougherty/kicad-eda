"""lint_netlist — structural netlist checks E1..E5 / W1..W2."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import lint_netlist

# a two-symbol library: one IC with two power_in pins, one plain passive
TEST_SYM = """(kicad_symbol_lib (version 20231120) (generator test)
  (symbol "TestIC" (pin_names (offset 1.016)) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "TestIC" (at 0 0 0))
    (symbol "TestIC_0_1"
      (rectangle (start -5.08 5.08) (end 5.08 -5.08)))
    (symbol "TestIC_1_1"
      (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VCC") (number "1"))
      (pin power_in line (at -7.62 -2.54 0) (length 2.54) (name "GND") (number "2"))
      (pin input line (at 7.62 0 180) (length 2.54) (name "IN") (number "3"))
      (pin output line (at 7.62 -2.54 180) (length 2.54) (name "OUT") (number "4"))))
  (symbol "TestR" (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0))
    (symbol "TestR_1_1"
      (pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))
      (pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2")))))
"""


def _design() -> dict:
    return {"components": [
                {"ref": "U1", "lib_id": "TestLib:TestIC", "value": "IC", "footprint": "F"},
                {"ref": "R1", "lib_id": "TestLib:TestR", "value": "10k", "footprint": "F"}],
            "nets": {"VCC": [["U1", "1"], ["R1", "1"]],
                     "GND": [["U1", "2"], ["R1", "2"]],
                     "SIG": [["U1", "3"], ["U1", "4"]]},
            "no_connect": []}


@pytest.fixture
def symbols_dir(tmp_path: Path) -> Path:
    (tmp_path / "TestLib.kicad_sym").write_text(TEST_SYM, encoding="utf-8")
    return tmp_path


# --- clean designs -----------------------------------------------------------

def test_every_example_netlist_lints_clean(example_netlists):
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        rep = lint_netlist.lint(design)
        assert rep["ok"] is True, f"{path.parent.name}: {rep['errors']}"
        assert rep["errors"] == [] and rep["warnings"] == []
        assert rep["components"] == len(design["components"])
        assert rep["nets"] == len(design["nets"])


def test_synthetic_clean_design():
    assert lint_netlist.lint(_design()) == {"ok": True, "errors": [], "warnings": [],
                                            "components": 2, "nets": 3}


def test_empty_design_is_structurally_ok():
    assert lint_netlist.lint({})["ok"] is True


# --- errors ------------------------------------------------------------------

def test_e3_single_pin_net():
    d = _design()
    d["nets"]["FLOAT"] = [["U1", "4"]]
    d["nets"]["SIG"] = [["U1", "3"]]
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert "E3 net 'FLOAT' has 1 pad(s) (needs >=2)" in rep["errors"]
    assert "E3 net 'SIG' has 1 pad(s) (needs >=2)" in rep["errors"]
    d["nets"]["EMPTY"] = []
    assert "E3 net 'EMPTY' has 0 pad(s) (needs >=2)" in lint_netlist.lint(d)["errors"]


def test_e1_pad_assigned_to_two_nets():
    d = _design()
    d["nets"]["SIG"].append(["R1", "1"])                 # R1.1 is already on VCC
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert rep["errors"] == ["E1 pad R1.1 assigned to multiple nets: ['VCC', 'SIG']"]


def test_e1_pad_numbers_compared_as_strings():
    d = _design()
    d["nets"]["SIG"].append(["R1", 1])                   # int pad vs "1"
    assert "E1 pad R1.1 assigned to multiple nets: ['VCC', 'SIG']" in \
        lint_netlist.lint(d)["errors"]


def test_e2_pad_both_netted_and_no_connect():
    d = _design()
    d["no_connect"] = [["U1", "3"], ["U1", 3]]           # duplicate spellings collapse
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert rep["errors"] == ["E2 pad U1.3 is both netted and no_connect"]


def test_e4_unknown_ref_in_net_and_no_connect():
    d = _design()
    d["nets"]["SIG"].append(["U9", "1"])
    d["no_connect"] = [["U8", "1"]]
    errs = lint_netlist.lint(d)["errors"]
    assert "E4 net 'SIG' references unknown ref U9" in errs
    assert "E4 no_connect references unknown ref U8" in errs
    assert len(errs) == 2


def test_e5_duplicate_component_ref():
    d = _design()
    d["components"].append({"ref": "R1", "lib_id": "TestLib:TestR", "value": "1k"})
    rep = lint_netlist.lint(d)
    assert rep["errors"] == ["E5 duplicate component ref: R1"]
    assert rep["components"] == 3


def test_multiple_errors_are_all_reported():
    d = _design()
    d["nets"]["X"] = [["U1", "3"]]                       # E3 + E1 (U1.3 on SIG too)
    d["no_connect"] = [["U1", "4"]]                      # E2
    rep = lint_netlist.lint(d)
    codes = sorted(e[:2] for e in rep["errors"])
    assert codes == ["E1", "E2", "E3"]


# --- warnings ----------------------------------------------------------------

def test_w1_case_collision_is_a_warning_not_an_error():
    d = _design()
    d["nets"]["gnd"] = [["U1", "4"], ["R1", "2"]]        # R1.2 also on GND -> E1 too
    rep = lint_netlist.lint(d)
    assert "W1 net names differ only by case: ['GND', 'gnd']" in rep["warnings"]
    d2 = _design()
    d2["nets"]["Sig"] = [["U1", "1"], ["U1", "2"]]
    d2["nets"].pop("VCC"), d2["nets"].pop("GND")
    rep2 = lint_netlist.lint(d2)
    assert rep2["ok"] is True and rep2["warnings"] == \
        ["W1 net names differ only by case: ['SIG', 'Sig']"]


def test_w2_unconnected_power_in_pin(symbols_dir):
    d = _design()
    d["nets"]["GND"] = [["R1", "2"], ["U1", "4"]]        # U1.2 (power_in GND) dropped
    d["nets"].pop("SIG")                                  # U1.3 (input) left open: no W2
    rep = lint_netlist.lint(d, str(symbols_dir))
    assert rep["ok"] is True                              # warning-grade
    assert rep["warnings"] == ["W2 U1.2 is a power_in pin but not connected"]


def test_w2_satisfied_by_net_or_no_connect(symbols_dir):
    assert lint_netlist.lint(_design(), str(symbols_dir))["warnings"] == []
    d = _design()
    d["nets"]["VCC"] = [["R1", "1"], ["U1", "4"]]
    d["nets"]["SIG"] = [["U1", "3"], ["R1", "2"]]
    d["nets"].pop("GND")
    d["no_connect"] = [["U1", "1"]]                       # explicit NC silences W2
    rep = lint_netlist.lint(d, str(symbols_dir))
    assert rep["warnings"] == ["W2 U1.2 is a power_in pin but not connected"]


def test_w2_is_skipped_without_a_symbols_dir(symbols_dir):
    d = _design()
    d["nets"].pop("GND")
    d["nets"]["SIG"].append(["R1", "2"])
    assert lint_netlist.lint(d)["warnings"] == []


def test_w2_symbol_lookup_failure_is_reported_not_raised(tmp_path):
    rep = lint_netlist.lint(_design(), str(tmp_path / "nope"))
    assert rep["ok"] is True
    assert len(rep["warnings"]) == 1
    assert rep["warnings"][0].startswith("W2 skipped (symbol lookup failed:")


def test_w2_unknown_symbol_in_library_is_reported(symbols_dir):
    d = _design()
    d["components"][0]["lib_id"] = "TestLib:Missing"
    w = lint_netlist.lint(d, str(symbols_dir))["warnings"]
    assert len(w) == 1 and w[0].startswith("W2 skipped (symbol lookup failed:")
    assert "Missing" in w[0]


def test_w2_one_unresolvable_symbol_does_not_skip_the_other_components(symbols_dir):
    # the unresolvable part comes FIRST: W2 used to abort its whole loop on it,
    # so U1's open power_in pin was never reported
    d = _design()
    d["components"].insert(0, {"ref": "X1", "lib_id": "TestLib:Missing", "value": "?"})
    d["nets"]["GND"] = [["R1", "2"], ["U1", "4"]]         # U1.2 (power_in GND) dropped
    d["nets"].pop("SIG")
    rep = lint_netlist.lint(d, str(symbols_dir))
    assert rep["ok"] is True                              # still warning-grade
    assert rep["warnings"] == [
        "W2 U1.2 is a power_in pin but not connected",
        "W2 skipped (symbol lookup failed: TestLib:Missing [X1]: "
        "symbol TestLib:Missing not found in TestLib.kicad_sym)"]


def test_w2_names_each_unresolvable_lib_id_once_with_its_refs(symbols_dir):
    d = _design()
    d["components"] += [{"ref": "X1", "lib_id": "TestLib:Missing"},
                        {"ref": "X2", "lib_id": "NoLib:Part"},
                        {"ref": "X3", "lib_id": "TestLib:Missing"},
                        {"ref": "X4", "lib_id": "NoColon"}]
    w = lint_netlist.lint(d, str(symbols_dir))["warnings"]
    assert len(w) == 1                                    # U1/R1 fully connected: no W2
    assert w[0].startswith("W2 skipped (symbol lookup failed: ")
    detail = w[0][len("W2 skipped (symbol lookup failed: "):-1].split("; ")
    assert [item.split(": ")[0] for item in detail] == [
        "TestLib:Missing [X1, X3]", "NoLib:Part [X2]", "NoColon [X4]"]
    assert detail[1].endswith("NoLib.kicad_sym")           # the library file is named
    assert detail[2] == "NoColon [X4]: lib_id is not 'Library:Symbol'"


def test_w2_malformed_symbol_is_reported_not_raised(symbols_dir):
    # a pin with an empty (number) breaks pin_etypes; W2 stays best-effort
    (symbols_dir / "BadLib.kicad_sym").write_text(
        '(kicad_symbol_lib (version 20231120) (generator test)\n'
        '  (symbol "Bad" (symbol "Bad_1_1"\n'
        '    (pin power_in line (at 0 0 0) (length 2.54) (name "V") (number)))))\n',
        encoding="utf-8")
    d = _design()
    d["components"].insert(0, {"ref": "B1", "lib_id": "BadLib:Bad"})
    d["no_connect"] = [["U1", "1"]]
    d["nets"]["VCC"] = [["R1", "1"], ["U1", "3"]]
    d["nets"].pop("SIG")
    w = lint_netlist.lint(d, str(symbols_dir))["warnings"]
    assert w == ["W2 skipped (symbol lookup failed: BadLib:Bad [B1]: "
                 "IndexError: list index out of range)"]


# --- CLI ---------------------------------------------------------------------

def test_main_exit_code_and_json_output(tmp_path, monkeypatch, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_design()), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["lint_netlist.py", str(good)])
    assert lint_netlist.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["components"] == 2

    bad = _design()
    bad["nets"]["X"] = [["U1", "3"]]
    bad_p = tmp_path / "bad.json"
    bad_p.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["lint_netlist.py", str(bad_p)])
    assert lint_netlist.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["errors"]

"""lint_netlist — structural netlist checks E1..E5 / W1..W2 (lint_rules_core).

The rule-semantics tests run the core rule module alone (``core_only``), so a
rule module added later (lint_rules_*.py, discovered by lint_netlist) cannot
change what they pin down; the registry tests are tests/test_lint_registry.py.
The example-netlist and empty-design tests run the full registry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import libtable
import lint_netlist
import lint_rules_core
import symlib
from lint_context import LintContext

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

W2_U1_GND = "W2 U1.2 is a power_in pin but not connected"


def _design() -> dict:
    return {"components": [
                {"ref": "U1", "lib_id": "TestLib:TestIC", "value": "IC", "footprint": "F"},
                {"ref": "R1", "lib_id": "TestLib:TestR", "value": "10k", "footprint": "F"}],
            "nets": {"VCC": [["U1", "1"], ["R1", "1"]],
                     "GND": [["U1", "2"], ["R1", "2"]],
                     "SIG": [["U1", "3"], ["U1", "4"]]},
            "no_connect": []}


def _gnd_dropped() -> dict:
    """U1.2 (power_in GND) left unconnected; U1.3 (input) open too (no W2)."""
    d = _design()
    d["nets"]["GND"] = [["R1", "2"], ["U1", "4"]]
    d["nets"].pop("SIG")
    return d


@pytest.fixture
def symbols_dir(tmp_path: Path) -> Path:
    (tmp_path / "TestLib.kicad_sym").write_text(TEST_SYM, encoding="utf-8")
    return tmp_path


@pytest.fixture
def core_only(monkeypatch):
    """lint() runs the core rule module (E1-E5, W1, W2) and nothing else."""
    core = tuple(r for r in lint_netlist.rule_modules() if r.name == "lint_rules_core")
    assert len(core) == 1 and core[0].check is lint_rules_core.check
    monkeypatch.setattr(lint_netlist, "rule_modules", lambda search_dir=None: core)


# --- clean designs (full registry) -------------------------------------------

def test_every_example_netlist_lints_clean(example_netlists):
    for path in example_netlists:
        design = json.loads(path.read_text(encoding="utf-8"))
        rep = lint_netlist.lint(design)
        assert rep["ok"] is True, f"{path.parent.name}: {rep['errors']}"
        assert rep["errors"] == []
        assert rep["components"] == len(design["components"])
        assert rep["nets"] == len(design["nets"])
        # the core rules find nothing at all (later rule modules may warn)
        assert lint_rules_core.check(design, LintContext()) == ([], [])


def test_empty_design_is_structurally_ok():
    assert lint_netlist.lint({})["ok"] is True


def test_main_on_an_example_netlist_exits_zero(example_netlists, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["lint_netlist.py", str(example_netlists[0])])
    assert lint_netlist.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["errors"] == []


# --- clean designs (core rules) ------------------------------------------------

def test_synthetic_clean_design(core_only):
    assert lint_netlist.lint(_design()) == {"ok": True, "errors": [], "warnings": [],
                                            "components": 2, "nets": 3}


def test_empty_design_core_rules(core_only):
    assert lint_netlist.lint({}) == {"ok": True, "errors": [], "warnings": [],
                                     "components": 0, "nets": 0}


def test_core_check_returns_errors_and_warnings_lists():
    errors, warnings = lint_rules_core.check(_design(), LintContext())
    assert (errors, warnings) == ([], [])


# --- errors ------------------------------------------------------------------

def test_e3_single_pin_net(core_only):
    d = _design()
    d["nets"]["FLOAT"] = [["U1", "4"]]
    d["nets"]["SIG"] = [["U1", "3"]]
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert "E3 net 'FLOAT' has 1 pad(s) (needs >=2)" in rep["errors"]
    assert "E3 net 'SIG' has 1 pad(s) (needs >=2)" in rep["errors"]
    d["nets"]["EMPTY"] = []
    assert "E3 net 'EMPTY' has 0 pad(s) (needs >=2)" in lint_netlist.lint(d)["errors"]


def test_e1_pad_assigned_to_two_nets(core_only):
    d = _design()
    d["nets"]["SIG"].append(["R1", "1"])                 # R1.1 is already on VCC
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert rep["errors"] == ["E1 pad R1.1 assigned to multiple nets: ['VCC', 'SIG']"]


def test_e1_pad_numbers_compared_as_strings(core_only):
    d = _design()
    d["nets"]["SIG"].append(["R1", 1])                   # int pad vs "1"
    assert "E1 pad R1.1 assigned to multiple nets: ['VCC', 'SIG']" in \
        lint_netlist.lint(d)["errors"]


def test_e2_pad_both_netted_and_no_connect(core_only):
    d = _design()
    d["no_connect"] = [["U1", "3"], ["U1", 3]]           # duplicate spellings collapse
    rep = lint_netlist.lint(d)
    assert rep["ok"] is False
    assert rep["errors"] == ["E2 pad U1.3 is both netted and no_connect"]


def test_e4_unknown_ref_in_net_and_no_connect(core_only):
    d = _design()
    d["nets"]["SIG"].append(["U9", "1"])
    d["no_connect"] = [["U8", "1"]]
    errs = lint_netlist.lint(d)["errors"]
    assert "E4 net 'SIG' references unknown ref U9" in errs
    assert "E4 no_connect references unknown ref U8" in errs
    assert len(errs) == 2


def test_e4_no_connect_errors_follow_the_declared_order(core_only):
    d = _design()
    d["no_connect"] = [["U8", "1"], ["U7", "2"], ["U8", 1], ["U6", "3"]]
    assert lint_netlist.lint(d)["errors"] == [
        "E4 no_connect references unknown ref U8",
        "E4 no_connect references unknown ref U7",
        "E4 no_connect references unknown ref U6"]


def test_e5_duplicate_component_ref(core_only):
    d = _design()
    d["components"].append({"ref": "R1", "lib_id": "TestLib:TestR", "value": "1k"})
    rep = lint_netlist.lint(d)
    assert rep["errors"] == ["E5 duplicate component ref: R1"]
    assert rep["components"] == 3


def test_multiple_errors_are_all_reported(core_only):
    d = _design()
    d["nets"]["X"] = [["U1", "3"]]                       # E3 + E1 (U1.3 on SIG too)
    d["no_connect"] = [["U1", "4"]]                      # E2
    rep = lint_netlist.lint(d)
    codes = sorted(e[:2] for e in rep["errors"])
    assert codes == ["E1", "E2", "E3"]


def test_error_order_is_e5_nets_e1_e2_then_no_connect_e4(core_only):
    d = _design()
    d["components"].append({"ref": "R1"})                # E5
    d["nets"]["X"] = [["U1", "3"], ["U9", "1"]]          # E4 (net), then E1 for U1.3
    d["no_connect"] = [["U1", "4"], ["U8", "2"]]         # E2 for U1.4, E4 (no_connect)
    assert lint_netlist.lint(d)["errors"] == [
        "E5 duplicate component ref: R1",
        "E4 net 'X' references unknown ref U9",
        "E1 pad U1.3 assigned to multiple nets: ['SIG', 'X']",
        "E2 pad U1.4 is both netted and no_connect",
        "E4 no_connect references unknown ref U8"]


def test_a_malformed_design_is_a_lint_internal_error_not_a_crash(core_only):
    rep = lint_netlist.lint({"components": [{"value": "no ref"}]})
    assert rep["ok"] is False
    assert rep["errors"] == ["LINT-INTERNAL lint_rules_core: KeyError: 'ref'"]


# --- warnings ----------------------------------------------------------------

def test_w1_case_collision_is_a_warning_not_an_error(core_only):
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


def test_w2_unconnected_power_in_pin(core_only, symbols_dir):
    rep = lint_netlist.lint(_gnd_dropped(), str(symbols_dir))
    assert rep["ok"] is True                              # warning-grade
    assert rep["warnings"] == [W2_U1_GND]


def test_w2_satisfied_by_net_or_no_connect(core_only, symbols_dir):
    assert lint_netlist.lint(_design(), str(symbols_dir))["warnings"] == []
    d = _design()
    d["nets"]["VCC"] = [["R1", "1"], ["U1", "4"]]
    d["nets"]["SIG"] = [["U1", "3"], ["R1", "2"]]
    d["nets"].pop("GND")
    d["no_connect"] = [["U1", "1"]]                       # explicit NC silences W2
    rep = lint_netlist.lint(d, str(symbols_dir))
    assert rep["warnings"] == [W2_U1_GND]


def test_w2_is_skipped_without_a_symbols_dir(core_only, symbols_dir):
    d = _design()
    d["nets"].pop("GND")
    d["nets"]["SIG"].append(["R1", "2"])
    assert lint_netlist.lint(d)["warnings"] == []
    assert lint_netlist.lint(d, ctx=LintContext(netlist_dir=symbols_dir))["warnings"] == []


def test_w2_symbol_lookup_failure_is_reported_not_raised(core_only, tmp_path):
    rep = lint_netlist.lint(_design(), str(tmp_path / "nope"))
    assert rep["ok"] is True
    assert len(rep["warnings"]) == 1
    assert rep["warnings"][0].startswith("W2 skipped (symbol lookup failed:")


def test_w2_unknown_symbol_in_library_is_reported(core_only, symbols_dir):
    d = _design()
    d["components"][0]["lib_id"] = "TestLib:Missing"
    w = lint_netlist.lint(d, str(symbols_dir))["warnings"]
    assert len(w) == 1 and w[0].startswith("W2 skipped (symbol lookup failed:")
    assert "Missing" in w[0]


def test_w2_one_unresolvable_symbol_does_not_skip_the_other_components(core_only, symbols_dir):
    # the unresolvable part comes FIRST: W2 used to abort its whole loop on it,
    # so U1's open power_in pin was never reported
    d = _gnd_dropped()
    d["components"].insert(0, {"ref": "X1", "lib_id": "TestLib:Missing", "value": "?"})
    rep = lint_netlist.lint(d, str(symbols_dir))
    assert rep["ok"] is True                              # still warning-grade
    assert rep["warnings"] == [
        W2_U1_GND,
        "W2 skipped (symbol lookup failed: TestLib:Missing [X1]: "
        "symbol TestLib:Missing not found in TestLib.kicad_sym)"]


def test_w2_names_each_unresolvable_lib_id_once_with_its_refs(core_only, symbols_dir):
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


def test_w2_malformed_symbol_is_reported_not_raised(core_only, symbols_dir):
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


def test_w2_reads_each_symbol_once_however_many_parts_use_it(core_only, symbols_dir,
                                                             monkeypatch):
    calls: list[str] = []
    real = symlib.pin_etypes

    def counting(flat: list) -> dict:
        calls.append(str(flat[1]))
        return real(flat)

    monkeypatch.setattr(symlib, "pin_etypes", counting)
    d = _gnd_dropped()
    d["components"] += [{"ref": f"R{i}", "lib_id": "TestLib:TestR"} for i in range(2, 12)]
    d["components"] += [{"ref": "U2", "lib_id": "TestLib:TestIC"},
                        {"ref": "X1", "lib_id": "TestLib:Missing"},
                        {"ref": "X2", "lib_id": "TestLib:Missing"}]
    w = lint_netlist.lint(d, str(symbols_dir))["warnings"]
    assert sorted(calls) == ["TestIC", "TestR"]          # once per lib_id, not per part
    assert w == [W2_U1_GND,
                 "W2 U2.1 is a power_in pin but not connected",
                 "W2 U2.2 is a power_in pin but not connected",
                 "W2 skipped (symbol lookup failed: TestLib:Missing [X1, X2]: "
                 "symbol TestLib:Missing not found in TestLib.kicad_sym)"]


# --- W2 symbol sources: a context, a resolver, a library map --------------------

def test_w2_through_an_explicit_context(core_only, symbols_dir):
    ctx = LintContext(symbols=str(symbols_dir), netlist_dir=symbols_dir)
    assert lint_netlist.lint(_gnd_dropped(), ctx=ctx)["warnings"] == [W2_U1_GND]
    # a context without symbols takes the positional symbols_dir
    bare = LintContext(netlist_dir=symbols_dir)
    assert lint_netlist.lint(_gnd_dropped(), str(symbols_dir), ctx=bare)["warnings"] == \
        [W2_U1_GND]


def test_w2_with_a_resolver_or_library_map_of_stock_libraries(core_only, symbols_dir):
    resolver = libtable.LibraryResolver.for_directory(symbols_dir, symbols_dir, None)
    for source in (resolver, resolver.library_map(), symbols_dir):
        assert lint_netlist.lint(_gnd_dropped(), source)["warnings"] == [W2_U1_GND], source
        ctx = LintContext(symbols=source)
        assert lint_netlist.lint(_gnd_dropped(), ctx=ctx)["warnings"] == [W2_U1_GND]


def test_w2_symbol_source_falls_back_to_the_stock_dir_on_an_older_symlib(
        symbols_dir, monkeypatch):
    # before symlib learned library resolution (F2-09 lib_path) it read only
    # <dir>/<lib>.kicad_sym: a resolver / library map is handed over as its stock dir
    monkeypatch.delattr(symlib, "lib_path", raising=False)
    resolver = libtable.LibraryResolver.for_directory(symbols_dir, symbols_dir, None)
    assert lint_rules_core.symbol_source(symlib, resolver) == str(symbols_dir)
    assert lint_rules_core.symbol_source(symlib, resolver.library_map()) == str(symbols_dir)
    assert lint_rules_core.symbol_source(symlib, "C:/syms") == "C:/syms"
    no_stock = libtable.LibraryResolver.for_directory(symbols_dir, None, None)
    assert lint_rules_core.symbol_source(symlib, no_stock) is no_stock


def test_w2_symbol_source_passes_through_when_symlib_resolves_libraries(
        symbols_dir, monkeypatch):
    monkeypatch.setattr(symlib, "lib_path", lambda source, lib: None, raising=False)
    resolver = libtable.LibraryResolver.for_directory(symbols_dir, symbols_dir, None)
    assert lint_rules_core.symbol_source(symlib, resolver) is resolver
    lib_map = resolver.library_map()
    assert lint_rules_core.symbol_source(symlib, lib_map) is lib_map


def test_w2_without_symlib_is_one_skipped_warning(core_only, symbols_dir, monkeypatch):
    def no_symlib() -> None:
        raise ImportError("No module named 'sexpdata'")

    monkeypatch.setattr(lint_rules_core, "_import_symlib", no_symlib)
    assert lint_netlist.lint(_gnd_dropped(), str(symbols_dir))["warnings"] == [
        "W2 skipped (symbol lookup failed: No module named 'sexpdata')"]


# --- CLI ---------------------------------------------------------------------

def test_main_exit_code_and_json_output(core_only, tmp_path, monkeypatch, capsys):
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


def test_main_with_a_symbols_dir_runs_w2(core_only, symbols_dir, capsys):
    netlist = symbols_dir / "netlist.json"
    netlist.write_text(json.dumps(_gnd_dropped()), encoding="utf-8-sig")   # BOM accepted
    assert lint_netlist.main([str(netlist), str(symbols_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == [W2_U1_GND]
    assert lint_netlist.main([str(netlist), str(symbols_dir), str(symbols_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == [W2_U1_GND]
    assert lint_netlist.main([str(netlist)]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []

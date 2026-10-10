"""lint_rules_stackup — the G1 stackup / controlled-impedance lint (ADR D7.2.7, D7.11).

Covers: registration (ORDER 50, discovered by lint_netlist); the resolve issues
(ST-E1..E8, E12, W1..W9) pass through with their JSON paths; the D7.11 policy
(ST-W12, floors -> ST-W3 / ST-W12); ST-E9 / ST-E10 / ST-W10 / ST-W11 from the line
geometry on every allowed layer; ST-E11 net names; ST-W13 the pad-layer rule;
IMP-E2 malformed net_specs values (as the impedance facade reports them); every
example netlist lints without a stackup error and rf_coupon_50r gives ST-W7.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import lint_netlist
import lint_rules_stackup as lrs
from lint_context import LintContext

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*/netlist.json"))
STACKUP_FIXTURES = sorted((ROOT / "tests" / "fixtures" / "stackups").glob("*.json"))
SMD = "Resistor_SMD:R_0603_1608Metric"
THT = "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical"


def _design(**keys) -> dict:
    """U1/U2 SMD on side F, J1 through-hole; SIG = U1.1 + J1.3."""
    return {"name": "st", "components": [
        {"ref": "U1", "lib_id": "Device:R", "footprint": SMD},
        {"ref": "U2", "lib_id": "Device:R", "footprint": SMD},
        {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x03", "footprint": THT}],
        "nets": {"GND": [["U1", "2"], ["U2", "2"], ["J1", "1"]],
                 "+3V3": [["J1", "2"], ["U2", "1"]], "SIG": [["U1", "1"], ["J1", "3"]]},
        **keys}


def _sig(spec: dict, **keys) -> dict:
    return _design(net_specs={"SIG": spec}, **keys)


def _ids(found: list[str]) -> list[str]:
    return [f.split(" ", 1)[0] for f in found]


def _lint(design: dict) -> tuple[list[str], list[str]]:
    return lrs.check(copy.deepcopy(design), LintContext())


def _has(found: list[str], ident: str, path: str) -> bool:
    return any(f.startswith(f"{ident} at {path}: ") for f in found)


PRESET_2116 = {"preset": "jlcpcb:JLC06161H-2116", "rationale": "6L test"}
TWO_LAYER_SIGNAL = {"rationale": "2L no plane", "layers": [
    {"type": "copper", "role": "signal"},
    {"type": "dielectric", "kind": "core", "material": "generic.fr4", "thickness": 1.51},
    {"type": "copper", "role": "signal"}]}


# --- registration ----------------------------------------------------------------
def test_order_50_and_discovered_by_lint_netlist():
    assert lrs.ORDER == 50
    rules = {r.name: r for r in lint_netlist.rule_modules()}
    assert rules["lint_rules_stackup"].check is lrs.check
    names = [r.name for r in lint_netlist.rule_modules()]
    assert names.index("lint_rules_stackup") > names.index("lint_rules_core")


def test_codes_cover_every_adr_id():
    ids = {f"ST-E{i}" for i in range(1, 13)} | {f"ST-W{i}" for i in range(1, 14)} | {"IMP-E2"}
    assert set(lrs.CODES) == ids
    assert all(isinstance(v, str) and v for v in lrs.CODES.values())


def test_check_returns_two_lists_of_str_and_never_mutates():
    d = _sig({"impedance": 50, "layers": ["In2.Cu"]}, stackup=dict(PRESET_2116))
    before = copy.deepcopy(d)
    errors, warnings = lrs.check(d, LintContext())
    assert d == before
    assert isinstance(errors, list) and isinstance(warnings, list)
    assert all(isinstance(x, str) for x in errors + warnings)
    assert lrs.check(d, LintContext()) == (errors, warnings)


# --- acceptance: the examples ----------------------------------------------------
def test_rf_coupon_50r_cli_prints_st_w7_and_no_errors(capsys):
    path = ROOT / "examples" / "rf_coupon_50r" / "netlist.json"
    assert lint_netlist.main([str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == []
    assert any(w.startswith("ST-W7 at $.stackup: ") for w in out["warnings"]), out["warnings"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_every_example_has_no_stackup_error(path):
    errors, _warnings = _lint(json.loads(path.read_text(encoding="utf-8")))
    assert errors == []


# --- resolve issues (D7-02 fixtures) -----------------------------------------------
@pytest.mark.parametrize("path", STACKUP_FIXTURES, ids=lambda p: p.stem)
def test_stackup_fixtures_give_their_id_and_path(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = _lint(fixture["design"])
    expect = fixture["expect"]
    if "error" in expect:
        assert _has(errors, expect["error"], expect["path"]), errors
    else:
        assert errors == [], errors
        for ident in expect["issues"]:
            assert ident in _ids(warnings), (ident, warnings)


def test_legacy_shorthand_is_st_w7():
    _errors, warnings = _lint(_design(stackup={"h": 0.2, "er": 4.2}))
    assert _has(warnings, "ST-W7", "$.stackup")


def test_info_findings_are_marked_and_kept_with_the_warnings():
    fixture = json.loads((ROOT / "tests" / "fixtures" / "stackups" / "d722_4l_hybrid.json")
                         .read_text(encoding="utf-8"))
    errors, warnings = _lint(fixture["design"])
    assert errors == []
    info = [w for w in warnings if w.startswith("ST-W6 (info) at ")]
    assert info and all(" at $." in w for w in info)


def test_fab_capability_is_st_w3():
    _e, warnings = _lint(_design(stackup={"fab": "jlcpcb", "rationale": "x",
                                          "impedance_control": {"required": True},
                                          "layers": TWO_LAYER_SIGNAL["layers"][:2] + [
                                              {"type": "copper", "role": "mixed", "net": "GND"}]}))
    assert "ST-W3" in _ids(warnings)


# --- D7.11 policy ------------------------------------------------------------------
def test_st_w12_when_no_layer_count_is_declared():
    _e, strict = _lint(_design())
    assert _has(strict, "ST-W12", "$.copper_layers")
    _e, napkin = _lint(_design(mode="napkin"))
    assert _has(napkin, "ST-W12", "$.copper_layers")


def test_st_w12_napkin_count_needs_a_rationale():
    _e, warnings = _lint(_design(mode="napkin", copper_layers=4))
    assert "ST-W12" in _ids(warnings)
    _e, warnings = _lint(_design(mode="napkin", copper_layers=4,
                                 stackup={"rationale": "4L: GND next to both outer layers"}))
    assert "ST-W12" not in _ids(warnings)


def test_floors_f1_is_st_w3_and_f2_is_st_w12():
    _e, w = _lint(_sig({"impedance": 50}, copper_layers=2, stackup={"rationale": "2L"}))
    assert any(x.startswith("ST-W3") and "floor F1" in x for x in w), w
    _e, w = _lint(_sig({"impedance": 50, "line": "stripline"}, copper_layers=4))
    assert any(x.startswith("ST-W12") and "floor F2" in x for x in w), w


# --- the line checks ---------------------------------------------------------------
def test_st_e9_no_reference_plane():
    errors, _w = _lint(_sig({"impedance": 50}, stackup=TWO_LAYER_SIGNAL))
    assert _has(errors, "ST-E9", "$.net_specs.SIG"), errors


def test_st_e9_a_declared_plane_or_missing_layer():
    errors, _w = _lint(_sig({"impedance": 50, "layers": ["In1.Cu"]}, copper_layers=4))
    assert _has(errors, "ST-E9", "$.net_specs.SIG.layers"), errors
    errors, _w = _lint(_sig({"impedance": 50, "layers": ["In7.Cu"]}, copper_layers=4))
    assert _has(errors, "ST-E9", "$.net_specs.SIG.layers"), errors


def test_st_e9_rf_net_without_an_outer_layer_reference():
    errors, _w = _lint(_sig({"impedance": 50, "freq_hz": 9.15e8}, stackup=TWO_LAYER_SIGNAL))
    assert "ST-E9" in _ids(errors)


def test_st_e10_rf_reference_on_a_power_plane():
    errors, _w = _lint(_sig({"impedance": 50, "freq_hz": 9.15e8, "layers": ["B.Cu"]},
                            copper_layers=4))
    assert _has(errors, "ST-E10", "$.net_specs.SIG.ref_nets"), errors
    errors, warnings = _lint(_sig({"impedance": 50, "freq_hz": 9.15e8, "layers": ["B.Cu"],
                                   "ref_nets": ["GND", "+3V3"]}, copper_layers=4))
    assert errors == [] and "ST-W10" in _ids(warnings)


def test_rf_net_defaults_to_the_outer_layer_and_passes_on_a_gnd_reference():
    errors, warnings = _lint(_sig({"impedance": 50, "freq_hz": 9.15e8}, copper_layers=4))
    assert errors == []
    assert not {"ST-W10", "ST-W11", "ST-W13"} & set(_ids(warnings))


def test_st_w10_jlc06161h_2116_in2_dual_stripline():
    errors, warnings = _lint(_sig({"impedance": 50, "layers": ["In2.Cu"]},
                                  stackup=dict(PRESET_2116)))
    assert errors == []
    assert _has(warnings, "ST-W10", "$.net_specs.SIG"), warnings
    assert "+3V3" in next(w for w in warnings if w.startswith("ST-W10"))


def test_st_w11_allowed_layers_on_different_reference_nets():
    errors, warnings = _lint(_sig({"impedance": 50}, stackup=dict(PRESET_2116)))
    assert errors == []
    assert _has(warnings, "ST-W11", "$.net_specs.SIG"), warnings


def test_single_sided_impedance_net_is_st_e12_and_st_e9():
    errors, _w = _lint(_sig({"impedance": 50}, copper_layers=1))
    assert "ST-E12" in _ids(errors) and "ST-E9" in _ids(errors)


def test_diff_pair_uses_the_pair_spec_and_impedance_wins_over_diff():
    errors, _w = _lint(_sig({"diff": 90, "gap": 0.15, "diff_pair": "SIG_N"}, copper_layers=4))
    assert errors == []
    errors, _w = _lint(_sig({"impedance": 50, "diff": 90, "pair": "broadside"},
                            copper_layers=4))
    assert errors == []                     # 'impedance' wins: single-ended, pair ignored


# --- ST-E11 ----------------------------------------------------------------------
def test_st_e11_net_names_dru_conditions_cannot_quote():
    d = _design(net_specs={"RF'1": {"impedance": 50}, "OK": {"impedance": 50}},
                copper_layers=4)
    errors, _w = _lint(d)
    assert [e for e in errors if e.startswith("ST-E11")] and \
        all("OK" not in e for e in errors if e.startswith("ST-E11"))
    d = _design(net_specs={"USB_DP": {"diff": 90, "diff_pair": "USB\\DM"}}, copper_layers=4)
    errors, _w = _lint(d)
    assert any(e.startswith("ST-E11 at $.net_specs.USB_DP.diff_pair") for e in errors), errors


# --- ST-W13 ----------------------------------------------------------------------
def test_st_w13_allowed_layers_exclude_the_smd_pad_layer():
    _e, warnings = _lint(_sig({"impedance": 50, "layers": ["In2.Cu"]}, stackup=dict(PRESET_2116)))
    w13 = [w for w in warnings if w.startswith("ST-W13")]
    assert len(w13) == 1 and "U1" in w13[0] and "F.Cu" in w13[0] and "J1" not in w13[0]


def test_st_w13_b_side_smd_pad_on_an_f_only_net():
    d = _sig({"impedance": 50, "layers": ["F.Cu"]}, copper_layers=4)
    d["components"][0]["side"] = "B"
    _e, warnings = _lint(d)
    assert any(w.startswith("ST-W13") and "B.Cu" in w for w in warnings), warnings


@pytest.mark.parametrize("extra", [{"route": "agent"}, {"route": "preroute"},
                                   {"freq_hz": 9.15e8}], ids=["agent", "preroute", "rf"])
def test_st_w13_only_for_freerouting_routed_impedance_nets(extra):
    spec = {"impedance": 50, "layers": ["In2.Cu"], **extra}
    _e, warnings = _lint(_sig(spec, stackup=dict(PRESET_2116)))
    assert "ST-W13" not in _ids(warnings)


def test_st_w13_through_hole_pads_reach_every_layer():
    d = _sig({"impedance": 50, "layers": ["In2.Cu"]}, stackup=dict(PRESET_2116))
    d["nets"]["SIG"] = [["J1", "3"], ["J1", "2"]]
    d["nets"]["+3V3"] = [["U2", "1"], ["U1", "1"]]
    _e, warnings = _lint(d)
    assert "ST-W13" not in _ids(warnings)


# --- IMP-E2 ----------------------------------------------------------------------
BAD_SPECS = {"W": {"impedance": 50, "width": "wide"},
             "L": {"impedance": 50, "layers": "F.Cu"},
             "R": {"impedance": 50, "freq_hz": 9e8, "ref_nets": "GND"},
             "V": {"impedance": 50, "void_layers": ["In1.Cu"]},
             "P": {"diff": 90, "pair": "side"},
             "Z": {"impedance": 0}}


def test_malformed_spec_values_are_imp_e2_errors_like_the_facade():
    import impedance
    d = _design(net_specs=copy.deepcopy(BAD_SPECS), copper_layers=4)
    errors, _w = _lint(d)
    mine = sorted(e.split(": ", 1)[0] for e in errors if e.startswith("IMP-E2"))
    facade = impedance.resolve_impedance(copy.deepcopy(d), errors="record")
    theirs = sorted(f"IMP-E2 at {i['path']}" for rec in facade.values() for i in rec["issues"]
                    if i["id"] == impedance.SPEC_ERROR)
    assert mine == theirs and len(mine) == len(BAD_SPECS)
    assert lrs.SPEC_ERROR == impedance.SPEC_ERROR


# --- robustness and integration ----------------------------------------------------
@pytest.mark.parametrize("design", [
    {}, {"components": "x", "nets": 5, "net_specs": [1], "planes": [1]},
    {"stackup": 5}, {"stackup": {"layers": "x"}}, {"copper_layers": 3},
    {"net_specs": {"SIG": "50 ohm", "X": {"impedance": 50}}, "nets": {"X": [["U1"], 7]},
     "components": [{"ref": "U1"}, "junk"]},
], ids=["empty", "junk-shapes", "stackup-number", "layers-string", "count-3", "junk-entries"])
def test_malformed_designs_never_raise(design):
    errors, warnings = lrs.check(design, LintContext())
    assert isinstance(errors, list) and isinstance(warnings, list)


def test_stackup_errors_fail_g1_through_lint_netlist():
    rep = lint_netlist.lint(_sig({"impedance": 50}, stackup=TWO_LAYER_SIGNAL))
    assert rep["ok"] is False
    assert any(e.startswith("ST-E9") for e in rep["errors"])
    assert not any(e.startswith("LINT-INTERNAL") for e in rep["errors"])


def test_stackup_issues_are_structured_and_unique():
    issues = lrs.stackup_issues(_sig({"impedance": 50, "layers": ["In2.Cu", "In2.Cu"]},
                                     stackup=dict(PRESET_2116)))
    keys = [(i.id, i.severity, i.path, i.message) for i in issues]
    assert len(keys) == len(set(keys))
    assert {i.severity for i in issues} <= {"error", "warning", "info"}

"""stackup_roles (ADR D7.2.3 / D7.2.4): default layer roles, default plane nets
(today's swig_build_board heuristic plus the GND fallback, M7), planes / roles
overrides (ST-E6) and the G1 decision drafts a defaulted net produces.

Unit tier only (pure data, stdlib).
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

import pytest
from conftest import load_example

import decisions
import fab_presets as fp
import stackup_roles as sr

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
EXAMPLES = sorted(p.parent.name for p in (ROOT / "examples").glob("*/netlist.json"))
S, P, M, U = "signal", "plane", "mixed", "unused"


def todays_planes(design: dict) -> dict:
    """swig_build_board.py:389-397 before D7 (the reference for 'as today')."""
    nets_avail = [n for n in design.get("nets", {}) if n != "GND"]
    default_power = (next((n for n in nets_avail if n.startswith("+")), None)
                     or next((n for n in nets_avail if "V" in n.upper()), None))
    planes = {"In1.Cu": "GND"}
    if default_power:
        planes["In2.Cu"] = default_power
    return planes


def triples(layers) -> list[tuple]:
    return [(x.name, x.role, x.net) for x in layers]


RAIL = {"nets": {"GND": [], "SIG": [], "+3V3": [], "VBUS": []}}


# ---------------------------------------------------------------------------
# D7.2.4 default roles 1..10 layers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n, expected", [
    (1, [("F.Cu", S, None), ("B.Cu", U, None)]),
    (2, [("F.Cu", S, None), ("B.Cu", M, "GND")]),
    (4, [("F.Cu", S, None), ("In1.Cu", P, "GND"), ("In2.Cu", P, "+3V3"), ("B.Cu", S, None)]),
    (6, [("F.Cu", S, None), ("In1.Cu", P, "GND"), ("In2.Cu", S, None), ("In3.Cu", S, None),
         ("In4.Cu", P, "+3V3"), ("B.Cu", S, None)]),
    (8, [("F.Cu", S, None), ("In1.Cu", P, "GND"), ("In2.Cu", S, None), ("In3.Cu", P, "+3V3"),
         ("In4.Cu", P, "GND"), ("In5.Cu", S, None), ("In6.Cu", P, "GND"), ("B.Cu", S, None)]),
    (10, [("F.Cu", S, None), ("In1.Cu", P, "GND"), ("In2.Cu", S, None), ("In3.Cu", P, "+3V3"),
          ("In4.Cu", P, "GND"), ("In5.Cu", S, None), ("In6.Cu", P, "GND"), ("In7.Cu", S, None),
          ("In8.Cu", P, "GND"), ("B.Cu", S, None)]),
])
def test_default_roles_follow_d724(n, expected):
    layers = sr.default_roles(n, RAIL)
    assert triples(layers) == expected
    assert all(not x.explicit for x in layers)
    assert all(x.net_source == ("default" if x.net else None) for x in layers)


def test_default_roles_8l_and_up_repeat_the_s_g_pattern():
    for n in range(8, 33, 2):
        roles = [x.role for x in sr.default_roles(n, RAIL)]
        assert roles[:5] == [S, P, S, P, P] and roles[-1] == S and len(roles) == n
        assert roles[5:-1] == [S, P] * ((n - 6) // 2)


@pytest.mark.parametrize("n", [0, 3, 5, 34, -2, 2.0, "4", None, True])
def test_default_roles_reject_impossible_counts(n):
    with pytest.raises(ValueError):
        sr.default_roles(n, RAIL)


def test_default_roles_without_a_design_use_gnd():
    assert triples(sr.default_roles(4)) == [("F.Cu", S, None), ("In1.Cu", P, "GND"),
                                            ("In2.Cu", P, "GND"), ("B.Cu", S, None)]


def test_copper_names_and_kicad_count():
    assert sr.copper_names(1) == ("F.Cu", "B.Cu") and sr.kicad_count(1) == 2
    assert sr.copper_names(6) == ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu")
    assert sr.kicad_count(32) == 32 and len(sr.copper_names(32)) == 32


# ---------------------------------------------------------------------------
# today's heuristic, moved: first rail, and every example's In1/In2 nets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, rail", [
    ("buck_5v3a", "VIN_RAW"), ("rf_lna_gainblock", "VBIAS"), ("rf_coupon_50r", None),
    ("cp2102n_uart", "+3V3"), ("stm32f103_dev", "+3V3"), ("lm358_buffer", "P5V")])
def test_first_power_rail_is_todays_heuristic(name, rail):
    assert sr.first_power_rail(load_example(name)) == rail


@pytest.mark.parametrize("name", EXAMPLES)
def test_default_4l_nets_match_today_except_the_gnd_fallback(name):
    design = load_example(name)
    planes = {x.name: x.net for x in sr.default_roles(4, design) if x.net}
    today = todays_planes(design)
    if "In2.Cu" in today:
        assert planes == today
    else:                          # M7: no rail -> In2 is GND instead of no plane at all
        assert planes == {"In1.Cu": "GND", "In2.Cu": "GND"}


def test_rf_coupon_50r_in2_is_gnd_and_buck_and_lna_keep_todays_rail():
    nets = {n: {x.name: x.net for x in sr.default_roles(4, load_example(n))}
            for n in ("rf_coupon_50r", "buck_5v3a", "rf_lna_gainblock")}
    assert nets["rf_coupon_50r"]["In2.Cu"] == "GND"
    assert nets["buck_5v3a"]["In2.Cu"] == "VIN_RAW"
    assert nets["rf_lna_gainblock"]["In2.Cu"] == "VBIAS"


def test_rail_heuristic_accepts_a_list_of_nets_and_no_nets():
    assert sr.first_power_rail({"nets": ["GND", "VCC", "+5V"]}) == "+5V"
    assert sr.first_power_rail({}) is None
    assert sr.first_power_rail({"nets": None}) is None


# ---------------------------------------------------------------------------
# default_plane_nets: drafts, partial declarations
# ---------------------------------------------------------------------------
def _bare(n: int) -> tuple:
    return tuple(sr.LayerRole(name, role) for name, role in
                 zip(sr.copper_names(n), ("signal", "plane", "plane", "signal"), strict=True))


def test_default_plane_nets_return_one_g1_draft_per_defaulted_layer(tmp_path):
    layers, drafts = sr.default_plane_nets(_bare(4), RAIL)
    assert triples(layers)[1:3] == [("In1.Cu", P, "GND"), ("In2.Cu", P, "+3V3")]
    assert len(drafts) == 2
    for draft in drafts:
        assert draft["stage"] == "G1" and draft["confidence"] == "med"
        assert "id" not in draft and "ts" not in draft
        entry = decisions.append_decision(tmp_path / "x.json", **draft)
        assert entry["stage"] == "G1"
    assert drafts[0]["override"] == "planes.In1.Cu" and "GND" in drafts[0]["decision"]
    assert "+3V3" in drafts[1]["decision"] and "GND" in drafts[1]["alternatives"]
    assert len(decisions.read_decisions(tmp_path / "x.json")) == 2


def test_the_no_rail_draft_says_why_it_is_gnd():
    _, drafts = sr.default_plane_nets(_bare(4), load_example("rf_coupon_50r"))
    assert "no power rail" in drafts[1]["rationale"]


def test_declared_gnd_moves_the_rail_to_the_next_undeclared_layer():
    layers = (sr.LayerRole("F.Cu", S), sr.LayerRole("In1.Cu", P, "GND", True, "declared"),
              sr.LayerRole("In2.Cu", P), sr.LayerRole("B.Cu", S))
    out, drafts = sr.default_plane_nets(layers, RAIL)
    assert triples(out)[2] == ("In2.Cu", P, "+3V3") and len(drafts) == 1


def test_a_declared_rail_leaves_gnd_for_the_undeclared_layer():
    layers = (sr.LayerRole("F.Cu", S), sr.LayerRole("In1.Cu", P, "+3V3", True, "declared"),
              sr.LayerRole("In2.Cu", P), sr.LayerRole("B.Cu", S))
    out, _ = sr.default_plane_nets(layers, RAIL)
    assert triples(out)[2] == ("In2.Cu", P, "GND")


def test_nothing_to_default_gives_no_drafts():
    layers = sr.default_roles(1, RAIL)
    assert sr.default_plane_nets(layers, RAIL) == (layers, ())


# ---------------------------------------------------------------------------
# apply_overrides: planes and preset roles (ST-E6)
# ---------------------------------------------------------------------------
def _defaulted(n: int) -> tuple:
    return tuple(sr.LayerRole(name, role) for name, role in
                 zip(sr.copper_names(n), fp.role_pattern(n), strict=True))


def test_planes_assign_nets_and_turn_a_defaulted_signal_layer_into_a_plane():
    out, issues = sr.apply_overrides(_defaulted(6), planes={"In2.Cu": "GND", "In1.Cu": "GND"})
    assert issues == ()
    assert triples(out)[1:3] == [("In1.Cu", P, "GND"), ("In2.Cu", P, "GND")]
    assert out[2].net_source == "planes" and not out[2].explicit


def test_planes_on_a_defaulted_outer_layer_make_it_mixed():
    out, issues = sr.apply_overrides(_defaulted(4), planes={"B.Cu": "GND"})
    assert issues == () and triples(out)[3] == ("B.Cu", M, "GND")


def test_preset_roles_override_role_and_net():
    out, issues = sr.apply_overrides(_defaulted(4),
                                     roles={"In2.Cu": {"role": "mixed", "net": "+3V3"}})
    assert issues == () and triples(out)[2] == ("In2.Cu", M, "+3V3")
    assert out[2].explicit and out[2].net_source == "declared"


@pytest.mark.parametrize("kwargs, path", [
    ({"planes": {"In7.Cu": "GND"}}, "$.planes['In7.Cu']"),
    ({"planes": {"In1.Cu": ""}}, "$.planes['In1.Cu']"),
    ({"planes": ["In1.Cu"]}, "$.planes"),
    ({"roles": {"In9.Cu": {"role": "plane"}}}, "$.stackup.roles['In9.Cu']"),
    ({"roles": {"In1.Cu": {"role": "ground"}}}, "$.stackup.roles['In1.Cu'].role"),
    ({"roles": {"In1.Cu": {"role": "plane", "colour": "red"}}}, "$.stackup.roles['In1.Cu']"),
    ({"roles": {"In1.Cu": {"role": "signal", "net": "GND"}}}, "$.stackup.roles['In1.Cu'].net"),
    ({"roles": {"In1.Cu": {"role": "unused"}}}, "$.stackup.roles['In1.Cu'].role"),
    ({"roles": {"In1.Cu": "plane"}}, "$.stackup.roles['In1.Cu']"),
    ({"roles": {"In1.Cu": {"role": "plane", "net": "GND"}}, "planes": {"In1.Cu": "+3V3"}},
     "$.planes['In1.Cu']"),
    ({"roles": {"In1.Cu": {"role": "signal"}}, "planes": {"In1.Cu": "GND"}},
     "$.planes['In1.Cu']"),
])
def test_override_errors_are_st_e6_with_their_json_path(kwargs, path):
    _, issues = sr.apply_overrides(_defaulted(4), **kwargs)
    assert [(i.id, i.severity, i.path) for i in issues] == [("ST-E6", "error", path)]
    assert str(issues[0]).startswith("ST-E6")


def test_planes_on_the_unused_layer_of_a_single_sided_board_is_st_e6():
    _, issues = sr.apply_overrides(_defaulted(1), planes={"B.Cu": "GND"})
    assert [(i.id, i.path) for i in issues] == [("ST-E6", "$.planes['B.Cu']")]


def test_overrides_do_not_mutate_their_inputs():
    layers = _defaulted(4)
    roles = {"In2.Cu": {"role": "mixed", "net": "+3V3"}}
    planes = {"In1.Cu": "GND"}
    snapshot = copy.deepcopy((layers, roles, planes))
    out, _ = sr.apply_overrides(layers, roles=roles, planes=planes)
    assert (layers, roles, planes) == snapshot and out is not layers


def test_json_path_matches_jsonschema():
    assert sr.json_path("planes", "In1.Cu") == "$.planes['In1.Cu']"
    assert sr.json_path("stackup", "layers", 3, "plies", 0) == "$.stackup.layers[3].plies[0]"
    assert sr.json_path("net_specs", "RF_IN") == "$.net_specs.RF_IN"
    assert sr.json_path("net_specs", "+3V3") == "$.net_specs['+3V3']"
    assert sr.json_path() == "$"


def test_layer_roles_are_frozen():
    layer = sr.LayerRole("F.Cu", S)
    with pytest.raises(AttributeError):
        layer.role = P                                   # type: ignore[misc]


def test_module_is_stdlib_only_and_short():
    path = TOOLS / "stackup_roles.py"
    text = path.read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    ours = {"fab_presets", "materials"}
    assert not {n for n in names if n not in sys.stdlib_module_names and n not in ours}
    assert len(text.splitlines()) < 800

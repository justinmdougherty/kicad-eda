"""ipc_rules — IPC-2221 clearance / width / via-ampacity engine.

Reference values are recomputed here from the IPC-2221 formulas the module
documents (not copied from its output), so a regression in the arithmetic is
caught rather than enshrined.
"""
from __future__ import annotations

import math

import pytest

import ipc_rules

COPPER_MILS = {0.5: 0.68, 1.0: 1.37, 2.0: 2.74, 3.0: 4.11}


def _ipc_width_mm(current_a: float, temp_rise_c: float, copper_oz: float,
                  layer: str) -> float:
    """IPC-2221: A[mil^2] = (I / (k * dT^0.44))^(1/0.725); W = A / thickness."""
    k = 0.048 if layer == "external" else 0.024
    area = (current_a / (k * temp_rise_c ** 0.44)) ** (1 / 0.725)
    return area / COPPER_MILS[copper_oz] * 0.0254


def _via_ampacity(drill_mm: float, plating_um: float, temp_rise_c: float) -> float:
    area_mils2 = math.pi * (drill_mm / 0.0254) * (plating_um / 25.4)
    return 0.024 * temp_rise_c ** 0.44 * area_mils2 ** 0.725


# --- trace width -------------------------------------------------------------

@pytest.mark.parametrize("current", [0.1, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0])
@pytest.mark.parametrize("temp_rise", [10.0, 20.0, 45.0])
@pytest.mark.parametrize("copper", [0.5, 1.0, 2.0, 3.0])
@pytest.mark.parametrize("layer", ["external", "internal"])
def test_trace_width_matches_ipc2221_formula(current, temp_rise, copper, layer):
    got = ipc_rules.trace_width_mm(current, temp_rise, copper, layer)
    want = _ipc_width_mm(current, temp_rise, copper, layer)
    assert got == pytest.approx(want, abs=6e-5)   # module rounds to 4 dp


def test_trace_width_reference_points():
    # the numbers the docs quote for 1 oz external at 10 C rise
    assert ipc_rules.trace_width_mm(1.0) == pytest.approx(0.302, abs=0.001)
    assert ipc_rules.trace_width_mm(3.0) == pytest.approx(1.375, abs=0.001)


@pytest.mark.parametrize("current", [0.0, -1.0])
def test_trace_width_non_positive_current_is_zero(current):
    assert ipc_rules.trace_width_mm(current) == 0.0


def test_trace_width_monotonic_in_current():
    widths = [ipc_rules.trace_width_mm(i / 10) for i in range(1, 100)]
    assert all(a < b for a, b in zip(widths, widths[1:], strict=False))


@pytest.mark.parametrize("current", [0.2, 1.0, 4.0])
def test_internal_layer_needs_wider_trace_than_external(current):
    ext = ipc_rules.trace_width_mm(current, layer="external")
    inn = ipc_rules.trace_width_mm(current, layer="internal")
    # k halves -> area grows by 2^(1/0.725) ~= 2.6x
    assert inn > ext
    assert inn / ext == pytest.approx(2 ** (1 / 0.725), rel=1e-3)


def test_heavier_copper_needs_narrower_trace():
    w = [ipc_rules.trace_width_mm(2.0, copper_oz=oz) for oz in (0.5, 1.0, 2.0, 3.0)]
    assert w == sorted(w, reverse=True)
    assert w[1] / w[2] == pytest.approx(2.74 / 1.37, rel=1e-3)


def test_higher_allowed_temperature_rise_needs_narrower_trace():
    assert (ipc_rules.trace_width_mm(2.0, temp_rise_c=10)
            > ipc_rules.trace_width_mm(2.0, temp_rise_c=20)
            > ipc_rules.trace_width_mm(2.0, temp_rise_c=45))


@pytest.mark.parametrize("oz,mils", [(0.5, 0.68), (1.0, 1.37), (2.0, 2.74), (3.0, 4.11)])
def test_copper_mils_table_weights(oz, mils):
    assert ipc_rules.copper_mils(oz) == mils


@pytest.mark.parametrize("oz", [0.25, 1.5, 4.0])
def test_copper_mils_other_weights_scale_at_1_37_mil_per_oz(oz):
    assert ipc_rules.copper_mils(oz) == pytest.approx(oz * 1.37)


@pytest.mark.parametrize("oz", [0.0, -1.0, float("nan"), "1", None, True])
def test_copper_mils_rejects_a_bad_weight(oz):
    with pytest.raises(ValueError, match="copper weight"):
        ipc_rules.copper_mils(oz)


def test_stackup_copper_weight_other_than_the_table_is_sized_by_its_thickness():
    # a 1.5 oz board (a stackup can declare any weight) is no longer sized as 1 oz
    area = (2.0 / (0.048 * 10 ** 0.44)) ** (1 / 0.725)
    assert ipc_rules.trace_width_mm(2.0, copper_oz=1.5) == \
        pytest.approx(area / (1.5 * 1.37) * 0.0254, abs=6e-5)
    assert ipc_rules.trace_width_mm(2.0, copper_oz=1.5) < ipc_rules.trace_width_mm(2.0)


# --- clearance (IPC-2221B Table 6-1) -----------------------------------------

@pytest.mark.parametrize("voltage,column,expected", [
    (5, "B2", 0.10), (12, "B2", 0.10), (30, "B2", 0.10),
    (31, "B2", 0.60), (48, "B2", 0.60), (100, "B2", 0.60),
    (101, "B2", 0.60), (150, "B2", 0.60), (170, "B2", 1.25),
    (250, "B2", 1.25), (300, "B2", 1.25), (301, "B2", 2.50), (500, "B2", 2.50),
    (5, "B1", 0.05), (48, "B1", 0.10), (150, "B1", 0.20), (400, "B1", 0.25),
    (5, "B4", 0.05), (48, "B4", 0.13), (150, "B4", 0.40), (400, "B4", 0.80),
])
def test_clearance_table_lookup(voltage, column, expected):
    assert ipc_rules.clearance_mm(voltage, column) == expected


def test_clearance_default_column_is_external_uncoated():
    assert ipc_rules.clearance_mm(48) == ipc_rules.clearance_mm(48, "B2")


@pytest.mark.parametrize("voltage,column,expected", [
    (600, "B2", 2.50 + 100 * 0.005),
    (1000, "B1", 0.25 + 500 * 0.0025),
    (600, "B4", 0.80 + 100 * 0.00305),
])
def test_clearance_over_500v_uses_per_volt_slope(voltage, column, expected):
    assert ipc_rules.clearance_mm(voltage, column) == pytest.approx(expected, abs=1e-4)


def test_clearance_is_continuous_at_500v():
    for col in ("B1", "B2", "B4"):
        assert ipc_rules.clearance_mm(500.0, col) == \
            pytest.approx(ipc_rules.clearance_mm(500.0001, col), abs=1e-3)


def test_clearance_uses_voltage_magnitude():
    assert ipc_rules.clearance_mm(-48) == ipc_rules.clearance_mm(48)


@pytest.mark.parametrize("column", ["B1", "B2", "B4"])
def test_clearance_monotonic_in_voltage(column):
    vals = [ipc_rules.clearance_mm(v, column) for v in range(0, 1200, 7)]
    assert all(a <= b for a, b in zip(vals, vals[1:], strict=False))


def test_columns_ordered_internal_lt_coated_lt_uncoated():
    for v in (5, 48, 150, 400, 800):
        assert (ipc_rules.clearance_mm(v, "B1") <= ipc_rules.clearance_mm(v, "B4")
                <= ipc_rules.clearance_mm(v, "B2"))


def test_clearance_invalid_column_raises():
    with pytest.raises(KeyError):
        ipc_rules.clearance_mm(48, "B3")


# --- vias ----------------------------------------------------------------------

def test_via_ampacity_docs_reference_points():
    # docs/PIPELINE.md: one 0.3 mm-drill via ~ 0.9 A, 0.9 mm ~ 2 A at dT 10 C
    assert 0.85 <= ipc_rules.via_ampacity_a(0.3) <= 0.95
    assert 1.90 <= ipc_rules.via_ampacity_a(0.9) <= 2.10


@pytest.mark.parametrize("drill", [0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 0.9, 1.2])
@pytest.mark.parametrize("plating", [20.0, 25.0, 35.0])
def test_via_ampacity_matches_barrel_formula(drill, plating):
    assert ipc_rules.via_ampacity_a(drill, plating) == \
        pytest.approx(_via_ampacity(drill, plating, 10.0), abs=6e-4)


def test_via_ampacity_monotonic_in_drill_and_plating():
    by_drill = [ipc_rules.via_ampacity_a(d / 10) for d in range(2, 13)]
    assert all(a < b for a, b in zip(by_drill, by_drill[1:], strict=False))
    assert ipc_rules.via_ampacity_a(0.3, plating_um=35) > ipc_rules.via_ampacity_a(0.3)


@pytest.mark.parametrize("drill", [0.0, -0.3])
def test_via_ampacity_non_positive_drill_is_zero(drill):
    assert ipc_rules.via_ampacity_a(drill) == 0.0


def test_vias_for_current_ceil_of_ratio():
    cap = ipc_rules.via_ampacity_a(0.3)
    assert ipc_rules.vias_for_current(5.0, 0.3) == math.ceil(5.0 / cap) == 6
    assert ipc_rules.vias_for_current(cap, 0.3) == 1        # exactly one barrel
    assert ipc_rules.vias_for_current(cap * 1.01, 0.3) == 2  # just over -> two


@pytest.mark.parametrize("current", [0.0, -2.0])
def test_vias_for_current_non_positive_needs_one(current):
    assert ipc_rules.vias_for_current(current, 0.3) == 1


def test_vias_for_current_zero_drill_needs_one():
    # a zero-capacity barrel cannot be divided by; the function returns the
    # minimum rather than raising ZeroDivisionError
    assert ipc_rules.vias_for_current(3.0, 0.0) == 1


# --- net_rule / resolve_netclasses ------------------------------------------

@pytest.mark.parametrize("cls", [1, 2, 3])
def test_net_rule_signal_net_sits_on_class_floor(cls):
    prof = ipc_rules.CLASS_PROFILES[cls]
    rule = ipc_rules.net_rule(3.3, 0.0, cls)
    assert rule["track_width"] == prof["min_track"]
    assert rule["clearance"] == prof["min_clearance"]
    assert rule["voltage"] == 3.3 and rule["current"] == 0.0


def test_net_rule_current_width_dominates_floor():
    rule = ipc_rules.net_rule(12, 3.0, 3)
    assert rule["track_width"] == pytest.approx(1.375, abs=0.001)
    assert rule["clearance"] == 0.20          # class floor beats 0.10 table value


def test_net_rule_voltage_clearance_dominates_floor():
    rule = ipc_rules.net_rule(250, 0.0, 3)
    assert rule["clearance"] == 1.25
    assert rule["track_width"] == 0.20        # floor


def test_net_rule_column_and_copper_are_honoured():
    assert ipc_rules.net_rule(250, 0.0, 1, column="B1")["clearance"] == 0.20
    assert (ipc_rules.net_rule(12, 3.0, 1, copper_oz=2.0)["track_width"]
            < ipc_rules.net_rule(12, 3.0, 1, copper_oz=1.0)["track_width"])


def test_net_rule_invalid_class_raises():
    with pytest.raises(KeyError):
        ipc_rules.net_rule(3.3, 0.0, 4)


def test_class_profiles_tighten_with_class():
    p = ipc_rules.CLASS_PROFILES
    for key in ("min_clearance", "min_track", "min_annular_ring", "min_drill",
                "edge_clearance"):
        assert p[1][key] <= p[2][key] <= p[3][key]


def test_resolve_netclasses_hat_yields_rule_per_net(hat_design):
    cls = hat_design["class"]
    rules = ipc_rules.resolve_netclasses(hat_design, cls)
    nets = set(hat_design["nets"])
    assert nets <= set(rules)
    assert rules["_class"] == ipc_rules.CLASS_PROFILES[cls]["name"]
    assert rules["_default"] == ipc_rules.net_rule(3.3, 0.0, cls)
    specs = hat_design["net_specs"]
    for net in nets:
        s = specs.get(net, {})
        assert rules[net] == ipc_rules.net_rule(
            s.get("voltage", 3.3), s.get("current", 0.0), cls)
    # a 4 A rail is sized far above the floor; an unannotated net is not
    assert rules["5V_PI"]["track_width"] > 1.5
    unannotated = next(n for n in nets if n not in specs)
    assert rules[unannotated]["track_width"] == ipc_rules.CLASS_PROFILES[cls]["min_track"]


def test_resolve_netclasses_without_specs_all_default(buck_design):
    del buck_design["net_specs"]
    rules = ipc_rules.resolve_netclasses(buck_design, 3)
    for net in buck_design["nets"]:
        assert rules[net] == rules["_default"]


def test_resolve_netclasses_empty_design():
    rules = ipc_rules.resolve_netclasses({}, 2)
    assert set(rules) == {"_default", "_class"}


# --- stackup layers (D7-21): inner copper per IPC-2221 internal k 0.024 -------

def _cu(name: str, oz: float, role: str = "signal", net: str | None = None) -> dict:
    return {"name": name, "oz": oz, "role": role, "net": net}


# JLC06161H-2116 roles: SIG / GND / SIG / SIG / +3V3 / SIG, 1 oz outer, 0.5 oz inner
COPPER_6L = (_cu("F.Cu", 1), _cu("In1.Cu", 0.5, "plane", "GND"), _cu("In2.Cu", 0.5),
             _cu("In3.Cu", 0.5), _cu("In4.Cu", 0.5, "plane", "+3V3"), _cu("B.Cu", 1))
# the 4L default: both inner layers are planes, so no net routes inside
COPPER_4L = (_cu("F.Cu", 1), _cu("In1.Cu", 0.5, "plane", "GND"),
             _cu("In2.Cu", 0.5, "plane", "+3V3"), _cu("B.Cu", 1))
TWO_AMPS = {"nets": {"VIN": [], "SDA": []}, "net_specs": {"VIN": {"current": 2.0}}}


def test_layer_widths_2a_outer_1oz_vs_inner_half_oz():
    widths = ipc_rules.layer_widths(2.0, COPPER_6L)
    assert list(widths) == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]     # plane layers excluded
    assert widths["F.Cu"] == widths["B.Cu"] == pytest.approx(0.786, abs=0.01)
    assert widths["In2.Cu"] == widths["In3.Cu"] == pytest.approx(4.119, abs=0.01)
    assert widths["In2.Cu"] == ipc_rules.trace_width_mm(2.0, 10.0, 0.5, "internal")
    assert widths["F.Cu"] == ipc_rules.trace_width_mm(2.0, 10.0, 1.0, "external")


def test_layer_widths_follow_each_layers_weight_and_role():
    copper = (_cu("F.Cu", 2), _cu("In1.Cu", 1, "mixed", "GND"), _cu("In2.Cu", 0.5),
              _cu("B.Cu", 1, "unused"))
    widths = ipc_rules.layer_widths(1.0, copper, temp_rise_c=20.0)
    assert list(widths) == ["F.Cu", "In1.Cu", "In2.Cu"]             # mixed routes, unused never
    assert widths["F.Cu"] == ipc_rules.trace_width_mm(1.0, 20.0, 2.0, "external")
    assert widths["In1.Cu"] == ipc_rules.trace_width_mm(1.0, 20.0, 1.0, "internal")
    assert widths["In2.Cu"] > widths["In1.Cu"]


def test_layer_widths_weight_from_thickness_when_oz_is_absent():
    copper = ({"name": "F.Cu", "thickness": 0.07, "role": "signal"},)
    assert ipc_rules.layer_widths(2.0, copper) == {
        "F.Cu": ipc_rules.trace_width_mm(2.0, 10.0, 2.0, "external")}


@pytest.mark.parametrize("entry", [{"name": "F.Cu", "role": "signal"},
                                   {"name": "F.Cu", "oz": -1, "role": "signal"},
                                   {"oz": 1, "role": "signal"}, "F.Cu"])
def test_layer_widths_reject_a_malformed_copper_entry(entry):
    with pytest.raises(ValueError):
        ipc_rules.layer_widths(1.0, (entry,))


def test_outer_copper_oz_is_the_thinnest_routable_outer_layer():
    assert ipc_rules.outer_copper_oz(COPPER_6L) == 1
    assert ipc_rules.outer_copper_oz((_cu("F.Cu", 2), _cu("B.Cu", 1, "mixed", "GND"))) == 1
    assert ipc_rules.outer_copper_oz((_cu("F.Cu", 2), _cu("B.Cu", 0.5, "unused"))) == 2
    assert ipc_rules.outer_copper_oz(()) is None


def test_layer_warnings_2a_outer_0786_inner_4119_is_a_warning_only():
    rules = ipc_rules.resolve_netclasses(TWO_AMPS, 3)
    warnings = ipc_rules.layer_warnings(rules, COPPER_6L)
    assert warnings == [
        "VIN: 2 A needs 0.786 mm on the outer 1 oz copper but 4.119 mm on In2.Cu/In3.Cu "
        "(0.5 oz) by the IPC-2221 internal chart (k 0.024); its class width 0.786 mm is "
        "sized for the outer layers - keep VIN on the outer layers or widen it where it "
        "runs inside (a warning, not a rule: IPC-2152 superseded the IPC-2221 charts)"]
    assert rules["VIN"]["track_width"] == pytest.approx(0.786, abs=0.001)   # never forced


def test_layer_warnings_group_inner_layers_by_weight():
    copper = (_cu("F.Cu", 1), _cu("In1.Cu", 1, "mixed", "GND"), _cu("In2.Cu", 0.5),
              _cu("B.Cu", 1))
    rules = ipc_rules.resolve_netclasses(TWO_AMPS, 3)
    (warning,) = ipc_rules.layer_warnings(rules, copper)
    w1 = ipc_rules.trace_width_mm(2.0, 10.0, 1.0, "internal")
    assert f"but {w1:.3f} mm on In1.Cu (1 oz) and 4.119 mm on In2.Cu (0.5 oz) by" in warning


def test_layer_warnings_none_without_routable_inner_copper():
    rules = ipc_rules.resolve_netclasses(TWO_AMPS, 3)
    assert ipc_rules.layer_warnings(rules, COPPER_4L) == []
    assert ipc_rules.layer_warnings(rules, (_cu("F.Cu", 1), _cu("B.Cu", 1, "mixed", "GND"))) == []


def test_layer_warnings_skip_plane_nets_and_nets_the_class_width_covers():
    design = {"nets": {"VIN": [], "SDA": [], "LED": []},
              "net_specs": {"VIN": {"current": 2.0}, "LED": {"current": 0.02}}}
    rules = ipc_rules.resolve_netclasses(design, 3)
    assert ipc_rules.layer_warnings(rules, COPPER_6L, plane_nets=("VIN",)) == []
    # 20 mA on 0.5 oz inner copper needs less than the 0.2 mm class floor
    assert ipc_rules.trace_width_mm(0.02, 10.0, 0.5, "internal") < 0.2
    assert [w.split(":")[0] for w in ipc_rules.layer_warnings(rules, COPPER_6L)] == ["VIN"]


def test_layer_warnings_follow_the_temperature_rise():
    rules = ipc_rules.resolve_netclasses(TWO_AMPS, 3, temp_rise_c=30.0)
    (warning,) = ipc_rules.layer_warnings(rules, COPPER_6L, temp_rise_c=30.0)
    inner = ipc_rules.trace_width_mm(2.0, 30.0, 0.5, "internal")
    assert f"{inner:.3f} mm on In2.Cu/In3.Cu" in warning


def test_layer_warnings_without_outer_copper_name_the_inner_need_only():
    rules = ipc_rules.resolve_netclasses(TWO_AMPS, 3)
    (warning,) = ipc_rules.layer_warnings(rules, (_cu("In2.Cu", 0.5),))
    assert warning.startswith("VIN: 2 A needs 4.119 mm on In2.Cu (0.5 oz) by the IPC-2221")


def test_ipc_rules_stays_stdlib_only_for_kicad_python():
    # swig_board_fixes / swig_routed_checks import it inside KiCad's bundled Python
    import ast
    import sys
    from pathlib import Path
    tree = ast.parse(Path(ipc_rules.__file__).read_text(encoding="utf-8"))
    nodes = list(ast.walk(tree))
    roots = {(n.module or "").split(".")[0] for n in nodes if isinstance(n, ast.ImportFrom)}
    roots |= {a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import)
              for a in n.names}
    assert roots - {"__future__"} <= set(sys.stdlib_module_names)

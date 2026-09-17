"""thermal_check — dissipation vs package limits (G2 input)."""
from __future__ import annotations

import pytest

import thermal_check as tc


def _design(*components: dict, nets: dict | None = None,
            net_specs: dict | None = None, thermal: dict | None = None) -> dict:
    d = {"components": list(components), "nets": nets or {},
         "net_specs": net_specs or {}}
    if thermal is not None:
        d["thermal"] = thermal
    return d


def _two_terminal(ref: str, lib: str, value: str, footprint: str,
                  i_a: float, extra: dict | None = None) -> dict:
    """A part in series between two nets both carrying `i_a`."""
    comp = {"ref": ref, "lib_id": lib, "value": value, "footprint": footprint,
            **(extra or {})}
    return _design(comp, nets={"IN": [[ref, "1"], ["X", "1"]],
                               "OUT": [[ref, "2"], ["X", "2"]]},
                   net_specs={"IN": {"voltage": 12, "current": i_a},
                              "OUT": {"voltage": 12, "current": i_a}})


# --- declared numbers gate hard ----------------------------------------------

def test_declared_p_diss_over_limit_blocks():
    d = _design({"ref": "U1", "lib_id": "Foo:Bar", "footprint": "Package_SO:SOIC-8",
                 "thermal": {"p_diss": 2.0}})
    rep = tc.check(d)
    assert rep["warnings"] == []
    assert len(rep["blockers"]) == 1
    b = rep["blockers"][0]
    # SOIC theta 110 K/W: Tj = 50 + 2*110 = 270 C vs 80% limit 130 C
    assert b.startswith("U1: ~2.00 W (declared p_diss) x thetaJA 110.0 K/W")
    assert "Tj ~270 C at Tamb 50.0 C" in b and "(130 C of Tj_max 150.0 C)" in b


def test_declared_theta_and_tj_max_override_package_defaults():
    d = _design({"ref": "U1", "lib_id": "Foo:Bar", "footprint": "Package_SO:SOIC-8",
                 "thermal": {"p_diss": 1.0, "theta_ja": 30, "tj_max": 175}})
    # Tj = 50 + 30 = 80 <= 50 + 0.8*(175-50) = 150
    assert tc.check(d) == {"blockers": [], "warnings": [], "ep_via_parts": []}


def test_declared_p_diss_applies_to_any_lib_even_passives():
    d = _design({"ref": "R9", "lib_id": "Device:R", "value": "10k",
                 "footprint": "Resistor_SMD:R_0603_1608Metric",
                 "thermal": {"p_diss": 5.0}})
    assert len(tc.check(d)["blockers"]) == 1


# --- estimates only warn -----------------------------------------------------

def test_switching_regulator_estimate_only_warns(hat_design):
    rep = tc.check(hat_design)
    assert rep["blockers"] == []
    assert [w.split(":")[0] for w in rep["warnings"]] == ["U2", "U3"]
    for w in rep["warnings"]:
        assert "estimate — declare thermal.p_diss for exact" in w
        assert "~4% of 20.0 V x" in w


def test_declaring_p_diss_turns_the_estimate_into_a_hard_verdict(hat_design):
    u2 = next(c for c in hat_design["components"] if c["ref"] == "U2")
    u2["thermal"] = {"p_diss": 2.8}
    rep = tc.check(hat_design)
    assert [b.split(":")[0] for b in rep["blockers"]] == ["U2"]
    assert [w.split(":")[0] for w in rep["warnings"]] == ["U3"]
    u2["thermal"] = {"p_diss": 0.5}
    rep = tc.check(hat_design)
    assert rep["blockers"] == [] and [w.split(":")[0] for w in rep["warnings"]] == ["U3"]


# --- ambient / margin overrides ----------------------------------------------

def test_ambient_and_margin_overrides_change_the_verdict(hat_design):
    # defaults (50 C, 80% of 150 C = 130 C): U2 ~162 C and U3 ~178 C both warn
    assert [w[:2] for w in tc.check(hat_design)["warnings"]] == ["U2", "U3"]
    # lab bench at 25 C, run to the full Tj_max: U2 = 25 + 2.8*40 = 137 C passes,
    # U3 = 25 + 3.2*40 = 153 C still over 150 C
    hat_design["thermal"] = {"t_ambient": 25, "tj_margin": 1.0}
    assert [w[:2] for w in tc.check(hat_design)["warnings"]] == ["U3"]
    hat_design["thermal"] = {"t_ambient": 20, "tj_margin": 1.0}
    assert tc.check(hat_design)["warnings"] == []       # U3 = 148 C <= 150 C
    hat_design["thermal"] = {"t_ambient": 20, "tj_margin": 0.8}
    assert len(tc.check(hat_design)["warnings"]) == 2   # limit back to 124 C
    hat_design["thermal"] = {"t_ambient": 85}
    ws = tc.check(hat_design)["warnings"]
    assert len(ws) == 2 and "Tamb 85.0 C" in ws[0]


def test_declared_blocker_clears_with_generous_margin():
    comp = {"ref": "U1", "lib_id": "Foo:Bar", "footprint": "Package_SO:SOIC-8",
            "thermal": {"p_diss": 0.8}}          # Tj = 50 + 88 = 138 > 130
    assert tc.check(_design(comp))["blockers"]
    assert tc.check(_design(comp, thermal={"tj_margin": 0.95}))["blockers"] == []
    assert tc.check(_design(comp, thermal={"t_ambient": 25}))["blockers"] == []


def test_non_dict_thermal_config_is_ignored(hat_design):
    hat_design["thermal"] = "hot"
    assert len(tc.check(hat_design)["warnings"]) == 2


# --- exposed-pad via farm ----------------------------------------------------

def test_ep_via_parts_for_dissipating_exposed_pad_parts(buck_design, hat_design):
    # buck: LM2596 in TO-263 dissipates ~4% of 12 V x 3 A = 1.44 W >= 0.8 W
    rep = tc.check(buck_design)
    assert rep["ep_via_parts"] == ["U1"] and rep["blockers"] == [] and rep["warnings"] == []
    assert tc.check(hat_design)["ep_via_parts"] == ["U2", "U3"]


def test_ep_via_parts_needs_both_power_and_an_ep_package():
    hot_no_ep = _design({"ref": "U1", "lib_id": "Foo:Bar", "footprint": "Package_SO:SOIC-8",
                         "thermal": {"p_diss": 2.0}})
    assert tc.check(hot_no_ep)["ep_via_parts"] == []
    cool_ep = _design({"ref": "U1", "lib_id": "Foo:Bar",
                       "footprint": "Package_TO_SOT_SMD:TO-263-5_TabPin3",
                       "thermal": {"p_diss": 0.5}})
    assert tc.check(cool_ep)["ep_via_parts"] == []
    warm_ep = _design({"ref": "U1", "lib_id": "Foo:Bar",
                       "footprint": "Package_TO_SOT_SMD:TO-263-5_TabPin3",
                       "thermal": {"p_diss": 0.8}})
    assert tc.check(warm_ep)["ep_via_parts"] == ["U1"]     # threshold is inclusive


def test_ep_via_parts_sorted_and_deduplicated():
    parts = [{"ref": r, "lib_id": "Foo:Bar", "footprint": "Package_SO:HSOP-8-1EP",
              "thermal": {"p_diss": 1.0}} for r in ("U3", "U1", "U2")]
    assert tc.check(_design(*parts))["ep_via_parts"] == ["U1", "U2", "U3"]


# --- resistors / shunts ------------------------------------------------------

def test_shunt_within_derated_rating_is_silent():
    d = _two_terminal("RS1", "Device:R", "0.01R 1% 3W",
                      "Resistor_SMD:R_2512_6332Metric", 3.0)   # 0.09 W vs 1.5 W
    assert tc.check(d) == {"blockers": [], "warnings": [], "ep_via_parts": []}


def test_resistor_over_declared_rating_blocks():
    d = _two_terminal("R1", "Device:R", "1R 0.1W",
                      "Resistor_SMD:R_0603_1608Metric", 3.0)    # 9 W vs 0.05 W
    rep = tc.check(d)
    assert rep["warnings"] == []
    assert rep["blockers"] == ["R1 (1R 0.1W): dissipates 9.00 W at 3.0 A but the "
                               "derated rating is 0.05 W (50% of 0.1 W)"]


def test_resistor_over_estimated_rating_only_warns():
    d = _two_terminal("R1", "Device:R", "1R", "Resistor_SMD:R_1206_3216Metric", 1.0)
    rep = tc.check(d)                                            # 1 W vs 0.125 W
    assert rep["blockers"] == []
    assert rep["warnings"] == ["R1 (1R): dissipates 1.00 W at 1.0 A but the "
                               "derated rating is 0.12 W (50% of 0.25 W)"]


def test_resistor_unknown_footprint_defaults_to_100mw():
    d = _two_terminal("R1", "Device:R", "1R", "Custom:R_odd", 1.0)
    assert "(50% of 0.1 W)" in tc.check(d)["warnings"][0]


@pytest.mark.parametrize("value", ["10k", "1M", "220R", "abc", ""])
def test_signal_level_or_unparseable_resistors_are_skipped(value):
    d = _two_terminal("R1", "Device:R", value, "Resistor_SMD:R_0402_1005Metric", 5.0)
    assert tc.check(d) == {"blockers": [], "warnings": [], "ep_via_parts": []}


def test_resistor_i_max_override_caps_series_current(hat_design):
    # RS2 (10R, 2512) sits on 3 A nets but declares i_max 1 mA (bypassed range
    # shunt): without the override it would dissipate 90 W
    rs2 = next(c for c in hat_design["components"] if c["ref"] == "RS2")
    assert rs2["thermal"] == {"i_max": 0.001}
    assert not any("RS2" in m for m in tc.check(hat_design)["warnings"])
    del rs2["thermal"]
    rep = tc.check(hat_design)
    assert any(m.startswith("RS2 (10R 1%): dissipates 90.00 W at 3.0 A") for m in rep["warnings"])


def test_series_current_takes_second_highest_attached_net():
    d = _design({"ref": "R1", "lib_id": "Device:R", "value": "1R", "footprint": "0603"},
                nets={"A": [["R1", "1"]], "B": [["R1", "2"]], "SENSE": [["R1", "3"]]},
                net_specs={"A": {"current": 3.0}, "B": {"current": 2.0},
                           "SENSE": {"current": 0.0}})
    assert tc._series_i(d, "R1") == 2.0
    assert tc._series_i(d, "R9") == 0.0
    assert tc._series_i({"nets": {"A": [["R1", "1"]]}, "net_specs": {"A": {"current": 3}}},
                        "R1") == 0.0          # a single net is not a series path


# --- FETs ----------------------------------------------------------------------

FET_FP = "Package_SO:Vishay_PowerPAK_1212-8_Single"


def test_fet_with_declared_rds_blocks_when_hot():
    d = _two_terminal("Q1", "Transistor_FET:Si7617DN", "Si7617DN", FET_FP, 3.0,
                      {"rds_mohm": 200})            # 1.8 W x 50 K/W -> 140 C
    rep = tc.check(d)
    assert rep["warnings"] == []
    assert rep["blockers"][0].startswith("Q1: ~1.80 W (I^2*Rds at 3.0 A, 200.0 mOhm)")
    assert rep["ep_via_parts"] == ["Q1"]            # PowerPAK is an EP package


def test_fet_rds_may_be_declared_under_thermal():
    d = _two_terminal("Q1", "Transistor_FET:Si7617DN", "Si7617DN", FET_FP, 3.0,
                      {"thermal": {"rds_mohm": 200}})
    assert len(tc.check(d)["blockers"]) == 1


def test_fet_estimated_rds_only_warns():
    d = _two_terminal("Q1", "Transistor_FET:Si7617DN", "Si7617DN", FET_FP, 15.0)
    rep = tc.check(d)                                # 2.25 W est -> 162 C
    assert rep["blockers"] == []
    assert "(estimate — declare rds_mohm)" in rep["warnings"][0]


def test_small_signal_fet_is_skipped():
    d = _two_terminal("Q2", "Transistor_FET:BSS138", "BSS138",
                      "Package_TO_SOT_SMD:SOT-23", 0.4, {"rds_mohm": 5000})
    assert tc.check(d)["blockers"] == []


# --- regulators --------------------------------------------------------------

def test_linear_regulator_dropout_power_blocks():
    d = _design({"ref": "U1", "lib_id": "Regulator_Linear:AMS1117-3.3",
                 "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2"},
                nets={"VIN": [["U1", "3"], ["J1", "1"]], "3V3": [["U1", "2"], ["C1", "1"]],
                      "GND": [["U1", "1"], ["C1", "2"]]},
                net_specs={"VIN": {"voltage": 12, "current": 0.5},
                           "3V3": {"voltage": 3.3, "current": 0.5},
                           "GND": {"voltage": 0, "current": 0.5}})
    rep = tc.check(d)                                 # 8.7 V x 0.5 A = 4.35 W
    assert rep["blockers"][0].startswith("U1: ~4.35 W ((Vin 12.0 - Vout 3.3) * 0.5 A)")
    assert rep["warnings"] == []


def test_linear_regulator_without_voltage_intent_is_skipped():
    d = _design({"ref": "U1", "lib_id": "Regulator_Linear:AMS1117-3.3",
                 "footprint": "SOT-223"},
                nets={"VIN": [["U1", "3"]], "3V3": [["U1", "2"]]})
    assert tc.check(d) == {"blockers": [], "warnings": [], "ep_via_parts": []}


def test_switching_regulator_without_intent_is_skipped(buck_design):
    del buck_design["net_specs"]
    assert tc.check(buck_design) == {"blockers": [], "warnings": [], "ep_via_parts": []}


def test_unknown_lib_without_declaration_is_ignored():
    d = _design({"ref": "J1", "lib_id": "Connector:USB_C", "footprint": "X"})
    assert tc.check(d) == {"blockers": [], "warnings": [], "ep_via_parts": []}


# --- parsers -----------------------------------------------------------------

@pytest.mark.parametrize("value,ohms", [
    ("0.01R 1% 3W", 0.01), ("10R", 10.0), ("10", 10.0), ("10m", 0.01),
    ("4.7k", 4700.0), ("10K", 10000.0), ("1M", 1e6), ("0R", 0.0),
    ("abc", None), ("", None), (None, None),
])
def test_parse_ohms(value, ohms):
    assert tc._parse_ohms(value) == ohms


def test_parse_ohms_r_notation_drops_trailing_digits():
    # BUG: "4k7" (standard R-notation for 4.7 k) parses as 4000 because the
    # regex consumes the digit after the multiplier and discards it. Any
    # "NkM"/"NRM" value is therefore under-read. Documented, not fixed here.
    assert tc._parse_ohms("4k7") == 4000.0
    assert tc._parse_ohms("0R01") == 0.0


@pytest.mark.parametrize("value,watts", [("0.01R 1% 3W", 3.0), ("1R 0.1W", 0.1),
                                         ("1R", None), ("2W", 2.0)])
def test_parse_rating_w(value, watts):
    assert tc._parse_rating_w(value) == watts


def test_theta_ja_lookup_and_defaults():
    assert tc._theta_ja("Package_SO:Texas_HSOP-8-1EP", None) == 40.0
    assert tc._theta_ja("Package_TO_SOT_SMD:SOT-23", None) == 250.0
    assert tc._theta_ja("Custom:weird", None) == 100.0
    assert tc._theta_ja("Package_SO:SOIC-8", 12) == 12.0
    assert tc._theta_ja("", None) == 100.0


def test_r_rating_by_size_code():
    assert tc._r_rating("Resistor_SMD:R_2512_6332Metric") == 1.0
    assert tc._r_rating("Resistor_SMD:R_0402_1005Metric") == 0.063
    assert tc._r_rating("Resistor_THT:R_Axial") == 0.1

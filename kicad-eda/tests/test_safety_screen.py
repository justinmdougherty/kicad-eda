"""safety_screen — the D5 safety detector behind the review report's HUMAN
REVIEW REQUIRED banner and the G5 safety warning: positives and negatives for
every heuristic documented in detect_safety, including the false positives
and misses an earlier review found.

Pure unit tier, no KiCad.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import safety_screen as ss

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

ALL_EXAMPLES = ("buck_5v3a", "cp2102n_uart", "esp32c3_env_node", "lm324_quad", "lm358_buffer",
                "rf_coupon_50r", "rf_lna_gainblock", "rpi5_battery_hat", "stm32f103_dev",
                "usb_afe")


# --- safety detector: positives ---------------------------------------------------

def _design(components=(), nets=None, net_specs=None, assumptions=None) -> dict:
    design = {"components": [dict(c) for c in components] or [
        {"ref": "R1", "lib_id": "Device:R", "value": "10k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"}],
        "nets": nets or {"GND": [["R1", "1"]], "SIG": [["R1", "2"]]}}
    if net_specs is not None:
        design["net_specs"] = net_specs
    if assumptions is not None:
        design["assumptions"] = assumptions
    return design


def _part(ref, lib_id, value="", footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", **kw):
    return {"ref": ref, "lib_id": lib_id, "value": value, "footprint": footprint, **kw}


def _kinds(design) -> set[str]:
    return {f.kind for f in ss.detect_safety(design)}


@pytest.mark.parametrize("design, needle", [
    (_design(net_specs={"HV_BUS": {"voltage": 170.0}}), "170 V"),
    (_design(net_specs={"VNEG": {"voltage": -60}}), "-60 V"),
    (_design(net_specs={"VIN": {"voltage": 50.5}}), "50.5 V"),
    (_design(nets={"AC_L": [["R1", "1"]], "AC_N": [["R1", "2"]]}), "AC_L"),
    (_design(nets={"MAINS_IN": [["R1", "1"]], "GND": [["R1", "2"]]}), "MAINS_IN"),
    (_design(nets={"HV": [["R1", "1"]], "GND": [["R1", "2"]]}), "'HV'"),
    (_design(nets={"230VAC": [["R1", "1"]], "GND": [["R1", "2"]]}), "230VAC"),
    (_design(nets={"+170V": [["R1", "1"]], "GND": [["R1", "2"]]}), "+170V"),
    (_design(nets={"L": [["R1", "1"]], "N": [["R1", "2"]]}), "'L' and 'N'"),
    (_design([_part("F1", "Device:Fuse", "T2A 250V", "Fuse:Fuse_1812_4532Metric")]), "F1"),
    (_design([_part("F1", "Device:Fuse", "2A", "Fuse:Fuse_1812_4532Metric",
                    mpn="0217002.MXP 250VAC")]), "250"),
    (_design([_part("PS1", "Converter_ACDC:HLK-PM01", "HLK-PM01")]), "AC-DC"),
    (_design([_part("J1", "Connector:Conn_Plug_3P_Protected", "IEC")]), "mains plug"),
    (_design([_part("RV1", "Device:Varistor", "S14K275", "Varistor:RV_Disc_D15.5mm_W5mm_P7.5mm")]),
     "275"),
    (_design([_part("F1", "Device:Fuse", "T1A 125V", "Fuse:Fuse_Littelfuse_372_D8.50mm")]),
     "125 V"),                                                   # the lowest mains fuse class
    (_design([_part("RV1", "Device:Varistor", "S10K130", "Varistor:RV_Disc_D12mm_W5mm_P7.5mm")]),
     "130"),                                                     # 120 VAC mains MOV
    (_design(nets={"HV_BUS": [["R1", "1"]], "GND": [["R1", "2"]]}), "HV_BUS"),
    (_design(nets={"HVDC": [["R1", "1"]], "LV": [["R1", "2"]]}), "HVDC"),   # LV spares only HV
    # isolated HV designs name their two sides HV_* and LV_*: not a level shifter
    (_design(nets={"HV+": [["R1", "1"]], "HV-": [["R1", "2"]], "LV_12V": [["R1", "1"]],
                   "GND": [["R1", "2"]]}), "'HV+'"),
    (_design(nets={"HV_BUS": [["R1", "1"]], "LV_GND": [["R1", "2"]]}), "HV_BUS"),
    (_design(nets={"HV_DC_LINK": [["R1", "1"]], "VCC_LV": [["R1", "2"]]}), "HV_DC_LINK"),
    (_design(nets={"HV_BUS": [["R1", "1"]], "LV": [["R1", "2"]]}), "HV_BUS"),
    (_design(assumptions=[{"id": "A1", "topic": "input", "value": "230 VAC mains, 50 Hz",
                           "rationale": "concept", "confidence": "high"}]), "A1"),
    # negation must not swallow the dangerous phrasings
    (_design(assumptions=[{"id": "A1", "topic": "input", "value": "non-isolated mains supply",
                           "rationale": "cost", "confidence": "med"}]), "A1"),
    (_design(assumptions=[{"id": "A1", "topic": "isolation", "value": "no isolation from mains",
                           "rationale": "cost", "confidence": "med"}]), "A1"),
    # ACIN nets are mains inputs like AC_L / AC_N (F5-07)
    (_design(nets={"ACIN": [["R1", "1"]], "GND": [["R1", "2"]]}), "'ACIN'"),
    (_design(nets={"ACIN_L": [["R1", "1"]], "ACIN_N": [["R1", "2"]]}), "ACIN_L"),
    (_design(nets={"AC_IN": [["R1", "1"]], "GND": [["R1", "2"]]}), "'AC_IN'"),
    # the level-shifter exemption's suffix is a channel or a <= 50 V rail, never HV_400
    (_design(nets={"HV_400": [["R1", "1"]], "LV_5V": [["R1", "2"]]}), "'HV_400'"),
    (_design(nets={"HV_60": [["R1", "1"]], "LV": [["R1", "2"]]}), "'HV_60'"),
    # disc-varistor codes: 14D471K = 47 x 10^1 V; Littelfuse V<volts>LA
    (_design([_part("RV1", "Device:Varistor", "14D471K", "Varistor:RV_Disc_D15.5mm_W5mm_P7.5mm")]),
     "470 V"),
    (_design([_part("RV1", "Device:Varistor", "10D561K", "Varistor:RV_Disc_D12mm_W5mm_P7.5mm")]),
     "560 V"),
    (_design([_part("RV1", "Device:Varistor", "MOV", "Varistor:RV_Disc_D12mm_W5mm_P7.5mm",
                    mpn="V275LA20AP")]), "275 V"),
])
def test_mains_or_high_voltage_is_flagged(design, needle):
    flags = ss.detect_safety(design)
    assert [f.kind for f in flags] and {f.kind for f in flags} == {ss.SAFETY_MAINS}
    assert any(needle in f.reason for f in flags), flags


def _assumed(topic: str, value: str) -> dict:
    return _design(assumptions=[{"id": "A1", "topic": topic, "value": value,
                                 "rationale": "concept", "confidence": "med"}])


@pytest.mark.parametrize("topic, value", [
    # OFF / EXTERNAL / NOT near the term is not negation (round-2 misses)
    ("power", "Off-line mains flyback, isolated"),
    ("input", "Mains on/off switch on the front panel"),
    ("input", "AC mains with power-off detection"),
    ("input", "Powered from mains through an external IEC inlet"),
    ("input", "Mains input via an external EMI filter"),
    ("input", "No transformer: mains-referenced logic"),
    ("input", "mains"),
    ("input", "120 V line voltage, not isolated"),
    # NO before the term that negates the next word, not the mains
    ("isolation", "no mains isolation (capacitive dropper)"),
    ("protection", "without mains fuse"),
    # F5-07: NO / WITHOUT negate only a term that ends its clause or is followed by
    # same-thing words; EXTERNALLY / ELSEWHERE only at clause end or before a link word
    ("protection", "No mains input fuse, relies on upstream breaker"),
    ("protection", "no mains inlet fuse"),
    ("protection", "No mains input protection"),
    ("filter", "no mains EMI filter"),                         # a documented miss before
    ("input", "mains externally fused 10 A"),
    ("supply", "no mains power indicator"),
    # the topic counts when the value does not name the term itself
    ("mains", "230 VAC through a 1 A fuse"),
    ("mains", "via an isolating transformer"),
])
def test_mains_assumptions_are_flagged(topic, value):
    assert _kinds(_assumed(topic, value)) == {ss.SAFETY_MAINS}


@pytest.mark.parametrize("part", [
    _part("U3", "Battery_Management:MCP73831-2-OT", "MCP73831-2-OT",
          "Package_TO_SOT_SMD:SOT-23-5"),
    _part("U3", "Device:Generic_IC", "TP4056", "Package_SO:SOP-8_3.9x4.9mm_P1.27mm"),
    _part("U3", "Battery_Management:TP4056-42-ESOP8", "TP4056"),
    _part("U3", "Battery_Management:BQ24075RGT", "bq24075", "Package_DFN_QFN:QFN-16"),
    _part("U3", "Power_Management:Custom", "PMIC", mpn="AXP2101"),
    _part("U3", "Power_Management:Custom", "IP5306"),
    _part("U3", "Battery_Management:LTC4054ES5-4.2", "LTC4054"),
    _part("U3", "Device:Generic_IC", "TP5100", "Package_SO:HSOP-8"),        # 1S/2S switcher
    _part("U3", "Battery_Management:MCP73213", "MCP73213", "Package_DFN_QFN:DFN-10"),  # 2-cell
    _part("U3", "Battery_Management:LTC4162-L", "LTC4162-L", "Package_DFN_QFN:QFN-28"),
    _part("U3", "Battery_Management:LTC4120", "LTC4120-4.2"),               # wireless charger
])
def test_li_ion_charger_ics_are_flagged(part):
    flags = ss.detect_safety(_design([part]))
    assert {f.kind for f in flags} == {ss.SAFETY_LIION}
    assert "U3" in flags[0].reason


@pytest.mark.parametrize("value", [
    "1S Li-ion 18650, charged from USB",
    "LiFePO4 cell charged from a 6 V solar panel",
    "LFP pack, rechargeable over USB-C",
    "LiPo pouch: no fuel gauge; charging over USB, discharge to 3.0 V",
    # an external adapter or part feeding an on-board charger is still charging here
    "LiPo cell, charging from an external 5 V adapter",
    "Li-ion charging with external NTC thermistor",
    "1S LiPo charging with an off-the-shelf inductor",
    # NOT / NEVER before a charge verb is a constraint on charging, not its absence
    "1S Li-ion 18650; do not charge below 0 C or above 45 C",
    "1S Li-ion that must never be charged below 0 C",
    "LiPo, charge current not above 1C",
    "Li-ion pack, USB charging",
    # NO before the term that negates the next word
    "Li-ion, no charge LED (status on the MCU)",
    "LiPo pack, no charge timer",
    # F5-07: an off-board word followed by more than a link is not off-board placement
    "LiPo charging elsewhere on the board",
    "LiPo, charging elsewhere on this PCB",
    "Li-ion charging externally supplied 5 V",
    # F5-07 vocabulary: Li ion, Li-Poly(mer), LiPoly, 18650 / 21700 cells
    "1S Li ion cell, charged over USB",
    "Li-Poly 2000 mAh pouch, USB charging",
    "LiPoly pouch, charging at 500 mA",
    "Li-Polymer cell, charge at 0.5C",
    "LIPOLYMER pack with a charger",
    "2x 18650 in parallel, charged over USB-C",
    "single INR21700 cell with USB-C charging",
])
def test_li_ion_charging_assumptions_are_flagged(value):
    design = _design(assumptions=[{"id": "A4", "topic": "battery", "value": value,
                                   "rationale": "concept", "confidence": "med"}])
    assert _kinds(design) == {ss.SAFETY_LIION}


def _assumptions(*pairs: tuple[str, str, str]) -> dict:
    return _design(assumptions=[{"id": i, "topic": t, "value": v, "rationale": "concept",
                                 "confidence": "med"} for i, t, v in pairs])


@pytest.mark.parametrize("pairs, ids", [
    # D5 assumptions are per topic: napkin agents split cell and charging like this
    ((("A3", "battery", "1S LiPo 1000 mAh"), ("A4", "charging", "500 mA from USB-C")),
     ("A3", "A4")),
    ((("A1", "battery", "2x 18650 in parallel"), ("A2", "input", "USB-C 5 V, charges the pack"),
      ("A5", "enclosure", "IP54")), ("A1", "A2")),
    ((("A1", "cells", "Li-ion"), ("A2", "backup", "lithium coin cell"),
      ("A3", "power", "recharged from a 6 V solar panel")), ("A1", "A2", "A3")),
])
def test_lithium_and_charging_in_separate_assumptions_are_one_flag(pairs, ids):
    (flag,) = ss.detect_safety(_assumptions(*pairs))
    assert flag.kind == ss.SAFETY_LIION
    assert all(f"{i} (" in flag.reason for i in ids), flag.reason
    assert "A5" not in flag.reason


def test_an_assumption_with_both_is_flagged_alone():
    design = _assumptions(("A1", "battery", "LiPo, USB charging"),
                          ("A2", "backup", "lithium coin cell"))
    (flag,) = ss.detect_safety(design)
    assert flag.reason.startswith("assumption A1 (battery)") and "A2" not in flag.reason


@pytest.mark.parametrize("pairs", [
    (("A1", "battery", "1S LiPo"), ("A2", "charging", "off-board, by an external charger")),
    (("A1", "battery", "1S LiPo 1000 mAh"), ("A2", "charging", "no on-board charging")),
    (("A1", "battery", "1S LiPo"), ("A2", "charging", "charged externally")),
    (("A1", "battery", "1S LiPo"), ("A2", "input", "USB-C 5 V sink")),
    (("A1", "backup", "non-rechargeable lithium coin cell"), ("A2", "input", "USB-C 5 V sink")),
    (("A1", "battery", "alkaline AA"), ("A2", "charging", "500 mA from USB-C")),
])
def test_separate_assumptions_without_both_are_not_flagged(pairs):
    assert ss.detect_safety(_assumptions(*pairs)) == []


def test_the_value_decides_when_it_names_the_term_itself():
    # the topic's CHARGING is not a second, un-negated mention
    assert ss.detect_safety(_assumed("Li-ion charging", "none here, charged off-board")) == []
    assert ss.detect_safety(_assumed("mains input", "no mains")) == []
    assert _kinds(_assumed("charging", "500 mA, Li-ion pack")) == {ss.SAFETY_LIION}


def test_charger_with_battery_net_names_the_net():
    design = _design([_part("U3", "Battery_Management:MCP73831-2-OT", "MCP73831")],
                     nets={"VBAT": [["U3", "3"]], "GND": [["U3", "2"]]})
    (flag,) = ss.detect_safety(design)
    assert flag.kind == ss.SAFETY_LIION and "VBAT" in flag.reason


def test_battery_and_charge_nets_without_a_known_ic_are_flagged():
    design = _design(nets={"VBAT": [["R1", "1"]], "CHRG": [["R1", "2"]]})
    (flag,) = ss.detect_safety(design)
    assert flag.kind == ss.SAFETY_LIION and "VBAT" in flag.reason and "CHRG" in flag.reason


def test_both_kinds_together():
    design = _design([_part("PS1", "Converter_ACDC:HLK-PM01", "HLK-PM01"),
                      _part("U3", "Battery_Management:MCP73831-2-OT", "MCP73831")])
    assert _kinds(design) == {ss.SAFETY_MAINS, ss.SAFETY_LIION}


# --- safety detector: negatives ---------------------------------------------------

@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_no_example_board_is_flagged(name, load_design):
    assert ss.detect_safety(load_design(name)) == []


@pytest.mark.parametrize("design", [
    _design(net_specs={"V48": {"voltage": 48.0}}),
    _design(net_specs={"VIN": {"voltage": 50.0}}),                 # D5: strictly > 50 V
    _design(nets={"VBAT": [["R1", "1"]], "GND": [["R1", "2"]]}),   # coin-cell RTC backup
    _design(nets={"LINE_OUT": [["R1", "1"]], "AC_COUPLED": [["R1", "2"]]}),
    _design(nets={"HVDD": [["R1", "1"]], "SHV": [["R1", "2"]]}),
    _design(nets={"12V_IN": [["R1", "1"]], "3V3": [["R1", "2"]]}),
    _design(nets={"-48V": [["R1", "1"]], "GND": [["R1", "2"]]}),
    _design([_part("F1", "Device:Polyfuse", "2A", "Fuse:Fuse_1812_4532Metric")]),
    _design([_part("F1", "Device:Fuse", "5A 32V", "Fuse:Fuse_1812_4532Metric")]),
    _design([_part("C1", "Device:C", "100nF 100V", "Capacitor_SMD:C_0805_2012Metric")]),
    _design([_part("U2", "Battery_Management:BQ27441-G1", "BQ27441")]),       # fuel gauge
    _design([_part("U2", "Battery_Management:DW01A", "DW01A")]),              # protector
    _design([_part("U4", "Potentiometer_Digital:MCP45HV51-ST", "MCP45HV51-502")]),
    _design(assumptions=[{"id": "A1", "topic": "input", "value": "USB-C 5 V sink",
                          "rationale": "default", "confidence": "high"}]),
    # low-voltage protection rated above 50 V is not mains-rated (mains classes: >= 125 V)
    _design([_part("F1", "Device:Fuse", "1A 63V", "Fuse:Fuse_1206_3216Metric")]),
    _design([_part("F1", "Device:Polyfuse", "1.1A 60V", "Fuse:Fuse_1812_4532Metric")]),
    _design([_part("RV1", "Device:Varistor", "S10K60", "Varistor:RV_Disc_D12mm_W5mm_P7.5mm")]),
    # battery monitors in the LTC41xx range are not chargers
    _design([_part("U2", "Battery_Management:LTC4150", "LTC4150", "Package_SO:MSOP-10")]),
    _design([_part("U2", "Sensor_Current:LTC4151", "LTC4151-1", "Package_SO:MSOP-10")]),
    # SparkFun-style level shifter: HV / LV are the two logic rails
    _design(nets={"HV": [["R1", "1"]], "LV": [["R1", "2"]]}),
    _design(nets={"HV_5V": [["R1", "1"]], "LV_3V3": [["R1", "2"]], "HV1": [["R1", "1"]],
                  "LV1": [["R1", "2"]]}),
    # lithium named but no charging on this board
    _design(assumptions=[{"id": "A2", "topic": "battery", "value": "1S Li-ion pack, no charging "
                          "on board", "rationale": "concept", "confidence": "med"}]),
    _design(assumptions=[{"id": "A2", "topic": "backup", "value": "non-rechargeable lithium "
                          "coin cell (CR2032)", "rationale": "RTC", "confidence": "high"}]),
    _design(assumptions=[{"id": "A2", "topic": "battery", "value": "LiPo pack charged off-board "
                          "by an external charger", "rationale": "concept", "confidence": "med"}]),
    _design(assumptions=[{"id": "A2", "topic": "battery", "value": "Li-ion, discharge only",
                          "rationale": "concept", "confidence": "med"}]),
    _assumed("battery", "1S Li-ion, no on-board charging"),
    _assumed("battery", "Li-ion pack, charged externally"),
    _assumed("battery", "LiPo pack; charged by an external USB charger"),
    _assumed("battery", "Li-ion cell, external charger"),
    _assumed("battery", "Li-ion pack without charging circuitry"),
    # mains named but not on this board
    _design(assumptions=[{"id": "A1", "topic": "input", "value": "USB-C 5 V sink, no mains",
                          "rationale": "default", "confidence": "high"}]),
    _design(assumptions=[{"id": "A1", "topic": "input", "value": "12 V from an external mains "
                          "adapter", "rationale": "concept", "confidence": "high"}]),
    _assumed("input", "12 V from an external mains power adapter"),
    _assumed("input", "battery powered, without mains"),
    # F5-07: NO / WITHOUT still negate a term followed only by same-thing words
    _assumed("input", "no mains power supply"),
    _assumed("input", "no mains input"),
    _assumed("input", "without mains connection"),
    _assumed("input", "no mains circuit on board"),
    _assumed("input", "no mains onboard"),
    _assumed("battery", "Li-ion pack, no charger IC"),
    _assumed("battery", "LiPo, no charging circuitry on the board"),
    _assumed("battery", "lithium primary cell, non-rechargeable"),
    # ... and EXTERNALLY / ELSEWHERE at clause end or before a link word
    _assumed("battery", "LiPo pack, charged elsewhere"),
    _assumed("battery", "Li-ion pack, charged externally by the user"),
    _assumed("battery", "LiPo; charging elsewhere in the system"),
    # level shifters keep their exemption for channel numbers and <= 50 V rails
    _design(nets={"HV_3V3": [["R1", "1"]], "LV_1.8V": [["R1", "2"]], "HV2": [["R1", "1"]]}),
    _design(nets={"HV_48": [["R1", "1"]], "LV": [["R1", "2"]]}),
    # disc varistors below the mains class: 82 V, 39 V
    _design([_part("RV1", "Device:Varistor", "14D820K", "Varistor:RV_Disc_D15.5mm_W5mm_P7.5mm")]),
    _design([_part("RV1", "Device:Varistor", "07D390K", "Varistor:RV_Disc_D7mm_W3.4mm_P5mm")]),
    {},
])
def test_ordinary_designs_are_not_flagged(design):
    assert ss.detect_safety(design) == []


@pytest.mark.parametrize("design", [
    # the known false positives detect_safety documents: flagging wins over a miss
    _assumed("mains", "none"),
    _assumptions(("A1", "battery", "1S LiPo"), ("A2", "charging", "none")),
    _assumed("battery", "1S LiPo, no Li-ion charging"),
    _assumed("battery", "LiPo, charging handled off-board"),
    _assumed("battery", "Li-ion, charging is not done here"),
    _assumed("input", "no mains present"),
    _assumed("input", "12 V from a mains adapter"),
    _design([_part("F1", "Device:Fuse", "T2A 250V", "Fuse:Fuse_1812_4532Metric")],
            nets={"VIN_12V": [["F1", "1"]], "GND": [["F1", "2"]]}),
    _design(nets={"ACIN": [["R1", "1"]], "GND": [["R1", "2"]]}),        # a PMIC's 5 V ACIN
    _design(nets={"HV_EN": [["R1", "1"]], "GND": [["R1", "2"]]}),
])
def test_documented_false_positives_are_still_flagged(design):
    assert ss.detect_safety(design)
    doc = ss.detect_safety.__doc__
    assert "Known false positives" in doc and "Known misses" in doc


@pytest.mark.parametrize("design, documented", [
    # the known misses detect_safety documents (a change that closes one updates both)
    (_assumed("battery", "1S 14500 cell, USB charging"), "14500"),
    (_design([_part("RV1", "Device:Varistor", "FNR-14K471",
                    "Varistor:RV_Disc_D15.5mm_W5mm_P7.5mm")]), "MOV codes other"),
    (_design(nets={"HV_12": [["R1", "1"]], "LV": [["R1", "2"]]}), "suffix of at most 50"),
])
def test_documented_misses_are_still_missed(design, documented):
    assert ss.detect_safety(design) == []
    assert documented in " ".join(ss.detect_safety.__doc__.split())


# --- G5 warning ---------------------------------------------------------------------

def test_safety_warnings_for_g5(buck_design):
    assert ss.safety_warnings(buck_design) == []
    design = _design([_part("U3", "Battery_Management:MCP73831-2-OT", "MCP73831")])
    (warning,) = ss.safety_warnings(design)
    assert warning.startswith(f"{ss.HUMAN_REVIEW}: Li-ion charging: U3")


def test_safety_lines_join_the_reasons_of_each_kind_mains_first():
    flags = [ss.SafetyFlag(ss.SAFETY_LIION, "U3 charger"), ss.SafetyFlag(ss.SAFETY_MAINS, "a"),
             ss.SafetyFlag(ss.SAFETY_MAINS, "b")]
    assert ss.safety_lines(flags) == ["mains or > 50 V: a; b", "Li-ion charging: U3 charger"]
    assert ss.safety_lines([]) == []


# --- purity -----------------------------------------------------------------------

def test_module_is_stdlib_only():
    tree = ast.parse((TOOLS / "safety_screen.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported

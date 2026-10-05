"""layer_policy (ADR D7.11, wave 2a): design features, the layer-count floors
F1-F3, the declared-count checks (ST-W12 + floor warnings) and the napkin
default (4 layers raised to the floors; JLC04161H-3313 with impedance/RF nets,
else -7628; 6 layers -> JLC06161H-2116) with its netlist patch and decision draft.

Unit tier only (pure data, stdlib).
"""
from __future__ import annotations

import ast
import builtins
import copy
import io
import json
import os
import pathlib
import socket
import sys
from pathlib import Path

import pytest
from conftest import load_example

import decisions
import fab_presets as fp
import layer_policy as lp
import stackup_model as sm

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools"
EXAMPLES = sorted(p.parent.name for p in (ROOT / "examples").glob("*/netlist.json"))
FABS = fp.load_fabs()
P3313, P7628, P2116 = ("jlcpcb:JLC04161H-3313", "jlcpcb:JLC04161H-7628",
                       "jlcpcb:JLC06161H-2116")
QFN48 = "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm_EP5.6x5.6mm"
BGA05 = "Package_BGA:BGA-64_4.0x4.0mm_Layout8x8_P0.5mm"
BGA08 = "Package_BGA:BGA-64_7.0x7.0mm_Layout8x8_P0.8mm"
JEDEC = "ABCDEFGHJKLMNPRTUVWY"


def napkin(design: dict) -> dict:
    return {**design, "mode": "napkin"}


def minimal(**extra) -> dict:
    """A small design: one connector, GND / +3V3 / SIG."""
    base = {"name": "mini",
            "components": [{"ref": "J1", "lib_id": "Connector_Generic:Conn_01x03",
                            "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm"}],
            "nets": {"GND": [["J1", "1"]], "+3V3": [["J1", "2"]], "SIG": [["J1", "3"]]}}
    return {**base, **extra}


def sps_node() -> dict:
    """The SPS target node (PCB_BRIEF): SAMD21 in QFN-48 0.5 mm, an RFM95W on
    LORA_ANT (915 MHz), an LG290P with an SMA feed chain, USB-C full speed."""
    return {
        "name": "sps_node", "mode": "napkin",
        "components": [
            {"ref": "U1", "lib_id": "MCU_Microchip_SAMD:ATSAMD21G18A-M", "footprint": QFN48},
            {"ref": "U2", "lib_id": "RF_Module:RFM95W-868S2",
             "footprint": "RF_Module:HOPERF_RFM9XW_SMD"},
            {"ref": "U3", "lib_id": "SPS_parts:LG290P", "footprint": "SPS_parts:Quectel_LG290P"},
            {"ref": "J1", "lib_id": "Connector:USB_C_Receptacle_USB2.0_16P",
             "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"},
            {"ref": "J2", "lib_id": "Connector:Conn_Coaxial",
             "footprint": "Connector_Coaxial:SMA_Amphenol_132289_EdgeMount"},
            {"ref": "J3", "lib_id": "Connector:Conn_Coaxial",
             "footprint": "Connector_Coaxial:SMA_Amphenol_132289_EdgeMount"},
            {"ref": "L1", "lib_id": "Device:L", "footprint": "Inductor_SMD:L_0402_1005Metric"},
            {"ref": "C1", "lib_id": "Device:C", "footprint": "Capacitor_SMD:C_0402_1005Metric"},
        ],
        "nets": {
            "GND": [["U1", "49"], ["U2", "1"], ["U3", "10"], ["J1", "A1"], ["J2", "2"],
                    ["J3", "2"]],
            "+3V3": [["U1", "36"], ["U2", "3"], ["U3", "4"]],
            "VBUS": [["J1", "A4"]],
            "USB_DP": [["U1", "32"], ["J1", "A6"]],
            "USB_DN": [["U1", "31"], ["J1", "A7"]],
            "LORA_ANT": [["U2", "9"], ["J2", "1"]],
            "GNSS_ANT": [["J3", "1"], ["C1", "1"], ["L1", "1"]],
            "GNSS_RF_IN": [["C1", "2"], ["U3", "11"]],
            "ANT_BIAS": [["L1", "2"], ["U3", "4"]],
        },
        "net_specs": {
            "USB_DP": {"diff": 90, "diff_pair": "USB_DN"},
            "LORA_ANT": {"impedance": 50, "freq_hz": 9.15e8},
            "GNSS_ANT": {"impedance": 50, "freq_hz": 1.57542e9},
            "GNSS_RF_IN": {"impedance": 50, "freq_hz": 1.57542e9},
        },
        "rf": {"chains": [{"name": "GNSS", "from": ["J3", "1"], "to": ["U3", "11"],
                           "nets": ["GNSS_ANT", "GNSS_RF_IN"], "series": ["C1"]}]},
    }


def stripline_design() -> dict:
    return napkin(minimal(net_specs={"SIG": {"impedance": 50, "line": "stripline"}}))


def bga_design(footprint: str = BGA05, **extra) -> dict:
    return napkin(minimal(components=[
        {"ref": "U1", "lib_id": "FPGA:X", "footprint": footprint},
        {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x03",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm"}], **extra))


def ball(row: int, col: int) -> str:
    return f"{JEDEC[row]}{col + 1}"


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------
def test_features_of_lm358_buffer():
    f = lp.features(load_example("lm358_buffer"))
    assert f["napkin"] is False and f["count_declared"] is False
    assert f["fab"] == "jlcpcb" and f["stackup_shape"] == "none"
    assert f["impedance_nets"] == [] and f["rf_nets"] == [] and f["bga"] == []
    assert f["inner_line_nets"] == [] and f["impedance_required"] is False
    json.dumps(f)                                       # plain JSON data


def test_features_of_rf_coupon_and_the_sps_node():
    f = lp.features(load_example("rf_coupon_50r"))
    assert f["impedance_nets"] == ["RF_IN", "RF_MID", "RF_OUT"]
    assert f["stackup_shape"] == "legacy" and f["count_declared"] is True
    s = lp.features(sps_node())
    assert s["napkin"] is True
    assert s["diff_nets"] == ["USB_DP"]
    assert s["rf_nets"] == ["LORA_ANT", "GNSS_ANT", "GNSS_RF_IN"]
    assert s["bga"] == []                               # QFN-48 and the LG290P LGA are not BGAs


def test_rf_nets_need_300_mhz_or_an_antenna_feed():
    d = minimal(net_specs={"SIG": {"freq_hz": 1e6}},
                rf={"antennas": [{"ref": "AE1", "kind": "chip", "feed_net": "ANT_FEED"}]})
    f = lp.features(d)
    assert f["rf_nets"] == ["ANT_FEED"]
    assert f["impedance_nets"] == ["SIG"]               # any freq_hz target is an impedance net


@pytest.mark.parametrize("design, fab", [
    (minimal(), "jlcpcb"),
    (minimal(layer_policy={"fab": "pcbway"}), "pcbway"),
    (minimal(stackup={"preset": "oshpark:4L"}, layer_policy={"fab": "pcbway"}), "oshpark"),
    (minimal(stackup={"preset": "oshpark:4L", "fab": "eurocircuits"}), "eurocircuits"),
    (minimal(stackup={"fab": None}), None),
])
def test_fab_comes_from_the_stackup_then_the_preset_then_layer_policy(design, fab):
    assert lp.features(design)["fab"] == fab


def test_inner_line_requests_explicit_and_auto():
    d = minimal(net_specs={"A": {"impedance": 50, "line": "stripline"},
                           "B": {"impedance": 50, "line": "dual_stripline"},
                           "C": {"impedance": 50, "line": "auto", "layers": ["In2.Cu"]},
                           "D": {"impedance": 50, "layers": ["F.Cu"]},
                           "E": {"impedance": 50, "line": "cpwg"}})
    f = lp.features(d)
    assert f["inner_line_nets"] == ["A", "B", "C"]
    assert f["line_kinds"] == {"A": "stripline", "B": "dual_stripline", "C": "stripline",
                               "E": "cpwg"}


@pytest.mark.parametrize("footprint, pitch, grid, rings", [
    (BGA05, 0.5, [8, 8], 4),
    ("Package_BGA:BGA-200_10x14.5mm_Layout12x22_P0.8x0.65mm", 0.65, [12, 22], 6),
    ("Package_BGA:BGA-16_1.92x1.92mm_Layout4x4_P0.5mm", 0.5, [4, 4], 2),
    ("Package_BGA:UFBGA-132_7x7mm_P0.5mm", 0.5, None, 6),
    ("Package_BGA:Xilinx_CLG225", None, None, None),
])
def test_bga_pitch_grid_and_rings_from_the_footprint_name(footprint, pitch, grid, rings):
    (b,) = lp.features(bga_design(footprint))["bga"]
    assert b["ref"] == "U1" and b["pitch_mm"] == pitch
    assert b["grid"] == grid and b["rings"] == rings


def test_bga_signal_rows_come_from_the_deepest_signal_ball():
    nets = {"GND": [["U1", ball(r, c)] for r in range(2, 6) for c in range(2, 6)],
            "+3V3": [["U1", ball(1, 1)]],
            "D0": [["U1", ball(0, 3)]], "D1": [["U1", ball(1, 4)]],
            "SIG": [["J1", "3"]]}
    (b,) = lp.features(bga_design(BGA08, nets=nets))["bga"]
    assert b["rings"] == 4 and b["signal_rows"] == 2   # inner rings carry only GND


def test_bga_rows_past_y_and_turned_layouts():
    grid22 = "Package_BGA:BGA-484_23.0x23.0mm_Layout22x22_P1.0mm"
    deep = {"D0": [["U1", "AA2"]], "SIG": [["J1", "3"]]}       # row 20 of 22, column 2
    (b,) = lp.features(bga_design(grid22, nets=deep))["bga"]
    assert b["signal_rows"] == 2
    turned = "Package_BGA:BGA-32_3.0x6.0mm_Layout4x8_P0.8mm"   # balls run 8 columns wide
    (b,) = lp.features(bga_design(turned, nets={"D0": [["U1", "B6"]]}))["bga"]
    assert b["signal_rows"] == 2
    gridless = "Package_BGA:UFBGA-132_7x7mm_P0.5mm"
    (b,) = lp.features(bga_design(gridless, nets={"D0": [["U1", "C3"]]}))["bga"]
    assert b["grid"] is None and b["signal_rows"] == 1


def test_malformed_inputs():
    with pytest.raises(TypeError):
        lp.features([])
    with pytest.raises(TypeError):
        lp.napkin_default("netlist")
    assert lp.features(minimal(stackup={"preset": "no-colon"},
                               layer_policy={"fab": "oshpark"}))["fab"] == "oshpark"
    bad_count = minimal(copper_layers=3, net_specs={"SIG": {"impedance": 50}})
    assert lp.check_declared(bad_count, fabs=FABS) == ()       # ST-E2 is stackup_model's


# ---------------------------------------------------------------------------
# floors
# ---------------------------------------------------------------------------
def ids(floors) -> list[tuple[str, int]]:
    return [(f.id, f.min_count) for f in floors]


def test_no_floor_for_a_plain_design():
    assert lp.floors(load_example("lm358_buffer"), fabs=FABS) == ()


def test_f1_impedance_targets_need_the_fabs_impedance_count():
    (f1,) = lp.floors(load_example("rf_coupon_50r"), fabs=FABS)
    assert (f1.id, f1.min_count, f1.severity, f1.issue_id) == ("F1", 4, "warning", "ST-W3")
    assert f1.subjects == ("RF_IN", "RF_MID", "RF_OUT") and "JLCPCB" in f1.reason
    required = minimal(stackup={"impedance_control": {"required": True}})
    assert ids(lp.floors(required, fabs=FABS)) == [("F1", 4)]


def test_f1_needs_a_fab_that_publishes_impedance_counts():
    d = load_example("rf_coupon_50r")
    assert lp.floors(d, None, "pcbway", fabs=FABS) == ()        # no published count list
    assert ids(lp.floors(d, None, "eurocircuits", fabs=FABS)) == [("F1", 4)]
    assert lp.floors(minimal(stackup={"fab": None},
                             net_specs={"SIG": {"impedance": 50}}), fabs=FABS) == ()


def test_f1_uses_the_resolved_stackups_fab():
    d = minimal(stackup={"preset": "oshpark:4L"}, net_specs={"SIG": {"impedance": 50}})
    rs, _ = sm.resolve(d, fabs=FABS)
    assert rs.fab == "oshpark" and lp.floors(d, rs, fabs=FABS) == ()


def test_f2_stripline_needs_six_layers_with_the_default_roles():
    (f2,) = lp.floors(stripline_design(), fabs=FABS)[1:]
    assert (f2.id, f2.min_count, f2.issue_id, f2.subjects) == ("F2", 6, "ST-W12", ("SIG",))
    embedded = minimal(net_specs={"SIG": {"impedance": 50, "line": "embedded_microstrip"}})
    assert ("F2", 6) in ids(lp.floors(embedded, fabs=FABS))


def test_f2_is_left_to_st_e9_when_the_role_layout_is_declared():
    layers = minimal(net_specs={"SIG": {"impedance": 50, "line": "stripline"}},
                     stackup={"layers": [{"type": "copper"},
                                         {"type": "dielectric", "thickness": 0.2, "dk": 4.4},
                                         {"type": "copper", "role": "plane", "net": "GND"},
                                         {"type": "dielectric", "thickness": 1.0, "dk": 4.4},
                                         {"type": "copper", "role": "signal"},
                                         {"type": "dielectric", "thickness": 0.2, "dk": 4.4},
                                         {"type": "copper", "role": "plane", "net": "GND"}]})
    assert "F2" not in [f.id for f in lp.floors(layers, fabs=FABS)]
    roles = minimal(net_specs={"SIG": {"impedance": 50, "line": "stripline"}},
                    stackup={"preset": P7628, "roles": {"In2.Cu": {"role": "signal"}}})
    assert "F2" not in [f.id for f in lp.floors(roles, fabs=FABS)]


def test_f3_bga_floors():
    (f3,) = lp.floors(bga_design(BGA05), fabs=FABS)
    assert (f3.id, f3.min_count, f3.confidence, f3.subjects) == ("F3", 6, "med", ("U1",))
    (f3,) = lp.floors(bga_design(BGA08), fabs=FABS)       # 8x8 = 4 rings, rows unknown
    assert (f3.min_count, f3.issue_id) == (4, "ST-W12")
    small = "Package_BGA:BGA-16_3.2x3.2mm_Layout4x4_P0.8mm"
    assert lp.floors(bga_design(small), fabs=FABS) == ()           # 2 rings escape on 2L
    coarse = "Package_BGA:BGA-256_17.0x17.0mm_Layout16x16_P1.0mm"
    assert lp.floors(bga_design(coarse), fabs=FABS) == ()


def test_f3_counts_signal_rows_not_supply_rows():
    shallow = {"GND": [["U1", ball(r, c)] for r in range(2, 6) for c in range(2, 6)],
               "D0": [["U1", ball(1, 3)]], "SIG": [["J1", "3"]]}
    assert lp.floors(bga_design(BGA08, nets=shallow), fabs=FABS) == ()
    deep = {**shallow, "D1": [["U1", ball(2, 3)]]}
    del deep["GND"]
    assert ids(lp.floors(bga_design(BGA08, nets=deep), fabs=FABS)) == [("F3", 4)]


# ---------------------------------------------------------------------------
# napkin_default: the five cases of the spec
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("make, count, preset, floor_ids", [
    (lambda: napkin(load_example("lm358_buffer")), 4, P7628, []),
    (lambda: {k: v for k, v in napkin(load_example("rf_coupon_50r")).items() if k != "stackup"},
     4, P3313, ["F1"]),
    (sps_node, 4, P3313, ["F1"]),
    (stripline_design, 6, P2116, ["F1", "F2"]),
    (lambda: bga_design(BGA05), 6, P2116, ["F3"]),
])
def test_napkin_default_cases(make, count, preset, floor_ids):
    design = make()
    before = copy.deepcopy(design)
    nd = lp.napkin_default(design, fabs=FABS)
    assert design == before                                  # input unchanged
    assert (nd.copper_layers, nd.preset) == (count, preset)
    assert [f.id for f in nd.floors] == floor_ids
    assert nd.patch == {"copper_layers": count,
                        "stackup": {"preset": preset, "origin": "layer_policy",
                                    "rationale": nd.rationale}}
    assert nd.decision_draft["decision"] == f"{count}L {preset}"
    for fid in floor_ids:
        assert fid in nd.rationale
    rs, issues = sm.resolve(nd.apply(design), fabs=FABS)    # the patched netlist resolves
    assert [i for i in issues if i.severity == "error"] == []
    assert (rs.source, rs.origin, rs.copper_layers, rs.preset) == ("policy", "layer_policy",
                                                                   count, preset)
    assert lp.check_declared(nd.apply(design), rs, fabs=FABS) == ()


def test_napkin_default_raises_four_to_the_floors_and_says_why():
    nd = lp.napkin_default(stripline_design(), fabs=FABS)
    assert "raised" in nd.rationale and "SIG" in nd.rationale
    plain = lp.napkin_default(napkin(load_example("lm358_buffer")), fabs=FABS)
    assert "no impedance or RF nets" in plain.rationale


@pytest.mark.parametrize("design", [
    load_example("lm358_buffer"),                                 # not napkin mode
    napkin(minimal(copper_layers=2)),
    napkin(minimal(stackup={"preset": P7628})),
    napkin(load_example("rf_coupon_50r")),                        # legacy shorthand declared
    napkin(minimal(stackup={"layers": []})),
    napkin(minimal(stackup="4 layers please")),
])
def test_no_napkin_default_when_not_napkin_or_a_count_is_declared(design):
    assert lp.napkin_default(design, fabs=FABS) is None


def test_the_decision_draft_is_accepted_by_append_decision(tmp_path):
    nd = lp.napkin_default(sps_node(), fabs=FABS)
    draft = nd.decision_draft
    assert (draft["stage"], draft["confidence"], draft["override"]) == (
        "requirements", "med", "stackup.preset")
    assert draft["reversible"] is True and draft["alternatives"]
    assert any(P7628 in a for a in draft["alternatives"])
    entry = decisions.append_decision(tmp_path / "sps_node.json", **draft)
    assert decisions.read_decisions(tmp_path / "sps_node.json") == [entry]
    assert decisions.validate_decision(entry) == []


def test_patch_and_draft_are_new_objects():
    design = napkin(minimal(stackup={"finish": "ENIG", "impedance_control": {"required": True},
                                     "rationale": "ENIG for the castellations"}))
    before = copy.deepcopy(design)
    nd = lp.napkin_default(design, fabs=FABS)
    st = nd.patch["stackup"]
    assert st["finish"] == "ENIG" and st["impedance_control"] == {"required": True}
    assert st["preset"] == P3313 and st["origin"] == "layer_policy"
    assert "ENIG for the castellations" in st["rationale"]
    st["impedance_control"]["required"] = False
    nd.decision_draft["alternatives"].append("x")
    assert design == before
    again = lp.napkin_default(design, fabs=FABS)
    assert again.patch["stackup"]["impedance_control"] == {"required": True}


def test_other_fabs_use_their_own_default_or_keep_the_fab_on_the_jlc_geometry():
    osh = lp.napkin_default(napkin(minimal(layer_policy={"fab": "oshpark"})), fabs=FABS)
    assert osh.preset == fp.default_template(FABS, "oshpark", 4)
    assert "fab" not in osh.patch["stackup"]
    pcbway = lp.napkin_default(napkin(minimal(layer_policy={"fab": "pcbway"})), fabs=FABS)
    assert pcbway.preset == P7628 and pcbway.patch["stackup"]["fab"] == "pcbway"
    assert "pcbway" in pcbway.rationale
    rs, issues = sm.resolve(pcbway.apply(napkin(minimal())), fabs=FABS)
    assert rs.fab == "pcbway" and not [i for i in issues if i.severity == "error"]


def test_explicit_fab_argument_and_unknown_fabs():
    d = napkin(minimal(layer_policy={"fab": "pcbway"}))
    assert lp.napkin_default(d, "oshpark", fabs=FABS).preset == "oshpark:4L"
    odd = lp.napkin_default(napkin(minimal(layer_policy={"fab": "nofab"})), fabs=FABS)
    assert odd.preset == P7628 and "fab" not in odd.patch["stackup"]
    assert "unknown fab 'nofab' ignored" in odd.rationale
    null = lp.napkin_default(napkin(minimal(stackup={"fab": None})), fabs=FABS)
    assert null.preset == P7628 and null.patch["stackup"]["fab"] is None


def test_napkin_presets_exist_in_the_shipped_fab_data():
    for fab, table in lp.NAPKIN_PRESETS.items():
        for count, pair in table.items():
            for name in pair.values():
                tpl = FABS.template(fab, name)
                assert tpl is not None and tpl["copper_layers"] == count


def test_napkin_default_is_deterministic():
    a = lp.napkin_default(sps_node(), fabs=FABS)
    b = lp.napkin_default(sps_node())
    assert a == b


# ---------------------------------------------------------------------------
# check_declared: ST-W12 and floor warnings
# ---------------------------------------------------------------------------
def by_id(issues) -> list[tuple[str, str]]:
    return [(i.id, i.path) for i in issues]


def test_napkin_with_nothing_declared_names_the_default():
    (w,) = lp.check_declared(sps_node(), fabs=FABS)
    assert (w.id, w.severity, w.path) == ("ST-W12", "warning", "$.copper_layers")
    assert "4L " + P3313 in w.message


@pytest.mark.parametrize("design, path", [
    (napkin(minimal(copper_layers=4)), "$.copper_layers"),
    (napkin(minimal(stackup={"preset": P7628})), "$.stackup.rationale"),
    (napkin(load_example("rf_coupon_50r")), "$.stackup"),
])
def test_napkin_count_without_a_rationale_is_st_w12(design, path):
    assert by_id(lp.check_declared(design, fabs=FABS)) == [("ST-W12", path)]


@pytest.mark.parametrize("design", [
    napkin(minimal(copper_layers=4, stackup={"rationale": "4L: GND under every layer"})),
    napkin(minimal(copper_layers=4, assumptions=[
        {"id": "A1", "topic": "copper_layers", "value": 4, "rationale": "cheap and routable",
         "confidence": "med"}])),
    napkin(minimal(stackup={"preset": P7628}, assumptions=[
        {"id": "A1", "topic": "stackup", "value": P7628, "rationale": "JLC standard",
         "confidence": "high"}])),
    minimal(copper_layers=2),                                  # strict mode: count is enough
])
def test_a_rationale_or_assumption_satisfies_st_w12(design):
    assert lp.check_declared(design, fabs=FABS) == ()


def test_blank_rationale_or_other_topic_does_not_count():
    d = napkin(minimal(copper_layers=4, stackup={"rationale": "  "}, assumptions=[
        {"id": "A1", "topic": "mounting", "value": 4, "rationale": "M3", "confidence": "med"}]))
    assert [i.id for i in lp.check_declared(d, fabs=FABS)] == ["ST-W12"]


def test_strict_mode_with_nothing_declared_warns_about_the_4l_default():
    (w,) = lp.check_declared(load_example("lm358_buffer"), fabs=FABS)
    assert (w.id, w.path) == ("ST-W12", "$.copper_layers")
    assert P7628 in w.message and "napkin" not in w.message


def test_declared_count_below_a_floor_warns_naming_the_floor():
    d = minimal(copper_layers=2, net_specs={"SIG": {"impedance": 50}})
    (w,) = lp.check_declared(d, sm.resolve(d, fabs=FABS)[0], fabs=FABS)
    assert (w.id, w.path) == ("ST-W3", "$.copper_layers")
    assert "F1" in w.message and "4" in w.message
    s = minimal(stackup={"preset": P7628, "rationale": "standard"},
                net_specs={"SIG": {"impedance": 50, "line": "stripline"}})
    assert by_id(lp.check_declared(s, sm.resolve(s, fabs=FABS)[0], fabs=FABS)) == [
        ("ST-W12", "$.stackup.preset")]


def test_f1_is_not_repeated_when_the_stackup_already_gives_st_w3():
    d = minimal(copper_layers=2, stackup={"impedance_control": {"required": True},
                                          "rationale": "2L"},
                net_specs={"SIG": {"impedance": 50}})
    rs, issues = sm.resolve(d, fabs=FABS)
    assert "ST-W3" in [i.id for i in issues]
    assert lp.check_declared(d, rs, fabs=FABS) == ()


def test_napkin_with_nothing_declared_skips_floor_warnings():
    issues = lp.check_declared(stripline_design(), fabs=FABS)
    assert [i.id for i in issues] == ["ST-W12"] and "6L " + P2116 in issues[0].message


@pytest.mark.parametrize("name", EXAMPLES)
def test_every_example_checks_without_error(name):
    for design in (load_example(name), napkin(load_example(name))):
        rs, _ = sm.resolve(design, fabs=FABS)
        for issue in lp.check_declared(design, rs, fabs=FABS):
            assert issue.severity == "warning" and issue.id in ("ST-W3", "ST-W12")
        lp.napkin_default(design, fabs=FABS)


# ---------------------------------------------------------------------------
# Purity and module shape
# ---------------------------------------------------------------------------
def test_module_is_pure(monkeypatch):
    designs = [sps_node(), stripline_design(), bga_design(BGA05), load_example("rf_coupon_50r"),
               napkin(minimal(copper_layers=2, net_specs={"SIG": {"impedance": 50}}))]
    resolved = [sm.resolve(d, fabs=FABS)[0] for d in designs]
    lp.shipped_fabs()                                   # the shipped data is read once

    def no_io(*_a, **_k):
        raise AssertionError("layer_policy did I/O")
    for target, name in ((builtins, "open"), (io, "open"), (os, "open"),
                         (pathlib.Path, "open"), (pathlib.Path, "read_bytes"),
                         (pathlib.Path, "read_text"), (socket, "socket")):
        monkeypatch.setattr(target, name, no_io)
    for design, rs in zip(designs, resolved, strict=True):
        lp.features(design)
        lp.floors(design, rs, fabs=FABS)
        lp.check_declared(design, rs, fabs=FABS)
        lp.check_declared(design)
        lp.napkin_default(design, fabs=FABS)
        lp.napkin_default(design)


def test_module_is_stdlib_only_short_and_small_functions():
    text = (TOOLS / "layer_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno - node.lineno + 1 < 50, node.name
    ours = {"fab_presets", "stackup_roles", "stackup_model"}
    assert not {n for n in names if n not in sys.stdlib_module_names and n not in ours}
    assert len(text.splitlines()) < 800


def test_results_are_frozen():
    nd = lp.napkin_default(sps_node(), fabs=FABS)
    with pytest.raises(AttributeError):
        nd.preset = "x"                                      # type: ignore[misc]
    with pytest.raises(AttributeError):
        nd.floors[0].min_count = 2                           # type: ignore[misc]
    assert nd.floors[0].to_dict()["id"] == "F1"

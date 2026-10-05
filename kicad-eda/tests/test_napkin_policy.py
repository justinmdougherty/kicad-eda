"""napkin_policy — the D5 hands-off policies: floating pins, G3 issue
classifiers (drift-tested against swig_gate_checks.py's SOURCE, never
imported: it needs pcbnew), connector face-out geometry, thermal variants."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import napkin_policy as npol

GATE_CHECKS = Path(__file__).resolve().parents[1] / "server" / "tools" / "swig_gate_checks.py"

IC = {"ref": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
      "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm"}
MODULE_BY_LIB = {"ref": "XB1", "lib_id": "RF_Module:ESP32-WROOM-32",
                 "footprint": "RF_Module:ESP32-WROOM-32"}
EAGLE_IC = {"ref": "U$3", "lib_id": "feather_imported:LG290P",
            "footprint": "feather_imported:LGA79"}
RESISTOR = {"ref": "R1", "lib_id": "Device:R", "footprint": "Resistor_SMD:R_0603_1608Metric"}
CONNECTOR = {"ref": "J1", "lib_id": "Connector:USB_C_Receptacle",
             "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"}
TRANSISTOR = {"ref": "Q1", "lib_id": "Device:Q_NMOS", "footprint": "Package_TO_SOT_SMD:SOT-23"}

ALLOWED_TYPES = ("output", "bidirectional", "passive")
BLOCKED_TYPES = ("input", "power_in", "power_out", "tri_state", "open_collector",
                 "open_emitter", "unspecified", None)


def _detail(ref: str = "U1", pad: str = "7", ptype: str | None = "output",
            name: str | None = "PA3", same: str | None = None) -> dict:
    return {"pin": f"{ref}.{pad}", "ref": ref, "pad": pad, "type": ptype,
            "name": name, "same_name_net": same}


# --- part kinds ----------------------------------------------------------------

@pytest.mark.parametrize("comp", [
    {"ref": "U1"}, {"ref": "IC12"}, {"ref": "M3"}, {"ref": "MOD1"}, {"ref": "A2"},
    {"ref": "u7"}, EAGLE_IC, {"ref": "U$12", "lib_id": "eagle_lbr:THING"},
    MODULE_BY_LIB, {"ref": "X1", "lib_id": "MCU_Module:Arduino_Nano_v3.x"},
    {"ref": "X2", "footprint": "Module:Pololu_Breakout-16_15.2x20.3mm"},
])
def test_ic_or_module_by_ref_prefix_or_module_library(comp):
    assert npol.is_ic_or_module(comp) is True


@pytest.mark.parametrize("comp", [
    RESISTOR, CONNECTOR, TRANSISTOR, {"ref": "UX"}, {"ref": "MP1"}, {"ref": "#PWR01"},
    {"ref": "C$4", "lib_id": "eagle:CAP"}, {"ref": "D1", "lib_id": "Device:LED"}, {},
])
def test_not_ic_or_module(comp):
    assert npol.is_ic_or_module(comp) is False


def test_ref_prefix_stops_at_first_digit_or_dollar():
    assert npol.ref_prefix("U$3") == "U"
    assert npol.ref_prefix("IC10") == "IC"
    assert npol.ref_prefix("MOD") == "MOD"
    assert npol.ref_prefix("") == ""


# --- floating pins: every pin type x part kind ------------------------------------

@pytest.mark.parametrize("comp", [IC, MODULE_BY_LIB, EAGLE_IC])
@pytest.mark.parametrize("ptype", ALLOWED_TYPES)
def test_allowed_types_on_ics_and_modules_are_auto_no_connect(comp, ptype):
    action = npol.floating_pin_action(_detail(comp["ref"], "7", ptype, "PA3"), comp)
    assert action[0] == "no_connect"
    assert action[1] in ("high", "med", "low") and action[2]


@pytest.mark.parametrize("comp", [IC, MODULE_BY_LIB, EAGLE_IC])
@pytest.mark.parametrize("ptype", BLOCKED_TYPES)
def test_input_power_and_other_types_stay_designer_tasks(comp, ptype):
    action = npol.floating_pin_action(_detail(comp["ref"], "7", ptype, "PA3"), comp)
    assert action[0] == "designer" and len(action) == 2
    assert f"{comp['ref']}.7" in action[1] and "must be netted" in action[1]


@pytest.mark.parametrize("comp", [RESISTOR, CONNECTOR, TRANSISTOR])
@pytest.mark.parametrize("ptype", ALLOWED_TYPES + BLOCKED_TYPES)
def test_pins_on_passives_connectors_and_discretes_stay_designer(comp, ptype):
    action = npol.floating_pin_action(_detail(comp["ref"], "2", ptype, "NC"), comp)
    assert action[0] == "designer"
    assert "not an IC or module" in action[1]


def test_no_connect_confidence_by_type():
    assert npol.floating_pin_action(_detail(ptype="output"), IC)[1] == "med"
    assert npol.floating_pin_action(_detail(ptype="bidirectional"), IC)[1] == "med"
    assert npol.floating_pin_action(_detail(ptype="passive"), IC)[1] == "low"
    assert npol.no_connect_verdict("input", "PA1") is None


@pytest.mark.parametrize("name", ["NC", "nc", "N.C.", "N.C", "NC3", "DNC", "dnc2", "NO_CONNECT",
                                  "NOCONNECT", "RESERVED", "Reserved", "RESERVED17", "RSVD",
                                  "rsvd4"])
def test_nc_named_pins_are_auto_no_connect_even_when_typed_input(name):
    assert npol.is_nc_name(name)
    action = npol.floating_pin_action(_detail(ptype="input", name=name), IC)
    assert action[0] == "no_connect" and action[1] == "high"
    assert name in action[2]


@pytest.mark.parametrize("name", ["NCS", "SYNC", "RESERVED_A", "NC_1", "CNC", "", None])
def test_names_that_are_not_nc(name):
    assert not npol.is_nc_name(name)


SUPPLY_NAMES = ["GND", "AGND", "DGND", "PGND", "GNDA", "VSS", "VSSA", "VDD", "VDDIO", "AVDD",
                "VCC", "VCCIO", "VBAT", "VIN", "VBUS", "VCAP_1", "3V3", "+3V3", "5V", "+5V",
                "1V8", "V+", "V-", "0V", "EARTH", "VEE", "vdd", "3.3V", "+3.3V", "-12V"]


@pytest.mark.parametrize("name", SUPPLY_NAMES)
def test_supply_names(name):
    assert npol.is_supply_name(name)


@pytest.mark.parametrize("name", ["PA3", "TXD1", "SDA", "RESET_N", "EN", "+", "-", "Rg",
                                  "3.3", "V", "", None])
def test_signal_names_are_not_supply(name):
    assert not npol.is_supply_name(name)


@pytest.mark.parametrize("name", ["GND", "VSS", "VDD", "3V3", "V+"])
# every type a stacked supply pin carries in the KiCad 9 stock libraries (scratch
# dup_name_scan: power_in / passive / power_out, plus ADP5063 'VINx' typed input,
# PIC16F1934 'VDD' bidirectional, SLM6800 'VSYS' output)
@pytest.mark.parametrize("ptype", ["passive", "power_in", "power_out", "output", "input",
                                   "bidirectional"])
def test_stacked_supply_pin_joins_the_same_name_net(name, ptype):
    # D1: stacked GND pins are passive; without this the LG290P's 55 GND pads
    # would be no-connected
    action = npol.floating_pin_action(_detail("U1", "12", ptype, name, same="GND_NET"), IC)
    assert action == ("net_same_name_pin", "GND_NET")


@pytest.mark.parametrize("name", ["GND", "VDD", "VBAT", "3V3", "AGND"])
@pytest.mark.parametrize("ptype", ALLOWED_TYPES)
def test_supply_named_pin_without_a_netted_sibling_is_never_auto_nc(name, ptype):
    action = npol.floating_pin_action(_detail("U1", "12", ptype, name), IC)
    assert action[0] == "designer"
    assert "supply" in action[1]


# A NON-supply name shared with a netted pin of the part is either a stacked pad of
# that net or the same-named pin of another unit / channel; the policy cannot tell
# which, so it neither joins (that would tie an unused op-amp unit's '+' to the live
# input, or short the AD620's gain resistor across its two 'Rg' pins) nor
# no-connects (that would orphan a stacked output pad): the agent decides.
@pytest.mark.parametrize("comp", [IC, EAGLE_IC, {"ref": "U3", "lib_id":
                                  "Amplifier_Operational:LM324"}])
@pytest.mark.parametrize("ptype,name", [
    ("input", "+"), ("input", "-"),           # LM324 / LM358: unused units' inputs
    ("passive", "Rg"),                         # AD620 / AD623 gain-resistor pins
    ("output", "OUTP"),                        # MAX98396 stacked class-D output pads
    ("passive", "SW"), ("open_collector", "DRAIN"), ("bidirectional", "IO"),
    ("passive", "EP"), ("power_in", "VA"),
])
def test_non_supply_name_shared_with_a_netted_pin_stays_a_designer_task(comp, ptype, name):
    ref = comp["ref"]
    action = npol.floating_pin_action(_detail(ref, "5", ptype, name, same="SENSOR"), comp)
    assert action[0] == "designer" and len(action) == 2
    assert f"{ref}.5" in action[1] and "SENSOR" in action[1]
    assert "another unit" in action[1]


def test_unnamed_output_of_an_unused_opamp_unit_is_auto_no_connect():
    # KiCad op-amp outputs are anonymous ('~'): no name, no same-name net
    action = npol.floating_pin_action(_detail("U3", "7", "output", None),
                                      {"ref": "U3", "lib_id": "Amplifier_Operational:LM358"})
    assert action[:2] == ("no_connect", "med")


def test_describe_pin_calls_anonymous_pins_unnamed():
    assert npol.describe_pin("output", "PA3") == "output pin 'PA3'"
    assert npol.describe_pin("output", None) == "unnamed output pin"
    assert npol.describe_pin(None, "") == "unnamed untyped pin"


def test_nc_name_wins_over_a_netted_same_name_pin():
    action = npol.floating_pin_action(_detail(ptype="passive", name="NC", same="X"), IC)
    assert action[0] == "no_connect"


def test_detail_without_pin_key_names_ref_and_pad():
    detail = {"ref": "U1", "pad": "9", "type": "input", "name": None}
    action = npol.floating_pin_action(detail, IC)
    assert action[0] == "designer" and "U1.9" in action[1]


# --- G3 classifiers: drift test against the SOURCE of swig_gate_checks.py -----------

SAMPLES = {"ref": "J7", "net": "+3V3", "dist": 7.46, "n": 1, "need": 4,
           "board.GetLayerName(layer)": "In2.Cu", "len(groups)": 2}
EXPECTED_KINDS = {
    "inward": {"type": "face_connector_out", "ref": "J7", "issue": "inward", "dist_mm": None},
    "far": {"type": "face_connector_out", "ref": "J7", "issue": "far", "dist_mm": 7.5},
    "plane": {"type": "loosen_placement",
              "reason": "+3V3 plane on In2.Cu is fractured into separate islands"},
    "ep_vias": {"type": "swap_thermal_footprint", "ref": "J7"},
}


def _render(node: ast.JoinedStr) -> str:
    """The text an f-string node would produce with SAMPLES substituted."""
    out = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            out.append(str(part.value))
            continue
        value = SAMPLES.get(ast.unparse(part.value), 0)
        spec = _render(part.format_spec) if part.format_spec else ""
        value = repr(value) if part.conversion == ord("r") else value
        out.append(format(value, spec))
    return "".join(out)


def classified_kinds(source: str) -> dict[str, dict]:
    """{kind: fix} for every f-string in ``source`` the classifiers recognise."""
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.JoinedStr):
            fix = npol.classify_g3_issue(_render(node))
            if fix is not None:
                kind = next(k for k, v in EXPECTED_KINDS.items() if v["type"] == fix["type"]
                            and v.get("issue") == fix.get("issue"))
                found[kind] = fix
    return found


def test_classifiers_match_the_literal_messages_in_swig_gate_checks_source():
    source = GATE_CHECKS.read_text(encoding="utf-8")
    assert classified_kinds(source) == EXPECTED_KINDS


def test_drift_check_notices_a_removed_message_prefix():
    source = GATE_CHECKS.read_text(encoding="utf-8")
    broken = source.replace("mating face points INTO", "mating side faces")
    assert "inward" not in classified_kinds(broken)


@pytest.mark.parametrize("text", [
    "antenna_keepout requested in the netlist but no no-fill rule area exists on the board",
    "U2: 12 mm tall inside lid with a 5 mm ceiling (F side)",
    "mounting holes requested but none placed on the board",
    "J1 mouth faces the board interior", "", None,
])
def test_other_g3_issues_are_not_classified(text):
    assert npol.classify_g3_issue(text) is None


def test_classifier_reads_refs_with_eagle_dollar():
    fix = npol.classify_g3_issue("JP$1: mating face is 12.0 mm from the board edge it faces")
    assert fix == {"type": "face_connector_out", "ref": "JP$1", "issue": "far", "dist_mm": 12.0}


def test_policy_table_covers_every_napkin_fix_kind():
    assert set(npol.POLICY_FOR) == set(npol.NAPKIN_FIX_KINDS)
    assert all(p.startswith("D5.") for p in npol.POLICY_FOR.values())


# --- rotation convention (KiCad display; measured on 9.0.4 and 10.0.5) --------------

def test_rotate_mouth_plus_90_maps_mx_my_to_my_minus_mx():
    assert npol.rotate_mouth((0, 1), 90) == (1, 0)      # down -> right
    assert npol.rotate_mouth((1, 0), 90) == (0, -1)     # right -> up
    assert npol.rotate_mouth((0, -1), 90) == (-1, 0)    # up -> left
    assert npol.rotate_mouth((-1, 0), 90) == (0, 1)     # left -> down
    assert npol.rotate_mouth((1, 0), 0) == (1, 0)
    assert npol.rotate_mouth((1, 0), 270) == (0, 1)
    assert npol.rotate_mouth((1, 0), -90) == (0, 1)


# --- face_out ------------------------------------------------------------------------
# board on the A4 sheet at (100, 80), 60 x 40 mm; a 10 x 4 mm courtyard
BOARD = [100.0, 80.0, 160.0, 120.0]


def _probe(cx: float, cy: float, mouth, *, w: float = 10.0, h: float = 4.0,
           rot: float = 0.0, origin=None, side: str = "F", pads=None) -> dict:
    entry = {"ref": "J1", "mouth": list(mouth) if mouth is not None else None,
             "courtyard": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
             "centre": [cx, cy], "origin": list(origin or (cx, cy + 1.0)), "rot": rot,
             "side": side}
    if pads is not None:
        entry["pads"] = pads
    return entry


# Every edge x issue x form. The 10 x 4 mm courtyard sits 7-8 mm in from the edge
# (so the far message would fire), probe rot 0, footprint origin 1 mm below the
# courtyard centre. Worked by hand in the board frame (sheet origin (100, 80)):
# flush = the courtyard's outer side on the edge; the origin offset (0, +1) turns
# with the part (180 -> (0, -1); +90 -> (+1, 0), since +90 maps (mx, my) to (my, -mx)).
EDGE_CENTRES = {"left": (112.0, 100.0), "right": (148.0, 100.0),
                "top": (130.0, 90.0), "bottom": (130.0, 110.0)}
FACE_MATRIX = [
    # edge, issue, probed mouth, (x, y, rot), at
    ("left", "inward", (1, 0), (5.0, 20.0, 180), [5.0, 19.0, 180]),
    ("right", "inward", (-1, 0), (55.0, 20.0, 180), [55.0, 19.0, 180]),
    ("top", "inward", (0, 1), (30.0, 2.0, 180), [30.0, 1.0, 180]),
    ("bottom", "inward", (0, -1), (30.0, 38.0, 180), [30.0, 37.0, 180]),
    ("left", "far", (-1, 0), (5.0, 20.0, 0), [5.0, 21.0, 0]),
    ("right", "far", (1, 0), (55.0, 20.0, 0), [55.0, 21.0, 0]),
    ("top", "far", (0, -1), (30.0, 2.0, 0), [30.0, 3.0, 0]),
    ("bottom", "far", (0, 1), (30.0, 38.0, 0), [30.0, 39.0, 0]),
    # mouth along the edge: one quarter turn, the courtyard becomes 4 x 10
    ("left", "sideways", (0, -1), (2.0, 20.0, 90), [3.0, 20.0, 90]),
    ("right", "sideways", (0, 1), (58.0, 20.0, 90), [59.0, 20.0, 90]),
    ("top", "sideways", (1, 0), (30.0, 5.0, 90), [31.0, 5.0, 90]),
    ("bottom", "sideways", (-1, 0), (30.0, 35.0, 90), [31.0, 35.0, 90]),
]


@pytest.mark.parametrize("edge,issue,mouth,xy,at", FACE_MATRIX)
def test_face_out_every_edge_issue_and_form(edge, issue, mouth, xy, at):
    entry = _probe(*EDGE_CENTRES[edge], mouth)
    assert npol.facing_edge(entry, BOARD) == edge
    placed = npol.face_out(entry, BOARD, {"ref": "J1", "x": 0.0, "y": 0.0, "rot": 0})
    assert placed == {"x": xy[0], "y": xy[1], "rot": xy[2]}
    assert npol.face_out(entry, BOARD, {"ref": "J1", "at": [0.0, 0.0, 0]}) == {"at": at}
    # the new rotation turns the probed mouth onto the edge's outward normal
    assert npol.rotate_mouth(mouth, xy[2]) == npol.EDGE_NORMALS[edge]


@pytest.mark.parametrize("edge,issue,mouth,xy,at", FACE_MATRIX)
def test_face_out_back_side_every_edge_issue_and_form(edge, issue, mouth, xy, at):
    # probed side B part DISPLAYED at 90; the builder rotates then flips, which
    # displays -rot, so x/y/rot carries -displayed while 'at' carries displayed
    entry = _probe(*EDGE_CENTRES[edge], mouth, rot=90.0, side="B")
    shown = (90 + xy[2]) % 360
    placed = npol.face_out(entry, BOARD, {"ref": "J1", "side": "B"})
    assert placed == {"x": xy[0], "y": xy[1], "rot": (360 - shown) % 360}
    at_form = npol.face_out(entry, BOARD, {"ref": "J1", "side": "B", "at": [0.0, 0.0, 90]})
    assert at_form == {"at": [at[0], at[1], shown]}


@pytest.mark.parametrize("mouth,delta", [
    ((0, 1), 270),          # down -> left takes three quarter turns
    ((1, 0), 180),
    ((0, -1), 90),
    ((-1, 0), 0),           # already facing the left edge
])
def test_face_out_rotation_delta_and_rotated_courtyard(mouth, delta):
    entry = _probe(108.0, 100.0, mouth)                # nearest edge: left
    out = npol.face_out(entry, BOARD, {"ref": "J1"})
    assert out["rot"] == delta
    assert npol.rotate_mouth(mouth, delta) == npol.EDGE_NORMALS["left"]
    w = 4.0 if delta in (90, 270) else 10.0            # 10 x 4 courtyard turned
    assert out["x"] == pytest.approx(w / 2)            # flush with the left edge
    assert out["y"] == pytest.approx(20.0)


def test_face_out_far_connector_moves_flush_to_the_edge_it_already_faces():
    # mouth up, 9 mm below the top edge, nearest edge is top: no rotation, just a move
    entry = _probe(140.0, 91.0, (0, -1), rot=180.0)
    out = npol.face_out(entry, BOARD, {"ref": "J1", "x": 40.0, "y": 11.0, "rot": 180})
    assert out == {"x": 40.0, "y": 2.0, "rot": 180}


@pytest.mark.parametrize("edge,centre,size,expected_xy", [
    # 1.5 mm overhang past the edge; the tangential coordinate is clamped so
    # the courtyard stays inside the board along the edge
    ("left", (102.0, 84.0), (4.0, 10.0), (0.5, 5.0)),
    ("right", (158.0, 116.0), (4.0, 10.0), (59.5, 35.0)),
    ("top", (104.0, 82.0), (10.0, 4.0), (5.0, 0.5)),
    ("bottom", (157.0, 118.0), (10.0, 4.0), (55.0, 39.5)),
])
def test_face_out_overhang_and_tangential_clamp(edge, centre, size, expected_xy):
    normal = npol.EDGE_NORMALS[edge]
    inward = (-normal[0], -normal[1])
    entry = _probe(*centre, inward, w=size[0], h=size[1])
    out = npol.face_out(entry, BOARD, {"ref": "J1", "overhang": 1.5})
    assert (out["x"], out["y"]) == (pytest.approx(expected_xy[0]), pytest.approx(expected_xy[1]))
    assert out["rot"] == 180


def test_face_out_tangential_centre_when_the_part_is_longer_than_the_edge():
    entry = _probe(104.0, 100.0, (1, 0), w=4.0, h=50.0)
    out = npol.face_out(entry, BOARD, {"ref": "J1"})
    assert out["y"] == pytest.approx(20.0)


def test_face_out_at_form_moves_the_origin_with_the_courtyard():
    # mouth +x (inward from the left edge), origin 3 mm below the courtyard centre
    entry = _probe(110.0, 100.0, (1, 0), w=10.0, h=4.0, rot=0.0, origin=(110.0, 103.0))
    out = npol.face_out(entry, BOARD, {"ref": "J1", "at": [10.0, 23.0, 0]})
    assert set(out) == {"at"}
    x, y, rot = out["at"]
    assert rot == 180
    # courtyard centre lands at x = 5 (flush, 10 wide), y = 20; the origin
    # offset (0, +3) rotates by 180 to (0, -3)
    assert (x, y) == (pytest.approx(5.0), pytest.approx(17.0))


def test_face_out_at_form_quarter_turn_rotates_the_origin_offset():
    entry = _probe(130.0, 86.0, (1, 0), w=10.0, h=4.0, rot=0.0, origin=(133.0, 86.0))
    out = npol.face_out(entry, BOARD, {"ref": "J1", "at": [33.0, 6.0, 0]})
    x, y, rot = out["at"]
    assert rot == 90                         # (1,0) +90 -> (0,-1): faces the top edge
    # rotated courtyard is 4 x 10: centre at y = 5; offset (3, 0) -> (0, -3)
    assert (x, y) == (pytest.approx(30.0), pytest.approx(2.0))


def test_face_out_back_side_xy_rot_is_the_pre_flip_value():
    # builder flips B parts after rotating: displayed = -rot (KiCad 9.0.4 / 10.0.5)
    entry = _probe(104.0, 100.0, (1, 0), rot=270.0, side="B")
    out = npol.face_out(entry, BOARD, {"ref": "J1", "side": "B", "x": 4.0, "y": 20.0,
                                       "rot": 90})
    assert out["rot"] == 270                  # displayed 270 + 180 = 90 -> netlist -90 = 270
    at = npol.face_out(entry, BOARD, {"ref": "J1", "side": "B", "at": [4.0, 21.0, 270]})
    assert at["at"][2] == 90                  # 'at' carries the displayed rotation


def test_face_out_rot_keeps_fractional_degrees():
    entry = _probe(104.0, 100.0, (1, 0), rot=12.5)
    assert npol.face_out(entry, BOARD, {"ref": "J1"})["rot"] == 192.5


@pytest.mark.parametrize("footprint", [
    "Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical",
    "Connector_PinSocket_2.54mm:PinSocket_1x08_P2.54mm_Vertical",
    "Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
])
def test_stock_header_families_are_board_internal(footprint):
    entry = _probe(104.0, 100.0, (1, 0))
    assert npol.face_out(entry, BOARD, {"ref": "J3", "footprint": footprint}) == {"interior": True}


HEADER_2X8 = {"count": 16, "tht": True, "rows": 2, "pitch_mm": 2.54}


def test_importer_nickname_2x8_header_is_interior_by_pads_summary():
    comp = {"ref": "JP1", "footprint": "feather_imported:2X08"}
    entry = _probe(104.0, 100.0, (1, 0), pads=HEADER_2X8)
    assert npol.is_board_internal_header(comp, entry) is True
    assert npol.face_out(entry, BOARD, comp) == {"interior": True}


@pytest.mark.parametrize("pads", [
    {"count": 8, "tht": True, "rows": 1, "pitch_mm": 2.0},
    {"count": 2, "tht": True, "rows": 1, "pitch_mm": 1.27},
    {"count": 10, "tht": True, "rows": 2, "pitch_mm": 2.541},
])
def test_header_geometry_variants(pads):
    assert npol.is_board_internal_header({"ref": "J2", "footprint": "imp:HDR"},
                                         {"pads": pads}) is True


@pytest.mark.parametrize("pads,comp", [
    ({"count": 16, "tht": False, "rows": 2, "pitch_mm": 2.54}, {"footprint": "imp:HDR"}),
    ({"count": 1, "tht": True, "rows": 1, "pitch_mm": None}, {"footprint": "imp:HDR"}),
    ({"count": 6, "tht": True, "rows": 3, "pitch_mm": 2.54}, {"footprint": "imp:HDR"}),
    ({"count": 4, "tht": True, "rows": 1, "pitch_mm": 3.5}, {"footprint": "imp:JST"}),
    ({"count": 4, "tht": True, "rows": 1, "pitch_mm": None}, {"footprint": "imp:HDR"}),
    (HEADER_2X8, {"footprint": "imp:PinHeader_2x08_P2.54mm_Horizontal"}),
    (HEADER_2X8, {"footprint": "imp:HDR_RightAngle"}),
    (HEADER_2X8, {"footprint": "imp:HDR", "edge_row": True}),
    (None, {"footprint": "imp:HDR"}),
    ("bogus", {"footprint": "imp:HDR"}),
])
def test_not_a_board_internal_header(pads, comp):
    assert npol.is_board_internal_header({"ref": "J2", **comp}, {"pads": pads}) is False


# --- the F5-09 probe contract (server/tools/swig_connector_probe.py) ------------------

def test_rotate_mouth_mirrors_the_connector_probe_convention():
    probe = pytest.importorskip("swig_connector_probe")
    for mouth in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        for delta in range(-720, 721, 90):
            assert npol.rotate_mouth(mouth, delta) == tuple(probe.rotate_mouth(mouth, delta))


def test_importer_2x8_header_is_interior_from_the_probes_own_pads_summary():
    probe = pytest.importorskip("swig_connector_probe")
    # PinHeader_2x08_P2.54mm_Vertical's pad grid, re-stamped by an importer
    pads = [(col * 2.54, row * 2.54, "pth") for row in range(8) for col in range(2)]
    summary = probe.pads_summary(pads)
    assert summary == {"count": 16, "tht": True, "rows": 2, "pitch_mm": 2.54}
    comp = {"ref": "JP1", "footprint": "feather_imported:2X08"}
    entry = {**_probe(104.0, 100.0, None), "pads": summary}   # vertical: mouth null
    assert npol.face_out(entry, BOARD, comp) == {"interior": True}


def test_usb_c_is_not_a_header():
    assert npol.is_board_internal_header(CONNECTOR, None) is False


@pytest.mark.parametrize("entry,board,match", [
    ({"mouth": None, "courtyard": [0, 0, 1, 1]}, BOARD, "no mating direction"),
    ({"mouth": [0, 0], "courtyard": [0, 0, 1, 1]}, BOARD, "no mating direction"),
    ({"mouth": [1, 0], "courtyard": [0, 0, 1]}, BOARD, "courtyard"),
    ({"mouth": [1, 0], "courtyard": [5, 0, 1, 1]}, BOARD, "courtyard"),
    ({"mouth": [1, 0], "courtyard": [0, 0, 1, 1]}, None, "board_bbox"),
    ({"mouth": [1, 0], "courtyard": [0, 0, "a", 1]}, BOARD, "courtyard"),
    ({"mouth": ["x", 0], "courtyard": [0, 0, 1, 1]}, BOARD, "no mating direction"),
    ({"mouth": [1, 0], "courtyard": [0, 0, float("nan"), 1]}, BOARD, "finite"),
])
def test_face_out_rejects_unusable_probe_entries(entry, board, match):
    with pytest.raises(npol.FaceOutError, match=match):
        npol.face_out(entry, board, {"ref": "J1"})


def test_face_out_needs_the_origin_for_the_at_form():
    entry = {**_probe(104.0, 100.0, (1, 0)), "origin": None}
    with pytest.raises(npol.FaceOutError, match="origin"):
        npol.face_out(entry, BOARD, {"ref": "J1", "at": [1, 2, 0]})


@pytest.mark.parametrize("entry_patch,comp_patch,match", [
    ({"rot": "ninety"}, {}, "rot must be a number"),
    ({"rot": [90]}, {}, "rot must be a number"),
    ({"rot": float("inf")}, {}, "rot must be a finite number"),
    ({}, {"overhang": "1 mm"}, "overhang must be a number"),
    ({}, {"overhang": float("nan")}, "overhang must be a finite number"),
])
def test_face_out_rejects_a_non_numeric_rot_or_overhang(entry_patch, comp_patch, match):
    entry = {**_probe(104.0, 100.0, (1, 0)), **entry_patch}
    with pytest.raises(npol.FaceOutError, match=match):
        npol.face_out(entry, BOARD, {"ref": "J1", **comp_patch})


def test_face_out_reads_numeric_strings_and_missing_values_as_numbers():
    entry = {**_probe(104.0, 100.0, (1, 0)), "rot": None}
    assert npol.face_out(entry, BOARD, {"ref": "J1", "overhang": None})["rot"] == 180
    entry = {**entry, "rot": "90"}
    assert npol.face_out(entry, BOARD, {"ref": "J1", "overhang": "0.5"}) == {
        "x": 4.5, "y": 20.0, "rot": 270}


def test_face_out_snaps_a_diagonal_mouth_to_its_dominant_axis():
    entry = _probe(104.0, 100.0, (0.9, 0.3))
    assert npol.face_out(entry, BOARD, {"ref": "J1"})["rot"] == 180


def test_face_out_does_not_mutate_its_inputs():
    entry = _probe(104.0, 100.0, (1, 0), pads=HEADER_2X8 | {"tht": False})
    comp = {"ref": "J1", "x": 4.0, "y": 20.0, "rot": 0}
    snapshot = (repr(entry), repr(comp), list(BOARD))
    npol.face_out(entry, BOARD, comp)
    assert (repr(entry), repr(comp), list(BOARD)) == snapshot


# --- thermal variants ---------------------------------------------------------------

def test_thermal_variants_for_a_stock_package():
    fp = "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"
    assert npol.thermal_variants(fp) == [f"{fp}_ThermalVias", f"{fp}_ThermalVias2"]


@pytest.mark.parametrize("fp", [
    "feather_imported:QFN16",                       # importer nickname: not stock
    "SPS_parts:LG290P",
    "Package_SO:HSOP-8-1EP_3.9x4.9mm_P1.27mm_EP2.3x2.3mm_ThermalVias",
    "Package_SO:Infineon_PG-HSOF-8-2_ThermalVias2",
    "QFN-16", "", None,
])
def test_thermal_variants_none(fp):
    assert npol.thermal_variants(fp) == []


def test_lib_and_name_split():
    assert npol.lib_of("A:B:C") == "A"
    assert npol.lib_of(None) == ""
    assert npol.name_of("Lib:Name") == "Name"
    assert npol.name_of("NoColon") == "NoColon"

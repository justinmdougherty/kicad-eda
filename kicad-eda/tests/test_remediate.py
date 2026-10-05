"""remediate — auto-applied corrective actions from gate tasks."""
from __future__ import annotations

import copy

import pytest

import netlist_schema as ns
import remediate

BYPASS_TASK = {"desc": "add 100nF bypass on 3V3 at U1", "auto": True,
               "fix": {"type": "add_decoupling", "ref": "U1", "rail": "3V3"}}


def _design() -> dict:
    return {"components": [
                {"ref": "U1", "lib_id": "MCU:X", "value": "X", "footprint": "F"},
                {"ref": "C1", "lib_id": "Device:C", "value": "1uF", "footprint": "F"},
                {"ref": "C10", "lib_id": "Device:C", "value": "10uF", "footprint": "F"},
                {"ref": "CX1", "lib_id": "Device:C", "value": "np", "footprint": "F"},
                {"ref": "R2", "lib_id": "Device:R", "value": "10k", "footprint": "F"}],
            "nets": {"3V3": [["U1", "1"], ["C1", "1"]],
                     "GND": [["U1", "2"], ["C1", "2"]]}}


def test_missing_bypass_adds_100nf_cap_and_wires_rail_and_gnd():
    d = _design()
    new, log = remediate.apply(d, [BYPASS_TASK])
    added = [c for c in new["components"] if c["ref"] not in {"U1", "C1", "C10", "CX1", "R2"}]
    assert len(added) == 1
    cap = added[0]
    assert cap == {"ref": "C11", "lib_id": "Device:C", "value": "100nF",
                   "footprint": "Capacitor_SMD:C_0603_1608Metric"}
    assert new["nets"]["3V3"][-1] == ["C11", "1"]
    assert new["nets"]["GND"][-1] == ["C11", "2"]
    assert len(new["nets"]["3V3"]) == 3 and len(new["nets"]["GND"]) == 3
    assert log == ["added C11 (100nF 0603) bypass: 3V3 -> GND, serving U1"]


def test_apply_returns_new_dict_and_never_mutates_input():
    d = _design()
    snapshot = copy.deepcopy(d)
    new, _ = remediate.apply(d, [BYPASS_TASK])
    assert d == snapshot                      # untouched
    assert new is not d
    assert new["components"] is not d["components"]
    assert new["nets"] is not d["nets"]
    assert new["nets"]["3V3"] is not d["nets"]["3V3"]
    assert new != d


def test_next_ref_skips_non_matching_prefixes_and_starts_at_one():
    assert remediate._next_ref(_design(), "C") == "C11"     # C10 is the max, CX1 ignored
    assert remediate._next_ref(_design(), "R") == "R3"
    assert remediate._next_ref({"components": []}, "C") == "C1"
    assert remediate._next_ref({"components": [{"ref": "CX1"}]}, "C") == "C1"


def test_two_bypass_tasks_get_consecutive_refs():
    t2 = {**BYPASS_TASK, "fix": {"type": "add_decoupling", "ref": "U1", "rail": "VBUS"}}
    new, log = remediate.apply(_design(), [BYPASS_TASK, t2])
    refs = [c["ref"] for c in new["components"]]
    assert refs[-2:] == ["C11", "C12"]
    assert new["nets"]["VBUS"] == [["C12", "1"]]           # rail created on demand
    assert [line.split()[1] for line in log] == ["C11", "C12"]


def test_missing_rail_and_gnd_nets_are_created():
    d = {"components": [{"ref": "U1"}], "nets": {}}
    new, log = remediate.apply(d, [BYPASS_TASK])
    assert new["nets"] == {"3V3": [["C1", "1"]], "GND": [["C1", "2"]]}
    assert d["nets"] == {}                                   # input untouched
    assert log[0].startswith("added C1 ")


def test_unknown_task_type_leaves_design_untouched_with_empty_log():
    d = _design()
    new, log = remediate.apply(d, [{"desc": "x", "auto": True,
                                    "fix": {"type": "reroute"}}])
    assert log == []
    assert new == d and new is not d


def test_designer_tasks_are_never_applied_even_with_known_fix():
    d = _design()
    task = {**BYPASS_TASK, "auto": False}
    new, log = remediate.apply(d, [task])
    assert log == [] and new == d


def test_task_without_fix_payload_is_skipped():
    new, log = remediate.apply(_design(), [{"desc": "manual", "auto": True}])
    assert log == [] and new == _design()


def test_no_tasks_is_a_noop_copy():
    d = _design()
    new, log = remediate.apply(d, [])
    assert (new, log) == (d, []) and new is not d


def test_log_names_the_served_ic_or_placeholder():
    task = {"auto": True, "fix": {"type": "add_decoupling", "rail": "3V3"}}
    _, log = remediate.apply(_design(), [task])
    assert log[0].endswith("serving ?")


# --- apply_structured: napkin appliers (D5) ------------------------------------------

BOARD_BBOX = [100.0, 80.0, 160.0, 120.0]          # 60 x 40 mm board on the sheet
QFN = "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"


def _napkin_design() -> dict:
    return {"name": "napkin", "board": {"w": 60.0, "h": 40.0},
            "components": [
                {"ref": "U1", "lib_id": "MCU:X", "value": "X", "footprint": QFN},
                {"ref": "U$3", "lib_id": "feather_imported:CHIP", "value": "Y",
                 "footprint": "feather_imported:QFN16"},
                {"ref": "J1", "lib_id": "Connector:USB_C", "value": "USB",
                 "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_"
                              "Horizontal",
                 "x": 30.0, "y": 20.0, "rot": 0},
                {"ref": "J2", "lib_id": "Connector:SMA", "value": "SMA",
                 "footprint": "Connector_Coaxial:SMA_Amphenol_132289_EdgeMount",
                 "at": [30.0, 30.0, 0]},
                {"ref": "JP1", "lib_id": "feather_imported:HDR", "value": "HDR",
                 "footprint": "feather_imported:2X08", "x": 30.0, "y": 10.0}],
            "nets": {"GND": [["U1", "17"], ["J1", "A1"]], "3V3": [["U1", "1"], ["J1", "A4"]]},
            "no_connect": [["U1", "9"]]}


def _task(fix: dict, policy: str | None = "D5.floating_pin", auto: bool = True) -> dict:
    return {"desc": f"do {fix['type']}", "auto": auto, "fix": fix, "policy": policy}


NC_TASK = _task({"type": "declare_no_connect", "ref": "U1", "pad": "7",
                 "pin_type": "output", "pin_name": "PA3"})
JOIN_TASK = _task({"type": "net_same_name_pin", "ref": "U1", "pad": "18", "net": "GND"})
FACE_J1 = _task({"type": "face_connector_out", "ref": "J1", "issue": "inward",
                 "dist_mm": None}, "D5.face_connector_out")


def _probe(ref: str, centre: tuple, mouth: tuple, *, w: float = 10.0, h: float = 4.0,
           rot: float = 0.0, origin: tuple | None = None, pads: dict | None = None) -> dict:
    cx, cy = centre
    entry = {"ref": ref, "mouth": list(mouth),
             "courtyard": [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
             "centre": [cx, cy], "origin": list(origin or (cx, cy)), "rot": rot,
             "side": "F", "interior": False}
    if pads is not None:
        entry["pads"] = pads
    return entry


def _probe_result(*entries: dict) -> dict:
    return {"ok": True, "board_bbox": BOARD_BBOX, "connectors": list(entries)}


def _comp(design: dict, ref: str) -> dict:
    return next(c for c in design["components"] if c["ref"] == ref)


def test_remedy_and_skip_shapes():
    assert remediate.Remedy._fields == ("gate", "text", "policy", "confidence", "rationale",
                                        "alternatives", "override")
    assert remediate.Skip._fields == ("gate", "desc", "fix", "reason")


def test_add_decoupling_remedy_is_high_confidence_with_default_policy():
    new, remedies, skipped = remediate.apply_structured(_design(), [BYPASS_TASK], gate="G2")
    assert skipped == []
    (r,) = remedies
    assert r.gate == "G2" and r.policy == "G2.add_decoupling" and r.confidence == "high"
    assert r.text == "added C11 (100nF 0603) bypass: 3V3 -> GND, serving U1"
    assert r.override == "components[C11]" and r.rationale and r.alternatives
    assert isinstance(r.alternatives, tuple)
    assert new["components"][-1]["ref"] == "C11"


def test_declare_no_connect_appends_once_with_type_confidence():
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [NC_TASK, NC_TASK],
                                                        gate="G2")
    assert new["no_connect"] == [["U1", "9"], ["U1", "7"]]
    (r,) = remedies
    assert r.policy == "D5.floating_pin" and r.confidence == "med"
    assert r.override == "no_connect" and "U1.7" in r.text
    assert [s.reason for s in skipped] == ["U1.7 is already declared no_connect"]


def test_declare_no_connect_creates_the_list_and_nc_names_are_high_confidence():
    d = {k: v for k, v in _napkin_design().items() if k != "no_connect"}
    task = _task({"type": "declare_no_connect", "ref": "U1", "pad": "5",
                  "pin_type": "input", "pin_name": "RESERVED"})
    new, (r,), _ = remediate.apply_structured(d, [task], gate="G2")
    assert new["no_connect"] == [["U1", "5"]] and "no_connect" not in d
    assert r.confidence == "high"


def test_declare_no_connect_names_an_anonymous_pin_unnamed():
    task = _task({"type": "declare_no_connect", "ref": "U1", "pad": "7",
                  "pin_type": "output", "pin_name": None})
    _, (r,), _ = remediate.apply_structured(_napkin_design(), [task], gate="G2")
    assert r.text == "declared U1.7 no_connect (unnamed output pin)"


@pytest.mark.parametrize("fix,reason", [
    ({"pad": "1", "pin_type": "output", "pin_name": "X"}, "U1.1 is netted to 3V3"),
    ({"pad": "8", "pin_type": "input", "pin_name": "PA0"},
     "U1.8: pin type 'input' is not auto-no-connectable (D5)"),
    ({"pad": None}, "malformed declare_no_connect fix"),
])
def test_declare_no_connect_refusals(fix, reason):
    task = _task({"type": "declare_no_connect", "ref": "U1", **fix})
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [task], gate="G2")
    assert remedies == [] and new == _napkin_design()
    assert skipped[0].reason.startswith(reason), skipped[0].reason
    assert skipped[0].gate == "G2"
    assert skipped[0].fix == task["fix"] and skipped[0].desc == task["desc"]


def test_net_same_name_pin_joins_the_net_once():
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [JOIN_TASK, JOIN_TASK],
                                                        gate="G2")
    assert new["nets"]["GND"] == [["U1", "17"], ["J1", "A1"], ["U1", "18"]]
    (r,) = remedies
    assert r.confidence == "high" and r.override == "nets.GND"
    assert r.policy == "D5.floating_pin"
    assert r.text == ("joined U1.18 to net GND (stacked / duplicate supply pin of a netted "
                      "same-name pin)")
    assert [s.reason for s in skipped] == ["U1.18 is already on net GND"]


@pytest.mark.parametrize("fix,reason", [
    ({"net": "NOPE"}, "net 'NOPE' is not in the netlist"),
    ({"pad": "9"}, "U1.9 is declared no_connect"),
    ({"pad": "1"}, "U1.1 is already on net 3V3"),
    ({"net": None}, "malformed net_same_name_pin fix"),
])
def test_net_same_name_pin_refusals(fix, reason):
    task = _task({**JOIN_TASK["fix"], **fix})
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [task], gate="G2")
    assert remedies == [] and new == _napkin_design()
    assert skipped[0].reason.startswith(reason), skipped[0].reason


def test_swap_thermal_footprint_takes_the_first_existing_variant():
    seen = []

    def exists(fp_id: str) -> bool:
        seen.append(fp_id)
        return fp_id.endswith("_ThermalVias2")

    task = _task({"type": "swap_thermal_footprint", "ref": "U1"}, "D5.thermal_vias")
    new, (r,), skipped = remediate.apply_structured(_napkin_design(), [task], gate="G3",
                                                    footprint_exists=exists)
    assert seen == [f"{QFN}_ThermalVias", f"{QFN}_ThermalVias2"]
    assert new["components"][0]["footprint"] == f"{QFN}_ThermalVias2"
    assert r.gate == "G3" and r.policy == "D5.thermal_vias" and r.confidence == "high"
    assert r.override == "components[U1].footprint" and skipped == []


@pytest.mark.parametrize("ref,exists,reason", [
    ("U$3", lambda _: True, "U$3: no stock ThermalVias variant for feather_imported"),
    ("U1", lambda _: False, f"U1: no ThermalVias variant of {QFN} exists"),
    ("U1", None, f"U1: cannot check that {QFN}_ThermalVias exists"),
    ("U9", lambda _: True, "U9: no component with that ref"),
])
def test_swap_thermal_footprint_skips(ref, exists, reason):
    task = _task({"type": "swap_thermal_footprint", "ref": ref})
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [task], gate="G3",
                                                        footprint_exists=exists)
    assert remedies == [] and new == _napkin_design()
    assert skipped[0].reason.startswith(reason), skipped[0].reason


def test_swap_thermal_footprint_already_a_variant():
    d = _napkin_design()
    d["components"][0]["footprint"] = f"{QFN}_ThermalVias"
    task = _task({"type": "swap_thermal_footprint", "ref": "U1"})
    _, _, skipped = remediate.apply_structured(d, [task], gate="G3", footprint_exists=bool)
    assert skipped[0].reason == f"U1: {QFN}_ThermalVias is already a ThermalVias variant"


def test_face_connector_out_xy_form_and_schema_valid():
    probe = _probe_result(_probe("J1", (104.0, 100.0), (1, 0)))
    new, remedies, skipped = remediate.apply_structured(_napkin_design(), [FACE_J1], gate="G3",
                                                        connector_probe=probe)
    j1 = _comp(new, "J1")
    assert (j1["x"], j1["y"], j1["rot"]) == (5.0, 20.0, 180) and "at" not in j1
    (r,) = remedies
    assert r.policy == "D5.face_connector_out" and r.confidence == "med"
    assert "left" in r.text and r.override == "components[J1].x/y/rot" and skipped == []
    assert "INTO the board" in r.rationale
    assert ns.validate(new) == []


def test_face_connector_out_at_form_replaces_at_only():
    task = _task({"type": "face_connector_out", "ref": "J2", "issue": "far", "dist_mm": 9.0})
    probe = _probe_result(_probe("J2", (130.0, 111.0), (0, 1), origin=(130.0, 112.0)))
    new, (r,), _ = remediate.apply_structured(_napkin_design(), [task], gate="G3",
                                              connector_probe=probe)
    j2 = _comp(new, "J2")
    assert j2["at"] == [30.0, 39.0, 0] and not {"x", "y", "rot"} & set(j2)
    assert r.override == "components[J2].at" and "9.0 mm" in r.rationale
    assert ns.validate(new) == []


def test_face_connector_out_header_by_pads_becomes_interior():
    task = _task({"type": "face_connector_out", "ref": "JP1", "issue": "inward",
                  "dist_mm": None})
    pads = {"count": 16, "tht": True, "rows": 2, "pitch_mm": 2.54}
    probe = {"ok": True, "board_bbox": BOARD_BBOX,
             "connectors": {"JP1": _probe("JP1", (130.0, 90.0), (0, 1), pads=pads)}}
    new, (r,), _ = remediate.apply_structured(_napkin_design(), [task], gate="G3",
                                              connector_probe=probe)
    jp1 = _comp(new, "JP1")
    assert jp1["interior"] is True and (jp1["x"], jp1["y"]) == (30.0, 10.0)
    assert r.override == "components[JP1].interior" and "interior" in r.text


@pytest.mark.parametrize("probe,auto_board,reason", [
    (None, False, "J1: no connector probe was run"),
    ({"ok": False, "reason": "LoadBoard returned None"}, False,
     "J1: connector probe failed: LoadBoard returned None"),
    ({"ok": False}, False, "J1: connector probe failed: no detail"),
    (_probe_result(), False, "J1: the connector probe has no entry for it"),
    ({"ok": True, "connectors": [_probe("J1", (104.0, 100.0), (1, 0))]}, False,
     "J1: the connector probe gave no board_bbox"),
    (_probe_result({**_probe("J1", (104.0, 100.0), (1, 0)), "mouth": None}), False,
     "J1: connector has no mating direction"),
    # a malformed probe value is a Skip, never a crash of apply_structured
    (_probe_result({**_probe("J1", (104.0, 100.0), (1, 0)), "rot": "ninety"}), False,
     "J1: rot must be a number"),
    (_probe_result(_probe("J1", (104.0, 100.0), (1, 0))), True,
     "J1: the board is auto-sized"),
])
def test_face_connector_out_skips(probe, auto_board, reason):
    d = _napkin_design()
    if auto_board:
        d = {k: v for k, v in d.items() if k != "board"}
    new, remedies, skipped = remediate.apply_structured(d, [FACE_J1], gate="G3",
                                                        connector_probe=probe)
    assert remedies == [] and new == d
    assert skipped[0].reason.startswith(reason), skipped[0].reason


def test_face_connector_out_malformed_overhang_is_a_skip():
    d = _napkin_design()
    d["components"] = [{**c, "overhang": "lots"} if c["ref"] == "J1" else c
                       for c in d["components"]]
    probe = _probe_result(_probe("J1", (104.0, 100.0), (1, 0)))
    new, remedies, (skip,) = remediate.apply_structured(d, [FACE_J1], gate="G3",
                                                        connector_probe=probe)
    assert remedies == [] and new == d
    assert skip.reason == "J1: overhang must be a number, got 'lots'"


def test_face_connector_out_unknown_ref_is_skipped():
    task = _task({"type": "face_connector_out", "ref": "J9", "issue": "inward"})
    _, _, skipped = remediate.apply_structured(_napkin_design(), [task], gate="G3",
                                               connector_probe=_probe_result())
    assert skipped[0].reason == "J9: no component with that ref"


def test_face_connector_out_on_an_outline_board():
    d = _napkin_design()
    d["board"] = {"outline": {"loop": [[0, 0], [60, 0], [60, 40], [0, 40]]}}
    probe = _probe_result(_probe("J1", (104.0, 100.0), (1, 0)))
    _, remedies, _ = remediate.apply_structured(d, [FACE_J1], gate="G3", connector_probe=probe)
    assert len(remedies) == 1


def test_apply_structured_ignores_designer_and_unknown_tasks_and_never_mutates():
    d = _napkin_design()
    snapshot = copy.deepcopy(d)
    tasks = [_task({"type": "loosen_placement", "reason": "x"}, "D5.fractured_plane"),
             {**NC_TASK, "auto": False}, {"desc": "no fix", "auto": True},
             {"desc": "null fix", "auto": True, "fix": None}, NC_TASK, JOIN_TASK]
    new, remedies, skipped = remediate.apply_structured(d, tasks, gate="G2")
    assert d == snapshot
    assert [r.override for r in remedies] == ["no_connect", "nets.GND"] and skipped == []
    assert new["nets"]["GND"] is not d["nets"]["GND"]


def test_apply_keeps_its_signature_and_runs_the_structured_appliers():
    new, log = remediate.apply(_napkin_design(), [NC_TASK, JOIN_TASK, FACE_J1])
    expected = remediate.apply_structured(_napkin_design(), [NC_TASK, JOIN_TASK], gate="G2")
    assert log == [r.text for r in expected[1]] and len(log) == 2
    assert new == expected[0]

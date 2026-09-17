"""remediate — auto-applied corrective actions from gate tasks."""
from __future__ import annotations

import copy

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
    assert [l.split()[1] for l in log] == ["C11", "C12"]


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

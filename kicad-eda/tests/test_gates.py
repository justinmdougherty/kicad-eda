"""gates — G1..G5 go/no-go verdicts, task generation and summary formatting."""
from __future__ import annotations

import pytest

import gates

CLEAN_LINT = {"ok": True, "errors": [], "warnings": []}
CLEAN_SCH = {"ok": True, "erc_violations": 0}
CLEAN_BOARD = {"ok": True, "missing_pads": []}
CLEAN_ROUTE = {"unconnected": 0}
CLEAN_DRC = {"errors": 0, "unconnected": 0, "types": {}}
CLEAN_FAB = {"ok": True, "files": 12, "bom": True, "cpl": True}


def _auto(tasks):
    return [t for t in tasks if t["auto"]]


def _manual(tasks):
    return [t for t in tasks if not t["auto"]]


# --- G1 ------------------------------------------------------------------------

def test_g1_passes_on_clean_annotated_netlist(buck_design):
    g = gates.g1_requirements(CLEAN_LINT, buck_design)
    assert g["passed"] is True
    assert (g["gate"], g["feeds"]) == ("G1", "SRR")
    assert g["blockers"] == [] and g["warnings"] == [] and g["tasks"] == []


def test_g1_lint_errors_become_blockers_and_warnings_pass_through(buck_design):
    lint = {"ok": False, "errors": ["E3 net 'X' has 1 pad(s) (needs >=2)"],
            "warnings": ["W1 net names differ only by case: ['gnd', 'GND']"]}
    g = gates.g1_requirements(lint, buck_design)
    assert g["passed"] is False
    assert g["blockers"] == lint["errors"]
    assert g["warnings"] == lint["warnings"]


def test_g1_no_nets_blocks_and_no_specs_warns():
    g = gates.g1_requirements(CLEAN_LINT, {"components": [], "nets": {}})
    assert g["passed"] is False
    assert g["blockers"] == ["netlist has no nets"]
    assert any("no net_specs" in w for w in g["warnings"])


def test_g1_does_not_mutate_lint_report():
    lint = {"ok": False, "errors": ["E5 duplicate component ref: R1"], "warnings": []}
    g = gates.g1_requirements(lint, {"nets": {"A": []}})
    g["blockers"].append("extra")
    assert lint["errors"] == ["E5 duplicate component ref: R1"]


# --- G2 ------------------------------------------------------------------------

def test_g2_clean_passes_without_tasks():
    g = gates.g2_schematic(CLEAN_SCH, {}, {"blockers": [], "warnings": []})
    assert g["passed"] and g["tasks"] == [] and g["warnings"] == []
    assert (g["gate"], g["feeds"]) == ("G2", "PDR")


def test_g2_thermal_report_optional():
    assert gates.g2_schematic(CLEAN_SCH, {}, None)["passed"]
    assert gates.g2_schematic(CLEAN_SCH, {})["passed"]


def test_g2_erc_not_run_is_a_warning_not_a_blocker():
    g = gates.g2_schematic({"ok": True}, {})
    assert g["passed"]
    assert "ERC did not run" in g["warnings"]


def test_g2_erc_violations_block_with_manual_task():
    g = gates.g2_schematic({"ok": True, "erc_violations": 3}, {})
    assert not g["passed"]
    assert "ERC reports 3 violation(s)" in g["blockers"]
    assert _manual(g["tasks"]) and not _auto(g["tasks"])
    assert "3 ERC violation(s)" in g["tasks"][0]["desc"]


def test_g2_schematic_failure_blocks():
    g = gates.g2_schematic({"ok": False, "error": "boom"}, {})
    assert "schematic generation failed" in g["blockers"]
    assert g["tasks"][0]["auto"] is False


def test_g2_thermal_blockers_are_designer_tasks_and_warnings_pass_through():
    th = {"blockers": ["U1: ~2.00 W ... over the 80% limit"],
          "warnings": ["U2: estimate — declare thermal.p_diss"]}
    g = gates.g2_schematic(CLEAN_SCH, {}, th)
    assert not g["passed"]
    assert g["blockers"] == ["thermal: U1: ~2.00 W ... over the 80% limit"]
    assert g["warnings"] == ["thermal: U2: estimate — declare thermal.p_diss"]
    assert len(g["tasks"]) == 1 and g["tasks"][0]["auto"] is False
    assert "resolve thermal violation" in g["tasks"][0]["desc"]


def test_g2_missing_bypass_is_an_auto_task_with_fix_payload():
    rep = {"decoupling": {"uncovered": [
        {"ref": "U1", "rails_without_cap": ["3V3", "VBUS"]}]}}
    g = gates.g2_schematic(CLEAN_SCH, rep)
    assert not g["passed"]
    assert g["blockers"] == ["U1: rail '3V3' has no bypass cap",
                             "U1: rail 'VBUS' has no bypass cap"]
    fixes = [t["fix"] for t in _auto(g["tasks"])]
    assert fixes == [{"type": "add_decoupling", "ref": "U1", "rail": "3V3"},
                     {"type": "add_decoupling", "ref": "U1", "rail": "VBUS"}]
    assert "add 100nF bypass on 3V3 at U1" == g["tasks"][0]["desc"]


def test_g2_floating_pin_is_a_designer_task_never_auto():
    rep = {"flags": {"floating_pins": ["U1.7"]}}
    g = gates.g2_schematic(CLEAN_SCH, rep)
    assert g["blockers"] == ["floating pin (not netted, not no_connect): U1.7"]
    assert g["tasks"] == [{"desc": "net pin U1.7 or declare it no_connect in "
                                   "the netlist", "auto": False, "fix": {}}]


def test_g2_design_report_errors_block_as_designer_tasks():
    # a symbol design_report could not read means its decoupling / power-tree /
    # floating-pin checks passed vacuously: the review did not run -> BLOCKER
    rep = {"errors": ["pin types unavailable for Nope:Chip (U2): SymbolLibError: "
                      "symbol library not found: Nope"],
           "decoupling": {"uncovered": []}, "flags": {"floating_pins": []}}
    g = gates.g2_schematic(CLEAN_SCH, rep)
    assert not g["passed"]
    assert g["blockers"] == ["design review could not run: pin types unavailable for "
                             "Nope:Chip (U2): SymbolLibError: symbol library not found: Nope"]
    assert len(g["tasks"]) == 1 and g["tasks"][0]["auto"] is False
    assert "Nope:Chip" in g["tasks"][0]["desc"]


def test_g2_empty_design_report_errors_do_not_block():
    g = gates.g2_schematic(CLEAN_SCH, {"errors": []})
    assert g["passed"] and g["blockers"] == []


def test_g2_advisory_flags_are_warnings_only():
    rep = {"flags": {"nets_missing_pull": ["NRST"],
                     "rails_missing_testpoint": ["3V3"]},
           "dfa": {"array_candidates": [{"count": 4, "value": "10k"}]}}
    g = gates.g2_schematic(CLEAN_SCH, rep)
    assert g["passed"] and g["tasks"] == []
    assert "net 'NRST' may need a pull resistor" in g["warnings"]
    assert "power rail '3V3' has no test point for bring-up" in g["warnings"]
    assert any(w.startswith("DFA: 4x 10k") for w in g["warnings"])


# --- G3 ------------------------------------------------------------------------

def test_g3_clean_passes_and_carries_place_warnings():
    g = gates.g3_placement(CLEAN_BOARD, {"courtyard_violations": 0}, [],
                           ["J2 mouth faces inward"])
    assert g["passed"] and g["warnings"] == ["J2 mouth faces inward"]
    assert g["gate"] == "G3" and g["tasks"] == []


def test_g3_place_warnings_default_to_empty():
    assert gates.g3_placement(CLEAN_BOARD, {}, [])["warnings"] == []


@pytest.mark.parametrize("rep,why", [
    ({"ok": False, "error": "pcbnew crashed"}, "pcbnew crashed"),
    ({"ok": False, "reason": "no footprint lib"}, "no footprint lib"),
    ({"ok": False, "stderr": "Traceback"}, "Traceback"),
    ({"ok": False}, "(no detail)"),
])
def test_g3_board_failure_reports_the_reason(rep, why):
    g = gates.g3_placement(rep, {}, [])
    assert g["blockers"][0] == f"board generation failed: {why}"
    assert g["tasks"][0]["auto"] is False and why in g["tasks"][0]["desc"]


def test_g3_missing_pads_are_designer_tasks():
    g = gates.g3_placement({"ok": True, "missing_pads": ["U1.99"]}, {}, [])
    assert g["blockers"] == ["pad not assignable: U1.99"]
    assert g["tasks"] == [{"desc": "correct netlist pad reference U1.99",
                           "auto": False, "fix": {}}]


def test_g3_courtyard_overlap_is_auto_loosen():
    g = gates.g3_placement(CLEAN_BOARD, {"courtyard_violations": 2}, [])
    assert g["blockers"] == ["2 courtyard overlap(s) — parts collide"]
    assert _auto(g["tasks"]) == [{"desc": "re-place with looser spacing (pack_scale+)",
                                  "auto": True, "fix": {"type": "loosen_placement"}}]


def test_g3_place_issues_are_designer_tasks():
    g = gates.g3_placement(CLEAN_BOARD, {}, ["J1 mouth faces the board interior"])
    assert g["blockers"] == ["J1 mouth faces the board interior"]
    assert g["tasks"][0] == {"desc": "fix placement: J1 mouth faces the board interior",
                             "auto": False, "fix": {}}


# --- G4 ------------------------------------------------------------------------

def test_g4_clean_passes():
    g = gates.g4_layout(CLEAN_ROUTE, CLEAN_DRC, [], ["dp skew 0.3 mm"])
    assert g["passed"] and g["warnings"] == ["dp skew 0.3 mm"]
    assert (g["gate"], g["feeds"]) == ("G4", "CDR")


def test_g4_router_no_result_is_a_designer_task():
    g = gates.g4_layout({"error": "java not found"}, CLEAN_DRC, [])
    assert g["blockers"] == ["autoroute failed: java not found"]
    assert g["tasks"][0]["auto"] is False
    g2 = gates.g4_layout({}, CLEAN_DRC, [])
    assert g2["blockers"] == ["autoroute failed: router produced no result"]


def test_g4_unrouted_nets_are_auto_reroute():
    g = gates.g4_layout({"unconnected": 3}, CLEAN_DRC, [])
    assert g["blockers"] == ["3 net(s) unrouted"]
    assert _auto(g["tasks"]) == [{"desc": "re-route with more effort / looser placement",
                                  "auto": True, "fix": {"type": "reroute"}}]


def test_g4_drc_violations_are_summarised_by_type():
    drc = {"errors": 3, "unconnected": 1,
           "types": {"clearance": 2, "shorting_items": 1}}
    g = gates.g4_layout(CLEAN_ROUTE, drc, [])
    assert "DRC: 3 violation(s) (clearancex2, shorting_itemsx1)" in g["blockers"]
    assert "DRC: 1 unconnected item(s)" in g["blockers"]
    # the unconnected-items blocker carries no task; the violations do
    assert len(g["tasks"]) == 1 and g["tasks"][0]["auto"] is False
    assert "clearance" in g["tasks"][0]["desc"]


def test_g4_width_issues_block_with_designer_tasks():
    g = gates.g4_layout(CLEAN_ROUTE, CLEAN_DRC, ["5V_PI: 0.25 mm < class 1.8 mm"])
    assert g["blockers"] == ["5V_PI: 0.25 mm < class 1.8 mm"]
    assert g["tasks"] == [{"desc": "fix net-class width violation: 5V_PI: 0.25 mm "
                                   "< class 1.8 mm", "auto": False, "fix": {}}]


# --- G5 ------------------------------------------------------------------------

def test_g5_clean_passes():
    g = gates.g5_fab(CLEAN_FAB)
    assert g["passed"] and g["tasks"] == []
    assert (g["gate"], g["feeds"]) == ("G5", "release to fab")


def test_g5_export_failure_and_thin_package_block():
    g = gates.g5_fab({"ok": False, "files": 3})
    assert "Gerber/drill export failed" in g["blockers"]
    assert "fab package has only 3 file(s) (expected >= 10)" in g["blockers"]
    assert gates.g5_fab({"ok": True, "files": 3}, min_files=3)["passed"]


def test_g5_non_integer_file_count_is_not_compared():
    assert gates.g5_fab({"ok": True, "files": "many"})["passed"]


def test_g5_assembly_requires_bom_and_cpl():
    g = gates.g5_fab({"ok": True, "files": 12}, require_assembly=True)
    assert set(g["blockers"]) == {"assembly BOM.csv missing from the fab package",
                                  "assembly CPL.csv missing from the fab package"}
    assert gates.g5_fab(CLEAN_FAB, require_assembly=True)["passed"]


def test_g5_sourcing_warns_unless_required():
    src = {"required": False, "missing": ["R1", "C1"],
           "bad": ["U1: lcsc 'X' is not Cxxxxx format"]}
    g = gates.g5_fab(CLEAN_FAB, sourcing=src)
    assert g["passed"]
    assert g["warnings"][0] == ("2 BOM part(s) have neither an LCSC number nor "
                                "an MPN: ['R1', 'C1']")
    assert g["warnings"][1] == "U1: lcsc 'X' is not Cxxxxx format"
    hard = gates.g5_fab(CLEAN_FAB, sourcing={**src, "required": True})
    assert not hard["passed"] and len(hard["blockers"]) == 2


def test_g5_sourcing_list_is_truncated_after_eight():
    src = {"required": False, "missing": [f"R{i}" for i in range(1, 12)], "bad": []}
    w = gates.g5_fab(CLEAN_FAB, sourcing=src)["warnings"][0]
    assert w.startswith("11 BOM part(s)") and w.endswith("...")
    assert "'R8'" in w and "'R9'" not in w


def test_g5_empty_sourcing_report_is_silent():
    g = gates.g5_fab(CLEAN_FAB, sourcing={"required": True, "missing": [], "bad": []})
    assert g["passed"] and g["warnings"] == []


# --- summarize ---------------------------------------------------------------

def test_summarize_formats_pass_fail_tasks_and_warnings():
    g_ok = gates.g1_requirements(CLEAN_LINT, {"nets": {"A": []}, "net_specs": {"A": {}}})
    g_bad = gates.g2_schematic({"ok": True, "erc_violations": 1},
                               {"decoupling": {"uncovered": [
                                   {"ref": "U1", "rails_without_cap": ["3V3"]}]},
                                "flags": {"nets_missing_pull": ["NRST"]}})
    text = gates.summarize([g_ok, g_bad])
    lines = text.splitlines()
    assert lines[0] == "[PASS] G1 Requirements / netlist review (feeds SRR)"
    assert lines[1] == "[FAIL] G2 Schematic review (capture complete, pre-layout) (feeds PDR)"
    assert "    BLOCKER: ERC reports 1 violation(s)" in lines
    assert "    BLOCKER: U1: rail '3V3' has no bypass cap" in lines
    assert "    -> [DESIGNER TASK] resolve 1 ERC violation(s)" in lines
    assert "    -> [auto] add 100nF bypass on 3V3 at U1" in lines
    assert "    warning: net 'NRST' may need a pull resistor" in lines
    # blockers are listed before tasks, tasks before warnings
    i_b = lines.index("    BLOCKER: ERC reports 1 violation(s)")
    i_t = lines.index("    -> [DESIGNER TASK] resolve 1 ERC violation(s)")
    i_w = lines.index("    warning: net 'NRST' may need a pull resistor")
    assert i_b < i_t < i_w


def test_summarize_empty():
    assert gates.summarize([]) == ""

"""gates — G1..G5 go/no-go verdicts, task generation and summary formatting."""
from __future__ import annotations

import pytest
from fixtures.kicad_install import KICAD, requires_symbols

import design_report
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
                                   "the netlist", "auto": False, "fix": {}, "policy": None}]


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
                           "auto": False, "fix": {}, "policy": None}]


def test_g3_courtyard_overlap_is_auto_loosen():
    g = gates.g3_placement(CLEAN_BOARD, {"courtyard_violations": 2}, [])
    assert g["blockers"] == ["2 courtyard overlap(s) — parts collide"]
    assert _auto(g["tasks"]) == [{"desc": "re-place with looser spacing (pack_scale+)",
                                  "auto": True, "fix": {"type": "loosen_placement"},
                                  "policy": None}]


def test_g3_place_issues_are_designer_tasks():
    g = gates.g3_placement(CLEAN_BOARD, {}, ["J1 mouth faces the board interior"])
    assert g["blockers"] == ["J1 mouth faces the board interior"]
    assert g["tasks"][0] == {"desc": "fix placement: J1 mouth faces the board interior",
                             "auto": False, "fix": {}, "policy": None}


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
                                  "auto": True, "fix": {"type": "reroute"}, "policy": None}]


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
                                   "< class 1.8 mm", "auto": False, "fix": {}, "policy": None}]


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


# --- napkin mode (D5): strict reproduces today, napkin turns policy cases into auto ----

ASSUMPTION = {"id": "A1", "topic": "input power", "value": "USB-C 5 V sink",
              "rationale": "napkin default", "confidence": "high"}
MCU = {"lib_id": "MCU_ST_STM32F1:STM32F103C8Tx", "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm"}


def _fd(ref: str, pad: str, ptype: str, name: str | None, same: str | None = None,
        **comp: str) -> dict:
    return {"pin": f"{ref}.{pad}", "ref": ref, "pad": pad, "type": ptype, "name": name,
            "same_name_net": same, **(comp or MCU)}


OPAMP = {"lib_id": "Amplifier_Operational:LM358",
         "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"}
FLOAT_REP = {"flags": {
    "floating_pins": ["J1.3", "U$3.4", "U1.12", "U1.7", "U1.9", "U2.1", "U4.5", "U4.7",
                      "U5.20"],
    "floating_detail": [
        _fd("J1", "3", "passive", None, lib_id="Connector:X", footprint="Connector_JST:X"),
        _fd("U$3", "4", "bidirectional", "IO4", lib_id="feather_imported:CHIP",
            footprint="feather_imported:QFN"),
        _fd("U1", "12", "passive", "GND", same="GND"),
        _fd("U1", "7", "output", "PA3"),
        _fd("U1", "9", "input", "PA5"),
        # unused LM358 unit B: '+' shares its name with unit A's netted '+'; the
        # output is anonymous
        _fd("U4", "5", "input", "+", same="SENSOR", **OPAMP),
        _fd("U4", "7", "output", None, **OPAMP),
        # a stacked non-supply pad (buck 'SW', passive) of a netted pin
        _fd("U5", "20", "passive", "SW", same="SW"),
    ]}}                                      # U2.1 has no detail entry
DESIGNER_FLOAT = "net pin {} or declare it no_connect in the netlist"

G3_ISSUES = [
    "J1: mating face points INTO the board interior (unusable in an enclosure)",
    "J2: mating face is 7.5 mm from the board edge it faces (must be at the edge, <=3 mm)",
    "+3V3: plane on In2.Cu is fractured into separate islands, splitting the net's pads "
    "into 2 groups (largest: ['U1'], stranded: ['C2']) — the router trusts the DSN plane "
    "rectangle and will return this net broken. Add a corridor preroute joining the "
    "groups, or re-place the wall-forming THT part",
    "U3: dissipating exposed-pad part has only 1 thermal via(s) under the EP (needs >= 4, "
    "IPC-7093) — use a ...ThermalVias footprint variant or add stitching vias",
    "antenna_keepout requested in the netlist but no no-fill rule area exists on the board",
]


def _strict_cases(buck_design: dict) -> list:
    return [
        (gates.g1_requirements, (CLEAN_LINT, buck_design)),
        (gates.g1_requirements, ({"errors": ["E1"], "warnings": ["W1"]}, {"nets": {}})),
        (gates.g1_requirements, (CLEAN_LINT, {**buck_design, "assumptions": [ASSUMPTION]})),
        (gates.g2_schematic, (CLEAN_SCH, FLOAT_REP)),
        (gates.g2_schematic, ({"ok": False, "erc_violations": 2}, {
            "errors": ["e"], "decoupling": {"uncovered": [
                {"ref": "U1", "rails_without_cap": ["3V3"]}]}}, {"blockers": ["hot"]})),
        (gates.g3_placement, (CLEAN_BOARD, {"courtyard_violations": 1}, G3_ISSUES, ["w"])),
        (gates.g3_placement, ({"ok": False, "missing_pads": ["U1.9"]}, {}, [])),
    ]


def test_strict_mode_is_the_default_and_every_task_has_policy_none(buck_design):
    for fn, args in _strict_cases(buck_design):
        default, strict = fn(*args), fn(*args, napkin=False)
        assert default == strict
        assert all(t["policy"] is None for t in strict["tasks"])
        assert all(set(t) == {"desc", "auto", "fix", "policy"} for t in strict["tasks"])


def test_strict_g2_ignores_floating_detail():
    g = gates.g2_schematic(CLEAN_SCH, FLOAT_REP)
    pins = FLOAT_REP["flags"]["floating_pins"]
    assert g["blockers"] == [f"floating pin (not netted, not no_connect): {p}" for p in pins]
    assert g["tasks"] == [{"desc": DESIGNER_FLOAT.format(p), "auto": False, "fix": {},
                           "policy": None} for p in pins]


def test_strict_g3_keeps_every_placement_issue_a_designer_task():
    g = gates.g3_placement(CLEAN_BOARD, {}, G3_ISSUES)
    assert g["blockers"] == G3_ISSUES
    assert g["tasks"] == [{"desc": f"fix placement: {i}", "auto": False, "fix": {},
                           "policy": None} for i in G3_ISSUES]


def test_napkin_g2_floating_pin_task_shapes():
    g = gates.g2_schematic(CLEAN_SCH, FLOAT_REP, napkin=True)
    pins = FLOAT_REP["flags"]["floating_pins"]
    assert not g["passed"]
    assert g["blockers"] == [f"floating pin (not netted, not no_connect): {p}" for p in pins]
    tasks = dict(zip(pins, g["tasks"], strict=True))
    # an op-amp's unused '+' and a stacked non-supply pad are never joined to the
    # same-name pin's net: today's designer task
    for designer in ("J1.3", "U1.9", "U2.1", "U4.5", "U5.20"):
        assert tasks[designer] == {"desc": DESIGNER_FLOAT.format(designer), "auto": False,
                                   "fix": {}, "policy": None}
    assert tasks["U1.12"]["fix"] == {"type": "net_same_name_pin", "ref": "U1", "pad": "12",
                                     "net": "GND"}
    assert tasks["U1.7"]["fix"] == {"type": "declare_no_connect", "ref": "U1", "pad": "7",
                                    "pin_type": "output", "pin_name": "PA3"}
    assert tasks["U$3.4"]["fix"] == {"type": "declare_no_connect", "ref": "U$3", "pad": "4",
                                     "pin_type": "bidirectional", "pin_name": "IO4"}
    assert tasks["U4.7"]["fix"] == {"type": "declare_no_connect", "ref": "U4", "pad": "7",
                                    "pin_type": "output", "pin_name": None}
    for auto in ("U1.12", "U1.7", "U$3.4", "U4.7"):
        assert tasks[auto]["auto"] is True and tasks[auto]["policy"] == "D5.floating_pin"
    assert tasks["U1.12"]["desc"] == "join U1.12 ('GND') to net GND (same-name pin on U1)"
    assert tasks["U1.7"]["desc"].startswith("declare U1.7 no_connect (output pin 'PA3'; med")
    assert tasks["U4.7"]["desc"].startswith("declare U4.7 no_connect (unnamed output pin; med")
    assert "None" not in tasks["U4.7"]["desc"]


def test_napkin_g2_keeps_other_findings_unchanged():
    rep = {**FLOAT_REP, "decoupling": {"uncovered": [{"ref": "U1", "rails_without_cap": ["3V3"]}]}}
    strict = gates.g2_schematic({"ok": True, "erc_violations": 1}, rep)
    napkin = gates.g2_schematic({"ok": True, "erc_violations": 1}, rep, napkin=True)
    assert napkin["blockers"] == strict["blockers"] and napkin["warnings"] == strict["warnings"]
    assert napkin["tasks"][:2] == strict["tasks"][:2]           # ERC + bypass cap


def test_napkin_g3_task_shapes():
    g = gates.g3_placement(CLEAN_BOARD, {"courtyard_violations": 1}, G3_ISSUES, napkin=True)
    assert g["blockers"][1:] == G3_ISSUES and not g["passed"]
    loosen, inward, far, plane, ep, keepout = g["tasks"]
    assert loosen == {"desc": "re-place with looser spacing (pack_scale+)", "auto": True,
                      "fix": {"type": "loosen_placement"}, "policy": None}
    assert inward["fix"] == {"type": "face_connector_out", "ref": "J1", "issue": "inward",
                             "dist_mm": None}
    assert far["fix"] == {"type": "face_connector_out", "ref": "J2", "issue": "far",
                          "dist_mm": 7.5}
    assert plane["fix"] == {"type": "loosen_placement",
                            "reason": "+3V3 plane on In2.Cu is fractured into separate islands"}
    assert ep["fix"] == {"type": "swap_thermal_footprint", "ref": "U3"}
    assert [t["policy"] for t in (inward, far, plane, ep)] == [
        "D5.face_connector_out", "D5.face_connector_out", "D5.fractured_plane",
        "D5.thermal_vias"]
    assert all(t["auto"] for t in (inward, far, plane, ep))
    assert keepout == {"desc": f"fix placement: {G3_ISSUES[-1]}", "auto": False, "fix": {},
                       "policy": None}
    assert inward["desc"] == "turn J1 to face out at the nearest board edge (mating face inward)"
    assert far["desc"] == "move J2 flush to the nearest board edge (mating face 7.5 mm away)"
    assert ep["desc"] == "swap U3 to its ...ThermalVias footprint variant"
    assert plane["desc"].startswith("re-place with looser spacing: +3V3 plane on In2.Cu")


def test_napkin_g3_board_failure_and_missing_pads_stay_designer_tasks():
    g = gates.g3_placement({"ok": False, "error": "boom", "missing_pads": ["U1.9"]}, {}, [],
                           napkin=True)
    assert [t["auto"] for t in g["tasks"]] == [False, False]


@pytest.mark.parametrize("napkin", [False, True])
def test_g3_missing_pads_none_does_not_crash(napkin):
    g = gates.g3_placement({"ok": True, "missing_pads": None}, {}, [], napkin=napkin)
    assert g["passed"] and g["tasks"] == []


@pytest.mark.parametrize("napkin", [False, True])
def test_g1_invalid_assumptions_block(napkin, buck_design):
    bad = [ASSUMPTION, {**ASSUMPTION, "confidence": "certain"}, {"id": "A1", "topic": " "}]
    g = gates.g1_requirements(CLEAN_LINT, {**buck_design, "assumptions": bad}, napkin=napkin)
    assert not g["passed"] and g["tasks"] == []
    assert any(b.startswith("assumptions[1] (id 'A1'): confidence must be one of")
               for b in g["blockers"])
    assert any("duplicate id 'A1'" in b for b in g["blockers"])
    assert any(b.startswith("assumptions[2] (id 'A1'): missing field 'value'")
               for b in g["blockers"])
    not_a_list = gates.g1_requirements(CLEAN_LINT, {**buck_design, "assumptions": {}},
                                       napkin=napkin)
    assert not_a_list["blockers"] == ["assumptions must be a list of entries, got dict"]


@pytest.mark.parametrize("block", [None, []])
def test_g1_napkin_without_assumptions_warns(block, buck_design):
    design = {**buck_design, "assumptions": block} if block is not None else buck_design
    g = gates.g1_requirements(CLEAN_LINT, design, napkin=True)
    assert g["passed"]
    assert g["warnings"] == ["napkin mode: the netlist records no requirements-level "
                             "'assumptions' (D5) — every default taken must be listed with "
                             "its rationale and confidence"]
    assert gates.g1_requirements(CLEAN_LINT, design)["warnings"] == []


def test_g1_napkin_with_valid_assumptions_is_clean(buck_design):
    g = gates.g1_requirements(CLEAN_LINT, {**buck_design, "assumptions": [ASSUMPTION]},
                              napkin=True)
    assert g["passed"] and g["warnings"] == [] and g["blockers"] == []


def test_summarize_marks_napkin_tasks_auto():
    g = gates.g2_schematic(CLEAN_SCH, FLOAT_REP, napkin=True)
    text = gates.summarize([g])
    assert "    -> [auto] join U1.12 ('GND') to net GND (same-name pin on U1)" in text
    assert "    -> [DESIGNER TASK] net pin U1.9 or declare it no_connect in the netlist" in text


# --- napkin G2 on real KiCad symbols: multi-unit op-amps (D5) ------------------------

OPAMP_CASES = [
    # lib_id, unit A (+, -, out), V+, V-, unused inputs, unused outputs
    ("Amplifier_Operational:LM358", ("3", "2", "1"), "8", "4", ["5", "6"], ["7"]),
    ("Amplifier_Operational:LM324", ("3", "2", "1"), "4", "11",
     ["5", "6", "9", "10", "12", "13"], ["7", "8", "14"]),
]


@requires_symbols
@pytest.mark.parametrize("lib_id,unit_a,vplus,vminus,inputs,outputs", OPAMP_CASES)
def test_napkin_g2_never_ties_unused_opamp_inputs_to_unit_a(lib_id, unit_a, vplus, vminus,
                                                            inputs, outputs):
    plus, minus, out = unit_a
    design = {"name": "amp", "components": [{"ref": "U1", "lib_id": lib_id, "value": "amp",
                                             "footprint": OPAMP["footprint"]}],
              "nets": {"SENSOR": [["U1", plus]], "FB": [["U1", minus], ["U1", out]],
                       "VCC": [["U1", vplus]], "GND": [["U1", vminus]]}}
    rep = design_report.analyze(design, str(KICAD.symbols))
    assert rep["errors"] == []
    detail = {d["pad"]: d for d in rep["flags"]["floating_detail"]}
    assert sorted(detail, key=int) == sorted(inputs + outputs, key=int)
    # the report states the fact (the unused '+' shares unit A's net name) ...
    assert {detail[p]["same_name_net"] for p in inputs} == {"SENSOR", "FB"}
    g = gates.g2_schematic(CLEAN_SCH, rep, napkin=True)
    pins = rep["flags"]["floating_pins"]                 # floating tasks come last
    tasks = dict(zip(pins, g["tasks"][-len(pins):], strict=True))
    # ... but the policy never joins it: an input stays a designer task
    for pad in inputs:
        assert tasks[f"U1.{pad}"] == {"desc": DESIGNER_FLOAT.format(f"U1.{pad}"),
                                      "auto": False, "fix": {}, "policy": None}
    for pad in outputs:
        task = tasks[f"U1.{pad}"]
        assert task["auto"] is True and "unnamed output pin" in task["desc"]
        assert task["fix"] == {"type": "declare_no_connect", "ref": "U1", "pad": pad,
                               "pin_type": "output", "pin_name": None}
    assert not any(t["fix"].get("type") == "net_same_name_pin" for t in g["tasks"])
